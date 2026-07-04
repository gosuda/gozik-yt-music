#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BINARY="${ROOT_DIR}/gozik-yt-music-server"
PLIST_TEMPLATE="${SCRIPT_DIR}/com.gosuda.gozik.ytmusic.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.gosuda.gozik.ytmusic.plist"

if [[ ! -x "${BINARY}" ]]; then
  echo "Missing executable: ${BINARY}" >&2
  exit 1
fi

mkdir -p "$(dirname "${PLIST_DST}")"
sed \
  -e "s#__BINARY_PATH__#${BINARY}#g" \
  -e "s#__BUNDLE_DIR__#${ROOT_DIR}#g" \
  "${PLIST_TEMPLATE}" > "${PLIST_DST}"

launchctl unload "${PLIST_DST}" >/dev/null 2>&1 || true
launchctl load -w "${PLIST_DST}"
echo "gozik YouTube Music daemon registered and started."
