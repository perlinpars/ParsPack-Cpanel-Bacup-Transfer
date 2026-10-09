#!/usr/bin/env python3
"""Operational commands for parspackctl. Root only. No secrets on stdout."""

from __future__ import print_function

import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time

ROOT = os.path.realpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_config  # noqa: E402
import parspack_state  # noqa: E402

DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
ACCOUNT_RE = re.compile(r"\A[A-Za-z0-9._+-]{1,64}\Z")
PROBE_NAME_RE = re.compile(r"\Aprobe-[0-9]+-[0-9]+-[0-9a-f]{16}\.txt\Z")
PROBE_TARGET_RE = re.compile(
    r"\A[A-Za-z0-9_-]{1,32}/[A-Za-z0-9][A-Za-z0-9.-]{1,61}[A-Za-z0-9]/parspack-probe/probe-[0-9]+-[0-9]+-[0-9a-f]{16}\.txt\Z"
)
RETRYABLE = ("UPLOAD_FAILED", "VERIFY_FAILED")
CHECK_NAMES = ("Endpoint", "TLS", "Auth", "Bucket", "PUT", "HEAD", "LIST", "DELETE", "Latency")


class OpsError(Exception):
    def __init__(self, message):
        super(OpsError, self).__init__(message)
        self.message = message


def parse_retry_selector(args):
    """Exactly one of --failed, --date YYYY-MM-DD, or --account NAME."""
    if args == ["--failed"]:
        return {"mode": "failed"}
    if len(args) == 2 and args[0] == "--date":
        if not DATE_RE.fullmatch(args[1]):
            raise OpsError("date must be YYYY-MM-DD")
        return {"mode": "date", "date": args[1]}
    if len(args) == 2 and args[0] == "--account":
        if not ACCOUNT_RE.fullmatch(args[1]) or args[1] in (".", ".."):
            raise OpsError("account name is invalid")
        return {"mode": "account", "account": args[1]}
    raise OpsError("usage: retry --failed | --date YYYY-MM-DD | --account NAME")


def parse_status_args(args):
    day = ""
    as_json = False
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--json":
            if as_json:
                raise OpsError("status was given --json twice")
            as_json = True
            index += 1
            continue
        if item == "--date":
            if day or index + 1 >= len(args):
                raise OpsError("usage: status [--date YYYY-MM-DD] [--json]")
            day = args[index + 1]
            if not DATE_RE.fullmatch(day):
                raise OpsError("date must be YYYY-MM-DD")
            index += 2
            continue
        raise OpsError("usage: status [--date YYYY-MM-DD] [--json]")
    return {"date": day, "json": as_json}


def parse_verify_args(args):
    if len(args) != 1 or not DATE_RE.fullmatch(args[0]):
        raise OpsError("usage: verify YYYY-MM-DD")
    return args[0]


def relative_key(conf, remote_key):
    prefix = parspack_config.object_target(conf, "", allow_empty=True)
    key = remote_key or ""
    if key.startswith(prefix + "/"):
        key = key[len(prefix) + 1:]
    key = parspack_config.sanitize_remote(key, allow_empty=False)
    if not DATE_RE.fullmatch(key.split("/", 1)[0]):
        raise OpsError("remote key is not under a backup date")
    return key


def _is_system(key):
    return "/system/" in ("/" + key)


def already_verified(conn, local_path, size):
    for row in conn.execute("SELECT * FROM uploads WHERE local_path = ?", (local_path,)):
        stored = row["local_checksum"] or ""
        if parspack_state.same_verified(row, size, stored):
            return True
    return False


def retry_rows(conn, spec):
    if spec["mode"] == "failed":
        query = "SELECT * FROM uploads WHERE status IN ('UPLOAD_FAILED', 'VERIFY_FAILED') ORDER BY id"
        args = ()
    elif spec["mode"] == "date":
        query = (
            "SELECT * FROM uploads WHERE backup_date = ? AND status IN ('UPLOAD_FAILED', 'VERIFY_FAILED') "
            "ORDER BY id"
        )
        args = (spec["date"],)
    else:
        query = (
            "SELECT * FROM uploads WHERE account = ? AND status IN ('UPLOAD_FAILED', 'VERIFY_FAILED') "
            "ORDER BY id"
        )
        args = (spec["account"],)
    return list(conn.execute(query, args))


def _safe_file(path):
    if not path or os.path.islink(path) or not os.path.isfile(path):
        return False
    return True


def local_candidates(conf, spec):
    """Files still on disk for a date or an account. --failed does not scan the disk."""
    if spec["mode"] == "failed":
        return []
    found = []
    for root in parspack_config.split_roots(conf.get("LOCAL_READ_ROOTS", "")):
        if os.path.islink(root) or not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not DATE_RE.fullmatch(name):
                continue
            if spec["mode"] == "date" and name != spec["date"]:
                continue
            day_dir = os.path.join(root, name)
            if os.path.islink(day_dir) or not os.path.isdir(day_dir):
                continue
            accounts = os.path.join(day_dir, "accounts")
            if os.path.isdir(accounts) and not os.path.islink(accounts):
                for filename in sorted(os.listdir(accounts)):
                    if not filename.endswith(".tar.gz") or filename.startswith("validate.tmp-"):
                        continue
                    account = filename[:-7]
                    if not ACCOUNT_RE.fullmatch(account):
                        continue
                    if spec["mode"] == "account" and account != spec["account"]:
                        continue
                    path = os.path.join(accounts, filename)
                    if _safe_file(path):
                        found.append((path, "%s/accounts/%s" % (name, filename)))
            if spec["mode"] == "account":
                continue
            system_dir = os.path.join(day_dir, "system")
            if os.path.isdir(system_dir) and not os.path.islink(system_dir):
                for dirpath, dirnames, filenames in os.walk(system_dir):
                    dirnames.sort()
                    for filename in sorted(filenames):
                        path = os.path.join(dirpath, filename)
                        if not _safe_file(path):
                            continue
                        rel = os.path.relpath(path, day_dir).replace("\\", "/")
                        try:
                            rel = parspack_config.sanitize_remote(rel, allow_empty=False)
                        except parspack_config.ConfigError:
                            continue
                        found.append((path, "%s/%s" % (name, rel)))
    return found


def run_retry(conf, conn, spec, uploader):
    """Upload failed rows, and for a date or account the local files not yet verified.

    A successful row with the same size and checksum is not uploaded again.
    """
    sent = failed = skipped = system_seen = system_failed = 0
    seen = set()
    jobs = []
    for row in retry_rows(conn, spec):
        if row["status"] not in RETRYABLE:
            continue
        try:
            key = relative_key(conf, row["remote_key"])
        except (OpsError, parspack_config.ConfigError):
            failed += 1
            continue
        jobs.append((row["local_path"], key))
    for path, key in local_candidates(conf, spec):
        jobs.append((path, key))
    for path, key in jobs:
        marker = path + "\n" + key
        if marker in seen:
            continue
        seen.add(marker)
        system = _is_system(key)
        if system:
            system_seen += 1
        if not _safe_file(path):
            failed += 1
            if system:
                system_failed += 1
            print("missing %s" % os.path.basename(path), file=sys.stderr)
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            failed += 1
            if system:
                system_failed += 1
            continue
        if already_verified(conn, path, size):
            skipped += 1
            continue
        try:
            code = uploader(path, key)
        except OSError:
            code = 1
        if code == 0:
            sent += 1
        else:
            failed += 1
            if system:
                system_failed += 1
    return {
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "system_seen": system_seen,
        "system_failed": system_failed,
    }


def format_result(summary):
    return "RESULT sent=%d failed=%d skipped=%d system_seen=%d system_failed=%d" % (
        summary["sent"], summary["failed"], summary["skipped"],
        summary["system_seen"], summary["system_failed"],
    )


def _counts(conn, day):
    query = "SELECT status, COUNT(*) AS n FROM uploads"
    args = ()
    if day:
        query += " WHERE backup_date = ?"
        args = (day,)
    query += " GROUP BY status"
    counts = {status: 0 for status in parspack_state.STATUSES}
    for row in conn.execute(query, args):
        counts[row["status"]] = int(row["n"])
    return counts


def status_snapshot(conn, day, state_dir):
    days = []
    if day:
        dates = [day]
    else:
        dates = [row["backup_date"] for row in conn.execute(
            "SELECT backup_date FROM backup_days ORDER BY backup_date"
        )]
    for item in dates:
        counts = _counts(conn, item)
        run = conn.execute(
            "SELECT run_state FROM backup_days WHERE backup_date = ?", (item,)
        ).fetchone()
        days.append({
            "date": item,
            "run_state": run["run_state"] if run else "",
            "verified": counts["VERIFIED"] + counts["LOCAL_DELETED"] + counts["LOCAL_CLEANUP_FAILED"],
            "failed": counts["UPLOAD_FAILED"] + counts["VERIFY_FAILED"],
            "cleanup_failed": counts["LOCAL_CLEANUP_FAILED"],
            "counts": counts,
        })
    pending = parspack_config.pending_system_days(state_dir)
    if day:
        pending = [item for item in pending if item == day]
    return {"dates": days, "pending": pending}


def format_status(snapshot, as_json):
    if as_json:
        return json.dumps(snapshot, sort_keys=True) + "\n"
    lines = []
    if not snapshot["dates"]:
        lines.append("no backup days")
    for item in snapshot["dates"]:
        lines.append(
            "%s %s verified=%d failed=%d cleanup=%d" % (
                item["date"], item["run_state"] or "unknown",
                item["verified"], item["failed"], item["cleanup_failed"],
            )
        )
    if snapshot["pending"]:
        lines.append("pending " + ",".join(snapshot["pending"]))
    return "\n".join(lines) + "\n"


def verify_snapshot(date, local_accounts, local_system, rows, remote_accounts, remote_system, remote_objects):
    """Compare one day. remote_objects maps a remote key suffix to size/sha/etag."""
    expected = set(local_accounts)
    for row in rows:
        account = row["account"] or ""
        if account and account != "system":
            expected.add(account + ".tar.gz")
    remote_names = set(remote_accounts)
    problems = []
    for name in sorted(expected - remote_names):
        problems.append({"kind": "missing", "name": name})
    for name in sorted(remote_names - expected):
        problems.append({"kind": "extra", "name": name})
    if local_system != remote_system:
        problems.append({
            "kind": "system-count",
            "local": local_system,
            "remote": remote_system,
        })
    levels = {"sha256": 0, "etag": 0, "size-only": 0, "": 0}
    for row in rows:
        level = row["verification_level"] or ""
        levels[level] = levels.get(level, 0) + 1
        matched = None
        for key, info in remote_objects.items():
            if row["remote_key"] == key or row["remote_key"].endswith("/" + key):
                matched = info
                break
        if matched is None:
            if row["status"] in parspack_state.SUCCESS_STATUSES:
                problems.append({"kind": "missing-object", "name": row["remote_key"]})
            continue
        if int(matched.get("size") or -1) != int(row["size"] or -2):
            problems.append({"kind": "size", "name": row["remote_key"], "level": level})
            continue
        remote_sha = (matched.get("sha") or "").lower()
        local_sha = (row["local_checksum"] or "").lower()
        remote_etag = (matched.get("etag") or "").lower()
        stored_remote = (row["remote_checksum"] or "").lower()
        if level == "sha256" and remote_sha and local_sha and remote_sha != local_sha:
            problems.append({"kind": "checksum", "name": row["remote_key"], "level": level})
        elif level == "etag" and remote_etag and stored_remote and remote_etag != stored_remote:
            problems.append({"kind": "checksum", "name": row["remote_key"], "level": level})
        elif level == "sha256" and not remote_sha and row["status"] in parspack_state.SUCCESS_STATUSES:
            problems.append({"kind": "checksum-unavailable", "name": row["remote_key"], "level": level})
    return {"date": date, "problems": problems, "levels": levels}


def format_verify(report):
    lines = ["date %s" % report["date"]]
    levels = report["levels"]
    lines.append(
        "levels sha256=%d etag=%d size-only=%d" % (
            levels.get("sha256", 0), levels.get("etag", 0), levels.get("size-only", 0),
        )
    )
    if not report["problems"]:
        lines.append("OK")
        return "\n".join(lines) + "\n"
    for item in report["problems"]:
        kind = item["kind"]
        if kind == "system-count":
            lines.append("system local=%d remote=%d" % (item["local"], item["remote"]))
        elif kind == "checksum":
            lines.append("checksum %s %s" % (item.get("level") or "-", item["name"]))
        else:
            lines.append("%s %s" % (kind, item.get("name", "")))
    return "\n".join(lines) + "\n"


def _walk_files(root):
    count = 0
    if os.path.islink(root) or not os.path.isdir(root):
        return count
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if _safe_file(path):
                count += 1
    return count


def local_expected(conf, date):
    accounts = set()
    system = 0
    for root in parspack_config.split_roots(conf.get("LOCAL_READ_ROOTS", "")):
        day = os.path.join(root, date, "accounts")
        if os.path.isdir(day) and not os.path.islink(day):
            for name in os.listdir(day):
                path = os.path.join(day, name)
                if name.endswith(".tar.gz") and not name.startswith("validate.tmp-") and _safe_file(path):
                    accounts.add(name)
        system += _walk_files(os.path.join(root, date, "system"))
    return accounts, system


def _classify_error(text):
    lowered = (text or "").lower()
    if "accessdenied" in lowered or "invalidaccesskeyid" in lowered or "signaturedoesnotmatch" in lowered:
        return "auth"
    if "nosuchbucket" in lowered or "does not exist" in lowered:
        return "missing"
    return "unreachable"


def _offline_mc(conf):
    return os.path.basename(conf.get("MC_BIN", "")) == "fake-mc.py"


def mode_is_private(mode):
    return stat.S_IMODE(mode) & 0o077 == 0


def config_perm_problem(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return "missing"
    if not mode_is_private(os.stat(path).st_mode):
        return "mode"
    return ""


def _check(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


def format_check(check):
    name = check["name"]
    status = check["status"]
    detail = check.get("detail") or ""
    if name == "Latency":
        return "Latency: %sms" % detail
    if name == "DELETE" and status == "skipped":
        return "DELETE: cleanup skipped"
    if name == "mc":
        return "mc: %s" % (detail or status)
    if name == "mc-config":
        return "mc-config: %s" % ("OK" if status == "OK" else "FAIL")
    if status == "OK":
        return "%s: OK" % name
    if status == "skipped":
        return "%s: skipped" % name
    if detail:
        return "%s: FAIL %s" % (name, detail)
    return "%s: FAIL" % name


def _mc_version(mc_bin):
    proc = subprocess.run(
        [mc_bin, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    line = (proc.stdout or proc.stderr or "").strip().splitlines()
    return line[0][:80] if line else ""


def _default_mc(conf, args):
    env = os.environ.copy()
    for key in ("PASSWORD", "SMTP_PASSWORD", "TRANSPORT_PASSWORD", "PARSPACK_ACCESS_KEY", "PARSPACK_SECRET_KEY"):
        env.pop(key, None)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    proc = subprocess.run(
        [conf["MC_BIN"], "--config-dir", conf["MC_CONFIG_DIR"]] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        universal_newlines=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _dns(host, port):
    socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    return "ok"


def _tls(host, port):
    context = ssl.create_default_context()
    sock = socket.create_connection((host, int(port)), timeout=10)
    try:
        tls = context.wrap_socket(sock, server_hostname=host)
        try:
            return tls.version() or "ok"
        finally:
            tls.close()
    finally:
        sock.close()


def probe_target(conf, name):
    if not PROBE_NAME_RE.fullmatch(name or ""):
        raise OpsError("probe name is invalid")
    target = "%s/%s/parspack-probe/%s" % (conf["MC_ALIAS"], conf["PARSPACK_BUCKET"], name)
    if not PROBE_TARGET_RE.fullmatch(target):
        raise OpsError("probe target is invalid")
    return target


def cleanup_allowed(conf):
    return conf.get("MODE") == "PRODUCTION" and conf.get("ALLOW_REMOTE_DELETE") == "1"


def connection_checks(conf, mc=None, version=None, resolve=None, tls=None, now=None, config_status=None):
    """Run the remote checks. mc stderr is classified and never returned."""
    mc = mc or _default_mc
    started = time.monotonic()
    checks = []
    version_text = version() if version else _mc_version(conf["MC_BIN"])
    version_ok = bool(version_text) and (
        "RELEASE" in version_text or version_text.lower().startswith("mc ")
    )
    if version_ok:
        checks.append(_check("mc", "OK", version_text.replace("\n", " ")[:80]))
    else:
        checks.append(_check("mc", "FAIL", "version"))
    if config_status is None:
        config_path = os.path.join(conf["MC_CONFIG_DIR"], "config.json")
        problem = config_perm_problem(config_path)
        checks.append(_check("mc-config", "FAIL" if problem else "OK", problem))
    else:
        checks.append(_check("mc-config", config_status, "" if config_status == "OK" else "mode"))
    host, port = _endpoint_host(conf["PARSPACK_ENDPOINT"])
    if _offline_mc(conf) and resolve is None:
        checks.append(_check("Endpoint", "skipped", "offline"))
        checks.append(_check("TLS", "skipped", "offline"))
    else:
        resolve = resolve or _dns
        tls = tls or _tls
        try:
            resolve(host, port)
            checks.append(_check("Endpoint", "OK"))
        except (OSError, socket.gaierror):
            checks.append(_check("Endpoint", "FAIL", "dns"))
        try:
            tls(host, port)
            checks.append(_check("TLS", "OK"))
        except (OSError, ssl.SSLError, socket.gaierror):
            checks.append(_check("TLS", "FAIL", "tls"))
    bucket = "%s/%s" % (conf["MC_ALIAS"], conf["PARSPACK_BUCKET"])
    code, _out, err = mc(conf, ["ls", "--", bucket])
    kind = _classify_error(err)
    if code == 0:
        checks.append(_check("Auth", "OK"))
        checks.append(_check("Bucket", "OK"))
    elif kind == "auth":
        checks.append(_check("Auth", "FAIL", "auth"))
        checks.append(_check("Bucket", "skipped"))
    elif kind == "missing":
        checks.append(_check("Auth", "OK"))
        checks.append(_check("Bucket", "FAIL", "missing"))
    else:
        checks.append(_check("Auth", "FAIL", "unreachable"))
        checks.append(_check("Bucket", "skipped"))
    probe_name = "probe-%d-%d-%s.txt" % (os.getpid(), int(time.time()), os.urandom(8).hex())
    target = probe_target(conf, probe_name)
    bucket_ok = any(item["name"] == "Bucket" and item["status"] == "OK" for item in checks)
    if not bucket_ok:
        checks.append(_check("PUT", "skipped"))
        checks.append(_check("HEAD", "skipped"))
        checks.append(_check("LIST", "skipped"))
        checks.append(_check("DELETE", "skipped"))
    else:
        directory = conf.get("TMPDIR") or os.path.join(ROOT, "tmp")
        os.makedirs(directory, 0o700, exist_ok=True)
        fd, local_probe = tempfile_probe(directory)
        try:
            os.write(fd, b"parspack-probe\n")
            os.close(fd)
            fd = None
            put_ok = False
            code, _out, err = mc(conf, ["put", "--", local_probe, target])
            if code != 0:
                checks.append(_check("PUT", "FAIL", _classify_error(err)))
                checks.append(_check("HEAD", "skipped"))
                checks.append(_check("LIST", "skipped"))
            else:
                put_ok = True
                checks.append(_check("PUT", "OK"))
                code, _out, err = mc(conf, ["stat", "--", target])
                checks.append(_check("HEAD", "OK" if code == 0 else "FAIL", "" if code == 0 else _classify_error(err)))
                code, out, err = mc(conf, ["ls", "--", "%s/%s/parspack-probe/" % (conf["MC_ALIAS"], conf["PARSPACK_BUCKET"])])
                listed = code == 0 and probe_name in (out or "")
                checks.append(_check("LIST", "OK" if listed else "FAIL", "" if listed else _classify_error(err)))
            if put_ok and cleanup_allowed(conf) and PROBE_TARGET_RE.fullmatch(target):
                code, _out, err = mc(conf, ["rm", "--", target])
                checks.append(_check("DELETE", "OK" if code == 0 else "FAIL", "" if code == 0 else "delete"))
            else:
                checks.append(_check("DELETE", "skipped"))
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(local_probe)
            except OSError:
                pass
    elapsed = int((time.monotonic() - started) * 1000)
    if now is not None:
        elapsed = int(now)
    checks.append(_check("Latency", "OK", str(max(0, elapsed))))
    return checks


def tempfile_probe(directory):
    handle = tempfile.NamedTemporaryFile(dir=directory, prefix="probe-", suffix=".txt", delete=False)
    name = handle.name
    handle.close()
    return os.open(name, os.O_WRONLY), name


def _endpoint_host(url):
    host = url[len("https://"):]
    if "/" in host:
        host = host.split("/", 1)[0]
    port = 443
    if host.startswith("["):
        end = host.find("]")
        name = host[1:end]
        if host[end + 1:end + 2] == ":":
            port = int(host[end + 2:])
        return name, port
    if host.count(":") == 1:
        name, port_text = host.split(":")
        return name, int(port_text)
    return host, port


def checks_failed(checks):
    return [item for item in checks if item["status"] == "FAIL"]


def render_checks(checks, include_local=False):
    lines = []
    for item in checks:
        if item["name"] in ("mc", "mc-config") and not include_local:
            continue
        lines.append(format_check(item))
    return "\n".join(lines) + "\n"


def _prepare_probe(conf):
    parspack_config.write_mc_config(conf)


def doctor_lines(conf, **kwargs):
    if kwargs.get("prepare", True):
        _prepare_probe(conf)
    checks = connection_checks(conf, **{key: value for key, value in kwargs.items() if key != "prepare"})
    return [(format_check(item), item["status"] == "FAIL") for item in checks]


def cmd_retry(args):
    spec = parse_retry_selector(args)
    conf = parspack_config.load_valid()
    if conf["MODE"] != "PRODUCTION":
        raise OpsError("MODE=TEST does not upload")
    conn = parspack_state.connect(None)
    summary = run_retry(conf, conn, spec, _engine_upload)
    sys.stdout.write(format_result(summary) + "\n")
    return 0 if summary["failed"] == 0 else 1


def _engine_upload(local_path, relative):
    proc = subprocess.run(
        ["/bin/bash", os.path.join(ROOT, "bin", "engine-upload.sh"), local_path, relative],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    return proc.returncode


def cmd_status(args):
    spec = parse_status_args(args)
    conf = parspack_config.load_valid()
    conn = parspack_state.connect(None)
    snapshot = status_snapshot(conn, spec["date"], conf["STATE_DIR"])
    sys.stdout.write(format_status(snapshot, spec["json"]))
    return 0


def _collect_remote(conf, date, mc):
    accounts = {}
    prefix = parspack_config.object_target(conf, date + "/accounts", require_child=True)
    code, out, err = mc(conf, ["ls", "--json", "--", prefix + "/"])
    if code != 0 and _classify_error(err) != "missing":
        raise OpsError("remote listing failed")
    objects = {}
    if code == 0:
        for info in _json_lines(out):
            name = str(info.get("key") or "").rstrip("/").split("/")[-1]
            if name.endswith(".tar.gz") and ACCOUNT_RE.fullmatch(name[:-7] or ""):
                accounts[name] = int(info.get("size") or 0)
                objects[date + "/accounts/" + name] = _object_info(info)
    system_target = parspack_config.object_target(conf, date + "/system", require_child=True)
    code, out, err = mc(conf, ["ls", "--recursive", "--json", "--", system_target + "/"])
    system_count = 0
    if code == 0:
        for info in _json_lines(out):
            if str(info.get("type", "")).lower() == "folder":
                continue
            key = str(info.get("key") or "").replace("\\", "/")
            system_count += 1
            objects[date + "/system/" + key] = _object_info(info)
    elif _classify_error(err) != "missing":
        raise OpsError("remote listing failed")
    return accounts, system_count, objects


def _json_lines(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _object_info(info):
    sha = parspack_config.checksum_sha256(info)
    return {
        "size": int(info.get("size") or 0),
        "sha": sha,
        "etag": parspack_config.object_etag(info),
    }


def cmd_verify(args):
    date = parse_verify_args(args)
    conf = parspack_config.load_valid()
    conn = parspack_state.connect(None)
    local_accounts, local_system = local_expected(conf, date)
    rows = list(conn.execute("SELECT * FROM uploads WHERE backup_date = ?", (date,)))
    try:
        remote_accounts, remote_system, objects = _collect_remote(conf, date, _default_mc)
    except OpsError:
        print("error: remote listing failed", file=sys.stderr)
        return 1
    report = verify_snapshot(date, local_accounts, local_system, rows, remote_accounts, remote_system, objects)
    sys.stdout.write(format_verify(report))
    return 1 if report["problems"] else 0


def cmd_connection(args):
    if args:
        raise OpsError("usage: connection-test")
    conf = parspack_config.load_valid()
    _prepare_probe(conf)
    checks = connection_checks(conf)
    sys.stdout.write(render_checks(checks, include_local=False))
    return 1 if checks_failed(checks) else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        parspack_config.require_root()
        if not argv:
            raise OpsError("usage: retry|verify|status|connection-test")
        command = argv[0]
        rest = argv[1:]
        if command == "retry":
            return cmd_retry(rest)
        if command == "status":
            return cmd_status(rest)
        if command == "verify":
            return cmd_verify(rest)
        if command == "connection-test":
            return cmd_connection(rest)
        raise OpsError("usage: retry|verify|status|connection-test")
    except parspack_config.ConfigError as exc:
        message = exc.errors[0] if getattr(exc, "errors", None) else "configuration error"
        print("error: %s" % message, file=sys.stderr)
        return 2
    except OpsError as exc:
        print("error: %s" % exc.message, file=sys.stderr)
        return 2
    except (OSError, parspack_state.sqlite3.Error, ValueError):
        print("error: command failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
