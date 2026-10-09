#!/bin/bash
# Shared helpers for the PARSPACK backup scripts.
# Configuration is loaded through lib/parspack_config.py. Do not source parspack.conf.

pp_on_exit() { :; }

pp_cleanup() {
  local file
  for file in "${PP_TMP[@]+"${PP_TMP[@]}"}"; do
    rm -f -- "$file"
  done
  pp_on_exit
}

pp_find_python() {
  if [[ -x /usr/bin/python3 ]]; then
    PYTHON3=/usr/bin/python3
  elif [[ -x /usr/local/bin/python3 ]]; then
    PYTHON3=/usr/local/bin/python3
  else
    echo "python3 not found at /usr/bin/python3 or /usr/local/bin/python3" >&2
    exit 1
  fi
}

pp_py() {
  "$PYTHON3" "$ROOT/lib/parspack_config.py" "$@"
}

pp_state() {
  "$PYTHON3" "$ROOT/bin/parspack-state" "$@"
}

pp_bootstrap() {
  local lib_dir root_guess envfile
  umask 077
  export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  unset PASSWORD SMTP_PASSWORD TRANSPORT_PASSWORD PARSPACK_ACCESS_KEY PARSPACK_SECRET_KEY
  unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT BASH_ENV ENV PYTHONPATH PYTHONHOME TMP TEMP
  PP_TMP=()
  trap pp_cleanup EXIT
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  root_guess="$(cd "$lib_dir/.." && pwd)"
  if [[ -n "${ROOT:-}" && "$ROOT" != "$root_guess" ]]; then
    echo "project root does not match the helper directory" >&2
    exit 1
  fi
  ROOT="$root_guess"
  if [[ "$(id -u)" == "0" ]]; then
    export HOME=/root
  fi
  mkdir -p -- "$ROOT/tmp"
  chmod 700 "$ROOT/tmp"
  export TMPDIR="$ROOT/tmp"
  pp_find_python
  pp_py prepare-runtime
  envfile="$(mktemp "$ROOT/tmp/pp-env.XXXXXX")"
  chmod 600 "$envfile"
  PP_TMP+=("$envfile")
  pp_py shell-exports > "$envfile"
  # shellcheck disable=SC1090
  source "$envfile"
  rm -f -- "$envfile"
  if [[ -f "$ROOT/config/parspack.conf" ]]; then
    local mode
    mode="$(stat -c '%a' "$ROOT/config/parspack.conf")"
    if [[ "$mode" != "600" ]]; then
      echo "configuration file is not mode 600" >&2
      exit 1
    fi
  fi
}

pp_prepare_mc() {
  pp_py write-mc-config
}

pp_mc() {
  env \
    -u PASSWORD \
    -u SMTP_PASSWORD \
    -u TRANSPORT_PASSWORD \
    -u PARSPACK_ACCESS_KEY \
    -u PARSPACK_SECRET_KEY \
    -u LD_PRELOAD \
    -u LD_LIBRARY_PATH \
    "$MC_BIN" --config-dir "$MC_CONFIG_DIR" "$@"
}

pp_log() {
  local level="$1"
  shift
  local msg="$*"
  msg="${msg//$'\n'/ }"
  msg="${msg//$'\r'/ }"
  local line
  line="$(printf '%s [%s] %s' "$(date -Is)" "$level" "$msg")"
  printf '%s\n' "$line" >&2
  if [[ -n "${LOG_DIR:-}" ]]; then
    mkdir -p -- "$LOG_DIR"
    chmod 700 "$LOG_DIR"
    touch "$LOG_DIR/backup.log"
    chmod 600 "$LOG_DIR/backup.log"
    printf '%s\n' "$line" >> "$LOG_DIR/backup.log"
  fi
}

pp_die() {
  pp_log ERROR "$*" || true
  exit 1
}

pp_mktemp() {
  local file
  file="$(mktemp "$ROOT/tmp/pp.XXXXXX")"
  chmod 600 "$file"
  PP_TMP+=("$file")
  printf '%s\n' "$file"
}

pp_flock() {
  # flock is the lock. The pid written inside is only a log hint. A dead pid
  # does not make the lock free, and this function never deletes the file.
  # The third argument is how many seconds to wait. Zero waits not at all.
  local fd="$1" file="$2" wait_seconds="${3:-0}" holder
  mkdir -p -- "$(dirname -- "$file")"
  chmod 700 "$(dirname -- "$file")"
  touch -- "$file"
  chmod 600 "$file"
  case "$fd" in
    8) exec 8<>"$file" ;;
    9) exec 9<>"$file" ;;
    *)
      echo "unsupported lock descriptor" >&2
      return 2
      ;;
  esac
  if [[ ! "$wait_seconds" =~ ^[0-9]+$ ]]; then
    wait_seconds=0
  fi
  local locked=1
  if (( wait_seconds > 0 )); then
    if flock -w "$wait_seconds" "$fd"; then
      locked=0
    fi
  elif flock -n "$fd"; then
    locked=0
  fi
  if (( locked )); then
    holder=""
    IFS= read -r holder <"$file" || true
    if [[ "$holder" =~ ^[0-9]+$ ]] && ! kill -0 "$holder" 2>/dev/null; then
      pp_log WARN "Lock $file records pid $holder which is not running; flock is still held, so this run will not take it"
    else
      pp_log WARN "Lock $file is already held"
    fi
    return 1
  fi
  printf '%s\n' "$$" >"$file" || true
  return 0
}

pp_assert_remote_delete_allowed() {
  if [[ "$MODE" == "TEST" || "$ALLOW_REMOTE_DELETE" != "1" ]]; then
    pp_die "refused: remote delete disabled"
  fi
}
