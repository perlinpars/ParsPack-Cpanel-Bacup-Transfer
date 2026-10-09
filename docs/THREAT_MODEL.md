# Threat model

Assets: account archives, the `system/` tree (copies of server configuration), S3 keys, `TRANSPORT_PASSWORD`, SMTP password, and the WHM session that can start a send.

## Trust boundaries

| Boundary | What crosses it | How it is checked |
| --- | --- | --- |
| cPanel to transport argv | Command, local directory, file names, host, user | Command must be one of the seven. Paths are sanitized in `parspack_config.py`. Host and user are not used as shell. |
| cPanel to environment | `PASSWORD` only, then the rest of the environment is replaced | Compared in constant time with `TRANSPORT_PASSWORD`, then unset. `TRANSPORT_AUTH_DISABLED=1` skips this. |
| Filesystem | Backup files under `LOCAL_READ_ROOTS` | Absolute path, no symlink, root-owned directory chain, not group or world writable. Live prefixes such as `/etc` and `/root` are refused. |
| Plugin to `mc` | Subprocess argv without keys | Keys are written to `mc-config/config.json` mode `0600`. The wrapper drops secret environment variables. |
| `mc` to S3 | HTTPS to `PARSPACK_ENDPOINT` | Endpoint must be `https`. Certificate checking is the TLS stack’s. The bucket name and prefix are single path segments. |
| WHM browser to `index.php` | Form posts | AppConfig `user=root` and `acls=all`. POST requires a 64-hex CSRF token compared with `hash_equals`. Mail updates accept only mail keys and force `SMTP_TLS=1`. |

`acls=all` is the WHM “all” privilege, so the page is not open to a reseller who lacks that privilege. The script still runs as root, so a bug in `index.php` is a root bug.

## STRIDE

| Threat | Mitigation now | Gap |
| --- | --- | --- |
| Spoofing a transport caller | Shared `TRANSPORT_PASSWORD` from the WHM destination. | Anyone who can run the script as root and read `config/parspack.conf` already has the secret. `TRANSPORT_AUTH_DISABLED=1` removes the check. |
| Tampering with a backup in flight | Upload goes to `.uploading`, then a verified copy to the final name. Mismatch does not replace a good final object. | A crash can leave an `.uploading` object. It is not the final name. |
| Tampering with config from WHM | Mail form cannot write S3 keys, `MODE`, or the transport password. | Root can still edit the conf file. That is expected. |
| Repudiation | `parspack.db`, `events.jsonl`, and the derived `uploads.tsv` record path, size, checksum, and time. | The text log line for a published upload still has no file name. |
| Information disclosure | Secrets stay out of `ps`, shell exports, and the WHM page. Trace log records only whether a password was supplied. | `system/` objects are copies of server files and sit in the bucket. A public bucket policy would expose them. The plugin does not set a public ACL. |
| Denial of service | One upload lock, waited on for at most 120 seconds per file. Five attempts then stop. Manual send is one pass. | A second cPanel run waits for the current file, then fails if that wait expires. A full bucket stops new uploads. Remote retention cannot free space while delete is off. |
| Elevation | No new user, no sudoers entry. WHM ACL is `all`. Paths cannot be `..` or a symlink. | `index.php` is mode `644` under the WHM docroot. It contains no secrets. It runs as root only through WHM. |
| Confused deputy on delete | `rmdir` and `delete` refuse the bucket root, an empty prefix, `..`, a non-date folder, today’s folder, and the newest verified day. | The flag still defaults to off. Turning it on does not accept a path outside `<prefix>/<YYYY-MM-DD>/`. |

## Out of scope for this plugin

Compromise of the root account, a stolen WHM session that already has `all`, and a bucket policy set in the ParsPack panel. Those bypass the controls above.
