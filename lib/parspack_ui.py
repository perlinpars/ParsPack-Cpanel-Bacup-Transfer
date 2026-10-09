#!/usr/bin/env python3
"""Read-only WHM dashboard data and outbound alerts.

The PHP page does not open the database and does not receive filesystem
paths. It asks this program for JSON.
"""

from __future__ import print_function

import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.request

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_config  # noqa: E402

USER_RE = re.compile(r"\A[A-Za-z0-9._-]{1,32}\Z")
ACCOUNT_RE = re.compile(r"\A[A-Za-z0-9._+-]{1,64}\Z")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
DEDUPE_SECONDS = 12 * 3600
RATE_LIMIT = 8
RATE_WINDOW = 60


class UiError(Exception):
    def __init__(self, message):
        super(UiError, self).__init__(message)
        self.message = message


def connect_readonly(path):
    """Open the state database without migrating, importing, or recovering."""
    if not path or os.path.islink(path) or not os.path.isfile(path):
        raise UiError("state database is not available")
    real = os.path.realpath(path).replace("\\", "/")
    if not real.startswith("/"):
        real = "/" + real
    uri = "file://%s?mode=ro" % real
    import sqlite3
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def redact(text, secrets):
    text = (text or "").replace("\n", " ").replace("\r", " ")
    for secret in secrets or ():
        if isinstance(secret, str) and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "configured")
    return text[:160]


def _secrets(raw):
    keys = (
        "PARSPACK_SECRET_KEY",
        "PARSPACK_ACCESS_KEY",
        "TRANSPORT_PASSWORD",
        "SMTP_PASSWORD",
        "WEBHOOK_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "SLACK_WEBHOOK_URL",
    )
    return [raw.get(key, "") for key in keys if raw.get(key, "")]


def dashboard_from_db(conn, day, pending, secrets=()):
    dates = [row["backup_date"] for row in conn.execute(
        "SELECT backup_date FROM backup_days ORDER BY backup_date DESC"
    )]
    if not day:
        day = dates[0] if dates else ""
    counts = {}
    remote_bytes = 0
    local_freed = 0
    levels = {"sha256": 0, "etag": 0, "size-only": 0}
    last_verified = ""
    failed = []
    run_state = ""
    if day:
        run = conn.execute(
            "SELECT run_state, created_at, updated_at FROM backup_days WHERE backup_date = ?",
            (day,),
        ).fetchone()
        if run:
            run_state = run["run_state"] or ""
            started = run["created_at"] or ""
            finished = run["updated_at"] or ""
        else:
            started = finished = ""
        for row in conn.execute("SELECT * FROM uploads WHERE backup_date = ? ORDER BY account", (day,)):
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1
            size = int(row["size"] or 0)
            if status in ("VERIFIED", "LOCAL_DELETED", "LOCAL_CLEANUP_FAILED"):
                remote_bytes += size
            if status == "LOCAL_DELETED":
                local_freed += size
            level = row["verification_level"] or ""
            if level in levels:
                levels[level] += 1
            if status == "VERIFIED" and (row["updated_at"] or "") > last_verified:
                last_verified = row["updated_at"] or ""
            if status in ("UPLOAD_FAILED", "VERIFY_FAILED"):
                account = row["account"] or ""
                if account and account != "system" and ACCOUNT_RE.fullmatch(account):
                    failed.append({
                        "account": account,
                        "date": day,
                        "status": status,
                        "reason": redact(row["error_message"] or row["error_class"] or "", secrets),
                    })
    else:
        started = finished = ""
    ok = counts.get("VERIFIED", 0) + counts.get("LOCAL_DELETED", 0) + counts.get("LOCAL_CLEANUP_FAILED", 0)
    account_failed = counts.get("UPLOAD_FAILED", 0) + counts.get("VERIFY_FAILED", 0)
    system = "none"
    if day and ("system-pending-%s" % day) in pending:
        system = "pending"
    elif run_state == "partial":
        system = "partial"
    elif run_state == "complete":
        system = "complete"
    elif run_state:
        system = run_state
    facts = {
        "verify_failed": counts.get("VERIFY_FAILED", 0),
        "upload_failed": counts.get("UPLOAD_FAILED", 0),
        "cleanup_failed": counts.get("LOCAL_CLEANUP_FAILED", 0),
        "remote_bytes": remote_bytes,
        "local_freed": local_freed,
        "run_state": run_state,
        "pending": system == "pending",
        "remote_ok": True,
    }
    warnings = classify_alerts({"status": "OK", "remote_ok": True}, facts, 0)["warnings"]
    return {
        "date": day,
        "dates": [item for item in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item)],
        "run_state": run_state,
        "started": started,
        "finished": finished,
        "accounts_ok": ok,
        "accounts_failed": account_failed,
        "system_status": system,
        "remote_bytes": remote_bytes,
        "remote_usage": "recorded",
        "local_freed_bytes": local_freed,
        "verification": {"levels": levels, "last": last_verified},
        "warnings": warnings,
        "failed": failed,
        "facts": facts,
    }


def classify_alerts(snapshot, facts, threshold):
    """Map a day to critical, warning, or info. Texts do not include secrets."""
    status = snapshot.get("status") or "OK"
    warnings = []
    severity = "info"
    if status == "CRITICAL" or facts.get("pending") or facts.get("run_state") == "partial":
        severity = "critical"
        warnings.append({"severity": "critical", "text": "system backup is incomplete"})
    if not snapshot.get("remote_ok", True):
        severity = "critical"
        warnings.append({"severity": "critical", "text": "remote storage was not readable"})
    if facts.get("verify_failed"):
        severity = "critical"
        warnings.append({"severity": "critical", "text": "verification failed"})
    if facts.get("upload_failed") and severity != "critical":
        severity = "warning"
    if facts.get("upload_failed"):
        warnings.append({"severity": "warning", "text": "some accounts failed"})
    if facts.get("cleanup_failed"):
        if severity == "info":
            severity = "warning"
        warnings.append({"severity": "warning", "text": "local cleanup failed"})
    remote_bytes = int(facts.get("remote_bytes") or 0)
    if threshold and remote_bytes > int(threshold):
        if severity == "info":
            severity = "warning"
        warnings.append({"severity": "warning", "text": "recorded remote usage is over the threshold"})
    if status in ("PROBLEM",) and severity == "info":
        severity = "warning"
        warnings.append({"severity": "warning", "text": "the daily report is incomplete"})
    return {"severity": severity, "warnings": warnings}


def fingerprint(day, severity, warnings):
    body = "%s|%s|%s" % (
        day,
        severity,
        ",".join(sorted(item["text"] for item in warnings)),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def overlay_report(view, history_dir, threshold):
    """Add the last saved report status when that file stays inside the history directory."""
    facts = dict(view.get("facts") or {})
    status = ""
    remote_ok = True
    day = view.get("date") or ""
    path = ""
    if parspack_config.DATE_SEGMENT_RE.fullmatch(day) and history_dir:
        path = os.path.join(history_dir, "%s.json" % day)
    if path and not os.path.islink(path) and os.path.isfile(path):
        if os.path.dirname(os.path.realpath(path)) == os.path.realpath(history_dir):
            try:
                data = json.loads(open(path, "r", encoding="utf-8").read() or "{}")
            except (OSError, ValueError):
                data = {}
            status = data.get("status") if data.get("status") in ("OK", "WARNING", "PROBLEM", "CRITICAL", "EMPTY") else ""
            remote_ok = bool(data.get("remote_ok", True))
            facts["remote_ok"] = remote_ok
            view["report_status"] = status
            view["remote_ok"] = remote_ok
            if not view.get("started"):
                view["started"] = str(data.get("started") or "")[:40]
            if not view.get("finished"):
                view["finished"] = str(data.get("finished") or "")[:40]
    view["warnings"] = classify_alerts(
        {"status": status or "OK", "remote_ok": remote_ok}, facts, threshold
    )["warnings"]
    view["facts"] = facts
    return view


def claim_dedupe(path, fingerprint_value, severity, now, window=DEDUPE_SECONDS):
    """Return True when this alert should be sent. A higher severity is not suppressed."""
    current = {}
    if path and os.path.isfile(path) and not os.path.islink(path):
        try:
            current = json.loads(open(path, "r", encoding="utf-8").read() or "{}")
        except (OSError, ValueError):
            current = {}
    previous = current.get(fingerprint_value) or {}
    last = int(previous.get("at") or 0)
    last_rank = SEVERITY_RANK.get(previous.get("severity") or "info", 0)
    rank = SEVERITY_RANK.get(severity, 0)
    if last and (now - last) < window and rank <= last_rank:
        return False
    current[fingerprint_value] = {"at": int(now), "severity": severity}
    if path:
        directory = os.path.dirname(path)
        os.makedirs(directory, 0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(current, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return True


def https_request(url):
    parsed_ok = parspack_config.public_https_url(url)
    if not parsed_ok:
        raise UiError("alert URL is not https")
    return parsed_ok


def post_json(url, payload, headers, opener=None):
    """POST JSON. TLS verification stays on. The URL is not logged here."""
    target = https_request(url)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(target, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    context = ssl.create_default_context()
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    opener.open(request, timeout=15).read(1024)


def channel_messages(raw, summary):
    """Build enabled channel calls. Secrets stay in the request, not the summary text."""
    text = summary["text"]
    calls = []
    if raw.get("WEBHOOK_ENABLED") == "1" and raw.get("WEBHOOK_URL"):
        headers = {}
        secret = raw.get("WEBHOOK_SECRET") or ""
        if secret:
            headers["Authorization"] = "Bearer " + secret
        calls.append((raw["WEBHOOK_URL"], {"text": text, "severity": summary["severity"]}, headers))
    if raw.get("TELEGRAM_ENABLED") == "1" and raw.get("TELEGRAM_BOT_TOKEN") and raw.get("TELEGRAM_CHAT"):
        url = "https://api.telegram.org/bot%s/sendMessage" % raw["TELEGRAM_BOT_TOKEN"]
        calls.append((url, {"chat_id": raw["TELEGRAM_CHAT"], "text": text}, {}))
    if raw.get("SLACK_ENABLED") == "1" and raw.get("SLACK_WEBHOOK_URL"):
        calls.append((raw["SLACK_WEBHOOK_URL"], {"text": text}, {}))
    return calls


def account_lines(rows):
    success = []
    failed = []
    for row in rows or ():
        name = str(row.get("name") or row.get("user") or "")
        if not name or name.startswith("system/"):
            continue
        if row.get("ok"):
            success.append(name)
        else:
            failed.append("%s: %s" % (name, row.get("reason") or row.get("state") or "failed"))
    return success, failed


def summary_text(day, severity, rows, facts, secrets=()):
    success, failed = account_lines(rows)
    lines = [
        "PARSPACK %s %s" % (severity.upper(), day),
        "SUCCESS %d" % len(success),
    ]
    lines.extend("SUCCESS %s" % item for item in success[:50])
    lines.append("FAILED %d" % len(failed))
    lines.extend("FAILED %s" % item for item in failed[:50])
    lines.append("REMOTE recorded-bytes %d" % int(facts.get("remote_bytes") or 0))
    lines.append("LOCAL cleanup-failed %d freed-bytes %d" % (
        int(facts.get("cleanup_failed") or 0),
        int(facts.get("local_freed") or 0),
    ))
    return redact("\n".join(lines), secrets)


def dispatch_alerts(raw, snapshot, rows, facts, dedupe_path, now, sender, poster):
    """Send email when SMTP is on, and optional channels when they are enabled."""
    decision = classify_alerts(snapshot, facts, int(raw.get("ALERT_REMOTE_BYTES") or 0))
    text = summary_text(snapshot.get("date") or "", decision["severity"], rows, facts, _secrets(raw))
    token = fingerprint(snapshot.get("date") or "", decision["severity"], decision["warnings"])
    if not claim_dedupe(dedupe_path, token, decision["severity"], now):
        return {"sent": False, "reason": "duplicate", "severity": decision["severity"], "channels_failed": 0}
    email = "skipped"
    if parspack_config.notification_wanted(raw, _status_for_severity(decision["severity"])):
        if sender is not None:
            sent_email = sender(text)
            if sent_email:
                email = "sent"
            elif sent_email is None:
                email = "unconfigured"
    failed = 0
    for url, payload, headers in channel_messages(raw, {"text": text, "severity": decision["severity"]}):
        try:
            poster(url, payload, headers)
        except Exception:
            failed += 1
    return {"sent": True, "reason": email, "severity": decision["severity"], "channels_failed": failed}


def _status_for_severity(severity):
    return {"critical": "CRITICAL", "warning": "WARNING", "info": "OK"}[severity]


def append_audit(path, user, keys):
    if not USER_RE.fullmatch(user or ""):
        user = "unknown"
    clean = []
    for key in keys:
        if key not in parspack_config.SETTINGS_KEYS:
            raise UiError("audit key is not a setting")
        if key not in clean:
            clean.append(key)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()), "user": user, "keys": clean}
    line = json.dumps(record, sort_keys=True)
    directory = os.path.dirname(path)
    os.makedirs(directory, 0o700, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return record


def read_audit(path, limit=20):
    if not path or not os.path.isfile(path) or os.path.islink(path):
        return []
    rows = []
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        rows.append({
            "ts": str(item.get("ts") or "")[:40],
            "user": item.get("user") if USER_RE.fullmatch(str(item.get("user") or "")) else "unknown",
            "keys": [key for key in item.get("keys") or [] if key in parspack_config.SETTINGS_KEYS],
        })
    return rows[-limit:]


def rate_allow(path, now, limit=RATE_LIMIT, window=RATE_WINDOW):
    stamps = []
    if path and os.path.isfile(path) and not os.path.islink(path):
        try:
            stamps = json.loads(open(path, "r", encoding="utf-8").read() or "[]")
        except (OSError, ValueError):
            stamps = []
    stamps = [int(item) for item in stamps if isinstance(item, (int, float)) and now - int(item) < window]
    if len(stamps) >= limit:
        return False
    stamps.append(int(now))
    if path:
        directory = os.path.dirname(path)
        os.makedirs(directory, 0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(stamps, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        parspack_config.require_root()
        if not argv or argv[0] not in ("dashboard", "audit", "rate-limit"):
            raise UiError("usage: dashboard [YYYY-MM-DD] | audit | rate-limit ACTION")
        if argv[0] == "dashboard":
            day = ""
            if len(argv) == 2:
                if not parspack_config.DATE_SEGMENT_RE.fullmatch(argv[1]):
                    raise UiError("date must be YYYY-MM-DD")
                day = argv[1]
            elif len(argv) != 1:
                raise UiError("usage: dashboard [YYYY-MM-DD]")
            raw = parspack_config.load_config_file()
            conf = parspack_config.validate_core(raw)
            db_path = os.path.join(conf["STATE_DIR"], "parspack.db")
            pending = parspack_config.pending_system_days(conf["STATE_DIR"])
            try:
                conn = connect_readonly(db_path)
                view = dashboard_from_db(
                    conn, day, ["system-pending-%s" % item for item in pending], _secrets(raw)
                )
            except UiError:
                view = {"date": day, "dates": [], "warnings": [], "failed": [], "facts": {}}
            view = overlay_report(
                view,
                os.path.join(conf["STATE_DIR"], "history"),
                int(raw.get("ALERT_REMOTE_BYTES") or 0),
            )
            sys.stdout.write(json.dumps(view, sort_keys=True) + "\n")
            return 0
        if argv[0] == "audit":
            raw = parspack_config.load_config_file()
            conf = parspack_config.validate_core(raw)
            sys.stdout.write(json.dumps(read_audit(os.path.join(conf["STATE_DIR"], "audit.log"))) + "\n")
            return 0
        if len(argv) != 2 or argv[1] not in ("save", "retry", "report", "connection", "send"):
            raise UiError("usage: rate-limit ACTION")
        raw = parspack_config.load_config_file()
        conf = parspack_config.validate_core(raw)
        if not rate_allow(os.path.join(conf["STATE_DIR"], "whm-rate.json"), int(time.time())):
            print("rate limit", file=sys.stderr)
            return 1
        print("ok")
        return 0
    except parspack_config.ConfigError as exc:
        print("error: %s" % (exc.errors[0] if exc.errors else "configuration"), file=sys.stderr)
        return 2
    except UiError as exc:
        print("error: %s" % exc.message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
