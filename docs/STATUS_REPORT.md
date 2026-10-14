# STATUS REPORT — PARSPACK Backup (stabilization)

**Date:** 2026-10-09  
**Tree:** `parspack-backup-plugin` VERSION `0.3.0`  
**Baseline:** `69c67f1` (pre-stabilization commit)  
**Tip:** every commit after that baseline, including this report.  
**Method:** `tests/run-tests.sh` as root under WSL against `tests/fake-mc.py` only. No live bucket.

---

## 1. Executive summary

1. Offline suite: **passed=78 failed=0**.
2. `ALLOW_REMOTE_DELETE` and `DELETE_LOCAL_AFTER_SUCCESS` stay **0** in `config/parspack.conf.example`. No guard was relaxed. The cPanel argument order for `put` and `get` is unchanged.
3. The previous blockers are fixed: `get` field splitting, `retry-delay` argv, suite abort under `set -e`, phase 5 under root, path-versus-config exit codes, `ls` of a missing directory, and pending-marker removal.
4. This is still not a live-bucket proof. ParsPack S3, TLS, the cPanel 300 second limit, and a real `setsid` detach were not exercised.
5. Mock tests cannot catch a compile error in the cPanel host's Perl. `perl -c transport/parspack_transport.pl` on the target server, using `/usr/bin/perl`, is a mandatory pre-install check. `install.sh` now runs that check on the source file and refuses to replace the live transport if it fails.

---

## 2. Fixes

| Commit | Change |
| --- | --- |
| `3f31605` | `get` parses `stat-fields` as size, SHA256, and etag. A checksum mismatch or a missing object fails and leaves no partial file. |
| `f4f83c4` | `retry-delay` reads three decimal arguments only. It does not read stdin. `fake-mc` does not print `ok` for a normal put or stat, and it consumes `--checksum VALUE`. |
| `76c4a3a` | The suite no longer aborts on an expected non-zero status. It prints `passed` and `failed` and exits 1 when any check fails. |
| `302ccdc` | Phase 5 runs the non-root rejection as `nobody` when the suite itself is root. |
| `3aaa456` | A refused path exits 1. Invalid configuration exits 2. Both write the reason to STDERR. Documented in `docs/CPANEL_COMPLIANCE.md`. |
| `3065bc0` | `ls` of a missing directory exits 0 with empty STDOUT. A missing-object message on stderr or stdout is enough. This matches Decision D. |
| `7f66231` | `send-remaining` no longer deletes `system-pending-*` from its own counters. Only `send-system.sh` removes it, after the same verified-tree and manifest path. |
| `959bd7e` | Fake-mc coverage for a verified upload with local delete off (state `VERIFIED`) and with delete on and delay 0. The local file is removed only after a second remote size check. A shrunk recheck keeps the file. |
| `bc71d3c` | Shellcheck warning clean. Ruff pyflakes findings fixed (`ssl` unused, `doctor` catching `ConfigError`, unused probe fd). |
| `2143177` | Pending-marker durability uses `IO::Handle` `flush` and `sync`. `POSIX::fsync` is not imported. A failed sync still fails that `put`. |
| `670144f` | Compliance note: system `/usr/bin/perl` runs the transport, and every `use` is core on Perl 5.16+. |
| `0dd228d` | `install.sh` validates sources before it touches the live transport or WHM page, then replaces each file by rename and restores the backup if a later step fails. |
| `719163e` | The suite runs `perl -c`, rejects `POSIX::fsync` in `.pl` files, and proves a broken transport source leaves the live file untouched. |

---

## 3. Behavior that changed

- **get.** The third `stat-fields` column is the etag. It is no longer compared as part of the SHA256. When the remote hash is absent and an etag is present, `verify-decision` can accept it. A failed check deletes the `.parspack-partial` file and does not replace the destination.
- **retry.** Delay inputs are the three numbers after an optional `--`. A stray `ok` on stdin or in argv is rejected. A failed put retries from those numbers.
- **Exit codes.** Path refusal during `object-key`, `check-local`, `sha256`, and `remote-compose` is exit 1. Loading a bad config, including `doctor`, stays exit 2. `MODE=TEST` put stays exit 3. Any non-zero status is still a cPanel failure with a STDERR message.
- **ls.** A missing prefix prints nothing and exits 0. The previous rc=1 came from `fake-mc` writing `Object does not exist` on stdout while `remote-ops` only inspected stderr.
- **Pending marker.** `send-remaining --run` calls `send-system.sh --complete`. The marker stays when the account set is not terminal or a system file fails. It is removed only after the system tree and `manifest.json` are verified.
- **Upload log.** A whole-object content hash used during publish is logged as `Remote object content SHA256 verified`.
- **Pending marker.** The file is still written, renamed into place, and forced to mode `0600`. Durability of the file contents is `IO::Handle::flush` then `IO::Handle::sync`. A sync failure deletes the temp file and `put` exits 1 with the same STDERR line as any other marker failure. The `state/` directory is still not synced. `put` and `get` argument order and the other exit codes are unchanged.
- **Install.** A failed `perl -c`, `php -l`, `bash -n`, or `python3 -m py_compile` exits non-zero and does not replace `/usr/local/cpanel/scripts/parspack_custom_transport.pl` or the WHM `index.php`. `config/parspack.conf`, `mc-config/`, and `state/` are not overwritten. Mode `600` is applied only to regular files directly in `lib/`.

Defaults that did not change: `ALLOW_REMOTE_DELETE=0`, `DELETE_LOCAL_AFTER_SUCCESS=0`, `DELETE_LOCAL_DELAY=0` when the key is omitted. Remote delete and local delete stay off.

---

## 4. Test result

Command: `wsl -u root -- bash tests/run-tests.sh`  
Result: **passed=78 failed=0** (the stabilization run before the compile fix was passed=75 failed=0)

Covered additions:

- get with etag present
- get checksum mismatch leaves no local or partial file
- get of a missing object leaves no local or partial file
- noisy `mc` stdout cannot change `retry-delay`
- `ls` of a missing directory is empty and exits 0
- pending marker survives a failed system pass and is removed only after `--complete` verifies the tree
- delete disabled: row `VERIFIED`, local file kept
- delete enabled, delay 0, recheck size mismatch: local file kept
- delete enabled, delay 0, recheck matches: local file removed after the recheck log line
- `perl -c` on the transport (the suite fails if `perl` is missing)
- no `POSIX::fsync` and no `fsync` inside a `use POSIX` import in any `.pl` file
- a broken transport source makes `install.sh` exit non-zero and leaves the fake live transport and WHM page byte-for-byte unchanged

What the mock still cannot prove: live ParsPack multipart ETags, TLS, the 300 second kill, a real `setsid` failure, overlapping cPanel puts, or WHM AppConfig registration on a host. It also cannot catch a compile error that exists only in the cPanel server's Perl. The suite's `perl -c` runs whatever `perl` is installed on the machine that runs the tests (here, Perl 5.38 under WSL). That is not a substitute for `perl -c` on the server.

---

## 5. Linters

- **shellcheck 0.9.0** (`-S warning`) on `bin/*.sh`, `lib/*.sh`, `tests/*.sh`, `install.sh`, `uninstall.sh`, `upgrade.sh`: clean.
- **ruff 0.16.10** pyflakes and pycodestyle rules `E,W,F` except line length: clean. `E501` is not in ruff's default set and was not applied; this tree is not wrapped to 88 columns.

Remaining shellcheck notes (info/style, not warning):

| Code | Count | Meaning |
| --- | --- | --- |
| SC2015 | 23 | `A && B \|\| C` is not a full if/else. Used by the test harness. |
| SC2317 | 6 | Trap handlers and the temp-dir cleanup look unreachable to shellcheck. |
| SC2016 | 4 | Single quotes in tests that intentionally do not expand. |
| SC2153 | 1 | `MODE` in `parspack-common.sh` is assigned by the sourced config export. |

Remaining ruff default-set findings (style and modernization, 245):

| Code | Count | Meaning |
| --- | --- | --- |
| UP031 | 153 | percent formatting |
| SIM115 | 17 | `open` without a context manager |
| RUF100 | 15 | `noqa: E402` on the intentional late imports |
| BLE001 | 8 | blind `except` |
| UP022 | 8 | `stdout=PIPE` style |
| UP021 | 6 | `universal_newlines` |
| UP008 | 5 | `super` arguments |
| UP010 | 5 | `from __future__ import print_function` |
| DTZ011 | 4 | `date.today()` without a timezone |
| SIM114 | 4 | identical if arms |
| DTZ005 | 3 | `datetime.now()` without tzinfo |
| PIE810 | 3 | repeated startswith/endswith |
| SIM103 | 3 | needless bool |
| PLW1510 | 3 | `subprocess.run` without `check` |
| SIM102 | 2 | collapsible if |
| DTZ006, DTZ003, UP036, FLY002, I001, TRY203 | 1 each | modernization nits |

---

## 6. Residual risk

1. Live transport, TLS, and the 300 second limit are untested. The offline suite cannot see an unimplemented function in the server's Perl. Run `perl -c` on the server before install.
2. The pending-marker directory entry is still not synced. A crash immediately after `rename` can lose the marker name. The next account `put` for that day writes it again. A failed sync of the file itself still fails that `put`.
3. Remote delete is implemented and default off. Turning `ALLOW_REMOTE_DELETE` on can delete a date folder that passes the guards.
4. Local delete is default off. With the flag on and delay 0, deletion happens only after a second size check. A wrong verify would still be able to remove the local archive.
5. `put` can exit 0 when the system mirror fails to detach. The pending marker stays and is reported as CRITICAL.
6. `mc` still opens the local path by name after the inode check (TOCTOU).
7. An upload-lock wait that expires fails that `put`. cPanel does not retry it by itself.

---

## 7. Rollback

The pre-fix tree is `69c67f1`. To undo the stabilization commits:

```bash
git revert --no-commit 69c67f1..HEAD
```

or check out `69c67f1` if this history has not been published. Do not reset a commit that has already been pushed unless that is an explicit request.

---

## 8. Staging checklist

Unchanged in intent. Run on a staging host with a disposable prefix. Keep both delete flags at 0 for the first production nights.

| # | Action | Expected |
| --- | --- | --- |
| 0 | On the server, before `install.sh`: `perl -c /root/parspack-backup/transport/parspack_transport.pl` and confirm `perl` is `/usr/bin/perl` | `syntax OK`, exit 0. If this fails, do not install. `install.sh` repeats the check and will not replace the live transport. |
| 1 | `MODE=TEST`, `parspackctl doctor` | Warnings only; no upload |
| 2 | WHM Validate while `MODE=TEST` | The probe object is uploaded and `put` exits 0. A real backup `put` still exits 3 and stores nothing |
| 3 | `MODE=PRODUCTION`, both delete flags 0, one account | Object under `prefix/YYYY-MM-DD/accounts/`; marker appears and clears after the mirror |
| 4 | Hold `upload.lock` | Second put waits, then a clear timeout message |
| 5 | Kill -9 during `mc put` | Row returns to FAILED; retry verifies |
| 6 | Break the system spawn | Marker remains; account `put` still exits 0 |
| 7 | `get` after the field fix | Object matches and a bad checksum leaves no partial file |
| 8 | `remote-ops delete` with the flag at 0 | STDERR `refused: remote delete disabled`, exit 1 |
