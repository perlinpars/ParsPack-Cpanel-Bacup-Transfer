# Verification

Size is always compared. A content check is recorded only when the algorithm really covers the object. The level stored on the upload row is one of `sha256`, `etag`, or `size-only`. The same value is written to `checksum_algo`.

## What ParsPack and mc can return

`mc stat` and `mc ls --json` can include `checksum.SHA256` when the upload was sent with `mc put --checksum SHA256`. A 32-byte SHA256, hex or standard base64, is a whole-object hash. That is level `sha256`.

MinIO's multipart checksum adds `-<part count>` to the checksum string. That suffix means the value is not the hash of the file. This plugin treats it as "no SHA256". It is not converted to MD5.

S3's ETag is a different field. For one part it is the MD5 of the object. For more than one part it is the MD5 of the concatenated part MD5s, then a hyphen and the part count. The part size is `PART_SIZE` (default `256MiB`). Matching that formula is level `etag`. A multipart ETag is not the MD5 of the whole file, and it is not used as one.

CRC32C is not read. The endpoint's extra checksum that this plugin trusts is SHA256, then the ETag formula above.

## Order

1. The sizes differ: the object does not match. Nothing is deleted.
2. A whole-object SHA256 is present. Equal hashes are `sha256`. Different hashes do not match.
3. No whole-object SHA256, and `etag` matches `etag_for_file(local, PART_SIZE)`: level `etag`. A well-formed ETag that does not match does not match the object.
4. No SHA256 and no usable ETag: `mc cat` downloads the object and SHA256 is computed locally. A match is level `sha256` because the bytes were hashed. This is the path already used when a multipart checksum suffix hides the real hash.
5. `sample_sha256` hashes the size plus the first and last 1 MiB. It is not a whole-file hash. It is level `size-only`, and only when that sample is supplied and matches. The engine does not use it to skip the full download. A production object with no SHA256 and no ETag still takes step 4.
6. If step 4 cannot be read, the attempt is retried. It is not stored as `size-only`.

`size-only` stays in the model so a supplied sample, or a later pass that has nothing but size, is visible on the row. A report that sees any `size-only` row raises an otherwise OK day to WARNING.

## Local delete

Delete runs only from `VERIFIED`.

| `DELETE_LOCAL_DELAY` | Behavior |
| --- | --- |
| `0` | Stat the remote object again. Delete the local file only when it is still a regular file and the remote size is still the verified size. |
| `10m` or `1h` | Stay `VERIFIED` and set `delete_not_before`. A later `--due` pass does the same recheck, at most 20 files, under `run/upload.lock`. |
| `manual` | Never delete automatically. |

`DELETE_LOCAL_AFTER_SUCCESS=0` deletes nothing. `MODE=TEST` deletes nothing and exits 3 before a state row is written.

If the recheck or the unlink fails, the row becomes `LOCAL_CLEANUP_FAILED`. The remote object is already verified, so this is a warning. `put` exits 0. The daily report does not call that a failed backup.

## Manifest

When the system pass finishes a complete day it writes `<local date>/manifest.json` and uploads it to `<prefix>/<date>/manifest.json` after every account and system object. The upload uses the same engine, so the manifest is verified after it lands. The local file then follows the same local-delete rule as any other verified file. A partial or deadline run does not upload it. Seeing that object means the day completed.

The file contains `plugin_version`, `cpanel_version`, `expected_accounts`, `uploaded_accounts`, each file's size, checksum, algorithm, and level, the system status, totals, and the start and finish timestamps.

## System trigger

cPanel calls `put` once per account archive. It does not pass the list of accounts. The expected set is therefore whatever regular files are in `<local date>/accounts/*.tar.gz`, excluding `validate.tmp-*`. The directory grows while cPanel is still packing, so the set is closed only after `SYSTEM_SET_STABLE_SECONDS` (default 120) with the same names and sizes.

`put` does not wait for that inside its 300 second process. It writes `state/system-pending-YYYY-MM-DD` and detaches `send-system.sh`. That process re-reads the directory.

It uploads `system/` when every expected account is terminal, or when `SYSTEM_DEADLINE_SECONDS` (default 21600) expires.

Terminal states are `VERIFIED`, `LOCAL_DELETED`, `LOCAL_CLEANUP_FAILED`, `UPLOAD_FAILED`, `VERIFY_FAILED`, `SKIPPED`, and `INVALID_PATH`. `DISCOVERED`, `WAITING_STABILITY`, `UPLOADING`, `VERIFYING`, and a tar with no database row are not terminal.

An empty directory is not "every account is done". It waits, then becomes partial at the deadline. A cPanel run that aborts before creating a tar leaves no name to wait for.

`backup_days.run_state` is the queue flag shared by account uploads and the system mirror: `open`, `system_running`, `complete`, or `partial`. The file lock is still `run/upload.lock`, held for one file and then released, so an account `put` and a system file take turns. `send-system.lock` stays one mirror, non-blocking. A second mirror exits while the first is running; the first loop sees a tar that appears late and restarts its stable window.

On partial: system files that already exist are still uploaded, `manifest.json` is not uploaded, the pending marker stays, and the report says the day is partial. The email is still sent from the end of `send-system.sh`. Its status comes from the database when `state/parspack.db` exists. Log parsing remains the fallback.

A late account after the manifest was uploaded is a new `put`. The previous manifest is then stale until another mirror closes the enlarged set and uploads a new manifest. A retry of a failed account uses the same database row and the same per-file lock.
