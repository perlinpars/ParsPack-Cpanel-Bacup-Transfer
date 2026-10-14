# cPanel transport compliance

Source: [How to Create a Custom Transport Script for Backups](https://docs.cpanel.net/knowledge-base/backup/how-to-create-a-custom-transport-script-for-backups/), last modified 2026-07-08. Argument order follows that page’s skeleton and “Required argument order” section. The summary table on the same page lists `put` and `get` parameters in a different order. See `docs/DECISIONS.md`.

Invocation shape from “Script operation”:

1. Command name.
2. Current directory.
3. Command-specific parameters.
4. Host.
5. Username.

Password arrives in `PASSWORD`. Each command is a new process. State must not be stored between commands. Output data goes to STDOUT. Failures go to STDERR and are logged by cPanel.

There is no official `describe` or `validate` command. WHM’s Validate action uses the commands below. This plugin treats a remote name matching `validate.tmp-<digits>-<digits>.txt` as that probe.

The transport page does not publish a numeric exit-code table. The skeleton exits `1` for usage errors. Any non-zero status is a cPanel failure, and the reason is written to STDERR. The pruning page separately says a transport process is limited to 300 seconds: [How to Troubleshoot a Remote Transport Pruning Failure](https://docs.cpanel.net/knowledge-base/backup/how-to-troubleshoot-a-remote-transport-pruning-failure/).

Exit status for this plugin:

| Status | Meaning | STDERR |
| --- | --- | --- |
| 0 | The transport command succeeded. A backup `put` in `MODE=TEST` does not use 0. The WHM validation probe does. | |
| 1 | A path was refused (traversal, symlink, empty file, hard link, illegal remote name) or the operation failed. | `config error: ...` for a path refusal, or the engine log line for an operation failure |
| 2 | The configuration is invalid: bad `MODE`, endpoint, prefix, permissions, or helper usage. `doctor` uses this status. | `config error: ...` |
| 3 | `put` of a backup while `MODE=TEST`. Nothing was uploaded. The WHM validation file is not this case. | An info line that the bucket was checked and nothing was stored |

A refused path is not a configuration error. `object-key`, `check-local`, `sha256`, and `remote-compose` exit 1 after a valid configuration has loaded. Loading that configuration still exits 2 when the file itself is wrong. cPanel treats every non-zero status as a failed transfer.

| Command | Documented contract | This plugin | Deviation |
| --- | --- | --- | --- |
| `put` | Copy one local file to the remote file. Skeleton args: local file, remote file, host, user. Errors on STDERR. | Resolves the local path, refuses symlinks and paths outside `LOCAL_READ_ROOTS`, uploads through `engine-upload.sh`, verifies size and checksum, then returns the engine status. On success for an account archive, writes `state/system-pending-YYYY-MM-DD` and detaches the system mirror. It does not wait for `system/`. | If the marker cannot be written, `put` exits 1 even though the account object is already verified. If detach fails, `put` still exits 0 and the marker stays. `MODE=TEST` returns exit `3` and uploads nothing, except WHM’s validation file (`/var/cpanel/backups/tmp.test.<pid>-<time>`, remote name `validate.tmp-<pid>-<time>.txt`). That probe is uploaded and the command exits 0 so Validate can succeed. cPanel can delete that one object afterward. A real backup still exits 3. A young file can block inside this process for up to 5 times 60 seconds, plus upload retries. |
| `get` | Copy remote file to local file. Skeleton args: remote, local, host, user. | Downloads to a sibling partial file, checks size and checksum, then renames into place. | Stricter than the sample. A missing checksum downloads the object a second time to hash it. |
| `ls` | Print `ls -l` compatible text on STDOUT. | Lists one prefix with `mc ls --json` and formats it. A missing directory prints nothing and exits 0, whether the missing-object message arrives on stderr or stdout. `.keep` markers are hidden. | A missing path does not return the non-zero status a local `ls` would. Decision D in `docs/DECISIONS.md`. |
| `mkdir` | Create the remote directory. Skeleton also receives a recurse flag. | Creates `<path>/.keep` so the prefix exists. An empty path or `/` exits 0. | The recurse flag is not read. `.keep` is an extra object. |
| `chdir` | Succeed only if the directory can be used, then print the directory the next command will receive as its local directory. | Checks the path is a legal key and prints the composed remote directory. | It does not create the directory. A later `ls` of a missing directory is empty rather than an error. |
| `rmdir` | Recursively delete that directory. Never delete `/`. | Requires `ALLOW_REMOTE_DELETE=1` and `MODE=PRODUCTION`. The path must be `<prefix>/<YYYY-MM-DD>/...`. Refuses an empty prefix, the bucket root, `..`, a non-date name, today’s folder, and the newest verified day. A failed listing is a refusal. Then `mc rm --recursive` on that one directory. | With the default flag, cPanel pruning fails and nothing is deleted. |
| `delete` | Delete one remote file. | Validation names are deleted even when remote delete is off. Every other name uses the same date-folder guard as `rmdir` and must name a file under that day. | Default configuration rejects the retention deletes cPanel will send. The validation exception is intentional so WHM Validate can remove its probe. |

## Retention when remote delete is off

[Backup Retention Behavior](https://docs.cpanel.net/knowledge-base/backup/backup-retention-behavior/) describes how many successful and failed backups cPanel keeps. It does not list the transport argv. The only delete operations in the transport interface are `delete` and `rmdir`, and the pruning article describes remote pruning as work done by the transport, with a 300 second limit per process.

With `ALLOW_REMOTE_DELETE=0`:

- `rmdir` exits 1 and writes `refused: remote delete disabled` to STDERR.
- `delete` of a backup object does the same.
- `delete` of `validate.tmp-<digits>-<digits>.txt` still removes that one object.

Result: cPanel’s remote retention cannot prune this destination while the flag is `0`. Objects remain. That is not silent data loss. The bucket can fill. Setting the flag to `1` still refuses any path that is not inside `<prefix>/<YYYY-MM-DD>/`, and it refuses today’s folder and the newest verified day. `REMOTE_KEEP_DAYS` defaults to `0`. A positive value also refuses a day that is still inside that window, and after a successful system transfer removes only the older date folders.

Local retention is separate. [Backup Configuration](https://docs.cpanel.net/whm/backup/backup-configuration/) says that if “Retain Backups in the Default Backup Directory” is off, cPanel deletes account backup files from the local backup directory only after a successful transfer to at least one additional destination and an attempt to every destination. That setting does not remove system backup files. This plugin can also delete a local file after the remote object is verified, then checked again for size, when `DELETE_LOCAL_AFTER_SUCCESS=1`. `DELETE_LOCAL_DELAY` is `0`, `10m`, `1h`, or `manual`. A failed local delete is `LOCAL_CLEANUP_FAILED` and the `put` still exits 0.

cPanel does not send the plugin the list of accounts for the run. The expected set is the regular `accounts/*.tar.gz` files in the local date directory, excluding `validate.tmp-*`. The detached system mirror, not `put`, waits until each of those accounts is terminal or until `SYSTEM_DEADLINE_SECONDS`. `put` still only writes the pending marker and detaches.

## Which Perl runs the transport

The file cPanel is configured to run is `/usr/local/cpanel/scripts/parspack_custom_transport.pl`. `install.sh` copies `transport/parspack_transport.pl` to that path. cPanel starts a new process with that path as the program (command, current directory, command arguments, host, username, and `PASSWORD` in the environment). The kernel then honors the script shebang.

The shebang is `#!/usr/bin/perl`. That is the system Perl, the same binary `perl` resolves to under `install.sh`'s `PATH` (`/usr/bin` is on that path, and `/usr/local/cpanel/3rdparty/bin` is not). It is not `/usr/local/cpanel/3rdparty/bin/perl`.

The production failure loaded `POSIX.pm` from `/usr/lib64/perl5/POSIX.pm`. That directory belongs to the RHEL-family system Perl. cPanel's third-party Perl keeps its libraries under `/usr/local/cpanel/3rdparty/perl/`. `perl -c` before install must be this system Perl, because that is the one that will execute the shebang.

## Modules the transport loads

Every `use` in `transport/parspack_transport.pl` is a core module in Perl 5.16.3 (RHEL 7) and in the later system Perls shipped on RHEL 8 (5.26) and RHEL 9 (5.32). None come from CPAN.

| Statement | Role | Core since |
| --- | --- | --- |
| `use strict` | Pragma | 5.0, still core |
| `use warnings` | Pragma | 5.6, still core |
| `use Fcntl qw(O_WRONLY O_APPEND O_CREAT O_NOFOLLOW O_EXCL F_GETFD F_SETFD FD_CLOEXEC)` | Open and fd flags | 5.0 (`Fcntl`). `O_NOFOLLOW` is present on Linux; this host compiled past that import |
| `use IO::Handle` | `flush` and `sync` on the pending-marker handle | 5.003 (`IO`) |
| `use IO::Select` | Two-second read on the detach pipe | 5.003 (`IO`) |
| `use POSIX qw(strftime setsid)` | Log timestamps and detach. `fsync` is not imported | 5.0. `strftime` and `setsid` are implemented on Linux |

`POSIX::fsync` is not implemented in this system Perl. Importing it aborts compilation inside `POSIX.pm` before any command runs. The marker file is synced with `$fh->flush` then `$fh->sync`. Both return numeric 0 on success (the POD's "0 but true") and `-1` or `undef` on failure. The writer treats any other result as a failed marker, deletes the temp file, and `put` exits 1 with a STDERR line. STDOUT stays empty.

The `state/` directory is not synced. The old code did not sync it either: `fsync` ran only on the file descriptor. A directory sync is not added. If one is needed later, open the directory and call `IO::Handle::sync` only when that call works; if the platform does not implement it, skip it and say so. Do not call `POSIX::fsync`.

No other Perl, shell, or PHP file calls `fsync`. `lib/parspack_config.py` calls `os.fsync` after `flush` when rewriting `parspack.conf` and the `mc` config. That is CPython's wrapper for `fsync(2)` and is valid on this runtime. An `OSError` from it still fails that rewrite.

## Perl 5.16 language level

The transport does not enable a feature bundle and does not use signatures, `say`, `state`, `given`/`when`, `fc`, postfix dereferencing, or `try`. The only syntax newer than Perl 5.8 is the defined-or operator `//` in the `get` argument check. That operator has been unconditional syntax since Perl 5.10, so it is valid on 5.16. `strict` and `warnings` are on. The `exec` that detaches the system mirror is written as `exec ... or do { ... }` so Perl does not warn `Statement unlikely to be reached`. That warning is printed at compile time, on every command, and cPanel treats it as a failed upload. The `do` block still runs only when `exec` fails.

## Not specified by the transport page

- No per-command timeout other than the 300 second pruning note.
- No required behavior when two backup runs overlap.
- No requirement to upload `system/` from a custom transport. cPanel uploads system files to an additional destination only when that destination’s system-backup option is enabled, and that path is one archive. This plugin uploads the existing local `system/` tree itself so the internal folders stay intact.
