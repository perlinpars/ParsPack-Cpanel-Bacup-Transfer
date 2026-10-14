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
import parspack_ui  # noqa: E402


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


def source_system_files(roots, day):
    """Relative paths under system/ still on the local day tree."""
    found = {}
    for root in roots:
        folder = os.path.join(root, day, "system")
        if os.path.islink(folder) or not os.path.isdir(folder):
            continue
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                rel = os.path.relpath(path, folder).replace("\\", "/")
                try:
                    found[rel] = os.path.getsize(path)
                except OSError:
                    continue
        break
    return found


def _mc_ls_json(conf, target, recursive=False):
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "TMPDIR": os.path.join(ROOT, "tmp"),
        "LANG": "C",
    }
    command = [conf["MC_BIN"], "--config-dir", conf["MC_CONFIG_DIR"], "ls", "--json"]
    if recursive:
        command.append("--recursive")
    command.extend(["--", target])
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def list_remote_accounts(conf, day):
    target = parspack_config.object_target(conf, day + "/accounts", require_child=True) + "/"
    text = _mc_ls_json(conf, target, recursive=False)
    if text is None:
        return {}, [], False
    accounts = {}
    leftovers = []
    for line in text.splitlines():
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


def list_remote_system_files(conf, day):
    """Remote files under <day>/system/. Keys are relative paths; excludes .uploading."""
    target = parspack_config.object_target(conf, day + "/system", require_child=True) + "/"
    text = _mc_ls_json(conf, target, recursive=True)
    if text is None:
        return {}, False
    files = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or str(row.get("type", "")).lower() == "folder":
            continue
        key = str(row.get("key") or row.get("name") or "").replace("\\", "/").lstrip("/")
        leaf = key.rstrip("/").split("/")[-1]
        if not leaf or leaf.endswith(".uploading") or leaf == ".keep":
            continue
        size = row.get("size")
        if not isinstance(size, int):
            size = 0
        # Prefer path relative to system/
        rel = key
        marker = "/system/"
        if marker in "/" + key:
            rel = key.split("system/", 1)[-1]
        files[rel] = size
    return files, True


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


def reason_fa(message):
    """Map common English engine/log lines to short Persian explanations."""
    return parspack_ui.reason_fa(message)


def status_cause_fa(status, db_note=""):
    """One Persian sentence for the reports card / email status."""
    if db_note:
        return db_note
    if status == "CRITICAL":
        return (
            "حساب‌های کاربری ممکن است منتقل شده باشند، ولی پوشه system و "
            "manifest.json هنوز تمام و تأیید نشده‌اند. برای تکمیل روز از ارسال مانده‌ها استفاده کنید."
        )
    if status == "PROBLEM":
        return "برخی حساب‌ها منتقل نشدند یا فهرست مقصد خوانده نشد."
    if status == "WARNING":
        return "انتقال اصلی انجام شد ولی هشدارهایی (مثل فایل محلی مانده یا prune مقصد) وجود دارد."
    if status == "EMPTY":
        return "برای این روز هنوز انتقالی ثبت نشده است."
    return ""


def db_day_note(status, day, state_dir="", accounts_complete=False):
    """Raise OK to WARNING from the state database. A pending marker stays CRITICAL."""
    state_dir = state_dir or os.path.join(ROOT, "state")
    notes = []
    db_path = os.path.join(state_dir, "parspack.db")
    if os.path.isfile(db_path):
        try:
            import parspack_state
            conn = parspack_state.connect(db_path)
            flags = parspack_state.day_flags(conn, day)
        except Exception:
            flags = None
        if flags:
            if flags["run_state"] == "partial":
                if status == "OK":
                    status = "WARNING"
                if accounts_complete:
                    notes.append(
                        "مهلت انتقال تمام شد؛ حساب‌ها منتقل شده‌اند ولی "
                        "پوشه system و manifest.json ارسال نشد."
                    )
                else:
                    notes.append("مهلت انتقال تمام شد و این روز ناقص مانده است. manifest.json ارسال نشد.")
            if flags["cleanup"]:
                if status == "OK":
                    status = "WARNING"
                notes.append("حذف فایل محلی بعد از تأیید مقصد انجام نشد.")
            if flags["size_only"]:
                if status == "OK":
                    status = "WARNING"
                notes.append("برخی فایل‌ها فقط با اندازه تأیید شدند.")
    if day in parspack_config.retention_failed_days(state_dir):
        if status == "OK":
            status = "WARNING"
        notes.append(
            "پاک‌سازی بکاپ‌های قدیمی مقصد (پنجرهٔ نگهداری) تمام نشد؛ "
            "انتقال این روز انجام شده است."
        )
    return status, " ".join(notes)


def build_report(conf, day):
    roots = parspack_config.split_roots(conf["LOCAL_READ_ROOTS"])
    local = source_accounts(roots, day)
    local_system = source_system_files(roots, day)
    remote, leftovers, remote_ok = list_remote_accounts(conf, day)
    remote_system, system_ok_listing = list_remote_system_files(conf, day)
    if not system_ok_listing:
        remote_system = {}
    outcomes, started, finished = parse_log(os.path.join(conf["LOG_DIR"], "backup.log"), day)
    # Account rows only (users under accounts/). System files are counted separately.
    names = sorted(set(local) | set(remote) | {
        name for name, item in outcomes.items() if item.get("scope") != "system"
    })
    rows = []
    transferred_bytes = 0
    remaining_bytes = 0
    for name in names:
        on_remote = name in remote
        on_local = name in local
        outcome = outcomes.get(name, {})
        if on_remote:
            state = "منتقل شد"
            transferred_bytes += remote[name]
            if on_local:
                state = "در مقصد هست و فایل محلی هم مانده"
                remaining_bytes += local[name]
            ok = True
        else:
            state = "منتقل نشد"
            remaining_bytes += local.get(name, 0)
            ok = False
        reason = outcome.get("reason") or ""
        if on_remote and outcome.get("status") == "failed":
            reason = "خطای قبلی بود و بعداً محتوا تأیید شد"
        else:
            reason = reason_fa(reason)
        rows.append({
            "name": name,
            "user": account_user(name),
            "state": state,
            "remote_size": remote.get(name, 0),
            "local_size": local.get(name, 0),
            "reason": reason,
            "start": outcome.get("start", ""),
            "end": outcome.get("end", ""),
            "ok": ok,
            "kind": "account",
        })
    failed = [row for row in rows if not row["ok"]]
    account_expected = len(set(local) | set(remote))
    # Prefer remote count when local accounts were already deleted after success.
    account_transferred = len(remote)
    account_failed = len(failed)
    system_expected = len(set(local_system) | set(remote_system))
    system_transferred = len(remote_system)
    for rel, size in remote_system.items():
        transferred_bytes += int(size or 0)
    for rel, size in local_system.items():
        if rel not in remote_system:
            remaining_bytes += int(size or 0)
    files_expected = account_expected + system_expected
    files_transferred = account_transferred + system_transferred
    accounts_complete = account_expected > 0 and account_failed == 0 and account_transferred >= account_expected
    if account_expected == 0 and account_transferred > 0 and account_failed == 0:
        accounts_complete = True
    warning = bool(leftovers) or any(row["ok"] and row["local_size"] for row in rows)
    if not remote_ok:
        status = "PROBLEM"
    elif failed:
        status = "PROBLEM"
    elif warning:
        status = "WARNING"
    else:
        status = "OK"
    status, db_note = db_day_note(status, day, conf["STATE_DIR"], accounts_complete=accounts_complete)
    pending = day in parspack_config.pending_system_days(conf["STATE_DIR"])
    if pending:
        status = "CRITICAL"
        if accounts_complete:
            primary = (
                "حساب‌های کاربری منتقل شده‌اند، ولی پوشه system و manifest.json "
                "هنوز تمام و تأیید نشده‌اند. برای تکمیل روز از ارسال مانده‌ها استفاده کنید."
            )
        else:
            primary = "انتقال فولدر system برای این روز تمام و تأیید نشده است."
        extras = []
        for part in (db_note or "").replace("؟", ".").split("."):
            chunk = part.strip()
            if not chunk:
                continue
            if any(token in chunk for token in ("system", "manifest", "مهلت انتقال", "ناقص مانده")):
                continue
            extras.append(chunk)
        db_note = primary
        if extras:
            db_note = primary + " " + ". ".join(extras) + "."
    status_note = status_cause_fa(status, db_note)
    return {
        "date": day,
        "status": status,
        "remote_ok": remote_ok,
        "source_count": account_expected,
        "transferred_count": account_transferred,
        "failed_count": account_failed,
        "account_expected": account_expected,
        "account_transferred": account_transferred,
        "account_failed": account_failed,
        "system_expected": system_expected,
        "system_transferred": system_transferred,
        "files_expected": files_expected,
        "files_transferred": files_transferred,
        "transferred_bytes": transferred_bytes,
        "remaining_bytes": remaining_bytes,
        "started": started,
        "finished": finished,
        "rows": rows,
        "leftovers": leftovers,
        "db_note": db_note,
        "status_note": status_note,
        "accounts_complete": accounts_complete,
        "system_pending": pending,
    }


def render_report(snapshot):
    status_class = {"OK": "ok", "WARNING": "warning", "PROBLEM": "problem", "CRITICAL": "problem"}[snapshot["status"]]
    status_text = {"OK": "کامل", "WARNING": "هشدار", "PROBLEM": "ناقص", "CRITICAL": "بحرانی"}[snapshot["status"]]
    account_expected = int(snapshot.get("account_expected", snapshot.get("source_count") or 0) or 0)
    account_transferred = int(snapshot.get("account_transferred", snapshot.get("transferred_count") or 0) or 0)
    account_failed = int(snapshot.get("account_failed", snapshot.get("failed_count") or 0) or 0)
    system_expected = int(snapshot.get("system_expected") or 0)
    system_transferred = int(snapshot.get("system_transferred") or 0)
    files_expected = int(snapshot.get("files_expected") or (account_expected + system_expected))
    files_transferred = int(snapshot.get("files_transferred") or (account_transferred + system_transferred))
    body = []
    for row in snapshot["rows"]:
        body.append(
            "<tr class=\"%s\"><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                "good" if row["ok"] else "bad",
                html.escape(row.get("user") or account_user(row["name"])),
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
            "<li>%s <span class=\"muted\">(%s)</span></li>"
            % (html.escape(item["name"]), html.escape(human_size(item["size"])))
        )
    notes = []
    if not snapshot["remote_ok"]:
        notes.append("فهرست مقصد خوانده نشد، پس تعداد منتقل‌شده کامل نیست.")
    cause = snapshot.get("status_note") or status_cause_fa(snapshot["status"], snapshot.get("db_note") or "")
    if cause:
        notes.append(cause)
    note_html = "".join("<p class=\"note\">%s</p>" % html.escape(item) for item in notes)
    return """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>گزارش بکاپ %s</title>
<style>
body{font-family:Tahoma,Arial,sans-serif;margin:0;padding:24px;background:#f4f7fb;color:#1f2933;line-height:1.7}
.wrap{max-width:920px;margin:0 auto;background:#fff;border:1px solid #d9e2ec;border-radius:12px;overflow:hidden}
.header{padding:20px 24px;border-bottom:1px solid #e4ebf2}
.header h1{margin:0 0 6px;font-size:22px}
.header .day{color:#627d98;font-size:14px}
.banner{padding:14px 24px;font-size:15px}
.ok{background:#e8f5e9;color:#1b5e20}.warning{background:#fff8e1;color:#8d6b00}.problem{background:#ffebee;color:#9b1c1c}
.section{padding:18px 24px;border-top:1px solid #eef2f5}
.section h2{margin:0 0 12px;font-size:16px}
.grid{display:flex;flex-wrap:wrap;gap:10px}
.stat{flex:1 1 140px;background:#f7f9fb;border:1px solid #e4ebf2;border-radius:10px;padding:12px}
.stat .k{display:block;color:#627d98;font-size:12px;margin-bottom:4px}
.stat .v{display:block;font-size:18px;font-weight:bold}
.stat .s{display:block;color:#627d98;font-size:12px;margin-top:4px}
.note{margin:0 0 8px;padding:10px 12px;background:#f7f9fb;border-right:3px solid #486581;border-radius:6px}
table{border-collapse:collapse;width:100%%}th,td{border:1px solid #e4ebf2;padding:8px 10px;text-align:right;font-size:13px}
th{background:#f7f9fb;color:#486581}tr.bad td{background:#fff5f5}tr.good td{background:#f8fff8}
ul{margin:0;padding:0 18px 0 0}.muted{color:#829ab1}
</style></head><body><div class="wrap">
<div class="header"><h1>گزارش بکاپ پارس‌پک</h1><div class="day">روز %s</div></div>
<div class="banner %s"><strong>وضعیت: %s</strong></div>
<div class="section">%s
<div class="grid">
<div class="stat"><span class="k">حساب‌های منتقل‌شده</span><span class="v">%d / %d</span><span class="s">فایل‌های accounts — منتقل‌نشده: %d</span></div>
<div class="stat"><span class="k">کل فایل‌های منتقل‌شده</span><span class="v">%d / %d</span><span class="s">accounts + system (system: %d / %d)</span></div>
<div class="stat"><span class="k">حجم منتقل‌شده</span><span class="v">%s</span><span class="s">مانده در مبدأ: %s</span></div>
<div class="stat"><span class="k">بازه زمانی</span><span class="v" style="font-size:14px">%s</span><span class="s">تا %s</span></div>
</div></div>
<div class="section"><h2>فایل‌های اضافی مقصد</h2><ul>%s</ul></div>
<div class="section"><h2>جزئیات حساب‌ها</h2>
<table><tr><th>حساب</th><th>وضعیت</th><th>حجم مقصد</th><th>حجم مبدأ</th><th>آخرین زمان</th><th>توضیح</th></tr>
%s</table></div>
</div></body></html>""" % (
        html.escape(snapshot["date"]),
        html.escape(snapshot["date"]),
        status_class,
        html.escape(status_text),
        note_html,
        account_transferred,
        account_expected,
        account_failed,
        files_transferred,
        files_expected,
        system_transferred,
        system_expected,
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
    # Newest last in sorted list; keep only the last 10 calendar days that exist.
    days = days[-10:] if len(days) > 10 else days
    day = requested or (days[-1] if days else "")
    if day and days and day not in days:
        day = days[-1]
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
            note = str(saved.get("status_note") or saved.get("db_note") or "")
            source_count = saved.get("source_count") if isinstance(saved.get("source_count"), int) else len(rows)
            transferred_count = saved.get("transferred_count") if isinstance(saved.get("transferred_count"), int) else 0
            failed_count = saved.get("failed_count") if isinstance(saved.get("failed_count"), int) else 0
            account_expected = saved.get("account_expected") if isinstance(saved.get("account_expected"), int) else source_count
            account_transferred = saved.get("account_transferred") if isinstance(saved.get("account_transferred"), int) else transferred_count
            account_failed = saved.get("account_failed") if isinstance(saved.get("account_failed"), int) else failed_count
            system_expected = saved.get("system_expected") if isinstance(saved.get("system_expected"), int) else 0
            system_transferred = saved.get("system_transferred") if isinstance(saved.get("system_transferred"), int) else 0
            files_expected = saved.get("files_expected") if isinstance(saved.get("files_expected"), int) else (account_expected + system_expected)
            files_transferred = saved.get("files_transferred") if isinstance(saved.get("files_transferred"), int) else (account_transferred + system_transferred)
            report = {
                "date": str(saved.get("date") or day),
                "status": saved.get("status") if saved.get("status") in ("OK", "WARNING", "PROBLEM", "CRITICAL", "EMPTY") else "EMPTY",
                "remote_ok": saved.get("remote_ok") if isinstance(saved.get("remote_ok"), bool) else None,
                "source": "history",
                "source_count": source_count,
                "transferred_count": transferred_count,
                "failed_count": failed_count,
                "account_expected": account_expected,
                "account_transferred": account_transferred,
                "account_failed": account_failed,
                "system_expected": system_expected,
                "system_transferred": system_transferred,
                "files_expected": files_expected,
                "files_transferred": files_transferred,
                "transferred_bytes": saved.get("transferred_bytes") if isinstance(saved.get("transferred_bytes"), int) else 0,
                "remaining_bytes": saved.get("remaining_bytes") if isinstance(saved.get("remaining_bytes"), int) else 0,
                "started": str(saved.get("started") or ""),
                "finished": str(saved.get("finished") or ""),
                "checked_at": checked,
                "rows": rows,
                "leftovers": leftovers,
                "status_note": note,
                "db_note": note,
                "accounts_complete": bool(saved.get("accounts_complete")),
                "system_pending": bool(saved.get("system_pending")),
            }
        else:
            report = log_status_rows(conf, day)
    if report is not None:
        if day in parspack_config.pending_system_days(conf["STATE_DIR"]):
            report["status"] = "CRITICAL"
            report["system_pending"] = True
            transferred = int(report.get("transferred_count") or 0)
            source = int(report.get("source_count") or 0)
            failed = int(report.get("failed_count") or 0)
            accounts_complete = failed == 0 and transferred > 0 and transferred >= source
            report["accounts_complete"] = accounts_complete
            if accounts_complete:
                note = (
                    "حساب‌های کاربری منتقل شده‌اند، ولی پوشه system و "
                    "manifest.json هنوز تمام و تأیید نشده‌اند. برای تکمیل روز "
                    "از ارسال مانده‌ها استفاده کنید."
                )
                existing = str(report.get("db_note") or report.get("status_note") or "")
                if note not in existing:
                    report["status_note"] = (note + " " + existing).strip()
                else:
                    report["status_note"] = existing or note
                report["db_note"] = report["status_note"]
            else:
                report["status_note"] = status_cause_fa("CRITICAL", report.get("db_note") or "")
        elif day in parspack_config.retention_failed_days(conf["STATE_DIR"]):
            if report.get("status") == "OK":
                report["status"] = "WARNING"
            note = report.get("status_note") or ""
            retention_msg = (
                "پاک‌سازی بکاپ‌های قدیمی مقصد (پنجرهٔ نگهداری) تمام نشد؛ "
                "انتقال این روز انجام شده است."
            )
            if retention_msg not in note:
                report["status_note"] = (note + " " + retention_msg).strip()
                report["db_note"] = report["status_note"]
        elif not report.get("status_note"):
            report["status_note"] = status_cause_fa(report.get("status") or "", report.get("db_note") or "")
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
        "accounts_ok": int(snapshot.get("account_transferred") or snapshot["transferred_count"] or 0),
        "accounts_failed": int(snapshot.get("account_failed") or snapshot["failed_count"] or 0),
        "accounts_complete": bool(snapshot.get("accounts_complete")),
    }
    try:
        db_path = os.path.join(conf["STATE_DIR"], "parspack.db")
        pending_days = parspack_config.pending_system_days(conf["STATE_DIR"])
        retention_days = parspack_config.retention_failed_days(conf["STATE_DIR"])
        view = parspack_ui.dashboard_from_db(
            parspack_ui.connect_readonly(db_path),
            day,
            ["system-pending-%s" % item for item in pending_days],
            parspack_ui._secrets(raw),
            retention_days,
        )
        facts = dict(view.get("facts") or facts)
        facts["remote_ok"] = snapshot["remote_ok"]
        if snapshot["status"] == "CRITICAL":
            facts["pending"] = True
        facts["retention_failed"] = day in retention_days
        facts["accounts_complete"] = bool(snapshot.get("accounts_complete"))
    except Exception:
        facts["retention_failed"] = day in parspack_config.retention_failed_days(conf["STATE_DIR"])
        facts["accounts_complete"] = bool(snapshot.get("accounts_complete"))
    decision = parspack_ui.classify_alerts(snapshot, facts, int(raw.get("ALERT_REMOTE_BYTES") or 0))
    report = render_report(snapshot)
    path = os.path.join(reports, "%s.html" % day)
    write_private(path, report)
    public = {
        "date": snapshot["date"],
        "status": snapshot["status"],
        "source_count": snapshot["source_count"],
        "transferred_count": snapshot["transferred_count"],
        "failed_count": snapshot["failed_count"],
        "account_expected": snapshot.get("account_expected", snapshot["source_count"]),
        "account_transferred": snapshot.get("account_transferred", snapshot["transferred_count"]),
        "account_failed": snapshot.get("account_failed", snapshot["failed_count"]),
        "system_expected": snapshot.get("system_expected", 0),
        "system_transferred": snapshot.get("system_transferred", 0),
        "files_expected": snapshot.get("files_expected", snapshot["source_count"]),
        "files_transferred": snapshot.get("files_transferred", snapshot["transferred_count"]),
        "transferred_bytes": snapshot["transferred_bytes"],
        "remaining_bytes": snapshot["remaining_bytes"],
        "started": snapshot["started"],
        "finished": snapshot["finished"],
        "remote_ok": snapshot["remote_ok"],
        "failed_accounts": [row["name"] for row in snapshot["rows"] if not row["ok"]],
        "leftovers": [item["name"] for item in snapshot["leftovers"]],
        "accounts": public_account_rows(snapshot["rows"]),
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status_note": snapshot.get("status_note") or "",
        "db_note": snapshot.get("db_note") or "",
        "accounts_complete": bool(snapshot.get("accounts_complete")),
        "system_pending": bool(snapshot.get("system_pending")),
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
