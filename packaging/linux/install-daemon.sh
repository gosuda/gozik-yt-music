#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTALL_DIR="${HOME}/.local/share/gozik/ytmusic"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_NAME="gozik-ytmusic.service"
LEGACY_UNIT_NAME="gozik-yt-music-server.service"

mkdir -p "${INSTALL_DIR}" "${UNIT_DIR}"
cp -a "${ROOT_DIR}/gozik-yt-music-server" "${INSTALL_DIR}/gozik-yt-music-server"
chmod -R +x "${INSTALL_DIR}/gozik-yt-music-server"
sed "s#%h/.local/share/gozik/ytmusic/gozik-yt-music-server/gozik-yt-music-server#${INSTALL_DIR}/gozik-yt-music-server/gozik-yt-music-server#g" \
  "${SCRIPT_DIR}/${UNIT_NAME}" > "${UNIT_DIR}/${UNIT_NAME}"

systemctl_user() {
  env -u LD_LIBRARY_PATH -u LD_PRELOAD systemctl --user "$@"
}

systemctl_user disable --now "${LEGACY_UNIT_NAME}" >/dev/null 2>&1 || true
systemctl_user daemon-reload
systemctl_user enable "${UNIT_NAME}"
systemctl_user restart "${UNIT_NAME}"
echo "gozik YouTube Music daemon registered and started."
