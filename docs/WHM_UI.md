# WHM page

The page is registered with AppConfig and served by WHM as root. The browser never receives a filesystem path, an S3 key, or a channel secret.

## Registration and ACL

cPanel’s registration guide says a plugin is registered as root with `/usr/local/cpanel/bin/register_appconfig`, and the configuration file is written to `/var/cpanel/apps/` mode `600`:

- [Guide to WHM plugins - AppConfig registration](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-appconfig-registration)

`whm/parspack_backup.conf` is the file `install.sh` registers. The configuration-file guide requires `service=whostmgr`, a `url` that starts with `/` and is relative to `/usr/local/cpanel/whostmgr/docroot/`, and an `acls` list unless the server has turned that requirement off. This plugin keeps the requirement on:

- [Guide to WHM plugins - AppConfig configuration file](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-appconfig-configuration-file/)

`url=/cgi/parspack_backup/index.php`, `user=root`, and `acls=all`. The ACL chart defines `all` as full WHM privileges:

- [Guide to WHM plugins - ACL reference chart](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-acl-reference-chart)

The ACL guide says a plugin must check the authenticated user even though the CGI runs as root. `root` is allowed. Any other user is allowed only when `/var/cpanel/resellers` lists that user with the `all` ACL. Everyone else gets “Access denied” and no command runs:

- [Guide to WHM plugins - Access control lists](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-access-control-lists)

Do not set `permit_appconfig_entries_without_acls`, `permit_unregistered_apps_as_root`, or `permit_unregistered_apps_as_reseller` to `1`.

## Header, token, and actions

The Interfaces guide documents `WHM::header` and `WHM::footer` from `/usr/local/cpanel/php/WHM.php`. This page already uses that library, so it stays on the PHP header and footer rather than moving to Template Toolkit:

- [Guide to WHM plugins - Interfaces](https://api.docs.cpanel.net/guides/guide-to-whm-plugins/guide-to-whm-plugins-plugin-files/guide-to-whm-plugins-interfaces)

The Quickstart says not to call cPanel or WHM interface URLs to perform actions. Forms post back to this script. The script then runs a fixed argument list:

- [Quickstart Development Guide - WHM](https://api.docs.cpanel.net/guides/quickstart-development-guide/quickstart-development-guide-whm)

cPanel puts a `/cpsess##########/` token in the session URL and rejects a request that omits it. Redirects keep that prefix only when the path is exactly `/cpsess…/cgi/parspack_backup/index.php`:

- [Basic Security Concepts](https://docs.cpanel.net/knowledge-base/security/basic-security-concepts/)

Every state-changing form also posts the 64-hex token from `state/whm-csrf`, compared with `hash_equals`.

## What the page shows

The default tab is the dashboard. It calls `lib/parspack_ui.py dashboard`, which opens `state/parspack.db` read-only. That path does not migrate, import, or recover rows. Counts, the system marker, recorded remote bytes, and local bytes freed come from the database. Local free/used space comes from `statvfs` on `LOCAL_READ_ROOTS`. Destination used size prefers a short cached `mc du` of the configured prefix (5 minutes) and falls back to the sum of verified sizes in the database. S3 usually does not expose free quota; when `ALERT_REMOTE_BYTES` is set, the bar is relative to that threshold.

Retry posts the account name and calls `lib/parspack_ops.py retry --account NAME`. The name must match `^[A-Za-z0-9._+-]{1,64}$`. The date in the form is only used to return to the same dashboard day. It is not passed to the retry command.

The reports user table shows 20 rows per page (`?tab=reports&date=YYYY-MM-DD&page=N`). Transfer status is summarized in Persian. Long reasons are truncated in the cell and kept full in the `title` attribute.

Settings calls `mail-settings` and shows S3 and channel secrets as `Configured` or `Missing`. The form is three closed sections: transfer and delete, email, and alerts. Saving posts `update-settings` with the whitelist in `SETTINGS_KEYS`. That whitelist includes `ALLOW_REMOTE_DELETE`, `REMOTE_KEEP_DAYS`, and `DELETE_LOCAL_AFTER_SUCCESS`. `REMOTE_KEEP_DAYS` is `0` through `3650`. A positive value keeps today through that many days ago. After a successful system transfer, `retention --apply` removes older date folders. Today and the newest verified day stay. An unchecked box writes `0`. A blank secret keeps the stored value. `SMTP_TLS` stays `1`. Test connection calls `connection-test`. The audit table reads `state/audit.log` and shows the key names only. A destination-full upload is FATAL and surfaces as «فضای مقصد پر است» on the dashboard.

The reports tab is unchanged in purpose: it can rebuild one day and start `send-remaining.sh`. Email is sent at the end of `send-system.sh`, then deduplicated. There is no upload cron.
