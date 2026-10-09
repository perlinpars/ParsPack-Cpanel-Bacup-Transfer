#!/usr/bin/env python3
"""Phase 5 CLI checks. No root, no network, no secrets in the output."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import parspack_ops  # noqa: E402
import parspack_state  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake-mc.py")
SECRET = "super-secret-value"
DATE = "2026-10-08"


def fail(message):
    print("FAIL " + message, file=sys.stderr)
    sys.exit(1)


def main():
    directory = tempfile.mkdtemp(prefix="parspack-phase5-")
    try:
        arguments()
        not_root()
        retry(directory)
        status(directory)
        verify()
        connection(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("OK")
    return 0


def arguments():
    try:
        parspack_ops.parse_retry_selector([])
    except parspack_ops.OpsError:
        pass
    else:
        fail("retry without a selector was accepted")
    try:
        parspack_ops.parse_retry_selector(["--failed", "--date", DATE])
    except parspack_ops.OpsError:
        pass
    else:
        fail("retry accepted two selectors")
    try:
        parspack_ops.parse_retry_selector(["--date", "08-10-2026"])
    except parspack_ops.OpsError:
        pass
    else:
        fail("retry accepted a bad date")
    try:
        parspack_ops.parse_retry_selector(["--account", "../root"])
    except parspack_ops.OpsError:
        pass
    else:
        fail("retry accepted a bad account")
    if parspack_ops.parse_retry_selector(["--failed"])["mode"] != "failed":
        fail("retry --failed was not recognized")
    try:
        parspack_ops.parse_status_args(["--date"])
    except parspack_ops.OpsError:
        pass
    else:
        fail("status accepted a missing date")
    try:
        parspack_ops.parse_status_args(["--json", "extra"])
    except parspack_ops.OpsError:
        pass
    else:
        fail("status accepted an extra argument")
    try:
        parspack_ops.parse_verify_args([])
    except parspack_ops.OpsError:
        pass
    else:
        fail("verify accepted no date")
    if not parspack_ops.PROBE_TARGET_RE.fullmatch("parspack/mybucket/cpanel/2026-10-08/accounts/alpha.tar.gz"):
        pass
    else:
        fail("a backup object matched the probe target")


def not_root():
    bad = ""
    held = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        for argv in (
            ["retry", "--failed"],
            ["verify", DATE],
            ["status"],
            ["connection-test"],
            ["nope"],
        ):
            if parspack_ops.main(argv) != 2:
                bad = " ".join(argv)
                break
    finally:
        sys.stderr.close()
        sys.stderr = held
    if bad:
        fail("command was not rejected for a non-root caller: %s" % bad)


def conf(directory):
    return {
        "MODE": "PRODUCTION",
        "MC_ALIAS": "parspack",
        "PARSPACK_BUCKET": "mybucket",
        "PARSPACK_PREFIX": "cpanel",
        "LOCAL_READ_ROOTS": directory,
        "ALLOW_REMOTE_DELETE": "0",
        "MC_BIN": FAKE,
        "MC_CONFIG_DIR": directory,
        "PARSPACK_ENDPOINT": "https://s3.test.local",
        "STATE_DIR": directory,
    }


def seed(conn, status, checksum, path, key):
    line = parspack_state.apply_note(conn, "discovered", {
        "local": path,
        "remote": key,
        "size": "4",
        "mtime": "1",
        "checksum": checksum,
    })
    upload_id = line.split()[1]
    if status == "VERIFIED":
        for verb in ("stable", "uploading", "uploaded", "verifying"):
            parspack_state.apply_note(conn, verb, {"id": upload_id, "checksum": checksum, "pid": "1"})
        parspack_state.apply_note(conn, "verified", {
            "id": upload_id, "checksum": checksum, "remote_checksum": checksum, "level": "sha256",
        })
    elif status == "UPLOAD_FAILED":
        parspack_state.apply_note(conn, "stable", {"id": upload_id})
        parspack_state.apply_note(conn, "uploading", {"id": upload_id, "checksum": checksum, "pid": "1"})
        parspack_state.apply_note(conn, "upload-failed", {"id": upload_id, "error": SECRET})
    return upload_id


def retry(directory):
    day = os.path.join(directory, DATE, "accounts")
    os.makedirs(day)
    path = os.path.join(day, "alpha.tar.gz")
    with open(path, "wb") as handle:
        handle.write(b"data")
    key = "parspack/mybucket/cpanel/%s/accounts/alpha.tar.gz" % DATE
    db = os.path.join(directory, "retry.db")
    conn = parspack_state.connect(db)
    seed(conn, "VERIFIED", "a" * 64, path, key)
    calls = []
    import parspack_config
    original = parspack_config.split_roots
    parspack_config.split_roots = lambda value: [directory] if value == directory else original(value)
    try:
        summary = parspack_ops.run_retry(
            conf(directory), conn, {"mode": "date", "date": DATE},
            lambda local, rel: calls.append((local, rel)) or 0,
        )
        if calls or summary["skipped"] != 1 or summary["sent"] != 0:
            fail("idempotent retry uploaded a verified file: %s" % summary)
        failed_path = os.path.join(day, "beta.tar.gz")
        with open(failed_path, "wb") as handle:
            handle.write(b"data")
        failed_key = "parspack/mybucket/cpanel/%s/accounts/beta.tar.gz" % DATE
        upload_id = seed(conn, "UPLOAD_FAILED", "b" * 64, failed_path, failed_key)

        def upload(local, rel):
            calls.append((local, rel))
            for verb in ("uploading", "uploaded", "verifying"):
                parspack_state.apply_note(conn, verb, {"id": upload_id, "checksum": "b" * 64, "pid": "1"})
            parspack_state.apply_note(conn, "verified", {
                "id": upload_id, "checksum": "b" * 64, "remote_checksum": "b" * 64, "level": "sha256",
            })
            return 0

        calls[:] = []
        summary = parspack_ops.run_retry(conf(directory), conn, {"mode": "failed"}, upload)
        if summary["sent"] != 1 or len(calls) != 1 or calls[0][1] != "%s/accounts/beta.tar.gz" % DATE:
            fail("failed retry did not upload the one failed account: %s %s" % (summary, calls))
        if SECRET in parspack_ops.format_result(summary):
            fail("retry result contained a secret")
        calls[:] = []
        summary = parspack_ops.run_retry(conf(directory), conn, {"mode": "failed"}, upload)
        if calls or summary["sent"] != 0:
            fail("second retry was not idempotent")
        account = parspack_ops.run_retry(conf(directory), conn, {"mode": "account", "account": "beta"}, upload)
        if account["sent"] != 0:
            fail("account retry uploaded an already verified file")
    finally:
        parspack_config.split_roots = original


def status(directory):
    folder = os.path.join(directory, "status-db")
    os.makedirs(folder)
    db = os.path.join(folder, "status.db")
    conn = parspack_state.connect(db)
    path = os.path.join(directory, "gamma.tar.gz")
    seed(conn, "VERIFIED", "c" * 64, path, "parspack/mybucket/cpanel/%s/accounts/gamma.tar.gz" % DATE)
    marker = os.path.join(folder, "system-pending-%s" % DATE)
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("pending\n")
    snapshot = parspack_ops.status_snapshot(conn, DATE, folder)
    text = parspack_ops.format_status(snapshot, False)
    blob = parspack_ops.format_status(snapshot, True)
    if "verified=1" not in text or DATE not in text or "pending" not in text:
        fail("status text was incomplete: %s" % text)
    parsed = json.loads(blob)
    if parsed["dates"][0]["verified"] != 1 or parsed["pending"] != [DATE]:
        fail("status json was incomplete")
    if SECRET in text or SECRET in blob:
        fail("status output contained a secret")
    try:
        parspack_ops.parse_status_args(["--date", DATE, "--json"])
    except parspack_ops.OpsError:
        fail("status rejected --date and --json")


def verify():
    rows = [{
        "account": "alpha",
        "remote_key": "parspack/bucket/cpanel/%s/accounts/alpha.tar.gz" % DATE,
        "size": 4,
        "status": "VERIFIED",
        "verification_level": "sha256",
        "local_checksum": "a" * 64,
        "remote_checksum": "a" * 64,
    }]
    remote = {
        "%s/accounts/alpha.tar.gz" % DATE: {"size": 4, "sha": "a" * 64, "etag": ""},
    }
    good = parspack_ops.verify_snapshot(DATE, {"alpha.tar.gz"}, 1, rows, {"alpha.tar.gz"}, 1, remote)
    if good["problems"] or good["levels"]["sha256"] != 1:
        fail("matching verify reported problems")
    if "OK" not in parspack_ops.format_verify(good):
        fail("matching verify did not print OK")
    missing = parspack_ops.verify_snapshot(DATE, {"alpha.tar.gz"}, 0, rows, set(), 0, {})
    kinds = [item["kind"] for item in missing["problems"]]
    if "missing" not in kinds or "missing-object" not in kinds:
        fail("verify missed a missing account: %s" % kinds)
    extra = parspack_ops.verify_snapshot(DATE, set(), 0, [], {"other.tar.gz"}, 2, {})
    kinds = [item["kind"] for item in extra["problems"]]
    if "extra" not in kinds or "system-count" not in kinds:
        fail("verify missed an extra account or system count")
    remote[DATE + "/accounts/alpha.tar.gz"]["sha"] = "b" * 64
    bad = parspack_ops.verify_snapshot(DATE, {"alpha.tar.gz"}, 1, rows, {"alpha.tar.gz"}, 1, remote)
    if not any(item["kind"] == "checksum" and item["level"] == "sha256" for item in bad["problems"]):
        fail("verify accepted a sha256 mismatch")
    remote[DATE + "/accounts/alpha.tar.gz"]["sha"] = "a" * 64
    remote[DATE + "/accounts/alpha.tar.gz"]["size"] = 3
    short = parspack_ops.verify_snapshot(DATE, {"alpha.tar.gz"}, 1, rows, {"alpha.tar.gz"}, 1, remote)
    if not any(item["kind"] == "size" for item in short["problems"]):
        fail("verify accepted a short object")
    rendered = parspack_ops.format_verify(bad)
    if SECRET in rendered:
        fail("verify output contained a secret")


def connection(directory):
    store = os.path.join(directory, "store")
    os.makedirs(os.path.join(store, "mybucket"))
    env = os.environ.copy()
    env["FAKE_STORE"] = store
    calls = []

    def mc(conf, args):
        calls.append(list(args))
        proc = subprocess.run(
            [sys.executable, FAKE, "--config-dir", directory] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            universal_newlines=True,
        )
        err = proc.stderr
        if "ls" in args and SECRET:
            err += SECRET
        return proc.returncode, proc.stdout, err

    settings = conf(directory)
    checks = parspack_ops.connection_checks(
        settings,
        mc=mc,
        version=lambda: "mc version RELEASE.2026-01-01T00-00-00Z",
        now=7,
        config_status="OK",
    )
    text = parspack_ops.render_checks(checks, include_local=True)
    if SECRET in text or "AKIATEST" in text:
        fail("connection output contained a secret")
    if "DELETE: cleanup skipped" not in text or "Latency: 7ms" not in text:
        fail("connection output missed cleanup or latency: %s" % text)
    if any(item[0] == "rm" for item in calls):
        fail("cleanup ran while remote delete is disabled")
    if not any(os.path.basename(path).startswith("probe-") for path in _files(os.path.join(store, "mybucket", "parspack-probe"))):
        fail("probe object was not created")
    settings["ALLOW_REMOTE_DELETE"] = "1"
    calls[:] = []
    checks = parspack_ops.connection_checks(
        settings,
        mc=mc,
        version=lambda: "mc version RELEASE.2026-01-01T00-00-00Z",
        now=1,
        config_status="OK",
    )
    removed = [args for args in calls if args[0] == "rm"]
    if len(removed) != 1 or not parspack_ops.PROBE_TARGET_RE.fullmatch(removed[0][-1]):
        fail("delete did not remove only the probe object: %s" % removed)
    if "/accounts/" in removed[0][-1]:
        fail("delete targeted a backup object")
    text = parspack_ops.render_checks(checks, include_local=False)
    if "DELETE: OK" not in text:
        fail("delete of the probe was not reported")

    def denied(conf, args):
        return 1, "", "AccessDenied " + SECRET

    denied_checks = parspack_ops.connection_checks(
        conf(directory),
        mc=denied,
        version=lambda: "mc version RELEASE.2026-01-01T00-00-00Z",
        resolve=lambda host, port: None,
        tls=lambda host, port: "TLSv1.2",
        now=1,
        config_status="OK",
    )
    # This mc binary is fake-mc.py and resolve is set, so DNS is actually called.
    text = parspack_ops.render_checks(denied_checks, include_local=True)
    if SECRET in text or "Auth: FAIL auth" not in text:
        fail("auth failure was not classified without the secret: %s" % text)

    def down(host, port):
        raise OSError("down")

    real_bin = conf(directory)
    real_bin["MC_BIN"] = "/usr/local/bin/mc"
    failed = parspack_ops.connection_checks(
        real_bin,
        mc=denied,
        version=lambda: "mc version RELEASE.2026-01-01T00-00-00Z",
        resolve=down,
        tls=down,
        now=2,
        config_status="OK",
    )
    names = {item["name"]: item["status"] for item in failed}
    if names["Endpoint"] != "FAIL" or names["TLS"] != "FAIL":
        fail("dns or tls failure was ignored")
    if not parspack_ops.mode_is_private(0o600) or parspack_ops.mode_is_private(0o644):
        fail("mc config mode check is wrong")
    if parspack_ops.checks_failed(failed) and "Endpoint: FAIL dns" not in parspack_ops.render_checks(failed):
        fail("dns failure was not rendered")


def _files(path):
    if not os.path.isdir(path):
        return []
    return [os.path.join(path, name) for name in os.listdir(path)]


if __name__ == "__main__":
    sys.exit(main())
