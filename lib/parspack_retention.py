#!/usr/bin/env python3
"""Guards for cPanel-driven delete and rmdir. This program never schedules a delete."""

from __future__ import print_function

import datetime
import json
import os
import re
import sys

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_config  # noqa: E402

SUCCESS_STATUSES = ("VERIFIED", "LOCAL_DELETED", "LOCAL_CLEANUP_FAILED")
WHM_CONFIG = "/var/cpanel/backups/config"


class RetentionError(Exception):
    def __init__(self, message):
        super(RetentionError, self).__init__(message)
        self.message = message


def valid_day(value):
    if not parspack_config.DATE_SEGMENT_RE.fullmatch(value or ""):
        return False
    year, month, day = (int(part) for part in value.split("-"))
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False
    return True


def authorize(conf, relative, kind, today, newest_verified):
    """Allow one key inside prefix/YYYY-MM-DD/. Refuse everything else."""
    if conf.get("MODE") != "PRODUCTION" or conf.get("ALLOW_REMOTE_DELETE") != "1":
        raise RetentionError("refused: remote delete disabled")
    if kind not in ("delete", "rmdir"):
        raise RetentionError("refused: unsupported delete command")
    prefix = parspack_config.sanitize_remote(conf.get("PARSPACK_PREFIX", ""), allow_empty=True)
    if not prefix:
        raise RetentionError("refused: empty prefix")
    try:
        relative = parspack_config.sanitize_remote(relative, allow_empty=False)
    except parspack_config.ConfigError as exc:
        message = exc.errors[0] if exc.errors else "remote path is invalid"
        if "traversal" in message or ".." in (relative or ""):
            raise RetentionError("refused: path traversal")
        if "empty" in message:
            raise RetentionError("refused: bucket root")
        raise RetentionError("refused: path is not a date folder")
    if relative in ("", "/"):
        raise RetentionError("refused: bucket root")
    parts = relative.split("/")
    if not valid_day(parts[0]):
        raise RetentionError("refused: not a date folder")
    if kind == "delete" and len(parts) < 2:
        raise RetentionError("refused: not a file inside a date folder")
    day = parts[0]
    if day == today:
        raise RetentionError("refused: today's folder")
    if not newest_verified:
        raise RetentionError("refused: newest verified day is unknown")
    if day == newest_verified:
        raise RetentionError("refused: newest verified day")
    return day


def newest_verified_day(db_path):
    """Return the newest day with a verified object, or None when that cannot be known."""
    try:
        import parspack_ui
        conn = parspack_ui.connect_readonly(db_path)
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT backup_date FROM uploads WHERE status IN (%s) "
            "ORDER BY backup_date DESC LIMIT 1" % ",".join("?" for _ in SUCCESS_STATUSES),
            SUCCESS_STATUSES,
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    day = row["backup_date"] or ""
    return day if valid_day(day) else None


def keys_from_listing(text):
    """Read mc JSON lines. A bad line refuses the whole listing."""
    found = []
    for raw in (text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            raise RetentionError("refused: listing could not be parsed")
        if not isinstance(data, dict) or data.get("status") == "error":
            raise RetentionError("refused: listing could not be parsed")
        key = str(data.get("key") or "")
        if "\\" in key or key.startswith("/") or ".." in key.split("/"):
            raise RetentionError("refused: listing could not be parsed")
        size = data.get("size", 0) or 0
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RetentionError("refused: listing could not be parsed")
        if data.get("type") == "folder" or key.endswith("/"):
            continue
        key = key.rstrip("/")
        if not key:
            raise RetentionError("refused: listing could not be parsed")
        found.append((key, size))
    return found


def confirm_listing(relative, text):
    """Return the relative keys that would be removed. Do not remove them."""
    day = relative.split("/")[0]
    keys = []
    for key, _size in keys_from_listing(text):
        full = key if key == day or key.startswith(day + "/") else day + "/" + key
        try:
            parspack_config.sanitize_remote(full, allow_empty=False)
        except parspack_config.ConfigError:
            raise RetentionError("refused: listing could not be parsed")
        if not full.startswith(day + "/") and full != day:
            raise RetentionError("refused: listing could not be parsed")
        keys.append(full)
    if not keys:
        keys.append(relative)
    return keys


def log_deleted(db_path, day, keys):
    import parspack_state
    conn = parspack_state.connect(db_path)
    for key in keys:
        parspack_state._event(conn, "REMOTE_DELETED", day, "", key, None, "", None)
    conn.commit()


def parse_whm_retention(text):
    """Read the retention count WHM stored. Unknown or ambiguous input deletes nothing."""
    values = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")

    def pick(*names):
        for name in names:
            if values.get(name, "") != "":
                return values[name]
        return ""

    named = [item.strip().lower() for item in pick("BACKUPDAYS", "backupdays").split(",") if item.strip()]
    enabled = [item for item in named if item in ("daily", "weekly", "monthly")]
    if not enabled:
        for schedule, flag in (
            ("daily", ("BACKUP_DAILY_ENABLE", "backup_daily_enable")),
            ("weekly", ("BACKUP_WEEKLY_ENABLE", "backup_weekly_enable")),
            ("monthly", ("BACKUP_MONTHLY_ENABLE", "backup_monthly_enable")),
        ):
            if pick(*flag).lower() in ("1", "yes", "true", "on"):
                enabled.append(schedule)
    if len(enabled) != 1:
        return {"count": None, "strict": False, "schedule": "", "reason": "ambiguous"}
    schedule = enabled[0]
    raw = pick(
        "BACKUP_%s_RETENTION" % schedule.upper(),
        "backup_%s_retention" % schedule,
    )
    if not re.fullmatch(r"[1-9][0-9]{0,3}", raw or ""):
        return {"count": None, "strict": False, "schedule": schedule, "reason": "unknown"}
    strict_raw = pick(
        "BACKUP_STRICTLY_ENFORCE_RETENTION",
        "backup_strictly_enforce_retention",
        "strictly_enforce_retention",
    ).lower()
    return {
        "count": int(raw),
        "strict": strict_raw in ("1", "yes", "true", "on"),
        "schedule": schedule,
        "reason": "ok",
    }


def plan_cpanel(days, verified, count, strict):
    """Steady state from cPanel's retention article. This plans only; it deletes nothing.

    Default mode does not prune when the newest day is not a verified success.
    Strict mode still keeps the newest verified day. Both keep the newest `count` days.
    """
    ordered = sorted({day for day in days if valid_day(day)}, reverse=True)
    verified_ordered = [day for day in ordered if day in set(verified or ())]
    if not count:
        return [], "retention count unknown"
    if not ordered:
        return [], "no date folders"
    if not strict and ordered[0] not in set(verified or ()):
        return [], "newest day is not verified, so default retention would not prune"
    keep = set(ordered[: int(count)])
    if verified_ordered:
        keep.add(verified_ordered[0])
    return [day for day in ordered if day not in keep], "ok"


def guard_blocks(day, today, newest_verified):
    if day == today:
        return "today"
    if not newest_verified:
        return "newest verified day is unknown"
    if day == newest_verified:
        return "newest verified day"
    return ""


def usage_bytes(entries):
    total = 0
    for _day, size in entries:
        total += int(size)
    return total


def dry_run_lines(entries, whm, today, newest, threshold):
    """Text report. No line is a delete command."""
    days = sorted({day for day, _size in entries if valid_day(day)})
    verified = [newest] if newest else []
    remove, reason = plan_cpanel(days, verified, whm.get("count"), whm.get("strict"))
    import parspack_ui
    warning = parspack_ui.classify_alerts(
        {"status": "OK", "remote_ok": True},
        {"remote_bytes": usage_bytes(entries), "upload_failed": 0, "verify_failed": 0,
         "cleanup_failed": 0, "pending": False, "run_state": ""},
        int(threshold or 0),
    )
    lines = [
        "remote-bytes %d" % usage_bytes(entries),
        "days %s" % (" ".join(days) if days else "none"),
        "retention-count %s" % (whm.get("count") if whm.get("count") else "unknown"),
        "retention-mode %s" % ("strict" if whm.get("strict") else "default"),
        "retention-note %s" % reason,
    ]
    if not remove:
        lines.append("would-remove none")
    for day in remove:
        lines.append("would-remove %s" % day)
        block = guard_blocks(day, today, newest)
        if block:
            lines.append("guard-keep %s %s" % (day, block))
    over = any(item["text"] == "recorded remote usage is over the threshold" for item in warning["warnings"])
    lines.append("storage-warning %s" % ("yes" if over else "no"))
    return lines


def cmd_check(conf, relative, kind):
    today = datetime.date.today().isoformat()
    newest = newest_verified_day(os.path.join(conf["STATE_DIR"], "parspack.db"))
    authorize(conf, relative, kind, today, newest)


def cmd_dry_run(conf, listing_text, whm_text):
    if not parspack_config.sanitize_remote(conf.get("PARSPACK_PREFIX", ""), allow_empty=True):
        raise RetentionError("refused: empty prefix")
    entries = []
    for key, size in keys_from_listing(listing_text):
        day = key.split("/")[0]
        if valid_day(day):
            entries.append((day, size))
    newest = newest_verified_day(os.path.join(conf["STATE_DIR"], "parspack.db"))
    whm = parse_whm_retention(whm_text)
    lines = dry_run_lines(
        entries,
        whm,
        datetime.date.today().isoformat(),
        newest,
        conf.get("ALERT_REMOTE_BYTES") or "0",
    )
    sys.stdout.write("\n".join(lines) + "\n")


def main(argv):
    parspack_config.require_root()
    if list(argv) != ["--dry-run"]:
        print("usage: retention --dry-run", file=sys.stderr)
        return 2
    conf = parspack_config.load_valid()
    prefix = parspack_config.sanitize_remote(conf.get("PARSPACK_PREFIX", ""), allow_empty=True)
    if not prefix:
        print("refused: empty prefix", file=sys.stderr)
        return 1
    import parspack_ops
    target = parspack_config.object_target(conf, "", allow_empty=True)
    code, out, _err = parspack_ops._default_mc(conf, ["ls", "--recursive", "--json", "--", target])
    if code != 0:
        print("refused: remote listing failed", file=sys.stderr)
        return 1
    whm_text = ""
    if os.path.isfile(WHM_CONFIG) and not os.path.islink(WHM_CONFIG):
        try:
            whm_text = open(WHM_CONFIG, "r", encoding="utf-8", errors="replace").read(65536)
        except OSError:
            whm_text = ""
    try:
        cmd_dry_run(conf, out, whm_text)
    except RetentionError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except parspack_config.ConfigError as exc:
        print("error: %s" % (exc.errors[0] if exc.errors else "configuration"), file=sys.stderr)
        sys.exit(2)
    except RetentionError as exc:
        print(exc.message, file=sys.stderr)
        sys.exit(1)
