#!/usr/bin/env bash
#
# Install (or replace) the Copy_Station configuration only -- no packages, no
# code, no service files. Companion to install.sh for pushing a prepared
# config.yaml onto a device. Idempotent.
#
#   sudo bash scripts/install-config.sh [my-config.yaml]
#
# With a file argument, that file is installed to /etc/copystation/config.yaml.
# Without one, a board-specific example is picked (same detection as
# install.sh) -- useful for resetting to a known-good starting point.
# An existing config is kept as a timestamped .bak next to it.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="/etc/copystation"
TARGET="${CONFIG_DIR}/config.yaml"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

# ----- pick the source file --------------------------------------------------
if [[ $# -gt 1 ]]; then
  echo "Usage: sudo bash scripts/install-config.sh [config.yaml]" >&2
  exit 1
fi

if [[ $# -eq 1 ]]; then
  SOURCE="$1"
  if [[ ! -f "${SOURCE}" ]]; then
    echo "Config file not found: ${SOURCE}" >&2
    exit 1
  fi
else
  # No file given: pick a board-specific example as the starting point, so the
  # suggested GPIO pins match the real header instead of being placeholders.
  MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
  SOURCE="${REPO_DIR}/config.example.yaml"
  if printf '%s' "${MODEL}" | grep -qi "raspberry pi"; then
    [[ -f "${REPO_DIR}/config.examples/raspberry-pi.yaml" ]] &&
      SOURCE="${REPO_DIR}/config.examples/raspberry-pi.yaml"
  elif printf '%s' "${MODEL}" | grep -qiE "cubie|radxa|a733|a7s"; then
    [[ -f "${REPO_DIR}/config.examples/cubie-a7s.yaml" ]] &&
      SOURCE="${REPO_DIR}/config.examples/cubie-a7s.yaml"
  fi
  echo ">> No config given -- using $(basename "${SOURCE}") (detected board: ${MODEL:-unknown})."
fi

# ----- sanity checks before touching the live config -------------------------
# Parse check (best-effort: only if python3 + PyYAML are around, which
# install.sh sets up). A config that does not parse would make the daemon
# fall back to pure defaults, which is easy to miss on a headless box.
if command -v python3 >/dev/null 2>&1; then
  if ! python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "${SOURCE}" 2>/dev/null; then
    if python3 -c "import yaml" 2>/dev/null; then
      echo "ERROR: ${SOURCE} is not valid YAML -- not installing it." >&2
      exit 1
    fi
    echo "   -> PyYAML not available, skipping the parse check."
  fi
fi

# The old shutdown-button key is ignored by the daemon (with a startup
# warning); catch it here where it is still cheap to fix.
if grep -qE '^\s*shutdown_button:' "${SOURCE}"; then
  echo "WARNING: ${SOURCE} still uses power.shutdown_button -- the daemon will" >&2
  echo "         ignore it. Migrate to buttons.userbutton_1 (see README)." >&2
fi

# ----- install ----------------------------------------------------------------
mkdir -p "${CONFIG_DIR}"
if [[ -f "${TARGET}" ]]; then
  if cmp -s "${SOURCE}" "${TARGET}"; then
    echo ">> ${TARGET} already matches $(basename "${SOURCE}") -- nothing to do."
    exit 0
  fi
  BACKUP="${TARGET}.bak-$(date +%Y%m%d-%H%M%S)"
  cp "${TARGET}" "${BACKUP}"
  echo ">> Existing config backed up to ${BACKUP}."
fi

install -m 0644 -o root -g root "${SOURCE}" "${TARGET}"
echo ">> Installed $(basename "${SOURCE}") as ${TARGET}."

# ----- apply ------------------------------------------------------------------
# Restart only if the service is actually installed; this script may run before
# install.sh on a fresh box.
if systemctl list-unit-files copystation.service >/dev/null 2>&1 \
   && systemctl list-unit-files copystation.service | grep -q '^copystation.service'; then
  echo ">> Restarting copystation to apply the new config ..."
  systemctl restart copystation.service
  echo ">> Done. Status:  systemctl status copystation"
  echo ">> Logs:          journalctl -u copystation -f"
else
  echo ">> copystation.service not installed yet -- config will be picked up"
  echo "   by the first start (run scripts/install.sh for the full setup)."
fi
