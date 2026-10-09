#!/bin/bash
# Phase 0 skeleton. This is not the cPanel transport and install.sh does not
# copy it. It invokes transport/parspack_transport.pl the way the transport
# page describes: command, local directory, command args, then host and user.
# Password is the environment variable PASSWORD, never an argv element.
#
# Docs: https://docs.cpanel.net/knowledge-base/backup/how-to-create-a-custom-transport-script-for-backups/
# Required argument order in that page's skeleton, not the summary table.
#
# Usage, from a disposable root-owned tree (see tests/run-tests.sh):
#   PARSPACK_TEST_ROOT=/path/to/proj PASSWORD='transport-pass' \
#     tests/cpanel-invoke.sh put /backup/2026-10-08 \
#     /backup/2026-10-08/accounts/user.tar.gz accounts/user.tar.gz \
#     backup.example root
#
# The existing offline bucket is tests/fake-mc.py. Point MC_BIN at it inside
# the disposable config. Do not point this at the live /root/parspack-backup
# config.
set -euo pipefail
IFS=$' \t\n'

if [[ $# -lt 2 || -z "${PARSPACK_TEST_ROOT:-}" ]]; then
  echo "usage: PARSPACK_TEST_ROOT=DIR PASSWORD=... $0 COMMAND LOCAL_DIR ARGS..." >&2
  exit 2
fi

if [[ "${PARSPACK_TEST_ROOT}" == "/root/parspack-backup" ]]; then
  echo "refusing to invoke against the live install" >&2
  exit 2
fi

transport="${PARSPACK_TEST_ROOT}/transport/parspack_transport.pl"
if [[ ! -f "$transport" ]]; then
  echo "transport not found under PARSPACK_TEST_ROOT" >&2
  exit 2
fi

exec perl "$transport" "$@"
