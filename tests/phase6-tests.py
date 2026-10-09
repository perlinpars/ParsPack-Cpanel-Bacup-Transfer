#!/usr/bin/env python3
import json
import os
import sys
import tempfile

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_config  # noqa: E402
import parspack_state  # noqa: E402
import parspack_ui  # noqa: E402


def fail(message):
    sys.stderr.write("FAIL %s\n" % message)
    sys.exit(1)


def test_dashboard():
    tmp = tempfile.mkdtemp(prefix="pp-ui-")
    db_path = os.path.join(tmp, "parspack.db")
    writer = parspack_state.connect(db_path)
    writer.execute(
        "INSERT INTO backup_days (backup_date, created_at, updated_at, run_state) VALUES (?,?,?,?)",
        ("2026-10-08", "2026-10-08T01:00:00", "2026-10-08T03:00:00", "partial"),
    )
    rows = [
        ("ok", "VERIFIED", 100, "sha256", "", ""),
        ("gone", "LOCAL_DELETED", 40, "etag", "", ""),
        ("stuck", "LOCAL_CLEANUP_FAILED", 5, "size-only", "", ""),
        ("bad", "UPLOAD_FAILED", 9, "", "auth", "secret-value-12345678 leaked"),
        ("check", "VERIFY_FAILED", 8, "", "checksum", "mismatch"),
    ]
    for account, status, size, level, error_class, error_message in rows:
        writer.execute(
            """
            INSERT INTO uploads (
                backup_date, account, local_path, remote_key, size, status,
                verification_level, error_class, error_message, attempts,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)
            """,
            (
                "2026-10-08",
                account,
                os.path.join(tmp, account + ".tar.gz"),
                "cpanel/2026-10-08/accounts/%s.tar.gz" % account,
                size,
                status,
                level,
                error_class,
                error_message,
                "2026-10-08T02:00:00",
                "2026-10-08T03:00:00",
            ),
        )
    writer.commit()
    writer.close()
    conn = parspack_ui.connect_readonly(db_path)
    view = parspack_ui.dashboard_from_db(
        conn, "", ["system-pending-2026-10-08"], ["secret-value-12345678"]
    )
    if view["accounts_ok"] != 3 or view["accounts_failed"] != 2:
        fail("counts %s %s" % (view["accounts_ok"], view["accounts_failed"]))
    if view["remote_bytes"] != 145 or view["local_freed_bytes"] != 40:
        fail("bytes %s %s" % (view["remote_bytes"], view["local_freed_bytes"]))
    if view["system_status"] != "pending":
        fail("system status %s" % view["system_status"])
    if any("local_path" in row for row in view["failed"]):
        fail("failed row exposed a path")
    blob = json.dumps(view)
    if "secret-value-12345678" in blob or ".tar.gz" in blob:
        fail("dashboard leaked a secret or a path")
    if view["verification"]["levels"]["sha256"] != 1:
        fail("verification level was not counted")
    over = parspack_ui.overlay_report(view, os.path.join(tmp, "missing-history"), 10)
    if not any(item["text"].startswith("recorded remote") for item in over["warnings"]):
        fail("threshold warning was missing")
    try:
        parspack_ui.connect_readonly(os.path.join(tmp, "missing.db"))
        fail("missing database was opened")
    except parspack_ui.UiError:
        pass


def test_alerts():
    facts = {
        "verify_failed": 0,
        "upload_failed": 0,
        "cleanup_failed": 0,
        "remote_bytes": 10,
        "local_freed": 0,
        "run_state": "complete",
        "pending": False,
        "remote_ok": True,
    }
    if parspack_ui.classify_alerts({"status": "OK", "remote_ok": True}, facts, 0)["severity"] != "info":
        fail("clean day was not info")
    facts["upload_failed"] = 1
    if parspack_ui.classify_alerts({"status": "OK", "remote_ok": True}, facts, 0)["severity"] != "warning":
        fail("failed account was not a warning")
    facts["verify_failed"] = 1
    if parspack_ui.classify_alerts({"status": "OK", "remote_ok": True}, facts, 0)["severity"] != "critical":
        fail("verification failure was not critical")
    facts["verify_failed"] = 0
    facts["pending"] = True
    if parspack_ui.classify_alerts({"status": "OK", "remote_ok": True}, facts, 0)["severity"] != "critical":
        fail("pending system was not critical")
    facts["pending"] = False
    if parspack_ui.classify_alerts({"status": "OK", "remote_ok": False}, facts, 0)["severity"] != "critical":
        fail("remote outage was not critical")
    facts = {
        "verify_failed": 0, "upload_failed": 0, "cleanup_failed": 1,
        "remote_bytes": 50, "local_freed": 0, "run_state": "complete",
        "pending": False, "remote_ok": True,
    }
    if parspack_ui.classify_alerts({"status": "OK", "remote_ok": True}, facts, 40)["severity"] != "warning":
        fail("threshold or cleanup was not a warning")
    path = os.path.join(tempfile.mkdtemp(prefix="pp-dedupe-"), "alert-dedupe.json")
    warnings = [{"severity": "warning", "text": "some accounts failed"}]
    token = parspack_ui.fingerprint("2026-10-08", "warning", warnings)
    if not parspack_ui.claim_dedupe(path, token, "warning", 1000):
        fail("first alert was suppressed")
    if parspack_ui.claim_dedupe(path, token, "warning", 1001):
        fail("duplicate alert was sent")
    higher = parspack_ui.fingerprint("2026-10-08", "critical", [{"severity": "critical", "text": "verification failed"}])
    if not parspack_ui.claim_dedupe(path, higher, "critical", 1002):
        fail("higher severity was suppressed")
    sent = []

    def sender(text):
        sent.append(text)
        return True

    def poster(url, payload, headers):
        fail("disabled channel was called")

    raw = {"ALERT_REMOTE_BYTES": "0", "NOTIFY_ON_SUCCESS": "1", "NOTIFY_ON_FAILURE": "1", "SMTP_ENABLED": "0"}
    first = parspack_ui.dispatch_alerts(
        raw, {"date": "2026-10-08", "status": "OK", "remote_ok": True},
        [{"name": "alice", "ok": True}, {"name": "bob", "ok": False, "reason": "timeout"}],
        facts, path + ".2", 2000, sender, poster,
    )
    if first["reason"] != "sent" or "SUCCESS alice" not in sent[0] or "FAILED bob: timeout" not in sent[0]:
        fail("account report was incomplete")
    second = parspack_ui.dispatch_alerts(
        raw, {"date": "2026-10-08", "status": "OK", "remote_ok": True},
        [{"name": "alice", "ok": True}], facts, path + ".2", 2001, sender, poster,
    )
    if second["reason"] != "duplicate" or len(sent) != 1:
        fail("repeat alert was not deduplicated")


def test_channels_and_settings():
    if parspack_config.public_https_url("http://hooks.example.com/a"):
        fail("http URL was accepted")
    if parspack_config.public_https_url("https://user:pass@hooks.example.com/a"):
        fail("URL userinfo was accepted")
    if parspack_config.public_https_url("https://hooks.example.com/a") != "https://hooks.example.com/a":
        fail("https URL was rejected")
    token = "123456789:telegramtokenvalue1234567890"
    raw = {
        "WEBHOOK_ENABLED": "0",
        "WEBHOOK_URL": "https://hooks.example.com/a",
        "WEBHOOK_SECRET": "webhook-secret-value",
        "TELEGRAM_ENABLED": "1",
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT": "42",
        "SLACK_ENABLED": "0",
    }
    calls = parspack_ui.channel_messages(raw, {"text": "hello", "severity": "info"})
    if len(calls) != 1 or token not in calls[0][0] or token in calls[0][1]["text"]:
        fail("telegram token leaked into the message")
    view = parspack_config.mail_settings_view({
        "MODE": "PRODUCTION",
        "PARSPACK_ENDPOINT": "https://s3.example.invalid",
        "PARSPACK_BUCKET": "bucket",
        "PARSPACK_PREFIX": "cpanel",
        "SMTP_PASSWORD": "smtp-password-value",
        "WEBHOOK_SECRET": "webhook-secret-value",
        "TELEGRAM_BOT_TOKEN": token,
        "SLACK_WEBHOOK_URL": "https://hooks.slack.test/services/secretvalue",
        "PARSPACK_ACCESS_KEY": "access-key-value",
        "PARSPACK_SECRET_KEY": "secret-key-value",
        "TRANSPORT_PASSWORD": "transport-password-value",
    })
    encoded = json.dumps(view)
    for secret in (
        "smtp-password-value",
        "webhook-secret-value",
        token,
        "https://hooks.slack.test/services/secretvalue",
        "access-key-value",
        "secret-key-value",
        "transport-password-value",
    ):
        if secret in encoded:
            fail("settings view exposed a secret")
    if view["smtp_password_set"] != "1" or view["secret_key"] != "Configured":
        fail("configured flag was missing")
    audit_path = os.path.join(tempfile.mkdtemp(prefix="pp-audit-"), "audit.log")
    parspack_ui.append_audit(audit_path, "root", ["SMTP_PASSWORD", "WEBHOOK_URL"])
    line = open(audit_path, "r", encoding="utf-8").read()
    if "SMTP_PASSWORD" not in line or "smtp-password-value" in line:
        fail("audit line did not record the key name only")
    rate_path = os.path.join(tempfile.mkdtemp(prefix="pp-rate-"), "rate.json")
    if not parspack_ui.rate_allow(rate_path, 5000, limit=2):
        fail("first action was limited")
    if not parspack_ui.rate_allow(rate_path, 5001, limit=2):
        fail("second action was limited")
    if parspack_ui.rate_allow(rate_path, 5002, limit=2):
        fail("third action was allowed")


if __name__ == "__main__":
    test_dashboard()
    test_alerts()
    test_channels_and_settings()
    print("OK")
