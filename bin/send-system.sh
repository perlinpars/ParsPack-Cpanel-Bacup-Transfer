#!/bin/bash
# Mirror one backup day's system directory next to the accounts that cPanel
# just uploaded. The relative path under system/ is kept as-is. This does not
# pack the tree into a new archive.
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
umask 077

if [[ "${1:-}" == "--complete" ]]; then
  if [[ $# -ne 3 ]]; then
    echo "usage: $0 --complete LOCAL_DAY REMOTE_BASE" >&2
    exit 2
  fi
elif [[ "${1:-}" != "--run" || $# -ne 3 ]]; then
  echo "started" >&2
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/parspack-common.sh"

LOCAL_ACCOUNT="$2"
REMOTE_ACCOUNT="$3"
LOCK="$ROOT/state/send-system.lock"

mkdir -p -- "$ROOT/state" "$ROOT/logs" "$ROOT/run"
chmod 700 "$ROOT/state" "$ROOT/logs" "$ROOT/run"
if ! pp_flock 8 "$LOCK"; then
  echo "system mirror already running"
  exit 0
fi

pp_bootstrap

if [[ "${1:-}" == "--complete" ]]; then
  local_day="$2"
  remote_base="$3"
  if [[ -z "$local_day" || "$local_day" == "/" || "$local_day" != /* || -L "$local_day" || ! -d "$local_day" ]]; then
    pp_log ERROR "system mirror refused an unexpected backup directory"
    exit 2
  fi
  day_name="$(basename "$local_day")"
  if [[ ! "$day_name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    pp_log ERROR "system mirror refused a backup directory without a date"
    exit 2
  fi
  if [[ "$remote_base" != "$day_name" && "$remote_base" != */"$day_name" ]]; then
    pp_log ERROR "system mirror refused a remote base that is not the backup day"
    exit 2
  fi
  pp_py object-key --require-child -- "$remote_base/system/.keep" >/dev/null
  system_dir="$local_day/system"
  if [[ -L "$system_dir" ]]; then
    pp_log ERROR "system mirror refused a symlink"
    exit 2
  fi
else
  LOCAL_ACCOUNT="$2"
  REMOTE_ACCOUNT="$3"
  account_name="$(basename "$LOCAL_ACCOUNT")"
  suffix="/accounts/${account_name}"
  if [[ "$LOCAL_ACCOUNT" != /* || "$LOCAL_ACCOUNT" != *"$suffix" || "$REMOTE_ACCOUNT" != *"$suffix" ]]; then
    pp_log ERROR "system mirror refused an unexpected account path"
    exit 2
  fi
  if [[ "$account_name" == validate.tmp-* || ! "$account_name" =~ ^[A-Za-z0-9._+-]+\.tar\.gz$ ]]; then
    exit 0
  fi

  local_day="${LOCAL_ACCOUNT%"$suffix"}"
  remote_base="${REMOTE_ACCOUNT%"$suffix"}"
  if [[ -z "$local_day" || "$local_day" == "/" || "$local_day" != /* ]]; then
    pp_log ERROR "system mirror refused an unexpected backup directory"
    exit 2
  fi
  day_name="$(basename "$local_day")"
  if [[ ! "$day_name" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    pp_log ERROR "system mirror refused a backup directory without a date"
    exit 2
  fi
  system_dir="$local_day/system"
  if [[ -L "$local_day" || -L "$system_dir" ]]; then
    pp_log ERROR "system mirror refused a symlink"
    exit 2
  fi
fi

safe_rel() {
  local rel="$1" part
  [[ -n "$rel" && "$rel" != /* && "$rel" != *//* && ${#rel} -le 900 ]] || return 1
  while [[ "$rel" == */* ]]; do
    part="${rel%%/*}"
    rel="${rel#*/}"
    [[ "$part" =~ ^[A-Za-z0-9._+-]+$ && "$part" != "." && "$part" != ".." && "$part" != -* ]] || return 1
  done
  [[ "$rel" =~ ^[A-Za-z0-9._+-]+$ && "$rel" != "." && "$rel" != ".." && "$rel" != -* ]]
}

lock_free() {
  # Probe only. The upload lock is taken later by each engine process and
  # released when that one file finishes.
  exec 7>"$ROOT/run/upload.lock"
  if flock -n 7; then
    flock -u 7
    exec 7>&-
    return 0
  fi
  exec 7>&-
  return 1
}

clear_pending_marker() {
  local marker="$ROOT/state/system-pending-$day_name"
  if [[ -L "$marker" ]]; then
    pp_log ERROR "system pending marker is a symlink and was left in place"
    return 1
  fi
  if [[ -e "$marker" ]]; then
    rm -f -- "$marker"
  fi
  if [[ -e "$marker" ]]; then
    pp_log ERROR "system pending marker could not be removed for $day_name"
    return 1
  fi
  return 0
}

account_set() {
  local dir="$local_day/accounts" file
  [[ -d "$dir" && ! -L "$dir" ]] || return 0
  find -P "$dir" -maxdepth 1 -type f -name '*.tar.gz' ! -name 'validate.tmp-*' -printf '%f\t%s\n' | sort
}

account_names() {
  local file size name
  names=""
  while IFS=$'\t' read -r file size; do
    [[ -n "$file" ]] || continue
    name="${file%.tar.gz}"
    [[ "$name" =~ ^[A-Za-z0-9._+-]+$ ]] || continue
    if [[ -n "$names" ]]; then
      names="${names},${name}"
    else
      names="$name"
    fi
  done
}

send_report() {
  local code="$1"
  pp_log INFO "Building the daily report after the system transfer"
  set +e
  "$PYTHON3" "$ROOT/bin/report.py"
  local report_rc=$?
  set -e
  if [[ "$report_rc" -ne 0 ]]; then
    pp_log ERROR "Daily report failed"
  fi
  exit "$code"
}

stable_for="${SYSTEM_SET_STABLE_SECONDS:-120}"
deadline=$(( $(date +%s) + ${SYSTEM_DEADLINE_SECONDS:-21600} ))
previous_set=""
stable_since=0
gate="waiting"
if [[ "${1:-}" == "--complete" ]]; then
  # Same gate as the detached mirror, without waiting. A day that is not
  # already terminal stays pending and the marker is not removed.
  names=""
  account_names <<< "$(account_set || true)"
  now="$(date +%s)"
  set +e
  gate="$(pp_state accounts-ready --date "$day_name" --names "$names" --set-stable 1 --now "$now" --deadline "$((now + 10))")"
  gate_rc=$?
  set -e
  if [[ "$gate_rc" -ne 0 || "$gate" != "ready" ]]; then
    pp_log ERROR "system tree is not a complete verified day; marker kept for $day_name"
    exit 1
  fi
else
pp_state set-run --date "$day_name" --state open || true
pp_log INFO "Waiting until every account tar in $local_day/accounts is terminal, or until the system deadline"
while (( $(date +%s) < deadline )); do
  now="$(date +%s)"
  current_set="$(account_set || true)"
  if [[ -n "$current_set" && "$current_set" == "$previous_set" ]]; then
    if (( stable_since == 0 )); then
      stable_since="$now"
    fi
  else
    stable_since=0
    previous_set="$current_set"
  fi
  names=""
  account_names <<< "$current_set"
  set_stable=0
  if (( stable_since > 0 && now - stable_since >= stable_for )); then
    set_stable=1
  fi
  set +e
  gate="$(pp_state accounts-ready --date "$day_name" --names "$names" --set-stable "$set_stable" --now "$now" --deadline "$deadline")"
  gate_rc=$?
  set -e
  if [[ "$gate_rc" -eq 0 && "$gate" == "ready" ]]; then
    break
  fi
  if [[ "$gate" == "partial" ]]; then
    break
  fi
  sleep 15
done
if [[ "$gate" != "ready" ]]; then
  gate="partial"
  pp_state set-run --date "$day_name" --state partial || true
  pp_log ERROR "system mirror deadline reached before every expected account was terminal; day $day_name is partial and manifest.json will not be uploaded"
fi
fi

if [[ "$gate" == "partial" && ! -d "$system_dir" ]]; then
  send_report 0
fi

if [[ "$gate" == "ready" ]]; then
  pp_state set-run --date "$day_name" --state system_running || true
fi
"$ROOT/bin/engine-upload.sh" --due || true

sent=0
failed=0
skipped=0
if [[ -d "$system_dir" ]]; then
while IFS= read -r -d '' file; do
  [[ -f "$file" && ! -L "$file" ]] || continue
  rel="${file#"$system_dir"/}"
  if [[ "$rel" == "$file" ]] || ! safe_rel "system/$rel"; then
    failed=$((failed + 1))
    pp_log ERROR "system mirror skipped an unsafe path under system"
    continue
  fi
  if [[ -n "$remote_base" ]]; then
    remote="$remote_base/system/$rel"
  else
    remote="system/$rel"
  fi
  size="$(stat -c '%s' "$file")"
  set +e
  pp_state same --local "$file" --size "$size"
  same_rc=$?
  set -e
  if [[ "$same_rc" -eq 0 ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$same_rc" -ne 1 ]]; then
    pp_die "state store could not be read"
  fi
  pp_log INFO "Mirroring system file $remote"
  rc=1
  attempt=1
  while (( attempt <= 5 )); do
    set +e
    PARSPACK_TREE_STABLE=1 "$ROOT/bin/engine-upload.sh" "$file" "$remote"
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      break
    fi
    if lock_free; then
      break
    fi
    sleep 30
    attempt=$((attempt + 1))
  done
  if [[ "$rc" -eq 0 ]]; then
    sent=$((sent + 1))
  else
    failed=$((failed + 1))
    pp_log ERROR "system mirror failed for $remote"
  fi
done < <(find -P "$system_dir" -type f -print0)
fi

pp_log INFO "system mirror finished sent=$sent failed=$failed skipped=$skipped gate=$gate"
if [[ "$gate" == "partial" ]]; then
  pp_state set-run --date "$day_name" --state partial || true
  pp_log ERROR "system mirror left day $day_name partial; marker kept and manifest.json was not uploaded"
  send_report 0
fi
if (( failed > 0 || sent + skipped == 0 )); then
  if (( sent + skipped == 0 )); then
    pp_log ERROR "system mirror verified no files; marker kept for $day_name"
  fi
  send_report 1
fi

plugin_version="unknown"
if [[ -f "$ROOT/VERSION" ]]; then
  plugin_version="$(tr -cd 'A-Za-z0-9._-' < "$ROOT/VERSION" | head -c 40)"
fi
cpanel_version="unknown"
if [[ -f /usr/local/cpanel/version ]]; then
  cpanel_version="$(tr -cd 'A-Za-z0-9._-' < /usr/local/cpanel/version | head -c 40)"
fi
manifest="$local_day/manifest.json"
pp_state manifest --date "$day_name" --dest "$manifest" --plugin-version "$plugin_version" --cpanel-version "$cpanel_version" --system-status complete --expected "$names"
manifest_remote="$remote_base/manifest.json"
set +e
PARSPACK_TREE_STABLE=1 "$ROOT/bin/engine-upload.sh" "$manifest" "$manifest_remote"
manifest_rc=$?
set -e
if [[ "$manifest_rc" -ne 0 ]]; then
  pp_log ERROR "manifest.json could not be uploaded and verified; marker kept for $day_name"
  send_report 1
fi
pp_state set-run --date "$day_name" --state complete || true
if ! clear_pending_marker; then
  send_report 1
fi
send_report 0
