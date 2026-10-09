#!/bin/bash
# Send the newest backup day that is still on disk. This is a thin alias of
# `parspackctl retry --date`: the state machine skips a file that is already
# verified with the same size and checksum. One pass only.
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/parspack-common.sh"

STATUS="$ROOT/state/send-remaining.status"
LOCK="$ROOT/state/send-remaining.lock"
LOG="$ROOT/logs/send-remaining.log"
STATUS_STATE=""
DAY=""
TOTAL=0
SENT=0
FAILED=0
SKIPPED=0
CURRENT=""
SYSTEM_SEEN=0
SYSTEM_FAILED=0

write_status() {
  local state="$1"
  local message="$2"
  STATUS_STATE="$state"
  message="${message//$'\n'/ }"
  message="${message//$'\r'/ }"
  message="${message//$'\t'/ }"
  mkdir -p -- "$ROOT/state"
  chmod 700 "$ROOT/state"
  local tmp
  tmp="$(mktemp "$ROOT/state/send-remaining.XXXXXX")"
  cat >"$tmp" <<EOF
state=${state}
day=${DAY}
total=${TOTAL}
sent=${SENT}
failed=${FAILED}
skipped=${SKIPPED}
current=${CURRENT}
message=${message}
EOF
  chmod 600 "$tmp"
  mv -f -- "$tmp" "$STATUS"
  chmod 600 "$STATUS"
}

pending_markers() {
  local name day
  for name in "$STATE_DIR"/system-pending-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do
    [[ -f "$name" && ! -L "$name" ]] || continue
    day="$(basename "$name")"
    printf '%s\n' "${day#system-pending-}"
  done
}

note_incomplete() {
  local day="$1"
  DAY="$day"
  write_status critical "CRITICAL: backup day ${day} is incomplete; system transfer marker is still present"
  exit 1
}

pp_on_exit() {
  if [[ "$STATUS_STATE" == "running" ]]; then
    write_status failed "ارسال دستی قبل از پایان قطع شد"
  fi
}

if [[ "${1:-}" != "--run" ]]; then
  mkdir -p -- "$ROOT/logs" "$ROOT/state"
  chmod 700 "$ROOT/logs" "$ROOT/state"
  touch "$LOG"
  chmod 600 "$LOG"
  setsid "$ROOT/bin/send-remaining.sh" --run </dev/null >>"$LOG" 2>&1 &
  echo "started"
  exit 0
fi

if ! pp_flock 8 "$LOCK"; then
  echo "already running"
  exit 0
fi

write_status running "در حال آماده‌سازی"
pp_bootstrap

if [[ "$MODE" != "PRODUCTION" ]]; then
  write_status failed "حالت TEST است و فایلی فرستاده نمی‌شود"
  exit 2
fi

latest=""
IFS=':' read -ra roots <<< "$LOCAL_READ_ROOTS"
for root in "${roots[@]}"; do
  [[ -d "$root" && ! -L "$root" ]] || continue
  for dir in "$root"/*; do
    [[ -d "$dir" && ! -L "$dir" ]] || continue
    base="$(basename "$dir")"
    [[ "$base" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
    [[ -d "$dir/accounts" && ! -L "$dir/accounts" ]] || continue
    if [[ -z "$latest" || "$base" > "$latest" ]]; then
      latest="$base"
    fi
  done
done

DAY="$latest"
if [[ -z "$DAY" ]]; then
  first_pending=""
  while IFS= read -r first_pending; do
    break
  done < <(pending_markers)
  if [[ -n "$first_pending" ]]; then
    note_incomplete "$first_pending"
  fi
  write_status done "فایل مانده‌ای برای ارسال پیدا نشد"
  exit 0
fi

TOTAL=0
SENT=0
FAILED=0
SKIPPED=0
SYSTEM_SEEN=0
SYSTEM_FAILED=0
write_status running "ارسال فایل‌های مانده از $DAY"
set +e
line="$("$PYTHON3" "$ROOT/lib/parspack_ops.py" retry --date "$DAY")"
rc=$?
set -e
if [[ "$line" =~ ^RESULT\ sent=([0-9]+)\ failed=([0-9]+)\ skipped=([0-9]+)\ system_seen=([0-9]+)\ system_failed=([0-9]+)$ ]]; then
  SENT="${BASH_REMATCH[1]}"
  FAILED="${BASH_REMATCH[2]}"
  SKIPPED="${BASH_REMATCH[3]}"
  SYSTEM_SEEN="${BASH_REMATCH[4]}"
  SYSTEM_FAILED="${BASH_REMATCH[5]}"
  TOTAL=$((SENT + FAILED + SKIPPED))
  pp_log INFO "manual retry sent=$SENT failed=$FAILED skipped=$SKIPPED system_seen=$SYSTEM_SEEN system_failed=$SYSTEM_FAILED"
else
  write_status failed "ارسال دستی نتوانست وضعیت را بخواند"
  exit 1
fi
if [[ "$rc" -eq 2 ]]; then
  write_status failed "ارسال دستی رد شد"
  exit 2
fi

CURRENT=""
marker="$STATE_DIR/system-pending-$DAY"
if [[ -f "$marker" && ! -L "$marker" ]]; then
  # Only send-system.sh removes the marker, and only after the same
  # verified-tree path that uploads manifest.json.
  day_dir=""
  IFS=':' read -ra roots <<< "$LOCAL_READ_ROOTS"
  for root in "${roots[@]}"; do
    if [[ -d "$root/$DAY" && ! -L "$root/$DAY" && -d "$root/$DAY/accounts" && ! -L "$root/$DAY/accounts" ]]; then
      day_dir="$root/$DAY"
      break
    fi
  done
  if [[ -n "$day_dir" ]]; then
    set +e
    "$ROOT/bin/send-system.sh" --complete "$day_dir" "$DAY"
    set -e
  fi
  if [[ -e "$marker" ]]; then
    note_incomplete "$DAY"
  fi
fi
write_status done "ارسال $DAY تمام شد. موفق: $SENT، ناموفق: $FAILED، رد شده: $SKIPPED"
exit 0
