#!/usr/bin/env bash
#
# Installer for the Copy_Station on the Radxa Cubie A7S (Bullseye CLI).
# Idempotent: can be run multiple times.
#
#   sudo ./scripts/install.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/copystation"
CONFIG_DIR="/etc/copystation"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

echo ">> Installing system dependencies ..."
apt-get update
apt-get install -y rsync python3 python3-pip python3-pyudev python3-libgpiod python3-yaml

# Web interface (FastAPI/uvicorn) -- only required if the web UI is enabled,
# but installed unconditionally so toggling web.enabled needs no extra steps.
echo ">> Installing web interface dependencies (pip) ..."
pip3 install --upgrade "fastapi>=0.100" "uvicorn>=0.20"

echo ">> Copying code to ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"
cp -r "${REPO_DIR}/copystation" "${INSTALL_DIR}/"

echo ">> Configuration in ${CONFIG_DIR} ..."
mkdir -p "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  cp "${REPO_DIR}/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  echo "   -> created ${CONFIG_DIR}/config.yaml (please enter GPIO pins)."
else
  echo "   -> ${CONFIG_DIR}/config.yaml already exists, left unchanged."
fi

echo ">> Installing systemd service ..."
cp "${REPO_DIR}/systemd/copystation.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable copystation.service
systemctl restart copystation.service

echo ">> Done. Status:  systemctl status copystation"
echo ">> Logs:          journalctl -u copystation -f"
