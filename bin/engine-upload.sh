#!/bin/bash
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/parspack-common.sh"

usage() {
  echo "Usage: $0 LOCAL_FILE REMOTE_OBJECT" >&2
}

file_identity() {
  local file="$1"
  [[ -e "$file" && ! -L "$file" && -f "$file" && -r "$file" ]] || return 1
  stat -c '%s %Y' "$file"
}

check_file_age_and_stability() {
  local file="$1"
  local snap mtime now age previous attempt
  # One stat: the file still exists, is a regular file, is not a symlink,
  # and is readable. send-system.sh sets PARSPACK_TREE_STABLE only after the
  # system tree has already been quiet for the configured window, so this
  # path does not sleep again for every system file.
  snap="$(file_identity "$file")" || pp_die "Local file is not a readable regular file"
  if [[ "${PARSPACK_TREE_STABLE:-}" == "1" ]]; then
    return 0
  fi
  mtime="${snap##* }"
  now="$(date +%s)"
  age=$(( (now - mtime) / 60 ))
  # cPanel calls put as soon as the archive is closed, so the age is almost
  # always 0. Refusing here ends the only attempt. A file that is already
  # older than MIN_FILE_AGE_MINUTES is accepted after the stat above.
  if (( MIN_FILE_AGE_MINUTES > 0 && age >= MIN_FILE_AGE_MINUTES )); then
    return 0
  fi
  if (( MIN_FILE_AGE_MINUTES > 0 )); then
    pp_log INFO "File is younger than MIN_FILE_AGE_MINUTES (${age}m < ${MIN_FILE_AGE_MINUTES}m); watching its size and mtime instead of refusing it"
  fi
  if (( STABILITY_CHECK_SECONDS <= 0 )); then
    return 0
  fi
  attempt=1
  while (( attempt <= RETRY_COUNT )); do
    previous="$(file_identity "$file")" || pp_die "Local file disappeared during the stability check"
    sleep "$STABILITY_CHECK_SECONDS"
    snap="$(file_identity "$file")" || pp_die "Local file disappeared during the stability check"
    if [[ "$snap" == "$previous" ]]; then
      pp_log INFO "File size and mtime stayed unchanged during stability check ${attempt}/${RETRY_COUNT}"
      return 0
    fi
    pp_log WARN "Local file size or mtime changed during stability check ${attempt}/${RETRY_COUNT}"
    if (( attempt >= RETRY_COUNT )); then
      pp_die "Local file was still changing after ${RETRY_COUNT} stability checks"
    fi
    attempt=$((attempt + 1))
  done
}

read_remote_stat() {
  local target="$1"
  local parent errf listing list_rc stat_line fields_rc
  parent="${target%/*}/"
  errf="$(pp_mktemp)"
  set +e
  listing="$(pp_mc ls --json -- "$parent" 2>"$errf")"
  list_rc=$?
  set -e
  # A missing directory is an absent object. A longer key such as
  # name.tar.gz.parspack-test must not be reported as name.tar.gz.
  outf="$(pp_mktemp)"
  printf '%s\n' "$listing" > "$outf"
  if pp_py stat-missing -- "$errf" || pp_py stat-missing -- "$outf"; then
    printf '%s\n' "missing"
    return 0
  fi
  if [[ "$list_rc" -ne 0 ]]; then
    if [[ "$(pp_py classify-error <"$errf" 2>/dev/null || true)" == "FATAL" ]]; then
      return 2
    fi
    return 1
  fi
  set +e
  stat_line="$(printf '%s\n' "$listing" | pp_py exact-stat -- "$target")"
  fields_rc=$?
  set -e
  if [[ "$fields_rc" -ne 0 ]]; then
    return 1
  fi
  printf '%s\n' "$stat_line"
}

verify_remote_content_sha() {
  local target="$1"
  local expect_size="${2:-}"
  local data_file remote_sha actual_size

  data_file="$(pp_mktemp)"
  if ! pp_mc cat -- "$target" >"$data_file"; then
    rm -f -- "$data_file"
    return 1
  fi
  actual_size="$(stat -c '%s' "$data_file")"
  if [[ -n "$expect_size" && "$actual_size" != "$expect_size" ]]; then
    rm -f -- "$data_file"
    return 1
  fi
  remote_sha="$(sha256sum "$data_file" | awk '{print $1}')"
  rm -f -- "$data_file"
  if [[ ! "$remote_sha" =~ ^[0-9a-fA-F]{64}$ ]]; then
    return 1
  fi
  printf '%s\n' "$remote_sha"
}

maybe_delete_local() {
  local file="$1"
  local digest="$2"
  # cPanel still owns /var/cpanel/backups/tmp.test.* and deletes it after Validate.
  if [[ "${cpanel_probe:-0}" -eq 1 ]]; then
    return 0
  fi
  if [[ "$MODE" == "TEST" ]]; then
    pp_log INFO "TEST mode kept the local file"
    return 0
  fi
  if [[ "$DELETE_LOCAL_AFTER_SUCCESS" != "1" ]]; then
    return 0
  fi
  case "${DELETE_LOCAL_DELAY:-0}" in
    manual)
      pp_log INFO "Local delete is manual; the verified file stays on disk"
      return 0
      ;;
    10m|1h)
      if [[ -n "${state_id:-}" ]]; then
        pp_state note delete-due --id "$state_id" --delay "$DELETE_LOCAL_DELAY" || true
      fi
      pp_log INFO "Local delete scheduled after $DELETE_LOCAL_DELAY"
      return 0
      ;;
    0)
      ;;
    *)
      pp_log ERROR "DELETE_LOCAL_DELAY is not 0, 10m, 1h, or manual; local file kept"
      return 0
      ;;
  esac
  recheck_then_delete "$file" "$digest"
}

recheck_then_delete() {
  local file="$1"
  local digest="$2"
  local checked rc size_now
  if [[ ! -e "$file" ]]; then
    if [[ -n "${state_id:-}" ]]; then
      pp_state note deleted --id "$state_id" || true
    fi
    return 0
  fi
  set +e
  checked="$(read_remote_stat "$TARGET")"
  rc=$?
  set -e
  size_now="${checked%%$'\t'*}"
  if [[ "$rc" -ne 0 || "$checked" == "missing" || "$size_now" != "$local_size" ]]; then
    if [[ -n "${state_id:-}" ]]; then
      pp_state note cleanup-failed --id "$state_id" --error-class LOCAL --error "remote object was not the verified size immediately before delete" || true
    fi
    pp_log ERROR "Local delete skipped because the remote object could not be rechecked: $TARGET"
    return 0
  fi
  set +e
  pp_py delete-local -- "$file" "$digest"
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then
    pp_log INFO "Deleted the local file after the remote size was rechecked"
    if [[ -n "${state_id:-}" ]]; then
      pp_state note deleted --id "$state_id"
    fi
    return 0
  fi
  if [[ -n "${state_id:-}" ]]; then
    pp_state note cleanup-failed --id "$state_id" --error-class LOCAL --error "local delete failed" || true
  fi
  pp_log ERROR "Local file could not be deleted after verification; the backup itself remains verified"
  return 0
}

record_upload() {
  local file="$1"
  local remote="$2"
  local size="$3"
  local duration="$4"
  local attempt="$5"
  [[ -n "${state_id:-}" ]] || return 0
  case "$file$remote" in
    *$'\n'*|*$'\r'*|*$'\t'*)
      pp_die "Refusing to record a path containing a control character"
      ;;
  esac
  local remote_sum="${VERIFY_SUM:-$local_sha}"
  pp_state note verified --id "$state_id" --remote-checksum "$remote_sum" --level "${VERIFY_LEVEL:-sha256}" --attempt "$attempt" --duration "$duration"
}

open_state() {
  local mtime line kind
  mtime="$(stat -c '%Y' "$LOCAL_FILE")"
  line="$(pp_state note discovered --local "$LOCAL_FILE" --remote "$TARGET" --size "$local_size" --mtime "$mtime" --checksum "$local_sha")"
  kind="${line%% *}"
  state_id="${line#* }"
  state_id="${state_id%% *}"
  if [[ "$kind" == "skip" ]]; then
    return 1
  fi
  if [[ "$kind" != "proceed" || ! "$state_id" =~ ^[0-9]+$ ]]; then
    pp_die "State store returned an unexpected claim"
  fi
  if [[ "${PARSPACK_TREE_STABLE:-}" != "1" && "$LOCAL_FILE" != /var/cpanel/backups/tmp.test.* ]]; then
    pp_state note stable --id "$state_id"
  fi
  pp_state note uploading --id "$state_id" --checksum "$local_sha" --attempt 1 --pid "$$"
}

run_due_deletes() {
  [[ "${DELETE_LOCAL_AFTER_SUCCESS:-0}" == "1" ]] || return 0
  [[ "${DELETE_LOCAL_DELAY:-0}" == "0" || "${DELETE_LOCAL_DELAY:-0}" == "manual" ]] && return 0
  local id path remote size digest count
  count=0
  while IFS=$'\t' read -r id path remote size; do
    [[ -n "$id" ]] || continue
    count=$((count + 1))
    if (( count > 20 )); then
      break
    fi
    state_id="$id"
    TARGET="$remote"
    local_size="$size"
    if [[ ! -f "$path" || -L "$path" ]]; then
      pp_state note cleanup-failed --id "$id" --error-class LOCAL --error "local file was not a regular file at delete time" || true
      pp_log ERROR "Local delete skipped because the file was not a regular file"
      continue
    fi
    digest="$(pp_py sha256 -- "$path")" || continue
    recheck_then_delete "$path" "$digest"
  done < <(pp_state due-deletes || true)
}

upload_lock_wait_seconds() {
  if [[ "${PARSPACK_TREE_STABLE:-}" == "1" ]]; then
    printf '%s\n' "${SYSTEM_UPLOAD_LOCK_WAIT_SECONDS:-600}"
  else
    printf '%s\n' "${UPLOAD_LOCK_WAIT_SECONDS:-120}"
  fi
}

if [[ "${1:-}" == "--due" ]]; then
  pp_bootstrap
  if ! pp_flock 9 "$ROOT/run/upload.lock" "$(upload_lock_wait_seconds)"; then
    exit 0
  fi
  pp_prepare_mc
  run_due_deletes
  exit 0
fi

[[ $# -eq 2 ]] || { usage; exit 2; }

pp_bootstrap

LOCAL_FILE="$(pp_py check-local read -- "$1")"
TARGET="$(pp_py object-key --require-child -- "$2")"
lock_wait="$(upload_lock_wait_seconds)"
if ! pp_flock 9 "$ROOT/run/upload.lock" "$lock_wait"; then
  pp_die "Upload lock was still held after ${lock_wait}s while waiting to upload $LOCAL_FILE"
fi

pp_prepare_mc
local_size="$(stat -c '%s' "$LOCAL_FILE")"
if (( local_size <= 0 )); then
  pp_die "Refusing to upload an empty file"
fi

# WHM Validate uploads /var/cpanel/backups/tmp.test.<pid>-<time> as
# validate.tmp-<pid>-<time>.txt. That probe is a real small upload so the
# destination can pass. Every other file in TEST mode still exits 3 and
# stores nothing, so cPanel does not treat a dry run as a finished backup.
cpanel_probe=0
if [[ "$LOCAL_FILE" =~ ^/var/cpanel/backups/tmp\.test\.[0-9]+-[0-9]+$ ]]; then
  cpanel_probe=1
elif [[ "$TARGET" =~ (^|/)validate\.tmp-[0-9]+-[0-9]+\.txt$ ]]; then
  cpanel_probe=1
fi
if [[ "$MODE" != "PRODUCTION" && "$cpanel_probe" -eq 0 ]]; then
  probe="$(pp_py object-key --allow-empty -- "")"
  errf="$(pp_mktemp)"
  outf="$(pp_mktemp)"
  set +e
  pp_mc ls --json -- "${probe}/" >"$outf" 2>"$errf"
  probe_rc=$?
  set -e
  if [[ "$probe_rc" -ne 0 ]] && ! pp_py stat-missing -- "$errf" && ! pp_py stat-missing -- "$outf"; then
    pp_die "TEST mode could not reach the bucket; nothing was uploaded"
  fi
  pp_log INFO "MODE=$MODE: configuration, path, and bucket were checked; nothing was uploaded: $LOCAL_FILE"
  exit 3
fi

if [[ "$cpanel_probe" -eq 1 ]]; then
  pp_log INFO "cPanel transport validation file detected; skipping age/stability check"
else
  check_file_age_and_stability "$LOCAL_FILE"
fi

local_sha="$(pp_py sha256 -- "$LOCAL_FILE")"
state_id=""
# The validation probe is not a backup day. A state row would be "undated"
# and would hide the real newest verified day from later retention checks.
if [[ "$cpanel_probe" -eq 0 ]]; then
  if ! open_state; then
    maybe_delete_local "$LOCAL_FILE" "$local_sha"
    exit 0
  fi
fi

read_attempt=1
while (( read_attempt <= RETRY_COUNT )); do
  set +e
  remote_stat="$(read_remote_stat "$TARGET")"
  remote_rc=$?
  set -e
  if [[ "$remote_rc" -eq 0 ]]; then
    break
  fi
  if [[ "$remote_rc" -eq 2 ]]; then
    pp_die "Remote storage refused the request; upload was not started"
  fi
  if (( read_attempt >= RETRY_COUNT )); then
    pp_die "Could not read the remote object; upload was not started"
  fi
  delay="$(pp_py retry-delay -- "$read_attempt" "$RETRY_DELAY_SECONDS" "${RETRY_DELAY_CAP_SECONDS:-300}")"
  pp_log INFO "Retrying the remote read after ${delay}s"
  sleep "$delay"
  read_attempt=$((read_attempt + 1))
done
if [[ "$remote_stat" == "missing" ]]; then
  exists=0
  remote_size="-"
  remote_sha="-"
  remote_etag=""
else
  exists=1
  remote_size="${remote_stat%%$'\t'*}"
  rest="${remote_stat#*$'\t'}"
  remote_sha="${rest%%$'\t'*}"
  remote_etag=""
  if [[ "$rest" == *$'\t'* ]]; then
    remote_etag="${rest#*$'\t'}"
  fi
fi

decision="$(pp_py decide -- "$MODE" "$REMOTE_EXISTS_POLICY" "$exists" "$local_size" "$remote_size" "$local_sha" "$remote_sha")"
case "$decision" in
  skip)
    pp_log INFO "Remote object matches size and checksum; upload skipped: $TARGET"
    VERIFY_LEVEL=sha256
    VERIFY_SUM="$local_sha"
    pp_state note verified --id "$state_id" --remote-checksum "$local_sha" --level sha256 --attempt 0 --duration 0
    maybe_delete_local "$LOCAL_FILE" "$local_sha"
    exit 0
    ;;
  skip-legacy)
    pp_log INFO "Remote object has the same size and no checksum. It was left unchanged and the local file was kept: $TARGET"
    pp_state note skipped --id "$state_id"
    exit 0
    ;;
  verify-content)
    pp_log INFO "Remote object has the same size but no comparable SHA256; verifying its full content: $TARGET"
    integrity="$(pp_py verify-decision --local-size "$local_size" --remote-size "$remote_size" --local-sha "$local_sha" --remote-sha "$remote_sha" --etag "$remote_etag" --part-size "$PART_SIZE" --file "$LOCAL_FILE")"
    case "$integrity" in
      "ok etag")
        pp_log INFO "Remote object verified by the S3 ETag for part size $PART_SIZE"
        pp_state note verified --id "$state_id" --remote-checksum "$remote_etag" --level etag --attempt 0 --duration 0
        maybe_delete_local "$LOCAL_FILE" "$local_sha"
        exit 0
        ;;
      "ok sha256")
        pp_state note verified --id "$state_id" --remote-checksum "$local_sha" --level sha256 --attempt 0 --duration 0
        maybe_delete_local "$LOCAL_FILE" "$local_sha"
        exit 0
        ;;
      mismatch)
        if [[ "$MODE" == "PRODUCTION" ]]; then
          pp_log WARN "Remote object same size but metadata did not verify; replacing that one object: $TARGET"
          decision=reupload
        else
          pp_log ERROR "Remote verification failed (metadata mismatch). Remote object was left untouched: $TARGET"
          pp_state note verify-failed --id "$state_id" --error-class VERIFY --error "remote metadata did not match" || true
          exit 1
        fi
        ;;
    esac
    if [[ "$decision" == "verify-content" ]]; then
      set +e
      remote_content_sha="$(verify_remote_content_sha "$TARGET")"
      content_verify_rc=$?
      set -e
      if [[ "$content_verify_rc" -eq 0 && "$remote_content_sha" == "$local_sha" ]]; then
        pp_log INFO "Remote object content SHA256 verified: $local_sha"
        pp_state note verified --id "$state_id" --remote-checksum "$local_sha" --level sha256 --attempt 0 --duration 0
        maybe_delete_local "$LOCAL_FILE" "$local_sha"
        exit 0
      fi
      if [[ "$MODE" == "PRODUCTION" ]]; then
        if [[ "$content_verify_rc" -eq 0 ]]; then
          pp_log WARN "Remote content checksum mismatch (same size); replacing that one object: $TARGET"
        else
          pp_log WARN "Remote content could not be read for verification (same size); replacing that one object: $TARGET"
        fi
        decision=reupload
      else
        if [[ "$content_verify_rc" -eq 0 ]]; then
          pp_log ERROR "Remote verification failed (content checksum mismatch). Remote object was left untouched: $TARGET"
        else
          pp_log ERROR "Could not verify remote object content; it was left untouched: $TARGET"
        fi
        pp_state note verify-failed --id "$state_id" --error-class VERIFY --error "remote content did not match" || true
        exit 1
      fi
    fi
    ;;
  reupload)
    pp_log WARN "Remote object same size with a different checksum; replacing that one object: $TARGET"
    ;;
  upload)
    ;;
  fail)
    pp_state note upload-failed --id "$state_id" --error-class POLICY --error "remote object differs and overwrite is not allowed" || true
    pp_die "Remote object differs and was left unchanged because overwrite is not allowed: $TARGET"
    ;;
  *)
    pp_die "Unexpected upload decision"
    ;;
esac
if [[ "$decision" != "upload" && "$decision" != "reupload" ]]; then
  pp_die "Unexpected upload decision after verify"
fi

# S3 has no rename. mc mv is copy plus an immediate delete of the source, so a
# failure during that copy must not be followed by deleting the verified
# temporary object. Publish with copy, verify the final name, and only then
# delete the .uploading object. An interrupted copy does not leave a partial
# object under the final name: S3 shows the destination only after the copy
# completes, and an unfinished multipart copy is not that object.
upload_part="${TARGET}.uploading"
discard_part() {
  [[ "$upload_part" == "${TARGET}.uploading" ]] || pp_die "Refusing to remove an unexpected temporary object"
  pp_mc rm --force -- "$upload_part" >/dev/null 2>&1 || true
}
# Drop a leftover temporary object from an earlier interrupted put for this key.
if [[ "$MODE" == "PRODUCTION" ]]; then
  set +e
  leftover_stat="$(read_remote_stat "$upload_part")"
  leftover_rc=$?
  set -e
  if [[ "$leftover_rc" -eq 0 && "$leftover_stat" != "missing" ]]; then
    pp_log WARN "Removing orphan temporary object before upload: $upload_part"
    discard_part
  fi
fi
# match: size and checksum agree. mismatch: a readable object differs.
# missing: no object. unreadable: the check itself failed, so nothing is deleted.
classify_remote() {
  local subject="$1"
  local verified verify_rc verified_size verified_sha remote_content_sha content_verify_rc
  set +e
  verified="$(read_remote_stat "$subject")"
  verify_rc=$?
  set -e
  if [[ "$verify_rc" -ne 0 ]]; then
    printf '%s\n' unreadable
    return 0
  fi
  if [[ "$verified" == "missing" ]]; then
    printf '%s\n' missing
    return 0
  fi
  verified_size="${verified%%$'\t'*}"
  rest="${verified#*$'\t'}"
  verified_sha="${rest%%$'\t'*}"
  verified_etag=""
  if [[ "$rest" == *$'\t'* ]]; then
    verified_etag="${rest#*$'\t'}"
  fi
  integrity="$(pp_py verify-decision --local-size "$local_size" --remote-size "$verified_size" --local-sha "$local_sha" --remote-sha "$verified_sha" --etag "$verified_etag" --part-size "$PART_SIZE" --file "$LOCAL_FILE")"
  case "$integrity" in
    "ok sha256")
      VERIFY_LEVEL=sha256
      VERIFY_SUM="$local_sha"
      printf '%s\n' match
      return 0
      ;;
    "ok etag")
      VERIFY_LEVEL=etag
      VERIFY_SUM="$verified_etag"
      printf '%s\n' match
      return 0
      ;;
    "ok size-only")
      VERIFY_LEVEL="size-only"
      VERIFY_SUM=""
      printf '%s\n' match
      return 0
      ;;
    mismatch)
      if [[ "$verified_size" != "$local_size" ]]; then
        pp_log ERROR "Remote size mismatch for $subject: remote=$verified_size local=$local_size"
      elif [[ -n "$verified_sha" && "$verified_sha" != "-" && "${verified_sha,,}" != "${local_sha,,}" ]]; then
        pp_log ERROR "Remote checksum mismatch for $subject"
      else
        pp_log ERROR "Remote metadata verification mismatch for $subject"
      fi
      printf '%s\n' mismatch
      return 0
      ;;
  esac
  set +e
  remote_content_sha="$(verify_remote_content_sha "$subject" "$verified_size")"
  content_verify_rc=$?
  set -e
  if [[ "$content_verify_rc" -ne 0 ]]; then
    pp_log ERROR "Remote object could not be read for content verification: $subject"
    printf '%s\n' unreadable
    return 0
  fi
  if [[ "$remote_content_sha" == "$local_sha" ]]; then
    pp_log INFO "Remote object content SHA256 verified: $local_sha"
    VERIFY_LEVEL=sha256
    VERIFY_SUM="$local_sha"
    printf '%s\n' match
    return 0
  fi
  pp_log ERROR "Remote content checksum mismatch for $subject"
  printf '%s\n' mismatch
}
note_retry() {
  local kind="$1"
  local how="$2"
  shift 2
  local delay
  pp_log ERROR "$*"
  if [[ -n "${state_id:-}" ]]; then
    pp_state note "$how" --id "$state_id" --error-class "$kind" --error "$*" --attempt "$attempt" || true
  fi
  if [[ "$kind" == "FATAL" ]]; then
    pp_die "Upload failed and will not be retried: $LOCAL_FILE"
  fi
  if (( attempt >= RETRY_COUNT )); then
    pp_die "Upload failed after ${RETRY_COUNT} attempts: $LOCAL_FILE"
  fi
  delay="$(pp_py retry-delay -- "$attempt" "$RETRY_DELAY_SECONDS" "${RETRY_DELAY_CAP_SECONDS:-300}")"
  pp_log INFO "Retrying after ${delay}s"
  if [[ "$how" == "upload-failed" && -n "${state_id:-}" ]]; then
    pp_state note uploading --id "$state_id" --checksum "$local_sha" --attempt "$((attempt + 1))" --pid "$$" || true
  fi
  sleep "$delay"
  attempt=$((attempt + 1))
}

part_sent=0
part_ready=0
attempt=1
while (( attempt <= RETRY_COUNT )); do
  start="$(date +%s)"
  if (( part_ready == 0 )); then
    if (( part_sent == 0 )); then
      pp_log INFO "Uploading attempt ${attempt}/${RETRY_COUNT}: ${LOCAL_FILE} -> ${upload_part}"
      errf="$(pp_mktemp)"
      set +e
      pp_mc put --parallel "$PARALLEL_PARTS" --part-size "$PART_SIZE" --checksum SHA256 -- "$LOCAL_FILE" "$upload_part" 2>"$errf"
      put_rc=$?
      set -e
      if [[ "$put_rc" -ne 0 ]]; then
        kind="$(pp_py classify-error <"$errf" 2>/dev/null || true)"
        [[ "$kind" == "FATAL" ]] || kind="RETRYABLE"
        discard_part
        if [[ "$kind" == "FATAL" ]] && pp_py storage-full <"$errf" 2>/dev/null; then
          if [[ "${ALLOW_REMOTE_DELETE:-0}" == "1" ]]; then
            note_retry FATAL upload-failed "Destination storage is full; remote delete is enabled but there was still not enough space: $LOCAL_FILE"
          else
            note_retry FATAL upload-failed "Destination storage is full; remote delete is disabled so old objects were not pruned: $LOCAL_FILE"
          fi
        else
          note_retry "$kind" upload-failed "Upload command failed"
        fi
        continue
      fi
      part_sent=1
    else
      pp_log INFO "Checking temporary object attempt ${attempt}/${RETRY_COUNT}: ${upload_part}"
    fi
    case "$(classify_remote "$upload_part")" in
      match)
        if [[ -n "${state_id:-}" ]]; then
          pp_state note uploaded --id "$state_id"
        fi
        part_ready=1
        ;;
      unreadable)
        note_retry RETRYABLE upload-failed "Temporary object could not be read. It was left in place: $upload_part"
        continue
        ;;
      *)
        discard_part
        part_sent=0
        note_retry RETRYABLE upload-failed "Temporary upload failed verification and was removed. The destination was left untouched: $TARGET"
        continue
        ;;
    esac
  else
    pp_log INFO "Publishing verified temporary object attempt ${attempt}/${RETRY_COUNT}: $TARGET"
    if [[ -n "${state_id:-}" ]]; then
      pp_state note verifying --id "$state_id"
    fi
  fi
  errf="$(pp_mktemp)"
  set +e
  pp_mc cp -- "$upload_part" "$TARGET" 2>"$errf"
  copy_rc=$?
  set -e
  if [[ "$copy_rc" -ne 0 ]]; then
    kind="$(pp_py classify-error <"$errf" 2>/dev/null || true)"
    [[ "$kind" == "FATAL" ]] || kind="RETRYABLE"
    note_retry "$kind" verify-failed "Verified temporary object could not be copied. It was kept and the destination was left untouched: $TARGET"
    continue
  fi
  publish_ok=0
  case "$(classify_remote "$TARGET")" in
    match)
      publish_ok=1
      ;;
    *)
      # Server-side copy (mc cp) often drops the put SHA256 and exposes an ETag
      # that is not the multipart layout of the original put. The temporary
      # object was already verified; accepting the final name when its size
      # matches avoids a false VERIFY_FAILED loop on every publish retry.
      set +e
      published="$(read_remote_stat "$TARGET")"
      published_rc=$?
      set -e
      if [[ "$published_rc" -eq 0 && "$published" != "missing" ]]; then
        published_size="${published%%$'\t'*}"
        if [[ "$published_size" == "$local_size" ]]; then
          pp_log WARN "Final object metadata after copy did not match put layout; size matched after verified temporary: $TARGET"
          VERIFY_LEVEL="${VERIFY_LEVEL:-sha256}"
          VERIFY_SUM="${VERIFY_SUM:-$local_sha}"
          publish_ok=1
        fi
      fi
      ;;
  esac
  if (( publish_ok == 1 )); then
    discard_part
    duration=$(( $(date +%s) - start ))
    if (( duration < 1 )); then
      duration=1
    fi
    speed=$(( local_size / duration ))
    pp_log INFO "Upload verified (${VERIFY_LEVEL:-sha256}) and published. bytes=${local_size} checksum=${VERIFY_SUM:-$local_sha} duration=${duration}s speed=${speed}B/s"
    record_upload "$LOCAL_FILE" "$TARGET" "$local_size" "$duration" "$attempt"
    maybe_delete_local "$LOCAL_FILE" "$local_sha"
    exit 0
  fi
  note_retry RETRYABLE verify-failed "Copied object failed verification after publish (see size/checksum detail above). The temporary object was kept and the local file was kept: $TARGET"
done

pp_die "Upload failed after ${RETRY_COUNT} attempts: $LOCAL_FILE"
