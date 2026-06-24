#!/usr/bin/env bash
#
# Uninstaller for the Copy_Station -- reverses scripts/install.sh.
#
#   sudo bash scripts/uninstall.sh            # remove service + code, KEEP config
#   sudo bash scripts/uninstall.sh --purge    # also remove /etc/copystation
#
# Use "bash" explicitly (a ZIP download drops the executable bit). System apt
# packages pulled in by the installer are intentionally left in place.
#
set -euo pipefail

INSTALL_DIR="/opt/copystation"
CONFIG_DIR="/etc/copystation"
SERVICE="copystation.service"
SERVICE_FILE="/etc/systemd/system/${SERVICE}"
RUN_DIR="/run/copystation"

PURGE=0
for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE=1 ;;
    -h|--help)
      echo "Usage: sudo bash scripts/uninstall.sh [--purge]"
      echo "  --purge   also delete the configuration in ${CONFIG_DIR}"
      exit 0
      ;;
    *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

echo ">> Stopping and disabling the service ..."
systemctl stop "${SERVICE}" 2>/dev/null || true
systemctl disable "${SERVICE}" 2>/dev/null || true

echo ">> Removing the systemd unit ..."
rm -f "${SERVICE_FILE}"
systemctl daemon-reload
systemctl reset-failed "${SERVICE}" 2>/dev/null || true

# Best effort: unmount anything the daemon may have left mounted, then drop the
# runtime dir (it lives on tmpfs and is recreated on the next start anyway).
echo ">> Unmounting leftover mountpoints under ${RUN_DIR} ..."
if [[ -d "${RUN_DIR}/mnt" ]]; then
  for mp in "${RUN_DIR}/mnt"/*; do
    [[ -d "${mp}" ]] || continue
    if mountpoint -q "${mp}"; then
      umount "${mp}" 2>/dev/null || true
    fi
  done
fi
rm -rf "${RUN_DIR}"

echo ">> Removing the code at ${INSTALL_DIR} ..."
rm -rf "${INSTALL_DIR}"

if [[ "${PURGE}" -eq 1 ]]; then
  echo ">> Purging configuration at ${CONFIG_DIR} ..."
  rm -rf "${CONFIG_DIR}"
elif [[ -d "${CONFIG_DIR}" ]]; then
  echo ">> Keeping configuration at ${CONFIG_DIR} (re-run with --purge to remove it)."
fi

echo ">> Done. The service and ${INSTALL_DIR} are removed."
echo "   apt packages from the installer (rsync, python3-pyudev, python3-libgpiod,"
echo "   gpiod, exFAT tools) were left in place -- remove them manually if nothing"
echo "   else needs them."
