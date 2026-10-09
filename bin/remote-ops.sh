#!/bin/bash
# cPanel transport commands other than put: ls, mkdir, chdir, get, delete, rmdir.
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/parspack-common.sh"

usage() {
  echo "Usage: $0 {ls|mkdir|chdir|get|delete|delete-validation|rmdir} [args...]" >&2
}

pp_bootstrap
pp_prepare_mc

cmd="${1:-}"
[[ -n "$cmd" ]] || { usage; exit 2; }
shift

case "$cmd" in
  mkdir)
    rel="${1:-}"
    if [[ -z "$rel" || "$rel" == "/" ]]; then
      exit 0
    fi
    target="$(pp_py object-key --allow-empty -- "$rel")"
    marker="$(pp_mktemp)"
    pp_mc put -- "$marker" "${target}/.keep" >/dev/null
    ;;

  chdir)
    pp_py object-key --allow-empty -- "${1:-}" >/dev/null
    ;;

  ls)
    rel="${1:-}"
    target="$(pp_py object-key --allow-empty -- "$rel")"
    errf="$(pp_mktemp)"
    set +e
    listing="$(pp_mc ls --json -- "${target}/" 2>"$errf")"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
      if pp_py stat-missing -- "$errf"; then
        exit 0
      fi
      pp_die "Remote listing failed"
    fi
    if [[ -z "$listing" && -n "$rel" && "$rel" != "/" ]]; then
      set +e
      listing="$(pp_mc ls --json -- "$target" 2>"$errf")"
      status=$?
      set -e
      if [[ "$status" -ne 0 ]]; then
        if pp_py stat-missing -- "$errf"; then
          exit 0
        fi
        pp_die "Remote listing failed"
      fi
    fi
    printf '%s\n' "$listing" | pp_py format-ls
    ;;

  get)
    remote_rel="${1:?remote path required}"
    local_file="${2:?local file required}"
    src="$(pp_py object-key --require-child -- "$remote_rel")"
    destination="$(pp_py check-local write -- "$local_file")"
    partial="$(pp_py check-local write -- "${destination}.parspack-partial")"
    cleanup_partial() { [[ -n "${partial:-}" ]] && rm -f -- "$partial"; }
    trap cleanup_partial EXIT
    if ! pp_mc cp -- "$src" "$partial"; then
      cleanup_partial
      pp_die "Download failed; the partial file was removed and the destination was left unchanged"
    fi
    got_size="$(stat -c '%s' -- "$partial")"
    got_sha="$(sha256sum -- "$partial" | awk '{print $1}')"
    errf="$(pp_mktemp)"
    set +e
    stat_json="$(pp_mc stat --json -- "$src" 2>"$errf")"
    stat_rc=$?
    set -e
    outf="$(pp_mktemp)"
    printf '%s\n' "$stat_json" > "$outf"
    if [[ "$stat_rc" -ne 0 ]] || pp_py stat-missing -- "$errf" || pp_py stat-missing -- "$outf"; then
      cleanup_partial
      pp_die "Downloaded object could not be checked; the partial file was removed"
    fi
    fields="$(printf '%s\n' "$stat_json" | pp_py stat-fields -- "$src")"
    # stat-fields is size, sha256, etag. The etag must not be glued onto the hash.
    remote_size="${fields%%$'\t'*}"
    rest="${fields#*$'\t'}"
    remote_sha="${rest%%$'\t'*}"
    remote_etag=""
    if [[ "$rest" == *$'\t'* ]]; then
      remote_etag="${rest#*$'\t'}"
    fi
    if [[ "$got_size" != "$remote_size" || ! "$got_sha" =~ ^[0-9a-fA-F]{64}$ ]]; then
      cleanup_partial
      pp_die "Downloaded file does not match the remote size; the destination was left unchanged"
    fi
    if [[ "$remote_sha" == "-" && -n "$remote_etag" ]]; then
      integrity="$(pp_py verify-decision --local-size "$got_size" --remote-size "$remote_size" --local-sha "$got_sha" --remote-sha "-" --etag "$remote_etag" --part-size "$PART_SIZE" --file "$partial")"
      case "$integrity" in
        "ok etag")
          remote_sha="$got_sha"
          ;;
        mismatch)
          cleanup_partial
          pp_die "Downloaded file checksum does not match the remote object; the destination was left unchanged"
          ;;
      esac
    fi
    if [[ "$remote_sha" == "-" ]]; then
      result="$(pp_mktemp)"
      set +e
      pp_mc cat -- "$src" | sha256sum >"$result"
      content_rc=$?
      set -e
      remote_sha="$(awk '{print $1}' "$result")"
      rm -f -- "$result"
      if [[ "$content_rc" -ne 0 || ! "$remote_sha" =~ ^[0-9a-fA-F]{64}$ ]]; then
        cleanup_partial
        pp_die "Could not hash the remote object; the partial file was removed"
      fi
    fi
    if [[ "${remote_sha,,}" != "${got_sha,,}" ]]; then
      cleanup_partial
      pp_die "Downloaded file checksum does not match the remote object; the destination was left unchanged"
    fi
    mv -f -- "$partial" "$destination"
    partial=""
    trap - EXIT
    pp_py check-local write -- "$destination" >/dev/null
    pp_py tighten-file -- "$destination"
    ;;

  delete-validation)
    remote_path="${1:?path required}"
    # The whole path must be exactly validate.tmp-<digits>-<digits>.txt.
    # A slash, a different prefix, or validate.tmp-foo-foo.txt is rejected.
    if pp_py validation-name -- "$remote_path"; then
      target="$(pp_py object-key --require-child -- "$remote_path")"
      pp_mc rm --force -- "$target"
    else
      pp_log ERROR "invalid cPanel validation filename"
      exit 1
    fi
    ;;

  delete)
    remote_path="${1:?path required}"
    if pp_py validation-name -- "$remote_path"; then
      target="$(pp_py object-key --require-child -- "$remote_path")"
      pp_mc rm --force -- "$target"
    else
      pp_assert_remote_delete_allowed
      pp_py retention-check delete "$remote_path"
      target="$(pp_py object-key --require-child -- "$remote_path")"
      pp_mc rm --force -- "$target"
      printf '%s\n' "$remote_path" | pp_py retention-log -- "$remote_path"
    fi
    ;;

  rmdir)
    pp_assert_remote_delete_allowed
    rel="${1:?path required}"
    pp_py retention-check rmdir "$rel"
    target="$(pp_py object-key --require-child -- "$rel")"
    case "$target" in
      */) pp_die "Refusing an ambiguous remote prefix" ;;
    esac
    errf="$(pp_mktemp)"
    set +e
    listing="$(pp_mc ls --recursive --json -- "${target}/" 2>"$errf")"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
      pp_die "refused: remote listing failed"
    fi
    keys="$(printf '%s\n' "$listing" | pp_py retention-confirm -- "$rel")"
    # The trailing slash is the prefix boundary: foo/ must not delete foobar/.
    pp_mc rm --recursive --force -- "${target}/"
    printf '%s\n' "$keys" | pp_py retention-log -- "$rel"
    ;;

  *)
    usage
    exit 2
    ;;
esac
