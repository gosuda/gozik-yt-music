#!/bin/bash
set -euo pipefail

UNIT_NAME="gozik-ytmusic.service"
LEGACY_UNIT_NAME="gozik-yt-music-server.service"
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT_NAME}"
LEGACY_UNIT_PATH="${HOME}/.config/systemd/user/${LEGACY_UNIT_NAME}"

env -u LD_LIBRARY_PATH -u LD_PRELOAD systemctl --user disable --now "${UNIT_NAME}" "${LEGACY_UNIT_NAME}" >/dev/null 2>&1 || true
rm -f "${UNIT_PATH}" "${LEGACY_UNIT_PATH}"
env -u LD_LIBRARY_PATH -u LD_PRELOAD systemctl --user daemon-reload >/dev/null 2>&1 || true
echo "gozik YouTube Music daemon unregistered."
