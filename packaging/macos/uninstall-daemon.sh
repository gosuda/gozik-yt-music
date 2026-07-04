#!/bin/bash
set -euo pipefail

PLIST_DST="${HOME}/Library/LaunchAgents/com.gosuda.gozik.ytmusic.plist"
launchctl unload "${PLIST_DST}" >/dev/null 2>&1 || true
rm -f "${PLIST_DST}"
echo "gozik YouTube Music daemon unregistered."
