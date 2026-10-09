#!/bin/bash
set -euo pipefail
IFS=$' \t\n'
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$ROOT/config/parspack.conf"
EXAMPLE="$ROOT/config/parspack.conf.example"
TRANSPORT_SRC="$ROOT/transport/parspack_transport.pl"
TRANSPORT_DST="/usr/local/cpanel/scripts/parspack_custom_transport.pl"
APPJSON="$ROOT/whm/parspack_backup.conf"
WHM_UI_SRC="$ROOT/whm/index.php"
WHM_UI_DST_DIR="/usr/local/cpanel/whostmgr/docroot/cgi/parspack_backup"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }
[[ "$ROOT" == "/root/parspack-backup" ]] || {
  echo "Install this project at /root/parspack-backup. The transport path is fixed." >&2
  exit 1
}
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
chmod 600 "$ROOT"/lib/*
chown -R root:root "$ROOT/config" "$ROOT/mc-config" "$ROOT/bin" "$ROOT/lib" \
  "$ROOT/transport" "$ROOT/logs" "$ROOT/state" "$ROOT/reports" "$ROOT/run" "$ROOT/tmp"

"$ROOT/bin/parspackctl" doctor

if [[ -f "$TRANSPORT_DST" ]]; then
  cp -a "$TRANSPORT_DST" "$TRANSPORT_DST.bak.$(date +%Y%m%d%H%M%S)"
fi
cp -a "$TRANSPORT_SRC" "$TRANSPORT_DST"
chown root:root "$TRANSPORT_DST"
chmod 700 "$TRANSPORT_DST"
perl -c "$TRANSPORT_DST"

mkdir -p "$WHM_UI_DST_DIR"
cp "$WHM_UI_SRC" "$WHM_UI_DST_DIR/index.php"
chmod 755 "$WHM_UI_DST_DIR"
chmod 644 "$WHM_UI_DST_DIR/index.php"

if [[ -x /usr/local/cpanel/bin/register_appconfig ]]; then
  /usr/local/cpanel/bin/register_appconfig "$APPJSON"
fi

echo "Installed PARSPACK Backup."
echo "Project root: $ROOT"
echo "Transport: $TRANSPORT_DST"
echo "WHM UI: $WHM_UI_DST_DIR/index.php"
echo "Set TRANSPORT_PASSWORD in $CONF and use that same password in WHM."
echo "Next: $ROOT/bin/parspackctl doctor && $ROOT/bin/parspackctl connection-test"
