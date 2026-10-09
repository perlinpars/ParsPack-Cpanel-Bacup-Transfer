#!/bin/bash
# Offline checks. No network and no cPanel. Run as root on the server:
#   tests/run-tests.sh
set -uo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "Run tests/run-tests.sh as root." >&2
  exit 2
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="/var/tmp"
[[ -d "$PARENT" && -w "$PARENT" ]] || PARENT="/tmp"
T="$(mktemp -d "$PARENT/parspack-test.XXXXXX")"
cleanup() {
  local status=$?
  rm -rf "$T"
  exit "$status"
}
trap cleanup EXIT
chmod 700 "$T"
P="$T/proj"
mkdir -p "$P"
tar -C "$SRC" \
  --exclude=./config/parspack.conf \
  --exclude=./mc-config \
  --exclude=./logs \
  --exclude=./state \
  --exclude=./reports \
  --exclude=./run \
  --exclude=./tmp \
  --exclude=./tests/fake-env \
  --exclude=__pycache__ \
  -cf - . | tar -C "$P" -xf -
chmod 755 "$P/bin/"* "$P/tests/fake-mc.py" "$P/transport/parspack_transport.pl"
chmod 700 "$P/lib" "$P/bin" "$P/config" "$P/transport" "$P/tests"

B="$T/local"
mkdir -p "$B"
chmod 700 "$B"
echo "backup-data-1234" > "$B/u.tar.gz"
chmod 644 "$B/u.tar.gz"

export FAKE_STORE="$T/store" FAKE_ARGV_LOG="$T/argv.log"
mkdir -p "$FAKE_STORE/mybucket"
: > "$FAKE_ARGV_LOG"
SECRET='super-secret-value'
KEY='AKIATESTKEY1'
PASS_N=0
FAIL_N=0

ok() { PASS_N=$((PASS_N + 1)); echo "  ok   - $1"; }
bad() { FAIL_N=$((FAIL_N + 1)); echo "  FAIL - $1"; }
# Expected failures must not abort the suite. set -e stays off for every check.
expect() {
  local want="$1" name="$2" rc
  shift 2
  set +e
  "$@" >"$T/out" 2>&1
  rc=$?
  set +e
  if [[ "$rc" == "$want" ]]; then
    ok "$name"
  else
    bad "$name (rc=$rc want $want)"
    sed 's/^/        /' "$T/out" | head -8
  fi
  return 0
}
run_to() {
  set +e
  "$@" >"$T/out" 2>&1
  RUN_RC=$?
  set +e
  return 0
}

conf() {
  cat > "$P/config/parspack.conf" <<C
MODE="PRODUCTION"
PARSPACK_ENDPOINT="https://s3.test.local"
PARSPACK_ACCESS_KEY="$KEY"
PARSPACK_SECRET_KEY="$SECRET"
PARSPACK_BUCKET="mybucket"
PARSPACK_PREFIX="cpanel"
TRANSPORT_PASSWORD="transport-pass-1"
TRANSPORT_AUTH_DISABLED="0"
TRANSPORT_TRACE="0"
LOCAL_READ_ROOTS="$B"
LOCAL_WRITE_ROOTS="$B"
MC_BIN="$P/tests/fake-mc.py"
MC_CONFIG_DIR="$P/mc-config"
MC_ALIAS="parspack"
PART_SIZE="256MiB"
PARALLEL_PARTS="4"
RETRY_COUNT="2"
RETRY_DELAY_SECONDS="0"
MIN_FILE_AGE_MINUTES="0"
STABILITY_CHECK_SECONDS="0"
REMOTE_EXISTS_POLICY="VERIFY"
DELETE_LOCAL_AFTER_SUCCESS="0"
ALLOW_REMOTE_DELETE="0"
LOG_DIR="$P/logs"
STATE_DIR="$P/state"
REPORT_DIR="$P/reports"
C
  local kv k
  for kv in "$@"; do
    k="${kv%%=*}"
    sed -i "s|^$k=.*|$k=\"${kv#*=}\"|" "$P/config/parspack.conf"
  done
  chmod 600 "$P/config/parspack.conf"
  chown root:root "$P/config/parspack.conf"
}

E="$P/bin/engine-upload.sh"
R="$P/bin/remote-ops.sh"
conf

set +e
echo "== config / doctor"
python3 "$P/lib/parspack_config.py" self-test >"$T/out" 2>&1
if [[ "$(tail -n 1 "$T/out")" == "OK" ]]; then ok "python self-test"; else bad "python self-test"; sed 's/^/        /' "$T/out" | head -8; fi
python3 "$P/tests/state-tests.py" >"$T/out" 2>&1
if [[ "$(tail -n 1 "$T/out")" == "OK" ]]; then ok "state store tests"; else bad "state store tests"; sed 's/^/        /' "$T/out" | head -12; fi
python3 "$P/tests/phase3-tests.py" >"$T/out" 2>&1
if [[ "$(tail -n 1 "$T/out")" == "OK" ]]; then ok "phase 3 tests"; else bad "phase 3 tests"; sed 's/^/        /' "$T/out" | head -20; fi
python3 "$P/tests/phase5-tests.py" >"$T/out" 2>&1
if [[ "$(tail -n 1 "$T/out")" == "OK" ]]; then ok "phase 5 tests"; else bad "phase 5 tests"; sed 's/^/        /' "$T/out" | head -20; fi
python3 "$P/tests/phase6-tests.py" >"$T/out" 2>&1
if [[ "$(tail -n 1 "$T/out")" == "OK" ]]; then ok "phase 6 tests"; else bad "phase 6 tests"; sed 's/^/        /' "$T/out" | head -20; fi
python3 "$P/tests/phase4-tests.py" >"$T/out" 2>&1
if [[ "$(tail -n 1 "$T/out")" == "OK" ]]; then ok "phase 4 tests"; else bad "phase 4 tests"; sed 's/^/        /' "$T/out" | head -20; fi
expect 0 "doctor passes" "$P/bin/parspackctl" doctor
chmod 644 "$P/config/parspack.conf"
expect 0 "doctor tightens mode 644" "$P/bin/parspackctl" doctor
[[ "$(stat -c %a "$P/config/parspack.conf")" == "600" ]] && ok "config is mode 600" || bad "config mode was not tightened"
conf PARSPACK_ENDPOINT=http://s3.test.local
expect 2 "http endpoint refused" "$P/bin/parspackctl" doctor
conf PARSPACK_PREFIX=../evil
expect 2 "prefix with .. refused" "$P/bin/parspackctl" doctor
conf MODE=WHATEVER
expect 2 "bad MODE refused" "$P/bin/parspackctl" doctor
conf

echo "== engine"
export FAKE_MC_NOISE=1 FAKE_MC_FAIL_PUTS=1
echo "noisy-retry" > "$B/noisy.tar.gz"
chmod 644 "$B/noisy.tar.gz"
expect 0 "noisy mc stdout cannot corrupt retry" "$E" "$B/noisy.tar.gz" "noisy.tar.gz"
if grep -q "Retrying after" "$P/logs/backup.log" && ! grep -q "usage: retry-delay" "$P/logs/backup.log" && [[ -f "$FAKE_STORE/mybucket/cpanel/noisy.tar.gz" ]]; then
  ok "retry delay ignored noisy mc stdout"
else
  bad "retry delay was corrupted by mc stdout"
fi
unset FAKE_MC_NOISE FAKE_MC_FAIL_PUTS
rm -f "$FAKE_STORE/.fail-puts"
conf MODE=TEST
expect 3 "TEST mode uploads nothing and exits 3" "$E" "$B/u.tar.gz" "2026-09-28/u.tar.gz"
[[ -z "$(find "$FAKE_STORE" -type f -print -quit)" ]] && ok "TEST mode stored nothing" || bad "TEST mode stored data"
conf
expect 0 "PRODUCTION upload" "$E" "$B/u.tar.gz" "2026-09-28/u.tar.gz"
[[ -f "$FAKE_STORE/mybucket/cpanel/2026-09-28/u.tar.gz" ]] && ok "object stored under PARSPACK_PREFIX" || bad "prefix not applied"
expect 0 "VERIFY skips a matching checksum" "$E" "$B/u.tar.gz" "2026-09-28/u.tar.gz"
echo "different-size-content-xx" > "$B/u.tar.gz"
expect 1 "VERIFY refuses a different size" "$E" "$B/u.tar.gz" "2026-09-28/u.tar.gz"
[[ "$(cat "$FAKE_STORE/mybucket/cpanel/2026-09-28/u.tar.gz")" == "backup-data-1234" ]] && ok "remote object was left untouched" || bad "remote object was overwritten"
conf REMOTE_EXISTS_POLICY=REPLACE
expect 0 "REPLACE overwrites" "$E" "$B/u.tar.gz" "2026-09-28/u.tar.gz"
conf REMOTE_EXISTS_POLICY=SKIP
echo "skip-me" > "$B/other.tar.gz"
expect 0 "SKIP leaves an existing object" "$E" "$B/u.tar.gz" "2026-09-28/u.tar.gz"
[[ "$(cat "$FAKE_STORE/mybucket/cpanel/2026-09-28/u.tar.gz")" == "different-size-content-xx" ]] && ok "SKIP did not overwrite" || bad "SKIP overwrote the object"
conf
expect 1 "remote .. is refused" "$E" "$B/u.tar.gz" "../../etc/x"
expect 1 "command substitution remote is refused" "$E" "$B/u.tar.gz" '$(id)'
expect 1 "backtick remote is refused" "$E" "$B/u.tar.gz" '`id`'
expect 1 "newline remote is refused" "$E" "$B/u.tar.gz" $'bad\nname'
expect 1 "leading dash remote is refused" "$E" "$B/u.tar.gz" "-rf"
expect 1 "unicode remote is refused" "$E" "$B/u.tar.gz" "café"
long="$(printf 'a%.0s' {1..901})"
expect 1 "very long remote name is refused" "$E" "$B/u.tar.gz" "$long"
ln -sf "$B/u.tar.gz" "$B/link"
expect 1 "symlink is refused" "$E" "$B/link" "x/y"
: > "$B/empty.tar.gz"
expect 1 "empty file is refused" "$E" "$B/empty.tar.gz" "empty.tar.gz"
echo "linked" > "$B/one.tar.gz"
ln "$B/one.tar.gz" "$B/two.tar.gz"
expect 1 "hard link is refused" "$E" "$B/two.tar.gz" "hard.tar.gz"
rm -f "$B/two.tar.gz"
echo "abc" > "$B/multi.tar.gz"
expect 0 "multipart checksum falls back to full content" "$E" "$B/multi.tar.gz" "multipart.tar.gz"
grep -q "full remote-content SHA256" "$P/logs/backup.log" && ok "content verification was logged" || bad "content verification was not logged"
conf DELETE_LOCAL_AFTER_SUCCESS=1
echo "abc" > "$B/del.tar"
expect 0 "local file removed after verified upload" "$E" "$B/del.tar" "del.tar"
[[ ! -e "$B/del.tar" && -f "$FAKE_STORE/mybucket/cpanel/del.tar" ]] && ok "only the local file was deleted" || bad "local/remote delete result was wrong"
conf
if grep -qF -e "$SECRET" -e "$KEY" "$FAKE_ARGV_LOG"; then
  bad "credentials were visible on the mc command line"
else
  ok "credentials were not on the mc command line"
fi

echo "== remote operations"
run_to "$R" delete "2026-09-28/u.tar.gz"
[[ "$RUN_RC" -eq 1 ]] && grep -F -q "refused: remote delete disabled" "$T/out" && ok "delete refused: remote delete disabled" || bad "delete refusal message was wrong (rc=$RUN_RC)"
mkdir -p "$FAKE_STORE/mybucket/cpanel"
echo x > "$FAKE_STORE/mybucket/cpanel/validate.tmp-1-2.txt"
expect 0 "validation object can be deleted" "$R" delete-validation "validate.tmp-1-2.txt"
expect 1 "validation delete refuses a backup name" "$R" delete-validation "2026-09-28/u.tar.gz"
conf MODE=TEST
run_to "$R" rmdir "2026-09-28"
[[ "$RUN_RC" -eq 1 ]] && grep -F -q "refused: remote delete disabled" "$T/out" && ok "TEST rmdir refused: remote delete disabled" || bad "TEST rmdir message was wrong (rc=$RUN_RC)"
conf
conf ALLOW_REMOTE_DELETE=1
expect 1 "rmdir / refused" "$R" rmdir /
expect 1 "rmdir .. refused" "$R" rmdir ../..
expect 1 "rmdir of a non-date folder refused" "$R" rmdir weekly
mkdir -p "$P/state"
python3 - <<PY
import os, sys
sys.path.insert(0, "$P/lib")
import parspack_state
db = "$P/state/parspack.db"
conn = parspack_state.connect(db)
conn.execute(
    "INSERT INTO backup_days (backup_date, created_at, updated_at, run_state) VALUES (?,?,?,?)",
    ("2026-10-01", "2026-10-01T00:00:00", "2026-10-01T00:00:00", "complete"),
)
conn.execute(
    "INSERT INTO uploads (backup_date, account, local_path, remote_key, size, status, attempts, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,0,?,?)",
    ("2026-10-01", "a", "/backup/2026-10-01/accounts/a.tar.gz", "cpanel/2026-10-01/accounts/a.tar.gz",
     1, "VERIFIED", "2026-10-01T00:00:00", "2026-10-01T00:00:00"),
)
conn.commit()
PY
mkdir -p "$FAKE_STORE/mybucket/cpanel/2026-09-28/accounts"
echo old > "$FAKE_STORE/mybucket/cpanel/2026-09-28/accounts/a.tar.gz"
mkdir -p "$FAKE_STORE/mybucket/cpanel/2026-10-01/accounts"
echo new > "$FAKE_STORE/mybucket/cpanel/2026-10-01/accounts/a.tar.gz"
expect 1 "rmdir of the newest verified day refused" "$R" rmdir "2026-10-01"
expect 0 "rmdir of an older date folder" "$R" rmdir "2026-09-28"
[[ -d "$FAKE_STORE/mybucket/cpanel/2026-10-01" && ! -e "$FAKE_STORE/mybucket/cpanel/2026-09-28" ]] && ok "only the older date folder was removed" || bad "rmdir removed an unexpected path"
grep -q 'REMOTE_DELETED' "$P/state/events.jsonl" && ok "deleted key was written to the event log" || bad "event log has no deleted key"
conf
expect 0 "mkdir" "$R" mkdir "2026-10-01/accounts"
[[ -f "$FAKE_STORE/mybucket/cpanel/2026-10-01/accounts/.keep" ]] && ok "mkdir creates a .keep object" || bad "mkdir did not create .keep"
echo z > "$FAKE_STORE/mybucket/cpanel/2026-10-01/accounts/a.tar.gz"
set +e
ls_out="$("$R" ls "2026-10-01/accounts" 2>/dev/null)"
ls_rc=$?
set +e
[[ "$ls_rc" -eq 0 && "$ls_out" == *a.tar.gz* && "$ls_out" != *.keep* ]] && ok "ls lists the object and hides .keep" || bad "ls output: $ls_out"
set +e
missing_out="$("$R" ls "2099-01-01/missing" 2>"$T/ls.err")"
missing_rc=$?
set +e
[[ "$missing_rc" -eq 0 && -z "$missing_out" ]] && ok "ls of a missing directory exits 0 with no output" || bad "ls missing rc=$missing_rc out=$missing_out"
expect 0 "get" "$R" get "2026-10-01/accounts/a.tar.gz" "$B/got.tar.gz"
[[ "$(cat "$B/got.tar.gz")" == "z" ]] && ok "get wrote the object" || bad "get contents were wrong"
export FAKE_MC_MODE=sha-and-etag
rm -f "$B/got-etag.tar.gz"
expect 0 "get with etag present" "$R" get "2026-10-01/accounts/a.tar.gz" "$B/got-etag.tar.gz"
[[ "$(cat "$B/got-etag.tar.gz")" == "z" ]] && ok "get with etag wrote the object" || bad "get with etag contents were wrong"
export FAKE_MC_MODE=mismatch
rm -f "$B/got-bad.tar.gz" "$B/got-bad.tar.gz.parspack-partial"
expect 1 "get with checksum mismatch" "$R" get "2026-10-01/accounts/a.tar.gz" "$B/got-bad.tar.gz"
[[ ! -e "$B/got-bad.tar.gz" && ! -e "$B/got-bad.tar.gz.parspack-partial" ]] && ok "checksum mismatch left no local file" || bad "checksum mismatch left a local file"
unset FAKE_MC_MODE
rm -f "$B/got-missing.tar.gz" "$B/got-missing.tar.gz.parspack-partial"
expect 1 "get when the object is missing" "$R" get "2099-01-01/no-such-object.tar.gz" "$B/got-missing.tar.gz"
[[ ! -e "$B/got-missing.tar.gz" && ! -e "$B/got-missing.tar.gz.parspack-partial" ]] && ok "missing object left no local file" || bad "missing object left a local file"
canon="$("$P/lib/parspack_config.py" check-local read --cwd "$B" -- u.tar.gz)"
[[ "$canon" == "$B/u.tar.gz" ]] && ok "relative local path uses the cPanel directory" || bad "relative path resolved to $canon"
grep -q 'my ($local_file, $dest_file) = @args\[0, 1\];' "$P/transport/parspack_transport.pl" && ok "put follows the Perl skeleton argument order" || bad "put argument order changed"
grep -q 'my ($remote_file, $local_file) = @args\[0, 1\];' "$P/transport/parspack_transport.pl" && ok "get follows the Perl skeleton argument order" || bad "get argument order changed"
grep -q 'exit 3' "$P/bin/engine-upload.sh" && ok "TEST mode put still exits 3" || bad "TEST mode exit 3 was removed"

echo
echo "passed=$PASS_N failed=$FAIL_N"
if [[ "$FAIL_N" -ne 0 ]]; then
  exit 1
fi
exit 0
