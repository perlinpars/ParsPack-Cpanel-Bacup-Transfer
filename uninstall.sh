#!/bin/bash
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
umask 077

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/whm/parspack_backup.conf"

if [[ -x /usr/local/cpanel/bin/unregister_appconfig && -f "$APP" ]]; then
  /usr/local/cpanel/bin/unregister_appconfig "$APP" || true
fi
rm -f -- /usr/local/cpanel/scripts/parspack_custom_transport.pl
rm -rf -- /usr/local/cpanel/whostmgr/docroot/cgi/parspack_backup

wipe_file() {
  local file="$1"
  [[ -e "$file" || -L "$file" ]] || return 0
  if [[ -L "$file" ]]; then
    rm -f -- "$file"
    return 0
  fi
  if [[ -f "$file" ]] && command -v shred >/dev/null 2>&1; then
    shred -u -n 1 -- "$file" || rm -f -- "$file"
    return 0
  fi
  rm -f -- "$file"
}

wipe_file "$ROOT/mc-config/config.json"
rm -rf -- "$ROOT/mc-config"

if [[ "${1:-}" == "--purge" ]]; then
  printf '%s' "Permanently delete config, logs, state, and reports in $ROOT? Type yes: "
  read -r answer
  if [[ "$answer" == "yes" ]]; then
    wipe_file "$ROOT/config/parspack.conf"
    rm -rf -- "$ROOT/logs" "$ROOT/state" "$ROOT/reports" "$ROOT/run" "$ROOT/tmp"
    echo "Purged credentials, logs, state, and reports."
  else
    echo "Purge cancelled. config/parspack.conf, logs, state, and reports were kept."
  fi
else
  echo "Transport and WHM UI removed."
  echo "Generated S3 client credentials were removed from mc-config."
  echo "config/parspack.conf was kept and still contains secrets. Remove it when you no longer need the settings."
  echo "Logs, reports, and state under $ROOT were kept. Use --purge to delete them."
fi
