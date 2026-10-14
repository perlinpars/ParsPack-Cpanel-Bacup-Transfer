# WHM security checklist

Run this on the WHM host after the page is installed. The Windows tests cover the Python rules. They do not execute `whm/index.php`.

## XSS

- Open the dashboard, reports, and settings tabs with a normal day.
- Confirm every account name, error, warning, and audit value is text. View source and confirm there is no `<script` and no secret value.
- If a failed row’s reason contains `<script>`, the page must show the characters, not run them. `pp_h()` uses `htmlspecialchars` with `ENT_QUOTES`.

## CSRF

- Save settings once, then replay that POST without the `token` field, and again with a different 64-hex token. Both must flash the rejection and must not change `config/parspack.conf`.
- Replay a valid POST from another site. WHM’s `/cpsess…/` token should reject it before this script runs. A request whose path is not `/cpsess…/cgi/parspack_backup/index.php` must not be used as the redirect target.

## Privilege escalation

- Log in as root. The three tabs load.
- Log in as a reseller that does not have the `all` ACL. The page must say “Access denied” and must not show counts or run a command.
- A reseller is allowed only when `/var/cpanel/resellers` gives that user `all`, which is the same ACL the AppConfig file requires.
- Confirm `whm/parspack_backup.conf` still has `user=root` and `acls=all`, and that the registered copy in `/var/cpanel/apps/` is mode `600`.

## Path injection

- Post `account=../etc/passwd`, `account=/root/parspack-backup/config/parspack.conf`, and `date=../../tmp`. The account must be rejected. The date must be ignored unless it is `YYYY-MM-DD`.
- Confirm the retry process list is `python3`, `lib/parspack_ops.py`, `retry`, `--account`, and the account name. There must be no shell string built from the form.
- Post `action=save-settings` with `PARSPACK_SECRET_KEY` or `LOCAL_READ_ROOTS` inside the JSON by calling the command directly. `update-settings` must reject the key.

## Secrets and headers

- Settings must show `Configured` for a stored SMTP password, webhook secret, Telegram token, and Slack URL, and the HTML and the audit log must not contain those values.
- Response headers should include `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: same-origin`, and a Content-Security-Policy with `default-src 'self'`. Same-origin scripts and styles must be allowed so `WHM::header` can draw the left navigation. Third-party hosts stay blocked.
- Send nine POSTs in one minute. The ninth must be refused by the rate limit.
- Leave `WEBHOOK_ENABLED=0` and confirm no webhook is attempted. An `http://` URL or a URL with userinfo must be rejected. Telegram and Slack stay off until their own enable flag is `1`.
