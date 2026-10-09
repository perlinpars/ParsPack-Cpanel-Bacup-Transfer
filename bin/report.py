#!/usr/bin/env python3
import datetime
import html
import json
import os
import re
import ssl
import smtplib
import subprocess
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_config  # noqa: E402


def read_events(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 6:
                continue
            try:
                rows.append({
                    "ts": parts[0],
                    "path": parts[1],
                    "remote": parts[2],
                    "size": int(parts[3]),
                    "duration": int(parts[4]),
                    "attempt": int(parts[5]),
                })
            except ValueError:
                continue
    return rows


def day_key(ts):
    return ts[:10]


LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[^ ]+) \[(INFO|ERROR|WARN)\] (.*)$")
ACCOUNT_RE = re.compile(r"([^/\s]+?\.tar\.gz)(?:\.uploading|\.parspack-test|\.parspack-partial)?(?:\s|$)")


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.2f %s" % (value, unit)
        value /= 1024
    return "%d B" % int(size)


def classify_filename(name):
    if name.endswith(".tar.gz.uploading") or name.endswith(".tar.gz.parspack-test") or name.endswith(".parspack-partial"):
        return "leftover"
    if name.endswith(".tar.gz") and not name.startswith("validate.tmp-"):
        return "account"
    return "other"


def account_name(text):
    match = ACCOUNT_RE.search(text or "")
    if not match:
        return ""
    name = match.group(1)
    if name.startswith("validate.tmp-") or classify_filename(name) != "account":
        return ""
    return name


def discover_dates(roots):
    found = []
    for root in roots:
        if os.path.islink(root) or not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not parspack_config.DATE_SEGMENT_RE.fullmatch(name):
                continue
            accounts = os.path.join(root, name, "accounts")
            if os.path.isdir(accounts) and not os.path.islink(accounts):
                found.append(name)
    return sorted(set(found))


def source_accounts(roots, day):
    found = {}
    for root in roots:
        folder = os.path.join(root, day, "accounts")
        if os.path.islink(folder) or not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if classify_filename(name) != "account":
                continue
            path = os.path.join(folder, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            found[name] = found.get(name, 0) + os.path.getsize(path)
    return found


def list_remote_accounts(conf, day):
    target = parspack_config.object_target(conf, day + "/accounts", require_child=True) + "/"
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "TMPDIR": os.path.join(ROOT, "tmp"),
        "LANG": "C",
    }
    command = [conf["MC_BIN"], "--config-dir", conf["MC_CONFIG_DIR"], "ls", "--json", "--", target]
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return {}, [], False
    if proc.returncode != 0:
        return {}, [], False
    accounts = {}
    leftovers = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or str(row.get("type", "")).lower() == "folder":
            continue
        key = str(row.get("key") or row.get("name") or "")
        leaf = key.rstrip("/").split("/")[-1]
        kind = classify_filename(leaf)
        size = row.get("size")
        if not isinstance(size, int):
            size = 0
        if kind == "account":
            accounts[leaf] = size
        elif kind == "leftover":
            leftovers.append({"name": leaf, "size": size})
    return accounts, leftovers, True


def parse_log(path, day):
    outcomes = {}
    stamps = []
    current = ""
    if not os.path.isfile(path):
        return outcomes, "", ""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            match = LOG_LINE_RE.match(raw_line.rstrip("\n"))
            if not match:
                continue
            stamp, level, message = match.groups()
            name = account_name(message)
            mentions_day = ("/" + day + "/") in message or message.endswith("/" + day)
            if name and (mentions_day or day in stamp[:10]):
                current = name
                item = outcomes.setdefault(name, {"status": "", "reason": "", "start": stamp, "end": stamp})
                item["end"] = stamp
                if not item["start"]:
                    item["start"] = stamp
                stamps.append(stamp)
                if "/system/" in message:
                    item["scope"] = "system"
                if level == "ERROR":
                    item["status"] = "failed"
                    item["reason"] = message
                elif "Uploading attempt" in message or "verifying its full content" in message:
                    if item["status"] != "ok":
                        item["status"] = "running"
            elif current and (
                "content SHA256 verified" in message
                or "and published" in message
                or "matches size and checksum" in message
            ):
                item = outcomes.setdefault(current, {"status": "", "reason": "", "start": stamp, "end": stamp})
                item["status"] = "ok"
                item["reason"] = ""
                item["end"] = stamp
                stamps.append(stamp)
            elif current and "Deleted the local file after the checksum was verified" in message:
                if current in outcomes:
                    outcomes[current]["end"] = stamp
                stamps.append(stamp)
    start = min(stamps) if stamps else ""
    end = max(stamps) if stamps else ""
    return outcomes, start, end


def write_private(path, content):
    directory = os.path.dirname(path)
    parspack_config.ensure_private_dir(directory)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(path, 0o600)


def account_user(name):
    if name.endswith(".tar.gz"):
        return name[:-7]
    return name


def clean_rows(rows):
    cleaned = []
    if not isinstance(rows, list):
        return cleaned
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name or "/" in name or len(name) > 200:
            continue
        remote_size = row.get("remote_size")
        local_size = row.get("local_size")
        cleaned.append({
            "user": str(row.get("user") or account_user(name))[:200],
            "name": name,
            "ok": bool(row.get("ok")),
            "state": str(row.get("state") or "")[:80],
            "remote_size": remote_size if isinstance(remote_size, int) else 0,
            "local_size": local_size if isinstance(local_size, int) else 0,
            "reason": str(row.get("reason") or "")[:500],
            "start": str(row.get("start") or "")[:40],
            "end": str(row.get("end") or "")[:40],
        })
    return cleaned


def public_account_rows(rows):
    exported = []
    for row in rows:
        exported.append({
            "user": account_user(row["name"]),
            "name": row["name"],
            "ok": bool(row["ok"]),
            "state": row["state"],
            "remote_size": row["remote_size"],
            "local_size": row["local_size"],
            "reason": (row["reason"] or "")[:500],
            "start": row.get("start", ""),
            "end": row.get("end", ""),
        })
    return clean_rows(exported)


def send_email(raw, subject, html_body):
    try:
        smtp = parspack_config.smtp_settings(raw)
    except parspack_config.ConfigError as exc:
        print("SMTP is not usable: %s" % "; ".join(exc.errors), file=sys.stderr)
        return
    if smtp is None:
        return
    if any(char in subject for char in "\r\n\0"):
        print("Refusing to send a report with an invalid subject", file=sys.stderr)
        return
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = smtp["mail_from"]
    message["To"] = smtp["mail_to"]
    message.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp["host"], smtp["port"], timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if smtp["user"]:
                server.login(smtp["user"], smtp["password"])
            server.sendmail(smtp["mail_from"], [smtp["mail_to"]], message.as_string())
            print("email sent")
            return True
    except Exception as exc:
        print("Failed to send report email: %s" % exc.__class__.__name__, file=sys.stderr)
        return False


def db_day_note(status, day):
    """Raise OK to WARNING from the state database. A pending marker stays CRITICAL."""
    db_path = os.path.join(ROOT, "state", "parspack.db")
    if not os.path.isfile(db_path):
        return status, ""
    try:
        import parspack_state
        conn = parspack_state.connect(db_path)
        flags = parspack_state.day_flags(conn, day)
    except Exception:
        return status, ""
    notes = []
    if flags["run_state"] == "partial" and status == "OK":
        status = "WARNING"
        notes.append("مهلت انتقال تمام شد و این روز ناقص مانده است. manifest.json ارسال نشد.")
    elif flags["run_state"] == "partial":
        notes.append("مهلت انتقال تمام شد و این روز ناقص مانده است. manifest.json ارسال نشد.")
    if flags["cleanup"]:
        if status == "OK":
            status = "WARNING"
        notes.append("حذف فایل محلی بعد از تأیید مقصد انجام نشد.")
    if flags["size_only"]:
        if status == "OK":
            status = "WARNING"
        notes.append("برخی فایل‌ها فقط با اندازه تأیید شدند.")
    return status, " ".join(notes)


def build_report(conf, day):
    roots = parspack_config.split_roots(conf["LOCAL_READ_ROOTS"])
    local = source_accounts(roots, day)
    remote, leftovers, remote_ok = list_remote_accounts(conf, day)
    outcomes, started, finished = parse_log(os.path.join(conf["LOG_DIR"], "backup.log"), day)
    names = sorted(set(local) | set(remote) | set(outcomes))
    rows = []
    transferred_bytes = 0
    remaining_bytes = 0
    for name in names:
        on_remote = name in remote
        on_local = name in local
        outcome = outcomes.get(name, {})
        system_ok = outcome.get("scope") == "system" and outcome.get("status") == "ok"
        if on_remote or system_ok:
            state = "منتقل شد"
            if on_remote:
                transferred_bytes += remote[name]
            if on_local:
                state = "در مقصد هست و فایل محلی هم مانده"
                remaining_bytes += local[name]
        else:
            state = "منتقل نشد"
            remaining_bytes += local.get(name, 0)
        reason = outcome.get("reason") or ""
        if on_remote and outcome.get("status") == "failed":
            reason = "خطای قبلی بود و بعداً محتوا تأیید شد"
        rows.append({
            "name": name,
            "state": state,
            "remote_size": remote.get(name, 0),
            "local_size": local.get(name, 0),
            "reason": reason,
            "start": outcome.get("start", ""),
            "end": outcome.get("end", ""),
            "ok": on_remote or system_ok,
        })
    failed = [row for row in rows if not row["ok"]]
    warning = bool(leftovers) or any(row["ok"] and row["local_size"] for row in rows)
    if not remote_ok:
        status = "PROBLEM"
    elif failed:
        status = "PROBLEM"
    elif warning:
        status = "WARNING"
    else:
        status = "OK"
    status, db_note = db_day_note(status, day)
    if day in parspack_config.pending_system_days(conf["STATE_DIR"]):
        status = "CRITICAL"
    return {
        "date": day,
        "status": status,
        "remote_ok": remote_ok,
        "source_count": len(names),
        "transferred_count": sum(1 for row in rows if row["ok"]),
        "failed_count": len(failed),
        "transferred_bytes": transferred_bytes,
        "remaining_bytes": remaining_bytes,
        "started": started,
        "finished": finished,
        "rows": rows,
        "leftovers": leftovers,
        "db_note": db_note,
    }


def render_report(snapshot):
    status_class = {"OK": "ok", "WARNING": "warning", "PROBLEM": "problem", "CRITICAL": "problem"}[snapshot["status"]]
    status_text = {"OK": "کامل", "WARNING": "هشدار", "PROBLEM": "ناقص", "CRITICAL": "بحرانی"}[snapshot["status"]]
    body = []
    for row in snapshot["rows"]:
        body.append(
            "<tr class=\"%s\"><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                "good" if row["ok"] else "bad",
                html.escape(row["name"]),
                html.escape(row["state"]),
                html.escape(human_size(row["remote_size"]) if row["remote_size"] else "-"),
                html.escape(human_size(row["local_size"]) if row["local_size"] else "-"),
                html.escape(row["end"] or row["start"] or "-"),
                html.escape(row["reason"] or "-"),
            )
        )
    leftover_rows = []
    for item in snapshot["leftovers"]:
        leftover_rows.append(
            "<li>%s (%s)</li>" % (html.escape(item["name"]), html.escape(human_size(item["size"])))
        )
    remote_note = "" if snapshot["remote_ok"] else "<p>فهرست مقصد خوانده نشد، پس تعداد منتقل‌شده کامل نیست.</p>"
    if snapshot["status"] == "CRITICAL":
        remote_note += "<p>انتقال فولدر system برای این روز تمام و تأیید نشده است.</p>"
    if snapshot.get("db_note"):
        remote_note += "<p>%s</p>" % html.escape(snapshot["db_note"])
    return """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><style>
    body{font-family:Tahoma,Arial,sans-serif;margin:32px} .ok{background:#e8f5e9} .warning{background:#fff8e1} .problem{background:#ffebee}
    .ok,.warning,.problem{padding:12px;border-radius:6px}
    table{border-collapse:collapse;width:100%%;margin-top:20px}th,td{border:1px solid #ddd;padding:8px;text-align:right}
    tr.bad td{background:#ffebee} tr.good td{background:#f7fff7}
    </style></head><body>
    <h1>گزارش بکاپ پارس‌پک</h1>
    <div class="%s"><strong>وضعیت: %s</strong></div>
    %s
    <h2>خلاصه %s</h2>
    <p>حساب‌های دیده‌شده در مبدأ و مقصد: %d<br>
    منتقل‌شده به مقصد: %d<br>
    منتقل‌نشده: %d<br>
    حجم منتقل‌شده: %s<br>
    حجم مانده در مبدأ: %s<br>
    شروع: %s<br>
    پایان: %s</p>
    <h2>فایل‌های اضافی مقصد</h2>
    <ul>%s</ul>
    <table><tr><th>حساب</th><th>وضعیت</th><th>حجم مقصد</th><th>حجم مبدأ</th><th>آخرین زمان</th><th>توضیح</th></tr>
    %s</table></body></html>""" % (
        status_class,
        html.escape(status_text),
        remote_note,
        html.escape(snapshot["date"]),
        snapshot["source_count"],
        snapshot["transferred_count"],
        snapshot["failed_count"],
        html.escape(human_size(snapshot["transferred_bytes"])),
        html.escape(human_size(snapshot["remaining_bytes"])),
        html.escape(snapshot["started"] or "-"),
        html.escape(snapshot["finished"] or "-"),
        "".join(leftover_rows) or "<li>ندارد</li>",
        "".join(body) or "<tr><td colspan=\"6\">حسابی پیدا نشد</td></tr>",
    )


def log_status_rows(conf, day):
    roots = parspack_config.split_roots(conf["LOCAL_READ_ROOTS"])
    local = source_accounts(roots, day)
    outcomes, started, finished = parse_log(os.path.join(conf["LOG_DIR"], "backup.log"), day)
    names = sorted(set(local) | set(outcomes))
    rows = []
    for name in names:
        outcome = outcomes.get(name, {})
        status = outcome.get("status") or ""
        if status == "ok":
            state, ok = "موفق", True
        elif status == "failed":
            state, ok = "ناموفق", False
        elif status == "running":
            state, ok = "در حال انتقال", False
        elif name in local:
            state, ok = "فقط در مبدأ", False
        else:
            state, ok = "نامشخص", False
        rows.append({
            "user": account_user(name),
            "name": name,
            "ok": ok,
            "state": state,
            "remote_size": 0,
            "local_size": local.get(name, 0),
            "reason": (outcome.get("reason") or "")[:500],
            "start": outcome.get("start", ""),
            "end": outcome.get("end", ""),
        })
    failed = [row for row in rows if row["state"] == "ناموفق"]
    if not rows:
        status_name = "EMPTY"
    elif failed:
        status_name = "PROBLEM"
    elif any(not row["ok"] for row in rows):
        status_name = "WARNING"
    else:
        status_name = "OK"
    if day in parspack_config.pending_system_days(conf["STATE_DIR"]):
        status_name = "CRITICAL"
    return {
        "date": day,
        "status": status_name,
        "remote_ok": False,
        "source": "log",
        "source_count": len(rows),
        "transferred_count": sum(1 for row in rows if row["ok"]),
        "failed_count": len(failed),
        "transferred_bytes": 0,
        "remaining_bytes": sum(row["local_size"] for row in rows if not row["ok"]),
        "started": started,
        "finished": finished,
        "checked_at": "",
        "rows": clean_rows(rows),
        "leftovers": [],
    }


def list_history_days(conf):
    directory = os.path.join(conf["STATE_DIR"], "history")
    if not os.path.isdir(directory):
        return []
    days = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        day = name[:-5]
        if parspack_config.DATE_SEGMENT_RE.fullmatch(day):
            days.append(day)
    return sorted(days)


def dashboard_cli():
    raw = parspack_config.load_config_file()
    conf = parspack_config.validate_core(raw)
    requested = sys.argv[2] if len(sys.argv) > 2 else ""
    if requested and not parspack_config.DATE_SEGMENT_RE.fullmatch(requested):
        raise parspack_config.ConfigError("report date must look like YYYY-MM-DD")
    roots = parspack_config.split_roots(conf["LOCAL_READ_ROOTS"])
    days = sorted(set(list_history_days(conf)) | set(discover_dates(roots)))
    day = requested or (days[-1] if days else "")
    report = None
    if day:
        path = os.path.join(conf["STATE_DIR"], "history", day + ".json")
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    saved = json.load(handle)
            except ValueError:
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            rows = clean_rows(saved.get("accounts"))
            if not rows:
                rows = log_status_rows(conf, day)["rows"]
            checked = saved.get("checked_at") or ""
            if not checked:
                checked = datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            leftovers = []
            for item in saved.get("leftovers") or []:
                if isinstance(item, str):
                    leftovers.append(item)
            report = {
                "date": str(saved.get("date") or day),
                "status": saved.get("status") if saved.get("status") in ("OK", "WARNING", "PROBLEM", "CRITICAL", "EMPTY") else "EMPTY",
                "remote_ok": saved.get("remote_ok") if isinstance(saved.get("remote_ok"), bool) else None,
                "source": "history",
                "source_count": saved.get("source_count") if isinstance(saved.get("source_count"), int) else len(rows),
                "transferred_count": saved.get("transferred_count") if isinstance(saved.get("transferred_count"), int) else 0,
                "failed_count": saved.get("failed_count") if isinstance(saved.get("failed_count"), int) else 0,
                "transferred_bytes": saved.get("transferred_bytes") if isinstance(saved.get("transferred_bytes"), int) else 0,
                "remaining_bytes": saved.get("remaining_bytes") if isinstance(saved.get("remaining_bytes"), int) else 0,
                "started": str(saved.get("started") or ""),
                "finished": str(saved.get("finished") or ""),
                "checked_at": checked,
                "rows": rows,
                "leftovers": leftovers,
            }
        else:
            report = log_status_rows(conf, day)
    if report is not None and day in parspack_config.pending_system_days(conf["STATE_DIR"]):
        report["status"] = "CRITICAL"
    document = {
        "mode": conf["MODE"],
        "bucket": conf["PARSPACK_BUCKET"],
        "prefix": conf["PARSPACK_PREFIX"] or "",
        "days": days,
        "report": report,
    }
    sys.stdout.write(json.dumps(document, ensure_ascii=False) + "\n")


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "dashboard":
        dashboard_cli()
        return
    raw = parspack_config.load_config_file()
    conf = parspack_config.validate_core(raw)
    reports = conf["REPORT_DIR"]
    history = os.path.join(conf["STATE_DIR"], "history")
    parspack_config.ensure_private_dir(reports)
    parspack_config.ensure_private_dir(history)

    roots = parspack_config.split_roots(conf["LOCAL_READ_ROOTS"])
    requested = sys.argv[1] if len(sys.argv) > 1 else ""
    if requested and not parspack_config.DATE_SEGMENT_RE.fullmatch(requested):
        raise parspack_config.ConfigError("report date must look like YYYY-MM-DD")
    day = requested or (discover_dates(roots) or [datetime.date.today().isoformat()])[-1]
    snapshot = build_report(conf, day)
    facts = {
        "verify_failed": 0,
        "upload_failed": snapshot["failed_count"],
        "cleanup_failed": 0,
        "remote_bytes": snapshot["transferred_bytes"],
        "local_freed": 0,
        "run_state": "",
        "pending": snapshot["status"] == "CRITICAL",
        "remote_ok": snapshot["remote_ok"],
    }
    try:
        import parspack_ui
        db_path = os.path.join(conf["STATE_DIR"], "parspack.db")
        view = parspack_ui.dashboard_from_db(
            parspack_ui.connect_readonly(db_path),
            day,
            [],
            parspack_ui._secrets(raw),
        )
        facts = dict(view.get("facts") or facts)
        facts["remote_ok"] = snapshot["remote_ok"]
        if snapshot["status"] == "CRITICAL":
            facts["pending"] = True
    except Exception:
        import parspack_ui
    decision = parspack_ui.classify_alerts(snapshot, facts, int(raw.get("ALERT_REMOTE_BYTES") or 0))
    extra = parspack_ui.summary_text(
        day, decision["severity"], snapshot["rows"], facts, parspack_ui._secrets(raw)
    )
    report = render_report(snapshot) + "\n<pre>%s</pre>\n" % html.escape(extra)
    path = os.path.join(reports, "%s.html" % day)
    write_private(path, report)
    public = {
        "date": snapshot["date"],
        "status": snapshot["status"],
        "source_count": snapshot["source_count"],
        "transferred_count": snapshot["transferred_count"],
        "failed_count": snapshot["failed_count"],
        "transferred_bytes": snapshot["transferred_bytes"],
        "remaining_bytes": snapshot["remaining_bytes"],
        "started": snapshot["started"],
        "finished": snapshot["finished"],
        "remote_ok": snapshot["remote_ok"],
        "failed_accounts": [row["name"] for row in snapshot["rows"] if not row["ok"]],
        "leftovers": [item["name"] for item in snapshot["leftovers"]],
        "accounts": public_account_rows(snapshot["rows"]),
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    write_private(os.path.join(history, "%s.json" % day), json.dumps(public, ensure_ascii=False, indent=2))
    subjects = {
        "OK": "PARSPACK Backup موفق",
        "WARNING": "PARSPACK Backup هشدار",
        "PROBLEM": "PARSPACK Backup ناموفق",
        "CRITICAL": "PARSPACK Backup بحرانی",
    }
    subject = "%s - %s" % (subjects.get(snapshot["status"], "PARSPACK Backup"), day)

    def sender(_text):
        return send_email(raw, subject, report)

    def poster(url, payload, headers):
        parspack_ui.post_json(url, payload, headers)

    result = parspack_ui.dispatch_alerts(
        raw,
        snapshot,
        snapshot["rows"],
        facts,
        os.path.join(conf["STATE_DIR"], "alert-dedupe.json"),
        time.time(),
        sender,
        poster,
    )
    if result["reason"] == "duplicate":
        print("alert skipped: duplicate")
    elif result["reason"] == "skipped":
        print("email skipped: notification for this result is turned off")
    if result.get("channels_failed"):
        print("alert channel failed", file=sys.stderr)
    print(path)
    print("date=%s status=%s source=%d transferred=%d failed=%d bytes=%d start=%s end=%s" % (
        snapshot["date"],
        snapshot["status"],
        snapshot["source_count"],
        snapshot["transferred_count"],
        snapshot["failed_count"],
        snapshot["transferred_bytes"],
        snapshot["started"] or "-",
        snapshot["finished"] or "-",
    ))


if __name__ == "__main__":
    try:
        main()
    except parspack_config.ConfigError as exc:
        for item in exc.errors:
            print("config error: %s" % item, file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print("error: %s" % exc.__class__.__name__, file=sys.stderr)
        sys.exit(1)
