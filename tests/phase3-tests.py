#!/usr/bin/env python3
"""Phase 3 checks. Fake mc, no network, no cPanel."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_config  # noqa: E402
import parspack_state  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake-mc.py")


def fail(message):
    print("FAIL " + message, file=sys.stderr)
    sys.exit(1)


def fake_stat(mode, store, remote):
    env = os.environ.copy()
    env["FAKE_STORE"] = store
    env["FAKE_MC_MODE"] = mode
    proc = subprocess.run(
        [sys.executable, FAKE, "--config-dir", store, "stat", remote],
        env=env,
        capture_output=True,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        fail("fake mc stat failed: " + proc.stderr)
    return json.loads(proc.stdout)


def main():
    directory = tempfile.mkdtemp(prefix="parspack-phase3-")
    try:
        checksum_and_partial(directory)
        etag_and_sample(directory)
        gates_and_manifest(directory)
        migrate_v1(directory)
        local_delete_state(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("OK")
    return 0


def checksum_and_partial(directory):
    store = os.path.join(directory, "store")
    remote_rel = os.path.join(store, "bucket", "day", "accounts")
    os.makedirs(remote_rel)
    path = os.path.join(remote_rel, "alpha.tar.gz")
    payload = b"account-backup-bytes"
    with open(path, "wb") as handle:
        handle.write(payload)
    remote = "parspack/bucket/day/accounts/alpha.tar.gz"
    local_sha = hashlib.sha256(payload).hexdigest()

    bad = fake_stat("mismatch", store, remote)
    line = parspack_config.stat_fields(json.dumps(bad))
    size, remote_sha, _etag = line.split("\t")
    if parspack_config.classify_integrity(len(payload), size, local_sha, remote_sha, "", "256MiB", path) != "mismatch":
        fail("checksum mismatch was accepted")

    short = fake_stat("partial", store, remote)
    if int(short["size"]) != len(payload) - 1:
        fail("partial upload did not shorten the reported size")
    if parspack_config.classify_integrity(len(payload), short["size"], local_sha, local_sha, "", "256MiB", path) != "mismatch":
        fail("partial upload was accepted")

    hidden = fake_stat("multipart-sha", store, remote)
    hidden_line = parspack_config.stat_fields(json.dumps(hidden))
    if not hidden_line.endswith("\t-\t"):
        fail("multipart SHA256 suffix was treated as a file hash: " + hidden_line)
    _size, sha, etag = hidden_line.split("\t")
    if parspack_config.classify_integrity(len(payload), len(payload), local_sha, sha, etag, "256MiB", path) != "need-content":
        fail("multipart suffix should fall through to a full re-hash")


def etag_and_sample(directory):
    store = os.path.join(directory, "etag")
    os.makedirs(os.path.join(store, "bucket"))
    path = os.path.join(store, "bucket", "file.tar.gz")
    payload = b"etag-body"
    with open(path, "wb") as handle:
        handle.write(payload)
    remote = "parspack/bucket/file.tar.gz"
    shown = fake_stat("etag-only", store, remote)
    if "checksum" in shown:
        fail("etag-only stat still exposed a SHA256")
    expected = parspack_config.etag_for_file(path, parspack_config.part_size_bytes("256MiB"))
    if shown["etag"] != expected:
        fail("fake mc etag is not the single-part S3 etag")
    local_sha = hashlib.sha256(payload).hexdigest()
    if parspack_config.classify_integrity(len(payload), shown["size"], local_sha, "-", shown["etag"], "256MiB", path) != "ok etag":
        fail("matching etag was not accepted")
    if parspack_config.classify_integrity(len(payload), shown["size"], local_sha, "-", "a" * 32, "256MiB", path) != "mismatch":
        fail("a different etag was accepted")

    bigger = os.path.join(directory, "parts.bin")
    with open(bigger, "wb") as handle:
        handle.write(b"a" * 1500 + b"b" * 1500)
    three = parspack_config.etag_for_file(bigger, 1024)
    if not three.endswith("-3"):
        fail("three-part etag was not marked with the part count")
    whole = hashlib.md5(open(bigger, "rb").read()).hexdigest()
    if three.startswith(whole):
        fail("multipart etag was treated as the MD5 of the whole file")

    sample = parspack_config.sample_sha256(path)
    if parspack_config.classify_integrity(len(payload), len(payload), local_sha, "-", "", "256MiB", path, sample) != "ok size-only":
        fail("matching sample was not size-only")
    if parspack_config.classify_integrity(len(payload), len(payload), local_sha, "-", "", "256MiB", path, "b" * 64) != "mismatch":
        fail("a different sample was accepted")


def gates_and_manifest(directory):
    db_path = os.path.join(directory, "parspack.db")
    conn = parspack_state.connect(db_path)
    local = "/backup/2026-10-08/accounts/alpha.tar.gz"
    remote = "parspack/bucket/cpanel/2026-10-08/accounts/alpha.tar.gz"
    first = parspack_state.apply_note(conn, "discovered", {
        "local": local, "remote": remote, "size": "4", "mtime": "1", "checksum": "a" * 64,
    })
    if not first.startswith("proceed"):
        fail("first claim did not proceed")
    upload_id = first.split()[1]
    for verb in ("stable", "uploading", "uploaded", "verifying"):
        parspack_state.apply_note(conn, verb, {"id": upload_id, "checksum": "a" * 64, "pid": "1"})
    parspack_state.apply_note(conn, "verified", {
        "id": upload_id, "remote_checksum": "a" * 64, "level": "sha256",
    })
    again = parspack_state.apply_note(conn, "discovered", {
        "local": local, "remote": remote, "size": "4", "mtime": "1", "checksum": "a" * 64,
    })
    if not again.startswith("skip"):
        fail("idempotent re-run did not skip")

    if parspack_state.system_gate(["alpha"], [], True, 100, 200) != "ready":
        fail("a closed terminal set was not ready")
    if parspack_state.system_gate(["alpha", "late"], ["late"], False, 100, 200) != "waiting":
        fail("a late account did not keep the day waiting")
    if parspack_state.system_gate([], [], True, 100, 200) != "waiting":
        fail("an empty account set was treated as complete")
    if parspack_state.system_gate([], [], True, 200, 200) != "partial":
        fail("an aborted run past the deadline was not partial")
    if parspack_state.system_gate(["alpha"], ["alpha"], False, 500, 400) != "partial":
        fail("deadline expiry was not partial")

    waiting = parspack_state.accounts_waiting(conn, "2026-10-08", ["alpha", "late"])
    if waiting != ["late"]:
        fail("late account was not the only one waiting: %s" % waiting)
    if parspack_state.accounts_waiting(conn, "2026-10-08", ["alpha"]):
        fail("verified account was not terminal")

    try:
        parspack_state.write_manifest(conn, "2026-10-08", os.path.join(directory, "no.json"), "0.3.0", "unknown", "partial", ["alpha"])
    except parspack_state.StateError:
        pass
    else:
        fail("partial day wrote a manifest")
    dest = os.path.join(directory, "manifest.json")
    parspack_state.write_manifest(conn, "2026-10-08", dest, "0.3.0", "11.0", "complete", ["alpha"])
    body = json.load(open(dest, encoding="utf-8"))
    if body["plugin_version"] != "0.3.0" or body["uploaded_accounts"] != ["alpha"]:
        fail("manifest is missing the uploaded account")
    if body["files"][0]["level"] != "sha256" or body["files"][0]["algo"] != "sha256":
        fail("manifest did not record the checksum algorithm")
    if os.path.exists(os.path.join(directory, "no.json")):
        fail("partial manifest file exists")


def migrate_v1(directory):
    folder = os.path.join(directory, "v1")
    os.makedirs(folder)
    path = os.path.join(folder, "v1.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE backup_days (
            backup_date TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE uploads (
            id INTEGER PRIMARY KEY,
            backup_date TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL,
            remote_key TEXT NOT NULL,
            size INTEGER,
            checksum_algo TEXT,
            local_checksum TEXT,
            remote_checksum TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(local_path, remote_key)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            account TEXT NOT NULL DEFAULT '',
            file TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE tsv_import (source TEXT PRIMARY KEY, imported_at TEXT NOT NULL, rows INTEGER NOT NULL);
        INSERT INTO schema_version(version, applied_at) VALUES (1, '2026-10-01T00:00:00');
        INSERT INTO backup_days(backup_date, created_at, updated_at) VALUES ('2026-10-01', 't', 't');
        """
    )
    raw.commit()
    raw.close()
    conn = parspack_state.connect(path)
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    if version != 2:
        fail("version 1 database did not migrate to 2")
    names = parspack_state._column_names(conn, "uploads")
    if "verification_level" not in names or "delete_not_before" not in names:
        fail("version 2 upload columns are missing")
    if "run_state" not in parspack_state._column_names(conn, "backup_days"):
        fail("run_state was not added")
    state = conn.execute("SELECT run_state FROM backup_days WHERE backup_date = '2026-10-01'").fetchone()[0]
    if state != "open":
        fail("existing day was not left open")


def local_delete_state(directory):
    db_path = os.path.join(directory, "delete.db")
    conn = parspack_state.connect(db_path)
    local = "/backup/2026-10-08/accounts/beta.tar.gz"
    remote = "parspack/bucket/cpanel/2026-10-08/accounts/beta.tar.gz"
    line = parspack_state.apply_note(conn, "discovered", {
        "local": local, "remote": remote, "size": "4", "mtime": "1", "checksum": "b" * 64,
    })
    upload_id = line.split()[1]
    for verb in ("stable", "uploading", "uploaded", "verifying"):
        parspack_state.apply_note(conn, verb, {"id": upload_id, "pid": "1"})
    parspack_state.apply_note(conn, "verified", {"id": upload_id, "level": "etag", "remote_checksum": "c" * 32})
    row = conn.execute("SELECT verification_level, checksum_algo FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if row["verification_level"] != "etag" or row["checksum_algo"] != "etag":
        fail("etag level was not stored")
    parspack_state.apply_note(conn, "delete-due", {"id": upload_id, "delay": "10m"})
    due = conn.execute("SELECT status, delete_not_before FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if due["status"] != "VERIFIED" or not due["delete_not_before"]:
        fail("delayed delete did not stay VERIFIED")
    if not parspack_state.due_deletes(conn, "2999-01-01T00:00:00+0000"):
        fail("due delete was not listed")
    parspack_state.apply_note(conn, "cleanup-failed", {"id": upload_id, "error": "disk"})
    status = conn.execute("SELECT status FROM uploads WHERE id = ?", (upload_id,)).fetchone()[0]
    if status != "LOCAL_CLEANUP_FAILED" or not parspack_state.backup_succeeded(status):
        fail("cleanup failure was treated as a failed backup")
    flags = parspack_state.day_flags(conn, "2026-10-08")
    if flags["cleanup"] != 1:
        fail("report flags did not count the cleanup warning")


if __name__ == "__main__":
    sys.exit(main())
