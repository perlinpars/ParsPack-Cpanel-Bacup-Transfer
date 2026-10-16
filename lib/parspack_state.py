"""Durable upload state for PARSPACK Backup.

cPanel ships Perl for the transport. AlmaLinux, Rocky, Ubuntu, and CloudLinux
install a system python3, and that interpreter's standard library includes
sqlite3 when the OS package was built against SQLite. This module is that
store. The shell and the Perl transport do not open the database.
"""

from __future__ import print_function

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
SCHEMA_VERSION = 2
HEARTBEAT_STALE_SECONDS = 900
DATE_RE = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2})(?:/|$)")
ACCOUNT_RE = re.compile(r"/accounts/([A-Za-z0-9._+-]+)\.tar\.gz\Z")

SUCCESS_STATUSES = ("VERIFIED", "LOCAL_DELETED", "LOCAL_CLEANUP_FAILED")
TERMINAL_STATUSES = SUCCESS_STATUSES + ("UPLOAD_FAILED", "VERIFY_FAILED", "SKIPPED", "INVALID_PATH")
RUN_STATES = ("open", "system_running", "complete", "partial")
VERIFICATION_LEVELS = ("sha256", "etag", "size-only")
TRANSIENT_STATUSES = ("UPLOADING", "VERIFYING")
STATUSES = (
    "DISCOVERED",
    "WAITING_STABILITY",
    "UPLOADING",
    "UPLOADED",
    "VERIFYING",
    "VERIFIED",
    "LOCAL_DELETED",
    "UPLOAD_FAILED",
    "VERIFY_FAILED",
    "LOCAL_CLEANUP_FAILED",
    "INVALID_PATH",
    "SKIPPED",
)
TRANSITIONS = {
    "DISCOVERED": {"WAITING_STABILITY", "UPLOADING", "INVALID_PATH", "SKIPPED"},
    "WAITING_STABILITY": {"UPLOADING", "INVALID_PATH", "UPLOAD_FAILED", "SKIPPED"},
    "UPLOADING": {"UPLOADED", "UPLOAD_FAILED", "VERIFY_FAILED", "SKIPPED"},
    "UPLOADED": {"VERIFYING", "UPLOAD_FAILED", "VERIFY_FAILED"},
    "VERIFYING": {"VERIFIED", "VERIFY_FAILED", "UPLOAD_FAILED"},
    "VERIFIED": {"LOCAL_DELETED", "LOCAL_CLEANUP_FAILED", "UPLOADING"},
    "LOCAL_DELETED": {"UPLOADING"},
    "UPLOAD_FAILED": {"UPLOADING", "WAITING_STABILITY", "INVALID_PATH"},
    "VERIFY_FAILED": {"VERIFYING", "UPLOADING"},
    "LOCAL_CLEANUP_FAILED": {"LOCAL_DELETED", "UPLOADING"},
    "INVALID_PATH": {"DISCOVERED"},
    "SKIPPED": {"UPLOADING", "DISCOVERED"},
}
RANK = {
    "DISCOVERED": 0,
    "WAITING_STABILITY": 1,
    "UPLOADING": 2,
    "UPLOADED": 3,
    "VERIFYING": 4,
    "VERIFIED": 5,
}


class StateError(Exception):
    def __init__(self, message):
        super(StateError, self).__init__(message)
        self.message = message


def backup_succeeded(status):
    """True when the remote copy is verified.

    LOCAL_CLEANUP_FAILED still means the backup itself succeeded. Only the
    local delete did not.
    """
    return status in SUCCESS_STATUSES


def default_db_path():
    return os.path.join(ROOT, "state", "parspack.db")


def resolve_db_path(requested):
    default = os.path.realpath(default_db_path())
    if not requested:
        return default
    real = os.path.realpath(requested)
    if os.path.realpath(ROOT) == "/root/parspack-backup" and _euid() == 0 and real != default:
        raise StateError("refusing to redirect the production state database")
    state_dir = os.path.realpath(os.path.join(ROOT, "state"))
    temp_root = os.path.realpath(tempfile.gettempdir())
    if real == default or real.startswith(state_dir + os.sep) or real.startswith(temp_root + os.sep):
        return real
    raise StateError("database path is outside the state directory and the temp directory")


def _euid():
    if hasattr(os, "geteuid"):
        return os.geteuid()
    return 1


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def backup_date_of(local_path, remote_key):
    for text in (remote_key or "", local_path or ""):
        match = DATE_RE.search(text.replace("\\", "/"))
        if match:
            return match.group(1)
    return "undated"


def account_of(local_path, remote_key):
    for text in (local_path or "", remote_key or ""):
        normal = text.replace("\\", "/")
        match = ACCOUNT_RE.search(normal)
        if match and not match.group(1).startswith("validate.tmp-"):
            return match.group(1)
        if "/system/" in normal or normal.startswith("system/"):
            return "system"
    return ""


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _clean_error(text):
    text = (text or "").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return text[:240]


def connect(db_path, stale_seconds=HEARTBEAT_STALE_SECONDS):
    db_path = resolve_db_path(db_path)
    directory = os.path.dirname(db_path)
    os.makedirs(directory, 0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    journal = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(journal).lower() != "wal":
        raise StateError("sqlite did not enable WAL mode")
    os.chmod(db_path, 0o600)
    for suffix in ("-wal", "-shm"):
        extra = db_path + suffix
        if os.path.exists(extra):
            try:
                os.chmod(extra, 0o600)
            except OSError:
                pass
    migrate(conn)
    import_legacy_tsv(conn, os.path.join(directory, "uploads.tsv"))
    recover(conn, stale_seconds)
    return conn


def migrate(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise StateError("state database version is newer than this program")
    if current >= SCHEMA_VERSION:
        return
    if current < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS backup_days (
                backup_date TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                run_state TEXT NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY,
                backup_date TEXT NOT NULL REFERENCES backup_days(backup_date),
                account TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL,
                remote_key TEXT NOT NULL,
                size INTEGER,
                mtime INTEGER,
                checksum_algo TEXT,
                local_checksum TEXT,
                remote_checksum TEXT,
                verification_level TEXT,
                delete_not_before TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error_class TEXT,
                error_message TEXT,
                owner_pid INTEGER,
                heartbeat_at TEXT,
                duration_seconds INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(local_path, remote_key)
            );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            backup_date TEXT,
            account TEXT NOT NULL DEFAULT '',
            file TEXT NOT NULL DEFAULT '',
            attempt INTEGER,
            error_class TEXT,
            upload_id INTEGER REFERENCES uploads(id)
        );
        CREATE TABLE IF NOT EXISTS tsv_import (
            source TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            rows INTEGER NOT NULL
        );
            CREATE INDEX IF NOT EXISTS uploads_status ON uploads(status);
            CREATE INDEX IF NOT EXISTS uploads_local ON uploads(local_path);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (1, ?)",
            (_now(),),
        )
    _migrate_v2(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (2, ?)",
        (_now(),),
    )


def _column_names(conn, table):
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def _migrate_v2(conn):
    """Add verification level, delayed local delete, and the day run state."""
    if "run_state" not in _column_names(conn, "backup_days"):
        conn.execute("ALTER TABLE backup_days ADD COLUMN run_state TEXT NOT NULL DEFAULT 'open'")
    uploads = _column_names(conn, "uploads")
    if "verification_level" not in uploads:
        conn.execute("ALTER TABLE uploads ADD COLUMN verification_level TEXT")
    if "delete_not_before" not in uploads:
        conn.execute("ALTER TABLE uploads ADD COLUMN delete_not_before TEXT")


def _backup_tsv(tsv_path):
    if not os.path.isfile(tsv_path) or os.path.islink(tsv_path):
        return ""
    backup = tsv_path + ".0.2.3.bak"
    if not os.path.exists(backup):
        shutil.copy2(tsv_path, backup)
        try:
            os.chmod(backup, 0o600)
        except OSError:
            pass
    return backup


def import_legacy_tsv(conn, tsv_path):
    """Copy a 0.2.3 uploads.tsv aside, then import it. The copy is never replaced."""
    backup = _backup_tsv(tsv_path)
    source = backup or ""
    if not source or not os.path.isfile(source):
        return 0
    already = conn.execute("SELECT rows FROM tsv_import WHERE source = ?", (source,)).fetchone()
    if already is not None:
        return int(already[0])
    imported = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        with open(source, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) != 6:
                    continue
                stamp, local_path, remote_key, size_text, duration_text, attempt_text = parts
                if not local_path or not remote_key or "\n" in local_path or "\n" in remote_key:
                    continue
                try:
                    size = int(size_text)
                    duration = int(duration_text)
                    attempts = int(attempt_text)
                except ValueError:
                    continue
                day = backup_date_of(local_path, remote_key)
                account = account_of(local_path, remote_key)
                _ensure_day(conn, day, stamp or _now())
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO uploads ("
                    "backup_date, account, local_path, remote_key, size, mtime, checksum_algo, "
                    "local_checksum, remote_checksum, attempts, status, error_class, error_message, "
                    "owner_pid, heartbeat_at, duration_seconds, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, NULL, '', '', '', ?, 'VERIFIED', '', '', NULL, NULL, ?, ?, ?)",
                    (day, account, local_path, remote_key, size, attempts, duration, stamp or _now(), stamp or _now()),
                )
                if cursor.rowcount:
                    imported += 1
                    upload_id = cursor.lastrowid
                    _event(conn, "VERIFIED", day, account, local_path, attempts, "", upload_id, stamp or _now())
        conn.execute(
            "INSERT INTO tsv_import(source, imported_at, rows) VALUES (?, ?, ?)",
            (source, _now(), imported),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return imported


def _ensure_day(conn, day, stamp):
    conn.execute(
        "INSERT OR IGNORE INTO backup_days(backup_date, created_at, updated_at) VALUES (?, ?, ?)",
        (day, stamp, stamp),
    )
    conn.execute("UPDATE backup_days SET updated_at = ? WHERE backup_date = ?", (stamp, day))


def _event(conn, event, day, account, local_path, attempt, error_class, upload_id, stamp=None):
    stamp = stamp or _now()
    conn.execute(
        "INSERT INTO events(ts, event, backup_date, account, file, attempt, error_class, upload_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (stamp, event, day, account or "", local_path or "", attempt, error_class or "", upload_id),
    )
    _append_jsonl(conn, {
        "ts": stamp,
        "event": event,
        "account": account or "",
        "file": local_path or "",
        "attempt": attempt,
        "error_class": error_class or "",
    })


def _append_jsonl(conn, payload):
    directory = os.path.dirname(resolve_db_path(None))
    # The caller may be using a temp database. The log stays beside that database.
    db_name = conn.execute("PRAGMA database_list").fetchone()
    path = ""
    if db_name is not None:
        file_row = conn.execute("PRAGMA database_list").fetchall()
        for item in file_row:
            if item[1] == "main" and item[2]:
                path = os.path.join(os.path.dirname(item[2]), "events.jsonl")
    if not path:
        path = os.path.join(directory, "events.jsonl")
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line.encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load(conn, upload_id):
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (int(upload_id),)).fetchone()
    if row is None:
        raise StateError("upload id was not found")
    return row


def _find(conn, local_path, remote_key):
    return conn.execute(
        "SELECT * FROM uploads WHERE local_path = ? AND remote_key = ?",
        (local_path, remote_key),
    ).fetchone()


def same_verified(row, size, checksum=""):
    if row is None or row["status"] not in SUCCESS_STATUSES:
        return False
    try:
        if row["size"] is None or int(size) != int(row["size"]):
            return False
    except (TypeError, ValueError):
        return False
    stored = row["local_checksum"] or ""
    if not stored:
        return False
    if checksum and checksum != stored:
        return False
    return True


def transition(conn, row, target, **fields):
    current = row["status"]
    if target not in STATUSES:
        raise StateError("unknown state")
    if current == target:
        _touch(conn, row, **fields)
        return _load(conn, row["id"])
    if target not in TRANSITIONS.get(current, ()):
        raise StateError("invalid transition %s -> %s" % (current, target))
    if current in SUCCESS_STATUSES and target == "UPLOADING":
        new_sum = fields.get("local_checksum") or ""
        if not new_sum or same_verified(row, fields.get("size", row["size"]), new_sum):
            raise StateError("already verified with the same size and checksum")
    _touch(conn, row, status=target, **fields)
    updated = _load(conn, row["id"])
    _event(
        conn,
        target,
        updated["backup_date"],
        updated["account"],
        updated["local_path"],
        updated["attempts"],
        updated["error_class"] or "",
        updated["id"],
    )
    return updated


def _touch(conn, row, status=None, **fields):
    stamp = _now()
    assignments = ["updated_at = ?"]
    values = [stamp]
    if status is not None:
        assignments.append("status = ?")
        values.append(status)
    mapping = {
        "size": "size",
        "mtime": "mtime",
        "local_checksum": "local_checksum",
        "remote_checksum": "remote_checksum",
        "attempts": "attempts",
        "error_class": "error_class",
        "error_message": "error_message",
        "owner_pid": "owner_pid",
        "duration_seconds": "duration_seconds",
        "checksum_algo": "checksum_algo",
        "verification_level": "verification_level",
        "delete_not_before": "delete_not_before",
    }
    for key, column in mapping.items():
        if key in fields and fields[key] is not None:
            value = fields[key]
            if key == "error_message":
                value = _clean_error(value)
            assignments.append(column + " = ?")
            values.append(value)
    if status in ("UPLOADING", "VERIFYING") or fields.get("heartbeat"):
        assignments.append("heartbeat_at = ?")
        values.append(stamp)
    values.append(row["id"])
    conn.execute("UPDATE uploads SET " + ", ".join(assignments) + " WHERE id = ?", values)
    conn.execute(
        "UPDATE backup_days SET updated_at = ? WHERE backup_date = ?",
        (stamp, row["backup_date"]),
    )


def _stale(row, stale_seconds, now):
    if row["status"] not in TRANSIENT_STATUSES:
        return False
    heartbeat = row["heartbeat_at"] or ""
    heartbeat_old = True
    if heartbeat:
        try:
            parsed = time.mktime(time.strptime(heartbeat[:19], "%Y-%m-%dT%H:%M:%S"))
            heartbeat_old = (now - parsed) > int(stale_seconds)
        except (ValueError, OverflowError):
            heartbeat_old = True
    return (not pid_alive(row["owner_pid"])) or heartbeat_old


def recover(conn, stale_seconds=HEARTBEAT_STALE_SECONDS, now=None):
    now = time.time() if now is None else now
    rows = list(conn.execute(
        "SELECT * FROM uploads WHERE status IN ('UPLOADING', 'VERIFYING')"
    ))
    changed = 0
    for row in rows:
        if not _stale(row, stale_seconds, now):
            continue
        target = "UPLOAD_FAILED" if row["status"] == "UPLOADING" else "VERIFY_FAILED"
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _load(conn, row["id"])
            if current["status"] == row["status"]:
                transition(
                    conn,
                    current,
                    target,
                    error_class="STALE",
                    error_message="owner pid is dead or the heartbeat is stale",
                    owner_pid=None,
                )
                changed += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return changed


def _walk_verified(conn, row, **fields):
    for step in ("UPLOADED", "VERIFYING", "VERIFIED"):
        row = _load(conn, row["id"])
        if row["status"] == "VERIFIED" and step != "VERIFIED":
            continue
        if RANK.get(row["status"], -1) > RANK[step]:
            continue
        extra = fields if step == "VERIFIED" else {}
        if row["status"] != step:
            row = transition(conn, row, step, **extra)
        elif step == "VERIFIED":
            row = transition(conn, row, step, **extra)
    return row


def apply_note(conn, verb, args):
    verb = verb.replace("-", "_")
    if verb == "discovered":
        return _note_discovered(conn, args)
    upload_id = args.get("id")
    if not upload_id:
        raise StateError("upload id is required")
    row = _load(conn, upload_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _note_existing(conn, row, verb, args)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if verb in ("verified", "deleted", "skipped"):
        export_tsv(conn, os.path.join(os.path.dirname(_db_file(conn)), "uploads.tsv"))
    return result


def _db_file(conn):
    for item in conn.execute("PRAGMA database_list"):
        if item[1] == "main" and item[2]:
            return item[2]
    return default_db_path()


def _note_discovered(conn, args):
    local_path = args.get("local") or ""
    remote_key = args.get("remote") or ""
    if not local_path or not remote_key:
        raise StateError("local and remote paths are required")
    size = int(args.get("size"))
    mtime = int(args.get("mtime") or 0)
    checksum = (args.get("checksum") or "").lower()
    day = backup_date_of(local_path, remote_key)
    account = account_of(local_path, remote_key)
    stamp = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_day(conn, day, stamp)
        row = _find(conn, local_path, remote_key)
        if row is None:
            conn.execute(
                "INSERT INTO uploads ("
                "backup_date, account, local_path, remote_key, size, mtime, checksum_algo, "
                "local_checksum, remote_checksum, attempts, status, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, 'DISCOVERED', ?, ?)",
                (day, account, local_path, remote_key, size, mtime, "sha256" if checksum else "", checksum, stamp, stamp),
            )
            row = _find(conn, local_path, remote_key)
            _event(conn, "DISCOVERED", day, account, local_path, 0, "", row["id"])
        if same_verified(row, size, checksum):
            conn.execute("COMMIT")
            return "skip %s %s" % (row["id"], row["status"])
        if row["status"] not in SUCCESS_STATUSES and checksum and checksum != (row["local_checksum"] or ""):
            _touch(conn, row, size=size, mtime=mtime, local_checksum=checksum, checksum_algo="sha256")
            row = _load(conn, row["id"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "proceed %s" % row["id"]


def _note_existing(conn, row, verb, args):
    fields = {}
    if args.get("checksum"):
        fields["local_checksum"] = args["checksum"].lower()
        fields["checksum_algo"] = "sha256"
    if args.get("level"):
        if args["level"] not in VERIFICATION_LEVELS:
            raise StateError("verification level is not sha256, etag, or size-only")
        fields["verification_level"] = args["level"]
        fields["checksum_algo"] = args["level"]
    if args.get("remote_checksum"):
        fields["remote_checksum"] = args["remote_checksum"].lower()
    if args.get("attempt"):
        fields["attempts"] = int(args["attempt"])
    if args.get("pid"):
        fields["owner_pid"] = int(args["pid"])
        fields["heartbeat"] = True
    if args.get("duration"):
        fields["duration_seconds"] = int(args["duration"])
    if args.get("size"):
        fields["size"] = int(args["size"])
    if args.get("error_class"):
        fields["error_class"] = args["error_class"]
    if args.get("error"):
        fields["error_message"] = args["error"]
    if verb == "stable":
        if row["status"] == "DISCOVERED":
            transition(conn, row, "WAITING_STABILITY", **fields)
        elif row["status"] not in ("WAITING_STABILITY", "UPLOADING", "UPLOADED", "VERIFYING", "VERIFIED"):
            transition(conn, row, "WAITING_STABILITY", **fields)
        return "ok"
    if verb == "uploading":
        row = _load(conn, row["id"])
        transition(conn, row, "UPLOADING", **fields)
        return "ok"
    if verb == "uploaded":
        transition(conn, _load(conn, row["id"]), "UPLOADED", **fields)
        return "ok"
    if verb == "verifying":
        transition(conn, _load(conn, row["id"]), "VERIFYING", **fields)
        return "ok"
    if verb == "verified":
        _walk_verified(conn, row, **fields)
        return "ok"
    if verb == "delete_due":
        delay = args.get("delay") or ""
        seconds = {"0": 0, "10m": 600, "1h": 3600}.get(delay)
        if seconds is None:
            raise StateError("delete delay is not scheduled")
        if row["status"] != "VERIFIED":
            raise StateError("local delete is only scheduled from VERIFIED")
        due = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + seconds))
        _touch(conn, row, delete_not_before=due)
        return "ok"
    if verb == "deleted":
        transition(conn, _load(conn, row["id"]), "LOCAL_DELETED", **fields)
        return "ok"
    if verb == "cleanup_failed":
        transition(conn, _load(conn, row["id"]), "LOCAL_CLEANUP_FAILED", error_class=fields.get("error_class") or "LOCAL", error_message=fields.get("error_message") or "local delete failed")
        return "ok"
    if verb == "upload_failed":
        transition(conn, _load(conn, row["id"]), "UPLOAD_FAILED", **fields)
        return "ok"
    if verb == "verify_failed":
        transition(conn, _load(conn, row["id"]), "VERIFY_FAILED", **fields)
        return "ok"
    if verb == "skipped":
        transition(conn, _load(conn, row["id"]), "SKIPPED", **fields)
        return "ok"
    if verb == "invalid":
        transition(conn, _load(conn, row["id"]), "INVALID_PATH", **fields)
        return "ok"
    raise StateError("unknown state note")


def export_tsv(conn, tsv_path):
    directory = os.path.dirname(tsv_path)
    os.makedirs(directory, 0o700, exist_ok=True)
    rows = conn.execute(
        "SELECT updated_at, local_path, remote_key, size, duration_seconds, attempts, status "
        "FROM uploads WHERE status IN ('VERIFIED', 'LOCAL_DELETED', 'LOCAL_CLEANUP_FAILED') "
        "ORDER BY id"
    )
    fd, temp_name = tempfile.mkstemp(prefix=".uploads.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (
                    row["updated_at"],
                    row["local_path"],
                    row["remote_key"],
                    int(row["size"] or 0),
                    int(row["duration_seconds"] or 0),
                    int(row["attempts"] or 0),
                ))
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, tsv_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(tsv_path, 0o600)
    except OSError:
        pass


def query_same(conn, local_path, size, checksum=""):
    rows = conn.execute("SELECT * FROM uploads WHERE local_path = ?", (local_path,))
    for row in rows:
        if same_verified(row, size, checksum):
            return True
    return False


def system_gate(names, waiting, set_stable, now, deadline):
    """The detached system job starts only when the account set is closed.

    An empty set is not closed: cPanel may still be creating the first tar.
    A deadline with anything still missing is partial, and no manifest is written.
    """
    if names and not waiting and set_stable:
        return "ready"
    if int(now) >= int(deadline):
        return "partial"
    return "waiting"


def accounts_waiting(conn, day, names):
    waiting = []
    for name in names:
        row = conn.execute(
            "SELECT status FROM uploads WHERE backup_date = ? AND account = ? "
            "ORDER BY id DESC LIMIT 1",
            (day, name),
        ).fetchone()
        if row is None or row["status"] not in TERMINAL_STATUSES:
            waiting.append(name)
    return waiting


def verified_account_names(conn, day):
    """Account basenames already terminal-success for this day (local tars may be gone)."""
    if not DATE_RE.fullmatch(day) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise StateError("backup date is invalid")
    # account='system' marks system-tree rows. Everything else with a success
    # status is an account archive (cPanel user basename).
    rows = conn.execute(
        "SELECT DISTINCT account FROM uploads WHERE backup_date = ? "
        "AND account != '' AND account != 'system' "
        "AND status IN (%s) ORDER BY account"
        % ",".join("?" for _ in SUCCESS_STATUSES),
        (day,) + SUCCESS_STATUSES,
    ).fetchall()
    names = []
    for row in rows:
        name = (row["account"] or "").strip()
        if not name or name == "system" or "/" in name or name.startswith("."):
            continue
        names.append(name)
    return names


def system_success_count(conn, day):
    """How many system-tree rows are already verified for this day."""
    if not DATE_RE.fullmatch(day) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise StateError("backup date is invalid")
    # Build IN placeholders by concat — never %-format a string that contains
    # LIKE '%/system/%' (Python treats %/s as an invalid conversion → ValueError).
    placeholders = ",".join("?" for _ in SUCCESS_STATUSES)
    return int(conn.execute(
        "SELECT COUNT(*) FROM uploads WHERE backup_date = ? "
        "AND (account = 'system' OR remote_key LIKE '%/system/%') "
        "AND status IN (" + placeholders + ")",
        (day,) + SUCCESS_STATUSES,
    ).fetchone()[0])


def _ts_epoch(stamp):
    stamp = (stamp or "").strip()
    if len(stamp) < 19:
        return 0
    try:
        return int(time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return 0


def accounts_day_gate(conn, day, idle_seconds, now, deadline):
    """Close the account phase from DB when local tars were already deleted.

    ready = every account row is terminal, at least one success, and no account
    upload row was updated in the last idle_seconds (new tars still arriving).
    """
    if not DATE_RE.fullmatch(day) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise StateError("backup date is invalid")
    now = int(now)
    deadline = int(deadline)
    idle_seconds = max(0, int(idle_seconds))
    rows = list(conn.execute(
        "SELECT account, status, updated_at FROM uploads WHERE backup_date = ? "
        "AND account != '' AND account != 'system'",
        (day,),
    ))
    if not rows:
        return ("partial" if now >= deadline else "waiting"), []
    if any(row["status"] not in TERMINAL_STATUSES for row in rows):
        return ("partial" if now >= deadline else "waiting"), []
    success = sorted({
        (row["account"] or "").strip()
        for row in rows
        if row["status"] in SUCCESS_STATUSES
        and (row["account"] or "").strip()
        and (row["account"] or "").strip() != "system"
        and "/" not in (row["account"] or "")
    })
    if not success:
        return ("partial" if now >= deadline else "waiting"), []
    last = 0
    for row in rows:
        last = max(last, _ts_epoch(row["updated_at"]))
    if last and (now - last) < idle_seconds:
        return "waiting", success
    return "ready", success


def set_run_state(conn, day, state):
    if state not in RUN_STATES:
        raise StateError("run state is not open, system_running, complete, or partial")
    if not DATE_RE.fullmatch(day) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise StateError("backup date is invalid")
    stamp = _now()
    _ensure_day(conn, day, stamp)
    conn.execute(
        "UPDATE backup_days SET run_state = ?, updated_at = ? WHERE backup_date = ?",
        (state, stamp, day),
    )


def due_deletes(conn, now_text=None):
    now_text = now_text or _now()
    return list(conn.execute(
        "SELECT id, local_path, remote_key, size FROM uploads "
        "WHERE status = 'VERIFIED' AND delete_not_before IS NOT NULL "
        "AND delete_not_before != '' AND delete_not_before <= ? ORDER BY id",
        (now_text,),
    ))


def day_flags(conn, day):
    cleanup = conn.execute(
        "SELECT COUNT(*) FROM uploads WHERE backup_date = ? AND status = 'LOCAL_CLEANUP_FAILED'",
        (day,),
    ).fetchone()[0]
    size_only = conn.execute(
        "SELECT COUNT(*) FROM uploads WHERE backup_date = ? AND verification_level = 'size-only'",
        (day,),
    ).fetchone()[0]
    row = conn.execute("SELECT run_state FROM backup_days WHERE backup_date = ?", (day,)).fetchone()
    return {
        "cleanup": int(cleanup),
        "size_only": int(size_only),
        "run_state": row["run_state"] if row else "",
    }


def write_manifest(conn, day, dest, plugin_version, cpanel_version, system_status, expected):
    if system_status != "complete":
        raise StateError("manifest is written only for a complete day")
    rows = list(conn.execute(
        "SELECT * FROM uploads WHERE backup_date = ? ORDER BY remote_key",
        (day,),
    ))
    uploaded = []
    seen = set()
    files = []
    total_bytes = 0
    verified = 0
    for row in rows:
        files.append({
            "path": row["remote_key"],
            "size": int(row["size"] or 0),
            "checksum": row["remote_checksum"] or "",
            "algo": row["checksum_algo"] or "",
            "level": row["verification_level"] or "",
            "status": row["status"],
            "account": row["account"] or "",
        })
        if row["status"] in SUCCESS_STATUSES:
            verified += 1
            total_bytes += int(row["size"] or 0)
            if row["account"] and row["account"] != "system" and row["account"] not in seen:
                seen.add(row["account"])
                uploaded.append(row["account"])
    day_row = conn.execute("SELECT created_at FROM backup_days WHERE backup_date = ?", (day,)).fetchone()
    payload = {
        "plugin_version": plugin_version,
        "cpanel_version": cpanel_version,
        "backup_date": day,
        "system_status": system_status,
        "expected_accounts": list(expected),
        "uploaded_accounts": uploaded,
        "files": files,
        "totals": {"files": len(files), "verified": verified, "bytes": total_bytes},
        "started": day_row["created_at"] if day_row else "",
        "finished": _now(),
    }
    directory = os.path.dirname(dest)
    os.makedirs(directory, 0o700, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".manifest.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, dest)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def _parse_flags(args):
    parsed = {}
    index = 0
    while index < len(args):
        item = args[index]
        if not item.startswith("--") or item == "--":
            raise StateError("unexpected argument")
        if index + 1 >= len(args):
            raise StateError("missing value for %s" % item)
        parsed[item[2:].replace("-", "_")] = args[index + 1]
        index += 2
    return parsed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    db_path = None
    if len(argv) >= 2 and argv[0] == "--db":
        db_path = argv[1]
        argv = argv[2:]
    if not argv:
        print("usage: parspack-state [--db PATH] COMMAND", file=sys.stderr)
        return 2
    command = argv[0].replace("-", "_")
    rest = argv[1:]
    verb = ""
    if command == "note":
        if not rest or rest[0].startswith("--"):
            print("state error: note verb is required", file=sys.stderr)
            return 2
        verb = rest[0]
        rest = rest[1:]
    flags = _parse_flags(rest)
    stale = int(flags.pop("stale_seconds", HEARTBEAT_STALE_SECONDS))
    try:
        if command == "self_test":
            return _self_test()
        conn = connect(None if db_path is None else db_path, stale_seconds=stale)
        if command == "init":
            print("ok")
            return 0
        if command == "recover":
            print(recover(conn, stale))
            return 0
        if command == "same":
            return 0 if query_same(conn, flags.get("local", ""), flags.get("size", ""), flags.get("checksum", "")) else 1
        if command == "export_tsv":
            export_tsv(conn, os.path.join(os.path.dirname(_db_file(conn)), "uploads.tsv"))
            print("ok")
            return 0
        if command == "set_run":
            set_run_state(conn, flags.get("date", ""), flags.get("state", ""))
            print("ok")
            return 0
        if command == "due_deletes":
            for row in due_deletes(conn, flags.get("now")):
                print("%s\t%s\t%s\t%s" % (row["id"], row["local_path"], row["remote_key"], int(row["size"] or 0)))
            return 0
        if command == "day_flags":
            flags_out = day_flags(conn, flags.get("date", ""))
            print("cleanup=%s size_only=%s run_state=%s" % (
                flags_out["cleanup"], flags_out["size_only"], flags_out["run_state"],
            ))
            return 0
        if command == "accounts_ready":
            names = [item for item in (flags.get("names") or "").split(",") if item]
            waiting = accounts_waiting(conn, flags.get("date", ""), names)
            gate = system_gate(
                names,
                waiting,
                flags.get("set_stable", "") == "1",
                flags.get("now", "0"),
                flags.get("deadline", "0"),
            )
            print(gate)
            return {"ready": 0, "waiting": 1, "partial": 2}[gate]
        if command == "verified_names":
            names = verified_account_names(conn, flags.get("date", ""))
            print(",".join(names))
            return 0 if names else 1
        if command == "system_success_count":
            print(system_success_count(conn, flags.get("date", "")))
            return 0
        if command == "accounts_day_gate":
            gate, names = accounts_day_gate(
                conn,
                flags.get("date", ""),
                flags.get("idle", "120"),
                flags.get("now", "0"),
                flags.get("deadline", "0"),
            )
            print("%s\t%s" % (gate, ",".join(names)))
            return {"ready": 0, "waiting": 1, "partial": 2}[gate]
        if command == "manifest":
            expected = [item for item in (flags.get("expected") or "").split(",") if item]
            path = write_manifest(
                conn,
                flags.get("date", ""),
                flags.get("dest", ""),
                flags.get("plugin_version", ""),
                flags.get("cpanel_version", "unknown"),
                flags.get("system_status", ""),
                expected,
            )
            print(path)
            return 0
        if command == "note":
            print(apply_note(conn, verb, flags))
            return 0
        raise StateError("unknown command")
    except StateError as exc:
        print("state error: %s" % exc.message, file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, ValueError) as exc:
        print("state error: %s" % exc.__class__.__name__, file=sys.stderr)
        return 1


def _self_test():
    directory = tempfile.mkdtemp()
    try:
        db_path = os.path.join(directory, "parspack.db")
        conn = connect(db_path, stale_seconds=0)
        line = apply_note(conn, "discovered", {
            "local": "/backup/2026-10-08/accounts/alpha.tar.gz",
            "remote": "parspack/bucket/cpanel/2026-10-08/accounts/alpha.tar.gz",
            "size": "4",
            "mtime": "1",
            "checksum": "a" * 64,
        })
        assert line.startswith("proceed ")
        upload_id = line.split()[1]
        apply_note(conn, "stable", {"id": upload_id})
        apply_note(conn, "uploading", {"id": upload_id, "pid": "1", "attempt": "1", "checksum": "a" * 64})
        apply_note(conn, "verified", {"id": upload_id, "remote_checksum": "a" * 64, "duration": "2"})
        row = _load(conn, upload_id)
        assert row["status"] == "VERIFIED"
        assert backup_succeeded(row["status"])
        try:
            apply_note(conn, "deleted", {"id": upload_id})
            apply_note(conn, "stable", {"id": upload_id})
            raise AssertionError("invalid transition was accepted")
        except StateError:
            pass
        print("OK")
        return 0
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
