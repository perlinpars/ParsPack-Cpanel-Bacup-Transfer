#!/bin/bash
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONDONTWRITEBYTECODE=1
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$ROOT/config/parspack.conf"
EXAMPLE="$ROOT/config/parspack.conf.example"
TRANSPORT_SRC="$ROOT/transport/parspack_transport.pl"
TRANSPORT_DST="${PARSPACK_TRANSPORT_DST:-/usr/local/cpanel/scripts/parspack_custom_transport.pl}"
APPJSON="$ROOT/whm/parspack_backup.conf"
WHM_UI_SRC="$ROOT/whm/index.php"
WHM_UI_DST_DIR="${PARSPACK_WHM_UI_DIR:-/usr/local/cpanel/whostmgr/docroot/cgi/parspack_backup}"
WHM_UI_DST="$WHM_UI_DST_DIR/index.php"

# PARSPACK_INSTALL_TEST=1 is for tests/run-tests.sh only. It relaxes the
# install-path check and skips AppConfig registration. Destinations still
# default to the live cPanel paths unless the test overrides them.
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
if [[ "${PARSPACK_INSTALL_TEST:-}" != "1" ]]; then
  [[ "$ROOT" == "/root/parspack-backup" ]] || {
    echo "Install this project at /root/parspack-backup. The transport path is fixed." >&2
    exit 1
  }
fi

WORK="$(mktemp -d)"
STAGED_TRANSPORT=""
STAGED_WHM=""
TRANSPORT_BACKUP=""
WHM_BACKUP=""
TRANSPORT_REPLACED=0
WHM_REPLACED=0

cleanup() {
  local status=$?
  set +e
  if [[ "$status" -ne 0 && "$TRANSPORT_REPLACED" -eq 1 ]]; then
    if [[ -n "$TRANSPORT_BACKUP" && -f "$TRANSPORT_BACKUP" ]]; then
      cp -a -- "$TRANSPORT_BACKUP" "$TRANSPORT_DST" || echo "FAILED to restore $TRANSPORT_DST from $TRANSPORT_BACKUP" >&2
    else
      rm -f -- "$TRANSPORT_DST"
    fi
  fi
  if [[ "$status" -ne 0 && "$WHM_REPLACED" -eq 1 ]]; then
    if [[ -n "$WHM_BACKUP" && -f "$WHM_BACKUP" ]]; then
      cp -a -- "$WHM_BACKUP" "$WHM_UI_DST" || echo "FAILED to restore $WHM_UI_DST from $WHM_BACKUP" >&2
    else
      rm -f -- "$WHM_UI_DST"
    fi
  fi
  [[ -n "$STAGED_TRANSPORT" ]] && rm -f -- "$STAGED_TRANSPORT"
  [[ -n "$STAGED_WHM" ]] && rm -f -- "$STAGED_WHM"
  rm -rf -- "$WORK"
  exit "$status"
}
trap cleanup EXIT

fail_check() {
  echo "CHECK FAILED: $*" >&2
  if [[ -f "$WORK/check.err" ]]; then
    cat "$WORK/check.err" >&2
  fi
  exit 1
}

# Syntax checks run before any live path is created, backed up, or replaced.
# config/parspack.conf, mc-config/, and state/ are never copied over.
if ! perl -c "$TRANSPORT_SRC" >"$WORK/check.err" 2>&1; then
  fail_check "perl -c $TRANSPORT_SRC"
fi
: >"$WORK/check.err"

if command -v php >/dev/null 2>&1; then
  if ! php -l "$WHM_UI_SRC" >"$WORK/check.err" 2>&1; then
    fail_check "php -l $WHM_UI_SRC"
  fi
else
  echo "php -l skipped: php is not on PATH"
fi
: >"$WORK/check.err"

while IFS= read -r -d '' script; do
  if ! bash -n "$script" >"$WORK/check.err" 2>&1; then
    fail_check "bash -n $script"
  fi
done < <(find "$ROOT" -type f \( -name '*.sh' -o -path "$ROOT/bin/parspackctl" \) ! -path '*/__pycache__/*' -print0)

shopt -s nullglob
py_files=("$ROOT"/lib/*.py "$ROOT"/bin/*.py)
shopt -u nullglob
if [[ ${#py_files[@]} -eq 0 ]]; then
  fail_check "python3 -m py_compile found no lib/*.py or bin/*.py"
fi
pyc_dir="$WORK/pyc"
mkdir -p "$pyc_dir"
export PYTHONPYCACHEPREFIX="$pyc_dir"
py_before="$(find "$ROOT/lib" "$ROOT/bin" -type d -name '__pycache__' | sort)"
py_n=0
for src in "${py_files[@]}"; do
  [[ -f "$src" ]] || continue
  py_n=$((py_n + 1))
  compiled="$pyc_dir/$py_n-$(basename -- "$src")"
  cp -a -- "$src" "$compiled"
  if ! python3 -m py_compile "$compiled" >"$WORK/check.err" 2>&1; then
    fail_check "python3 -m py_compile $src"
  fi
done
rm -rf "$pyc_dir"
unset PYTHONPYCACHEPREFIX
py_after="$(find "$ROOT/lib" "$ROOT/bin" -type d -name '__pycache__' | sort)"
if [[ "$py_before" != "$py_after" ]]; then
  fail_check "python3 -m py_compile wrote __pycache__ under $ROOT"
fi

[[ -f "$CONF" ]] || { echo "Create $CONF from $EXAMPLE first." >&2; exit 1; }
[[ -x /usr/local/bin/mc ]] || { echo "mc not found at /usr/local/bin/mc." >&2; exit 1; }
[[ -x /usr/bin/python3 || -x /usr/local/bin/python3 ]] || {
  echo "python3 not found at /usr/bin/python3 or /usr/local/bin/python3." >&2
  exit 1
}

mkdir -p "$ROOT"/{logs,reports,state,run,tmp,mc-config,state/history,lib,bin,transport,config}
chmod 700 "$ROOT" "$ROOT/config" "$ROOT/mc-config" "$ROOT/logs" "$ROOT/reports" \
  "$ROOT/state" "$ROOT/state/history" "$ROOT/run" "$ROOT/tmp" \
  "$ROOT/bin" "$ROOT/lib" "$ROOT/transport"
chmod 600 "$CONF"
chmod 700 "$ROOT"/bin/* "$TRANSPORT_SRC"
# Mode 600 is only for regular files directly in lib/. Nested directories,
# their contents, and __pycache__ are not included in this glob.
shopt -s nullglob
for lib_file in "$ROOT"/lib/*; do
  [[ -f "$lib_file" && ! -L "$lib_file" ]] || continue
  [[ "$(basename -- "$lib_file")" == "__pycache__" ]] && continue
  chmod 600 "$lib_file"
done
shopt -u nullglob
chown -R root:root "$ROOT/config" "$ROOT/mc-config" "$ROOT/bin" "$ROOT/lib" \
  "$ROOT/transport" "$ROOT/logs" "$ROOT/state" "$ROOT/reports" "$ROOT/run" "$ROOT/tmp"

"$ROOT/bin/parspackctl" doctor

stage_replace() {
  local src="$1"
  local dst="$2"
  local mode="$3"
  local kind="$4"
  local dir tmp backup=""
  dir="$(dirname -- "$dst")"
  mkdir -p -- "$dir"
  tmp="$(mktemp "$dir/.parspack-stage.XXXXXX")"
  if [[ "$kind" == "perl" ]]; then
    STAGED_TRANSPORT="$tmp"
  elif [[ "$kind" == "php" ]]; then
    STAGED_WHM="$tmp"
  else
    fail_check "unknown install stage $kind"
  fi
  if ! cp -a -- "$src" "$tmp"; then
    fail_check "copy $src to $tmp"
  fi
  chown root:root -- "$tmp"
  chmod "$mode" -- "$tmp"
  if [[ "$kind" == "perl" ]]; then
    if ! perl -c "$tmp" >"$WORK/check.err" 2>&1; then
      fail_check "perl -c $tmp"
    fi
  elif command -v php >/dev/null 2>&1; then
    if ! php -l "$tmp" >"$WORK/check.err" 2>&1; then
      fail_check "php -l $tmp"
    fi
  fi
  if [[ -e "$dst" ]]; then
    backup="${dst}.bak.$(date +%Y%m%d%H%M%S%N)"
    if ! cp -a -- "$dst" "$backup"; then
      fail_check "backup $dst"
    fi
  fi
  if ! mv -f -- "$tmp" "$dst"; then
    fail_check "rename $tmp onto $dst"
  fi
  if [[ "$kind" == "perl" ]]; then
    STAGED_TRANSPORT=""
    TRANSPORT_BACKUP="$backup"
    TRANSPORT_REPLACED=1
  else
    STAGED_WHM=""
    WHM_BACKUP="$backup"
    WHM_REPLACED=1
    chmod 755 -- "$dir"
  fi
}

stage_replace "$TRANSPORT_SRC" "$TRANSPORT_DST" 700 perl
stage_replace "$WHM_UI_SRC" "$WHM_UI_DST" 644 php

if [[ "${PARSPACK_INSTALL_TEST:-}" != "1" && -x /usr/local/cpanel/bin/register_appconfig ]]; then
  if ! /usr/local/cpanel/bin/register_appconfig "$APPJSON"; then
    fail_check "register_appconfig $APPJSON"
  fi
fi

echo "Installed PARSPACK Backup."
echo "Project root: $ROOT"
echo "Transport: $TRANSPORT_DST"
if [[ -n "$TRANSPORT_BACKUP" ]]; then
  echo "Transport backup: $TRANSPORT_BACKUP"
else
  echo "Transport backup: none (no previous file)"
fi
echo "WHM UI: $WHM_UI_DST"
if [[ -n "$WHM_BACKUP" ]]; then
  echo "WHM UI backup: $WHM_BACKUP"
else
  echo "WHM UI backup: none (no previous file)"
fi
echo "Left unchanged: $CONF, $ROOT/mc-config/, $ROOT/state/"
echo "Set TRANSPORT_PASSWORD in $CONF and use that same password in WHM."
echo "Next: $ROOT/bin/parspackctl doctor && $ROOT/bin/parspackctl connection-test"
