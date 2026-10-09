# Phase 7 design

Nothing in this note is implemented. Phases 1–6 are in the Windows tree and are not all on the server yet. These items wait until that tree has run in production.

Constraints that apply to every item: `put` must return inside cPanel’s 300 second transport limit, `put` must not wait for `system/`, `MODE=TEST` must not create or delete objects, `ALLOW_REMOTE_DELETE` stays `0`, TLS verification stays on, and secrets stay out of the UI, logs, and argv.

## 1. Resumable multipart

Recommendation: do not build this yet.

`engine-upload.sh` already calls `mc put --part-size` and `--parallel`. That is one multipart upload inside one process. If the process dies, the next attempt starts over. `mc` does not hand back an S3 upload id, so the state database cannot resume those parts.

A real resume would speak the S3 multipart API itself, store the upload id and each finished part in a new schema, and continue from `retry` or `send-system.sh`. It cannot finish inside `put`, because cPanel stops that process at 300 seconds. Aborting an orphan upload is a remote delete. The only acceptable rule is the same shape as `validate.tmp`: abort an id this program stored, and never an id it did not store. `TEST` would create none.

Worth doing only after a real account archive is still failing because one `mc put` cannot finish. Until then the current `.uploading` object and the five retries stay the recovery path.

## 2. Bandwidth windows

Recommendation: do not add this inside `put`.

A rate limit or a quiet window makes the account `put` more likely to hit 300 seconds and fail. The detached system mirror and `retry` are the only places a window can wait. The default would be off. The server’s `mc` version has to be checked for a rate flag before any flag is named in the config. No flag is chosen here.

## 3. Concurrency above 1

Recommendation: do not change the lock.

cPanel already starts one `put` per file. Those processes share `run/upload.lock` on purpose. The state row is per file, and the lock is global. Two workers are safe only after a per-key lock exists and a test shows two different files uploading while a second worker cannot claim the same row. That test does not exist. Raising concurrency now would let two processes upload one key.

## 4. GFS retention

Recommendation: do not build a second retention policy. Phase 4 already handles cPanel's own delete and rmdir inside a date folder, and it is not a cron.

Phase 4 is the date-folder guard. A grandfather-father-son schedule is not part of it. The dry-run does not delete.

## 5. Monthly restore sample

Recommendation: a manual command later. Do not install a cron.

The command would download one small verified account object to `$ROOT/tmp`, list it with `tar -tzf`, write the result into the report, and delete that temp file. It would not extract the archive and would not delete the remote object. `MODE=TEST` would exit 3 and download nothing. A size cap would refuse a large object. A commented cron example can sit beside `cron/report.cron`. `install.sh` would not enable it. The nightly backup would not call it.

## 6. Multiple destinations

Recommendation: skip.

The install root, config file, and transport path are one destination. cPanel already runs a second additional destination as a second transport. Fan-out inside this `put` would have to decide which bucket must succeed before the local file is deleted, and it would share the 300 second budget. A second bucket needs a second install, not a second prefix inside this process.

## 7. Client-side encryption

Recommendation: skip.

WHM restore expects the cPanel `tar.gz`. Encrypting the object breaks `get`, the SHA256 and ETag checks, and restore. A usable design would need a key store outside the bucket, a written rotation, a backup of the key, and a recovery drill that proves an old object still decrypts. None of that exists. Without it, a lost key makes the backups unreadable. This item stays out.

## 8. Dashboard health score

Recommendation: this is the only item ready to build, and only after an explicit yes.

The dashboard already has the inputs: system pending or `partial`, `VERIFY_FAILED`, remote listing failure, `UPLOAD_FAILED`, `LOCAL_CLEANUP_FAILED`, and `size-only`. The score would be a label derived from those facts: critical, warning, or ok. It would not be a percentage and it would not query the bucket for a quota. The page would keep reading the database with `mode=ro`. No upload, delete, or config behavior would change.
