#!/usr/bin/env bash
#
# Installer for the Copy_Station. Works on the Radxa Cubie A7S (Debian Bullseye)
# and on Raspberry Pi 4/5 (Raspberry Pi OS Bookworm). Idempotent.
#
#   sudo bash scripts/install.sh
#
# Use "bash" explicitly: a GitHub ZIP download drops the executable bit, and
# "sudo ./scripts/install.sh" on a non-executable file fails with the misleading
# "command not found". Invoking via bash works regardless of file permissions.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/copystation"
CONFIG_DIR="/etc/copystation"
VENV_DIR="${INSTALL_DIR}/venv"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

# Detect the Debian/Raspbian codename to pick the right exFAT package.
CODENAME="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")"

echo ">> Installing system dependencies ..."
apt-get update
# python3-pil + fonts-dejavu-core are for the optional e-paper backend (Pillow
# renders the status frame; DejaVu gives it crisp text). Harmless if unused.
apt-get install -y rsync python3 python3-venv python3-pyudev python3-libgpiod python3-spidev python3-yaml python3-pil fonts-dejavu-core gpiod

# exFAT support so camera/SD cards mount (package name differs by release:
# Bullseye = exfat-fuse + exfat-utils; Bookworm/Trixie = exfatprogs).
echo ">> Installing exFAT support ..."
if [[ "${CODENAME}" == "bullseye" ]]; then
  apt-get install -y exfat-fuse exfat-utils || true
else
  apt-get install -y exfatprogs || apt-get install -y exfat-fuse || true
fi

echo ">> Copying code to ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"
cp -r "${REPO_DIR}/copystation" "${INSTALL_DIR}/"

# Web interface (FastAPI/uvicorn) in a venv with access to the system packages
# (pyudev/libgpiod). This is PEP 668-safe, so it works on Bookworm where a plain
# "pip install" into the system Python is blocked.
echo ">> Setting up Python venv and web dependencies ..."
python3 -m venv --system-site-packages "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install "fastapi>=0.100" "uvicorn>=0.20"

echo ">> Configuration in ${CONFIG_DIR} ..."
mkdir -p "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  # Pick a board-specific example as the starting point, so the suggested GPIO
  # pins match the real header instead of being empty placeholders.
  MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
  EXAMPLE="${REPO_DIR}/config.example.yaml"
  if printf '%s' "${MODEL}" | grep -qi "raspberry pi"; then
    [[ -f "${REPO_DIR}/config.examples/raspberry-pi.yaml" ]] &&
      EXAMPLE="${REPO_DIR}/config.examples/raspberry-pi.yaml"
  elif printf '%s' "${MODEL}" | grep -qiE "cubie|radxa|a733|a7s"; then
    [[ -f "${REPO_DIR}/config.examples/cubie-a7s.yaml" ]] &&
      EXAMPLE="${REPO_DIR}/config.examples/cubie-a7s.yaml"
  fi
  cp "${EXAMPLE}" "${CONFIG_DIR}/config.yaml"
  echo "   -> created ${CONFIG_DIR}/config.yaml from $(basename "${EXAMPLE}")"
  echo "      (detected board: ${MODEL:-unknown})."

  # Ask whether to enable the local web interface (default: yes). Skip the
  # prompt when there is no terminal (e.g. a piped install) and keep the
  # example's value in that case.
  if [[ -t 0 ]]; then
    read -r -p "   Enable the local web interface on http://<device-ip>:8080/ ? [Y/n] " WEB_ANS || WEB_ANS=""
    case "${WEB_ANS}" in
      [nN]*) WEB_ENABLED="false" ;;
      *)     WEB_ENABLED="true" ;;
    esac
    # Flip ONLY web.enabled. The config also has buttons.userbutton_1.enabled,
    # so the substitution must stay inside the web: section (from `web:` to the
    # next top-level key/comment) -- a file-wide sed would toggle the user
    # button on too, which then warns about a missing 'line'.
    sed -i -E "/^web:/,/^[^[:space:]]/ s/^([[:space:]]*enabled:[[:space:]]*).*/\1${WEB_ENABLED}/" "${CONFIG_DIR}/config.yaml"
    echo "   -> web interface set to enabled=${WEB_ENABLED}."
  fi
  echo "   -> review/confirm the GPIO pins in ${CONFIG_DIR}/config.yaml (see README)."
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
