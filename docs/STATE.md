# State store

## Runtime choice

The transport stays the cPanel Perl script. cPanel does not ship a database library for that script, and the supported systems (AlmaLinux, Rocky Linux, Ubuntu, CloudLinux) install Python 3 with the OS package manager. That Python includes the standard-library `sqlite3` module when it was built against the system SQLite, which the distribution packages are.

`bin/parspack-state` is the only program that opens the database. Shell scripts call it with an argument list. They do not link SQLite and they do not parse the schema.

`doctor` fails if `import sqlite3` does not work or if SQLite is older than 3.7. Check it on a server with:

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

## Database

On the installed server the file is `/root/parspack-backup/state/parspack.db`. A test copy of the tree uses its own `state/` directory. The file is mode `0600`. The connection sets WAL mode, `busy_timeout=5000`, and foreign keys. `schema_version` records applied versions. Version 1 creates `backup_days`, `uploads`, `events`, and `tsv_import`. A newer database than this program is refused. Running the migration again does not rewrite existing tables.

`uploads` holds `backup_date`, `account`, `local_path`, `remote_key`, `size`, `mtime`, `checksum_algo`, `local_checksum`, `remote_checksum`, `verification_level`, `delete_not_before`, `attempts`, `status`, `error_class`, `error_message`, the owner pid, a heartbeat, and timestamps. A date that is not in the path is stored as `undated`. `backup_days.run_state` is the shared queue flag for the account uploads and the system mirror.

## States

```
DISCOVERED -> WAITING_STABILITY -> UPLOADING -> UPLOADED -> VERIFYING -> VERIFIED -> LOCAL_DELETED
```

Side states: `UPLOAD_FAILED`, `VERIFY_FAILED`, `LOCAL_CLEANUP_FAILED`, `INVALID_PATH`, `SKIPPED`.

Any other jump raises an error and the command exits 2. `VERIFIED`, `LOCAL_DELETED`, and `LOCAL_CLEANUP_FAILED` are successful backups. `LOCAL_CLEANUP_FAILED` means the remote object was verified and the local delete failed. That is a warning. The transport process still exits 0 so cPanel treats the transfer as successful.

Version 2 adds `uploads.verification_level` (`sha256`, `etag`, or `size-only`), `uploads.delete_not_before`, and `backup_days.run_state` (`open`, `system_running`, `complete`, or `partial`). An existing version 1 database gains those columns on the next open. `checksum_algo` stores the same level that verified the object.

The same file can be claimed again. If it is already a successful backup and the size and checksum both match, the claim returns `skip` and the upload does not run. A different checksum may return to `UPLOADING`. Rows imported from 0.2.3 have no checksum, so the first later pass checks the remote object and then stores the checksum. After that, `send-remaining` and the system mirror skip a file when the stored checksum exists and the size matches.

`UPLOADING` and `VERIFYING` rows whose owner pid is dead, or whose heartbeat is older than 15 minutes, are moved to `UPLOAD_FAILED` or `VERIFY_FAILED` on the next open. Those two states can be uploaded again.

## Logs kept for one release

Each transition appends `state/events.jsonl` with `ts`, `event`, `account`, `file`, `attempt`, and `error_class`. The same row is stored in `events`.

`state/uploads.tsv` is rewritten from the successful rows. It is no longer the source of truth. The first time a 0.2.3 file is seen it is copied to `state/uploads.tsv.0.2.3.bak` and that copy is never replaced. The import is recorded in `tsv_import` and is not repeated.

## Rollback

1. Do not start another backup while replacing the files.
2. Copy `state/uploads.tsv.0.2.3.bak` back to `state/uploads.tsv` if that backup exists. That file is the pre-import TSV.
3. Remove `state/parspack.db`, `state/parspack.db-wal`, `state/parspack.db-shm`, and `state/events.jsonl`.
4. Restore the previous `bin/engine-upload.sh`, `bin/send-system.sh`, `bin/send-remaining.sh`, `lib/parspack-common.sh`, and `lib/parspack_config.py`, and remove `bin/parspack-state` and `lib/parspack_state.py`.

The code snapshot `parspack-backup-plugin-backup-2026-10-08` is from before Phase 1. Restoring it also removes the Phase 1 security changes. Use step 4 when only the state layer should come out.
