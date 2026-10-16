# Decisions

## Phase 0

No runtime behavior was changed. The audit documents were compared with these pages, read on 2026-10-08:

- [How to Create a Custom Transport Script for Backups](https://docs.cpanel.net/knowledge-base/backup/how-to-create-a-custom-transport-script-for-backups/) (valid for versions 82 through latest, last modified 2026-07-08). Sections used: Script operation, Script commands, The put functions, The get function, The ls function, The chdir function, The rmdir function, The delete function.
- [Backup Retention Behavior](https://docs.cpanel.net/knowledge-base/backup/backup-retention-behavior/).
- [Backup Configuration](https://docs.cpanel.net/whm/backup/backup-configuration/), section “Retain Backups in the Default Backup Directory” and Additional Destinations “Script” / “Validate”.
- [How to Troubleshoot a Remote Transport Pruning Failure](https://docs.cpanel.net/knowledge-base/backup/how-to-troubleshoot-a-remote-transport-pruning-failure/). The page states a 300 second limit per transport process.

The summary table on the transport page lists `put` parameters as remote directory, remote file, then local file. The skeleton and the later “Required argument order” section use `($local, $remote, $host, $user)` after the already-consumed local directory. This plugin follows the skeleton order. See `docs/CPANEL_COMPLIANCE.md`.

There is no `describe` or `validate` command in the official command list. WHM’s Validate action is implemented by cPanel calling the normal commands, including a `validate.tmp-<pid>-<time>.txt` object.

## Phase 1

Path checks resolve the local file, reject a symlink, a non-regular file, and an empty file, then open that path with `O_NOFOLLOW` and compare the opened inode with a later `lstat`. Extra hard links are allowed for reads only after the file is confirmed inside a trusted `LOCAL_READ_ROOTS` tree (cPanel often hard-links account/system archives). Symlinks stay refused. `mc` still opens the path by name for the upload, so a replacement between the check and `mc` is outside this process. The local delete still unlinks by path after the inode check (one directory entry; other hard links are left alone).

A remote 403, invalid key, missing bucket, invalid path, or permission error stops on the first attempt. Other transport errors, including timeout, connection reset, HTTP 429, and HTTP 5xx, use the existing attempt limit. The delay is `RETRY_DELAY_SECONDS` doubled after each failure, plus a small jitter, and it does not exceed `RETRY_DELAY_CAP_SECONDS` (default 300). A base of 0 stays 0, which is what the offline tests use. An old config that omits the cap uses 300.

`flock` remains the lock. A pid inside the lock file is only a log hint. A dead pid does not release or replace a held lock. `send-system.lock` and `send-remaining.lock` are still non-blocking. `run/upload.lock` waits up to `UPLOAD_LOCK_WAIT_SECONDS` (default 120, maximum 240) so one account `put` can start after the current file finishes. The system mirror takes that lock once per file and releases it when that file's upload process exits.

`put` still exits 0 or 1 for cPanel, and TEST still exits 3 without creating an object. A system-tree upload that already waited for a quiet tree checks the file once and does not sleep again for every file.

## Owner decisions applied in Phase 1

A. `put` does not wait for `system/`. Before it detaches the mirror it writes `state/system-pending-YYYY-MM-DD` with an atomic rename and mode `0600`. If that write fails, `put` exits 1. If `setsid` or the spawn fails, the marker stays, the error is logged, and `put` still exits 0 because the account file was already verified. The marker is removed only after every regular file under that day's `system/` tree has been uploaded and verified. A leftover marker makes `report.py`, `send-remaining`, and `doctor` report that day as `CRITICAL`.

B. An account `put` waits for `run/upload.lock` for at most `UPLOAD_LOCK_WAIT_SECONDS`. The mirror does not hold that lock across the tree. Account uploads and the mirror therefore take turns per file. A wait that expires still fails that `put` with a clear message. The bound stays at or below 240 seconds so the transport process is not asked to sit for the whole 300 second cPanel limit.

C. `ALLOW_REMOTE_DELETE` stays `0` unless the operator turns it on. `MODE=TEST` still refuses remote delete. The refusal message is `refused: remote delete disabled`. The `validate.tmp-<digits>-<digits>.txt` probe can still be removed. Phase 4 is the date-folder guard used when the flag is on. `REMOTE_KEEP_DAYS` stays `0` unless the operator sets it. A positive value keeps today through that many days ago. `send-system.sh` calls `retention --apply` only after that day's system transfer succeeds. There is no separate retention cron. Today and the newest verified day are still refused. If `--apply` fails, `state/retention-failed-YYYY-MM-DD` is written, the day report is elevated from OK to WARNING, and WHM alerts show `remote keep window did not finish`. A later successful apply removes the marker.

D. `ls` of a missing directory still prints nothing and exits 0. `mkdir` still creates a `.keep` object and ignores the recurse flag. `put` and `get` keep the official Perl skeleton argument order: `put` receives the local file then the remote file, and `get` receives the remote file then the local file.

E. `MODE=TEST` uploads nothing for a backup. That `put` exits 3. cPanel treats that non-zero status as a failed transfer, which is the intended result of a dry run. WHM Validate’s own file is uploaded and exits 0; otherwise the destination cannot be saved.

## Phase 2

The upload record moved from `state/uploads.tsv` to `state/parspack.db`. The program that opens it is `bin/parspack-state`, using Python's standard-library `sqlite3`. cPanel's Perl transport does not open the database. The choice and the rollback steps are in `docs/STATE.md`.

## Phase 3

Verification always compares size. A whole-object SHA256 from `mc stat` is level `sha256`. A value with a `-<count>` suffix is not that hash and is not an MD5. The next check is the S3 ETag for the configured `PART_SIZE`: one part is the MD5 of the file, and more than one part is the MD5 of the part MD5s plus the part count. That match is level `etag`. If neither value is present, the existing full `mc cat` re-hash is still level `sha256`. A sample of the first and last 1 MiB is only level `size-only`, and only when the caller supplies that sample. The level is stored in `verification_level` and `checksum_algo`. Details are in `docs/VERIFICATION.md`.

In `PRODUCTION` with `REMOTE_EXISTS_POLICY=VERIFY`, a same-size object whose checksum or content check fails is replaced for that one key (`reupload`). Different sizes still refuse unless `REPLACE` is set. `TEST` still refuses. Before a new put, a leftover `.uploading` object for that key is removed. After a successful account `put`, `mirror-needed` skips detaching `send-system` when that day's `run_state` is already `partial` or `complete`, so transporters do not pile up on a finished or deadline-closed day.

Local delete runs only from `VERIFIED`. `DELETE_LOCAL_DELAY` is `0`, `10m`, `1h`, or `manual`. Delay `0` stats the remote object again and deletes only when the size still matches. A failure is `LOCAL_CLEANUP_FAILED`, a warning, and `put` exits 0.

The expected accounts are the regular files `accounts/*.tar.gz` in the local date directory, excluding `validate.tmp-*`. cPanel's custom transport is not given the account list. The detached `send-system.sh` re-reads that directory. It starts when the name and size list has been unchanged for `SYSTEM_SET_STABLE_SECONDS` (default 120) and every name is terminal (`VERIFIED`, `LOCAL_DELETED`, `LOCAL_CLEANUP_FAILED`, `UPLOAD_FAILED`, `VERIFY_FAILED`, `SKIPPED`, `INVALID_PATH`), or when `SYSTEM_DEADLINE_SECONDS` (default 21600) expires. The deadline path is `partial`: system files are not uploaded (so they do not compete with cPanel account puts on `upload.lock`), `manifest.json` is not uploaded, and the pending marker stays for `send-remaining`. System uploads run only when the gate is `ready`. `SYSTEM_UPLOAD_LOCK_WAIT_SECONDS` (default 600) is the lock wait for those system puts. `backup_days.run_state` is the shared queue flag. Account puts and system files still take `run/upload.lock` one file at a time.

`manifest.json` is written at `<prefix>/<date>/manifest.json` only after that complete pass, uploaded last, and verified. Its presence means the day completed. The report email is still sent from the end of `send-system.sh`, and the status comes from the database when `parspack.db` exists.

Edge cases:

- A late account changes the name/size list, so the stable window starts over. A second mirror exits immediately if the first still holds `send-system.lock`; the first loop sees the new tar.
- An account that arrives after `manifest.json` was uploaded is a new `put`. The next mirror uploads a new manifest only when the enlarged set is closed again. Until then the old manifest is stale.
- A cPanel run that aborts before creating a tar is invisible. A tar that exists but never reaches a terminal state waits until the deadline, then the day is partial.
- A later retry is another `put` or the manual remaining-send. Both use the same database row and the same per-file lock.

## Phase 4

Checked on 2026-10-08.

[How to Create a Custom Transport Script for Backups](https://docs.cpanel.net/knowledge-base/backup/how-to-create-a-custom-transport-script-for-backups/), sections “Script operation”, “Script commands”, “The rmdir function”, and “The delete function” (valid for versions 82 through latest, last modified 2026-07-08):

- The script receives the command, then the current directory, then that command’s parameters, then the host and username. The password arrives in the environment.
- `delete` takes one `$path` and removes one file. The skeleton unlinks it and returns. It prints nothing on success.
- `rmdir` takes one `$path` and removes that directory recursively. The skeleton returns only when the directory is gone, and it tells the caller to verify the path. Passing `/` is called out as dangerous.
- Data for the caller goes to stdout. A failure is printed to stderr. `usage()` exits 1. The page does not publish a larger exit-code table, so a refused delete exits 1 with the reason on stderr.

[Backup Retention Behavior](https://docs.cpanel.net/knowledge-base/backup/backup-retention-behavior/) describes when WHM keeps or drops the oldest backup. It does not name `delete`, `rmdir`, or the arguments. [How to Troubleshoot a Remote Transport Pruning Failure](https://docs.cpanel.net/knowledge-base/backup/how-to-troubleshoot-a-remote-transport-pruning-failure/) says pruning is a directory operation with a 300 second limit per transport process. Its example directory is a date folder. It also does not name the custom-script command.

When the flag is 1, `delete` and `rmdir` still run because cPanel called them, or because `retention --apply` selected a date folder older than `REMOTE_KEEP_DAYS`. The path must be `<prefix>/<YYYY-MM-DD>/...`. An empty prefix, the bucket root, `..`, a non-date folder, today’s folder, the newest verified day, and a day inside the keep window are refused. A listing or parse error refuses the delete. Each removed key is appended to the event log. `parspackctl retention --dry-run` prints usage and the folders the retention article would drop, and it does not delete. The count is read from `/var/cpanel/backups/config` when that file has one enabled schedule. If the count is missing or ambiguous, the dry-run removes nothing from the prediction. `ALERT_REMOTE_BYTES` is the storage warning threshold already used by the Phase 6 alerter.

## Phase 5

`parspackctl retry` is the operational retry. It selects `UPLOAD_FAILED` and `VERIFY_FAILED` rows, and for a date or account also local files that are not already verified. The upload engine applies the state machine, so a second run skips a matching verified file. `send-remaining` only finds the newest day, writes the WHM status file, and calls `retry --date`. Exit codes and the probe object rule are in `docs/CLI.md`.

## Phase 6

The WHM page stays on `WHM::header` / `WHM::footer` and AppConfig `acls=all`. The dashboard reads `parspack.db` with `mode=ro` through `lib/parspack_ui.py`. It does not call `parspack_state.connect()`, because that function migrates and imports. A failed-row Retry posts the account name only, with the existing CSRF token, and the PHP page calls `retry --account` as an argument list.

Settings still refuse to echo secrets. New channel keys are optional and off by default. Blank secret fields keep the stored value. TLS verification stays on for SMTP and for webhook, Telegram, and Slack. The same report send is skipped for 12 hours when the severity and warning text are unchanged. A different severity is sent. SMTP can stay disabled.

`uploads.tsv` is still rewritten from the successful rows for this release. The first import copies the 0.2.3 file to `uploads.tsv.0.2.3.bak` and does not replace that copy. `TEST` still exits 3 before any state row is written. `put` still exits 0 or 1.
