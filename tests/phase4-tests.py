#!/usr/bin/env python3
import os
import sys
import tempfile

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_retention  # noqa: E402
import parspack_state  # noqa: E402


def fail(message):
    sys.stderr.write("FAIL %s\n" % message)
    sys.exit(1)


CONF = {"MODE": "PRODUCTION", "ALLOW_REMOTE_DELETE": "1", "PARSPACK_PREFIX": "cpanel"}
TODAY = "2026-10-08"
NEWEST = "2026-10-07"


def refused(path, kind="rmdir", conf=None, newest=NEWEST):
    try:
        parspack_retention.authorize(conf or CONF, path, kind, TODAY, newest)
    except parspack_retention.RetentionError as exc:
        return exc.message
    fail("allowed %s" % path)


def test_guards():
    samples = {
        "": "refused: bucket root",
        "/": "refused: bucket root",
        "..": "refused: path traversal",
        "../secret": "refused: path traversal",
        "2026-09-01/../../etc": "refused: path traversal",
        "weekly": "refused: not a date folder",
        "2026-13-40": "refused: not a date folder",
        "2026-02-31": "refused: not a date folder",
        "cpanel": "refused: not a date folder",
        TODAY: "refused: today's folder",
        TODAY + "/accounts/a.tar.gz": "refused: today's folder",
        NEWEST: "refused: newest verified day",
        NEWEST + "/accounts/a.tar.gz": "refused: newest verified day",
    }
    for path, expected in samples.items():
        kind = "delete" if "/" in path and path not in ("/", "../secret", "2026-09-01/../../etc") else "rmdir"
        if path in (TODAY + "/accounts/a.tar.gz", NEWEST + "/accounts/a.tar.gz"):
            kind = "delete"
        message = refused(path, kind)
        if message != expected:
            fail("%s -> %s" % (path, message))
    if parspack_retention.authorize(CONF, "2026-09-01", "rmdir", TODAY, NEWEST) != "2026-09-01":
        fail("older date folder was refused")
    if parspack_retention.authorize(CONF, "2026-09-01/accounts/a.tar.gz", "delete", TODAY, NEWEST) != "2026-09-01":
        fail("file inside an older date folder was refused")
    message = refused("2026-09-01", conf={"MODE": "PRODUCTION", "ALLOW_REMOTE_DELETE": "0", "PARSPACK_PREFIX": "cpanel"})
    if message != "refused: remote delete disabled":
        fail(message)
    message = refused("2026-09-01", conf={"MODE": "TEST", "ALLOW_REMOTE_DELETE": "1", "PARSPACK_PREFIX": "cpanel"})
    if message != "refused: remote delete disabled":
        fail(message)
    message = refused("2026-09-01", conf={"MODE": "PRODUCTION", "ALLOW_REMOTE_DELETE": "1", "PARSPACK_PREFIX": ""})
    if message != "refused: empty prefix":
        fail(message)
    message = refused("2026-09-01", newest=None)
    if message != "refused: newest verified day is unknown":
        fail(message)
    try:
        parspack_retention.keys_from_listing('{"key":"../x","size":1}')
        fail("hostile listing was accepted")
    except parspack_retention.RetentionError as exc:
        if exc.message != "refused: listing could not be parsed":
            fail(exc.message)
    try:
        parspack_retention.keys_from_listing("{")
        fail("broken listing was accepted")
    except parspack_retention.RetentionError:
        pass


def test_many_dates():
    days = ["2026-08-01", "2026-08-15", "2026-09-01", "2026-09-20", "2026-10-07"]
    bucket = {day + "/accounts/a.tar.gz": 10 for day in days}
    remove, reason = parspack_retention.plan_cpanel(days, ["2026-10-07"], 2, False)
    if reason != "ok" or set(remove) != {"2026-08-01", "2026-08-15", "2026-09-01"}:
        fail("plan %s %s" % (remove, reason))
    kept = dict(bucket)
    for day in remove:
        block = parspack_retention.guard_blocks(day, TODAY, NEWEST)
        if block:
            continue
        for key in list(kept):
            if key.startswith(day + "/"):
                del kept[key]
    if any(key.startswith("2026-10-07/") for key in kept) is False:
        fail("newest verified day was removed")
    if any(key.startswith("2026-08-01/") for key in kept):
        fail("an old day survived the plan")
    held, why = parspack_retention.plan_cpanel(["2026-10-08", "2026-09-01"], ["2026-09-01"], 1, False)
    if held or "would not prune" not in why:
        fail("default mode pruned after an unverified newest day")
    whm = parspack_retention.parse_whm_retention("BACKUPDAYS=daily,weekly\nBACKUP_DAILY_RETENTION=2\n")
    if whm["count"] is not None:
        fail("ambiguous retention was accepted")
    whm = parspack_retention.parse_whm_retention('BACKUPDAYS="daily"\nBACKUP_DAILY_RETENTION="2"\n')
    lines = parspack_retention.dry_run_lines(
        [(day, 40) for day in days], whm, TODAY, NEWEST, 10
    )
    text = "\n".join(lines)
    if "would-remove 2026-08-01" not in text or "mc rm" in text or "storage-warning yes" not in text:
        fail(text)
    unknown = parspack_retention.parse_whm_retention("")
    lines = parspack_retention.dry_run_lines([(day, 1) for day in days], unknown, TODAY, NEWEST, 0)
    if "would-remove none" not in lines or "retention-count unknown" not in lines:
        fail("unknown retention predicted a delete")


def test_keep_window():
    today = "2026-10-09"
    window = dict(CONF)
    window["REMOTE_KEEP_DAYS"] = "10"
    if parspack_retention.keep_cutoff(today, 10) != "2026-09-29":
        fail("keep cutoff was wrong")
    if parspack_retention.keep_cutoff(today, 20) != "2026-09-19":
        fail("20 day cutoff was wrong")
    days = ["2026-08-01", "2026-09-28", "2026-09-29", "2026-10-01", "2026-10-09"]
    remove, reason = parspack_retention.plan_keep_window(days, today, 10, "2026-10-01")
    if reason != "ok" or remove != ["2026-08-01", "2026-09-28"]:
        fail("keep window plan %s %s" % (remove, reason))
    held, why = parspack_retention.plan_keep_window(days, today, 10, None)
    if held or why != "newest verified day is unknown":
        fail("unknown newest day was pruned")
    older_newest, why = parspack_retention.plan_keep_window(days, today, 10, "2026-08-01")
    if "2026-08-01" in older_newest or why != "ok":
        fail("newest verified day was planned for removal")
    message = refused("2026-10-01", conf=window, newest="2026-09-01")
    if message != "refused: inside the keep window":
        fail(message)
    message = refused("2026-09-29", conf=window, newest="2026-09-01")
    if message != "refused: inside the keep window":
        fail(message)
    if parspack_retention.authorize(window, "2026-09-28", "rmdir", today, "2026-10-01") != "2026-09-28":
        fail("a day older than the window was refused")
    if parspack_retention.authorize(CONF, "2026-09-28", "rmdir", today, "2026-10-01") != "2026-09-28":
        fail("keep days 0 refused an older folder")


def test_event_log():
    tmp = tempfile.mkdtemp(prefix="pp-ret-")
    db_path = os.path.join(tmp, "parspack.db")
    conn = parspack_state.connect(db_path)
    conn.execute(
        "INSERT INTO backup_days (backup_date, created_at, updated_at, run_state) VALUES (?,?,?,?)",
        ("2026-09-01", "2026-09-01T00:00:00", "2026-09-01T00:00:00", "complete"),
    )
    conn.commit()
    conn.close()
    parspack_retention.log_deleted(db_path, "2026-09-01", ["2026-09-01/accounts/a.tar.gz"])
    text = open(os.path.join(tmp, "events.jsonl"), "r", encoding="utf-8").read()
    if "REMOTE_DELETED" not in text or "2026-09-01/accounts/a.tar.gz" not in text:
        fail("event log missed the deleted key")
    if "secret" in text:
        fail("event log contained an unexpected secret marker")


if __name__ == "__main__":
    test_guards()
    test_many_dates()
    test_keep_window()
    test_event_log()
    print("OK")
