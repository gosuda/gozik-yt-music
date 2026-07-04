#!/bin/bash
set -euo pipefail

UNIT_NAME="gozik-ytmusic.service"
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT_NAME}"

systemctl --user disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
rm -f "${UNIT_PATH}"
systemctl --user daemon-reload >/dev/null 2>&1 || true
echo "gozik YouTube Music daemon unregistered."
