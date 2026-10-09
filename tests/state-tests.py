#!/usr/bin/env python3
"""State-store checks. No root, no network, no cPanel."""
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_state  # noqa: E402


def fail(message):
    print("FAIL " + message, file=sys.stderr)
    sys.exit(1)


def main():
    directory = tempfile.mkdtemp(prefix="parspack-state-")
    try:
        transitions(directory)
        crash(directory)
        concurrent(directory)
        migration(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("OK")
    return 0


def sample(conn, name, checksum, size=4):
    line = parspack_state.apply_note(conn, "discovered", {
        "local": "/backup/2026-10-08/accounts/%s.tar.gz" % name,
        "remote": "parspack/bucket/cpanel/2026-10-08/accounts/%s.tar.gz" % name,
        "size": str(size),
        "mtime": "10",
        "checksum": checksum,
    })
    if not line.startswith("proceed "):
        fail("discover %s returned %s" % (name, line))
    return line.split()[1]


def transitions(directory):
    db_path = os.path.join(directory, "transitions.db")
    conn = parspack_state.connect(db_path, stale_seconds=900)
    checksum = "ab" * 32
    upload_id = sample(conn, "alpha", checksum)
    parspack_state.apply_note(conn, "stable", {"id": upload_id})
    parspack_state.apply_note(conn, "uploading", {"id": upload_id, "pid": str(os.getpid()), "attempt": "1", "checksum": checksum, "size": "4"})
    parspack_state.apply_note(conn, "uploaded", {"id": upload_id})
    parspack_state.apply_note(conn, "verifying", {"id": upload_id})
    parspack_state.apply_note(conn, "verified", {"id": upload_id, "remote_checksum": checksum, "duration": "3"})
    row = parspack_state._load(conn, upload_id)
    if row["status"] != "VERIFIED" or not parspack_state.backup_succeeded(row["status"]):
        fail("verified row was %s" % row["status"])
    again = parspack_state.apply_note(conn, "discovered", {
        "local": row["local_path"],
        "remote": row["remote_key"],
        "size": "4",
        "mtime": "10",
        "checksum": checksum,
    })
    if not again.startswith("skip "):
        fail("same checksum was not skipped: %s" % again)
    parspack_state.apply_note(conn, "cleanup-failed", {"id": upload_id, "error_class": "LOCAL", "error": "local delete failed"})
    row = parspack_state._load(conn, upload_id)
    if row["status"] != "LOCAL_CLEANUP_FAILED" or not parspack_state.backup_succeeded(row["status"]):
        fail("cleanup failure was treated as a failed backup")
    try:
        parspack_state.apply_note(conn, "stable", {"id": upload_id})
        fail("invalid transition was accepted")
    except parspack_state.StateError:
        pass
    different = "cd" * 32
    changed = parspack_state.apply_note(conn, "discovered", {
        "local": row["local_path"],
        "remote": row["remote_key"],
        "size": "5",
        "mtime": "11",
        "checksum": different,
    })
    if not changed.startswith("proceed "):
        fail("changed checksum did not proceed")
    parspack_state.apply_note(conn, "uploading", {"id": upload_id, "pid": str(os.getpid()), "attempt": "2", "checksum": different, "size": "5"})
    if parspack_state._load(conn, upload_id)["status"] != "UPLOADING":
        fail("re-upload did not enter UPLOADING")


def crash(directory):
    db_path = os.path.join(directory, "crash.db")
    conn = parspack_state.connect(db_path, stale_seconds=900)
    checksum = "ef" * 32
    upload_id = sample(conn, "beta", checksum)
    parspack_state.apply_note(conn, "uploading", {"id": upload_id, "pid": "99999999", "attempt": "1", "checksum": checksum, "size": "4"})
    parspack_state.recover(conn, 900)
    if parspack_state._load(conn, upload_id)["status"] != "UPLOAD_FAILED":
        fail("dead owner was not recovered")
    parspack_state.apply_note(conn, "uploading", {"id": upload_id, "pid": str(os.getpid()), "attempt": "2", "checksum": checksum, "size": "4"})
    parspack_state.apply_note(conn, "verified", {"id": upload_id, "remote_checksum": checksum})
    parspack_state.apply_note(conn, "uploading", {"id": upload_id, "pid": str(os.getpid()), "attempt": "3", "checksum": "11" * 32, "size": "9"})
    conn.execute("UPDATE uploads SET heartbeat_at = ? WHERE id = ?", ("1970-01-01T00:00:00", upload_id))
    moved = parspack_state.recover(conn, 60)
    if moved != 1 or parspack_state._load(conn, upload_id)["status"] != "UPLOAD_FAILED":
        fail("stale heartbeat was not recovered")
    fresh = sample(conn, "gamma", checksum)
    parspack_state.apply_note(conn, "uploading", {"id": fresh, "pid": str(os.getpid()), "attempt": "1", "checksum": checksum, "size": "4"})
    if parspack_state.recover(conn, 900) != 0:
        fail("a live upload was recovered")


def concurrent(directory):
    db_path = os.path.join(directory, "concurrent.db")
    parspack_state.connect(db_path, stale_seconds=900).close()
    errors = []

    def worker(index):
        try:
            conn = parspack_state.connect(db_path, stale_seconds=900)
            checksum = ("%02x" % index) * 32
            upload_id = sample(conn, "c%02d" % index, checksum, size=100 + index)
            parspack_state.apply_note(conn, "uploading", {
                "id": upload_id,
                "pid": str(os.getpid()),
                "attempt": "1",
                "checksum": checksum,
                "size": str(100 + index),
            })
            parspack_state.apply_note(conn, "verified", {"id": upload_id, "remote_checksum": checksum, "duration": "1"})
            conn.close()
        except Exception as exc:
            errors.append("%s: %s" % (index, exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        fail("concurrent writers failed: %s" % errors[0])
    conn = parspack_state.connect(db_path, stale_seconds=900)
    count = conn.execute("SELECT COUNT(*) FROM uploads WHERE status = 'VERIFIED' AND account LIKE 'c%'").fetchone()[0]
    if count != 8:
        fail("concurrent writers stored %s rows" % count)


def migration(directory):
    tree = os.path.join(directory, "sample-0.2.3", "state")
    os.makedirs(tree)
    tsv = os.path.join(tree, "uploads.tsv")
    original = "\n".join((
        "2026-10-04T03:31:26+0330\t/MainBackup/2026-10-04/accounts/irarmenia.tar.gz\tparspack/c275063/cpanel/2026-10-04/accounts/irarmenia.tar.gz\t1526939084\t18\t1",
        "2026-10-04T03:31:26+0330\t/MainBackup/2026-10-04/system/dirs/_etc_cpanel.tar.gz\tparspack/c275063/cpanel/2026-10-04/system/dirs/_etc_cpanel.tar.gz\t155652\t4\t1",
        "not-a-valid-row",
        "",
    )) + "\n"
    with open(tsv, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(original)
    db_path = os.path.join(tree, "parspack.db")
    conn = parspack_state.connect(db_path, stale_seconds=900)
    rows = list(conn.execute("SELECT account, status, size FROM uploads ORDER BY account"))
    if len(rows) != 2:
        fail("migration imported %s rows" % len(rows))
    accounts = sorted(row["account"] for row in rows)
    if accounts != ["irarmenia", "system"]:
        fail("migration accounts were %s" % accounts)
    if any(row["status"] != "VERIFIED" for row in rows):
        fail("migration did not mark the old rows verified")
    backup = tsv + ".0.2.3.bak"
    with open(backup, "r", encoding="utf-8") as handle:
        if handle.read() != original:
            fail("0.2.3 backup does not match the original tsv")
    with open(tsv, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("replaced-after-import\n")
    again = parspack_state.connect(db_path, stale_seconds=900)
    if again.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] != 2:
        fail("a second migration changed the row count")
    with open(backup, "r", encoding="utf-8") as handle:
        if handle.read() != original:
            fail("second open replaced the 0.2.3 backup")
    parspack_state.export_tsv(again, tsv)
    exported = open(tsv, "r", encoding="utf-8").read()
    if "irarmenia.tar.gz" not in exported or "replaced-after-import" in exported:
        fail("compat export did not rebuild uploads.tsv from the database")
    if not os.path.isfile(os.path.join(tree, "events.jsonl")):
        fail("jsonl event log was not written")


if __name__ == "__main__":
    sys.exit(main())
