# PARSPACK Backup for cPanel & WHM

Commercial-grade foundation for direct cPanel backup transport to PARSPACK S3-compatible Object Storage.

## Architecture

Primary transport:

cPanel Backup -> Custom Transport -> PARSPACK Engine -> S3 Multipart -> PARSPACK

The transport is intentionally stateless because cPanel invokes custom transport commands independently. Durable state, reports, configuration and audit data live in the project directory.

## Project layout

- `config/` configuration
- `bin/` CLI, upload engine (`engine-upload.sh`), and non-upload remote
  operations (`remote-ops.sh`: ls/mkdir/chdir/get/delete/delete-validation/rmdir)
- `transport/` cPanel Custom Backup Destination transport
- `lib/` shared helpers
- `tests/` offline checks. Run `tests/run-tests.sh` as root on the server
- `state/` runtime state and history
- `logs/` logs
- `reports/` generated reports
- `whm/` WHM plugin package (deployed by `install.sh` to cPanel's docroot)
- `install.sh` installer
- `uninstall.sh` uninstaller
- `upgrade.sh` upgrade helper

**Fixed install path:** the transport script and cron job reference this
project at `/root/parspack-backup` directly (see Installation below). Don't
move the extracted folder after installing without re-running `install.sh`
and updating the hardcoded path in `transport/parspack_transport.pl`.

## Safety defaults

- `MODE=TEST` checks the local file, the remote path, and that the bucket can be listed, then exits 3 without creating an object
- each backup object keeps the Gregorian `YYYY-MM-DD` directory cPanel already supplied. The transport does not choose a new day from the clock
- a new upload is written to a `.uploading` object, copied to the final name, verified there, and only then is `.uploading` removed
- `REMOTE_EXISTS_POLICY=VERIFY` keeps an existing object when its size and sha256 match. If the size matches but `mc stat` has no comparable sha256, the engine hashes the full remote content. A different object is left unchanged and the upload fails
- `REPLACE` overwrites only when `MODE=PRODUCTION`
- `PARSPACK_PREFIX` is applied to `put`, `get`, `ls`, `mkdir`, `chdir`, `delete`, and `rmdir`
- S3 credentials are stored in `mc-config/config.json` with mode `600`. They are not passed on the `mc` command line
- the WHM custom destination password must match `TRANSPORT_PASSWORD`
- the endpoint must be `https://`
- backup files must stay inside root-owned directories that are not writable by group or others
- a new upload stores sha256 metadata and the remote object is checked after the transfer. The local file is removed only after that check succeeds. A failed check leaves both the local file and the remote object in place

## Phase 1

This phase installs the engine and cPanel custom transport. The WHM plugin has two tabs: a report of the latest backup (accounts, success, and failure) and notification settings. The report tab can send the latest day's remaining account archives once; each file then stops after `RETRY_COUNT` tries. S3 keys and the transport password stay in `config/parspack.conf` and are not shown in WHM.

## Installation

1. Extract this package under `/root/parspack-backup`.
2. Copy `config/parspack.conf.example` to `config/parspack.conf`.
3. Edit the config. Use an `https://` endpoint, set `TRANSPORT_PASSWORD` to a unique value of at least 12 characters, and enter that same password as the WHM custom destination password. Do not put credentials in chat.
4. Run:

```bash
cd /root/parspack-backup
chmod 700 install.sh bin/* transport/*
./install.sh
```

5. Run:

```bash
./bin/parspackctl doctor
./bin/parspackctl connection-test
```

`retry`, `verify`, and `status` are documented in `docs/CLI.md`.

6. Keep `MODE=TEST`.
7. Configure cPanel WHM Backup Configuration with a Custom destination using the installed transport script. Do not enable deletion until successful end-to-end tests are complete.

## Important

The cPanel custom transport interface is the production integration point. cPanel's own documentation states the transport script **must implement** `put`, `get`, `ls`, `mkdir`, `chdir`, `rmdir`, and `delete` — if any of these hard-fail, cPanel can treat the whole destination as broken before a backup ever reaches `put`. All seven are implemented (`put` via `bin/engine-upload.sh`, the rest via `bin/remote-ops.sh`). The transport refuses to run until `PASSWORD` matches `TRANSPORT_PASSWORD`.

`PARSPACK_PREFIX` is the root for every command. A WHM remote directory of `weekly` and a prefix of `cpanel` stores objects at `bucket/cpanel/weekly/...`. `delete` and `rmdir` refuse to run unless `MODE=PRODUCTION` and `ALLOW_REMOTE_DELETE=1`. Even then the path must be a real `YYYY-MM-DD` folder under that prefix. They refuse an empty prefix, the bucket root, `..`, a non-date name, today's folder, and the newest verified day. The only exception is cPanel's own validation object, whose name matches `validate.tmp-<pid>-<timestamp>.txt`: that temporary object can be removed while `MODE=TEST` and `ALLOW_REMOTE_DELETE=0`.

`get` can write only under `LOCAL_WRITE_ROOTS` (and the local directory cPanel passed, when that directory itself is root-owned and private). `put` can read only under `LOCAL_READ_ROOTS` with the same ownership rule.

Do not place the project archive, credentials, or `mc-config` inside `/backup`. Uninstall removes the generated `mc-config` credentials. `config/parspack.conf` is kept until you delete it.

## Reporting

`bin/report.py` (see `cron/report.cron`) writes a daily HTML report under `reports/` and a JSON summary under `state/history/`. Both are mode `600`. If `SMTP_ENABLED="1"`, `SMTP_TLS="1"`, and `SMTP_HOST`/`REPORT_TO` are set, it emails that report over STARTTLS with certificate checking. `NOTIFY_ON_SUCCESS` and `NOTIFY_ON_FAILURE` choose whether a successful day (including a warning) or a failed day is mailed. Cleartext SMTP is refused. A failed send is logged to stderr and never blocks report generation. The WHM settings tab writes only these mail keys.

## File-age / stability safety check

`MIN_FILE_AGE_MINUTES` is not a hard refusal. cPanel calls `put` as soon as the archive is closed, so the file is almost always younger than that limit. `bin/engine-upload.sh` watches the size for `STABILITY_CHECK_SECONDS` instead. A stable size continues into the upload. If the size changes, that watch repeats up to `RETRY_COUNT` times and then that file stops. The upload command has the same limit: `RETRY_COUNT` tries, then stop. One file's failure does not cancel the other accounts, and nothing schedules an endless catch-up. Empty files are refused. A cPanel validation file under `/var/cpanel/backups/tmp.test.*` skips the age and stability wait; every other file still goes through it. After a successful upload the remote size must match. If `mc stat` reports a comparable SHA256, it must match too. A multipart checksum, such as a base64 value with a part-count suffix, is not treated as a whole-file hash; the engine hashes the remote content instead. `DELETE_LOCAL_AFTER_SUCCESS=1` removes the local file only in `PRODUCTION`, and only after that verification succeeds. A failed verification does not delete the local file or the remote object.
