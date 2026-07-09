#!/usr/bin/env bash
#
# Installer for the Copy_Station. Works on the Radxa Cubie A7S (Debian Bullseye)
# and on Raspberry Pi 4/5 (Raspberry Pi OS Bookworm). Idempotent.
#
#   sudo bash scripts/install.sh                                # full install
#   sudo bash scripts/install.sh my-config.yaml                 # ... with a prepared config
#   sudo bash scripts/install.sh --config-only [my-config.yaml] # only (re)install the config
#
# Without a config argument the board-matched example is used. Replacing an
# existing /etc/copystation/config.yaml is confirmed interactively and the old
# file is kept in <repo>/config.backup/ (never overwritten there).
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

CONFIG_ONLY=0
CONFIG_SRC=""
for arg in "$@"; do
  case "${arg}" in
    --config-only) CONFIG_ONLY=1 ;;
    -*)
      echo "Unknown option: ${arg}" >&2
      echo "Usage: sudo bash scripts/install.sh [--config-only] [config.yaml]" >&2
      exit 1
      ;;
    *)
      if [[ -n "${CONFIG_SRC}" ]]; then
        echo "Only one config file argument is allowed." >&2
        exit 1
      fi
      CONFIG_SRC="${arg}"
      ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

if [[ ${CONFIG_ONLY} -eq 0 ]]; then
  # Detect the Debian/Raspbian codename to pick the right exFAT package.
  CODENAME="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")"

  echo ">> Installing system dependencies ..."
  apt-get update
  # python3-pil + fonts-dejavu-core are for the optional e-paper backend (Pillow
  # renders the status frame; DejaVu gives it crisp text). Harmless if unused.
  apt-get install -y rsync python3 python3-venv python3-pyudev python3-libgpiod python3-spidev python3-yaml python3-pil fonts-dejavu-core gpiod

  # ffmpeg powers the optional video transcoding feature (also provides ffprobe).
  # Best-effort: the feature is a no-op if it is missing, so don't fail install.
  echo ">> Installing ffmpeg (optional video transcoding) ..."
  apt-get install -y ffmpeg || echo "   -> ffmpeg not installed; transcoding will be unavailable."

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
fi

echo ">> Configuration in ${CONFIG_DIR} ..."
mkdir -p "${CONFIG_DIR}"
TARGET="${CONFIG_DIR}/config.yaml"

# Pick the config source: an explicitly given file, or a board-specific example
# so the suggested GPIO pins match the real header instead of being placeholders.
if [[ -n "${CONFIG_SRC}" ]]; then
  if [[ ! -f "${CONFIG_SRC}" ]]; then
    echo "Config file not found: ${CONFIG_SRC}" >&2
    exit 1
  fi
  # A config that does not parse would make the daemon silently fall back to
  # the pure defaults -- catch that up front (best-effort, needs PyYAML).
  if python3 -c "import yaml" 2>/dev/null; then
    if ! python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "${CONFIG_SRC}"; then
      echo "ERROR: ${CONFIG_SRC} is not valid YAML -- not installing it." >&2
      exit 1
    fi
  fi
  # The old shutdown-button key is ignored by the daemon (with a startup
  # warning); catch it here where it is still cheap to fix.
  if grep -qE '^[[:space:]]*shutdown_button:' "${CONFIG_SRC}"; then
    echo "WARNING: ${CONFIG_SRC} still uses power.shutdown_button -- the daemon" >&2
    echo "         ignores that key. Migrate to buttons.userbutton_1 (README)." >&2
  fi
  SOURCE="${CONFIG_SRC}"
else
  MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
  SOURCE="${REPO_DIR}/config.example.yaml"
  if printf '%s' "${MODEL}" | grep -qi "raspberry pi"; then
    [[ -f "${REPO_DIR}/config.examples/raspberry-pi.yaml" ]] &&
      SOURCE="${REPO_DIR}/config.examples/raspberry-pi.yaml"
  elif printf '%s' "${MODEL}" | grep -qiE "cubie|radxa|a733|a7s"; then
    [[ -f "${REPO_DIR}/config.examples/cubie-a7s.yaml" ]] &&
      SOURCE="${REPO_DIR}/config.examples/cubie-a7s.yaml"
  fi
fi

CONFIG_INSTALLED=0
if [[ ! -f "${TARGET}" ]]; then
  cp "${SOURCE}" "${TARGET}"
  CONFIG_INSTALLED=1
  echo "   -> created ${TARGET} from $(basename "${SOURCE}")."
elif cmp -s "${SOURCE}" "${TARGET}"; then
  echo "   -> ${TARGET} already matches $(basename "${SOURCE}") -- unchanged."
elif [[ -n "${CONFIG_SRC}" || ${CONFIG_ONLY} -eq 1 ]]; then
  # Replacing an existing config: confirm, and keep the old file in the repo's
  # config.backup/ folder under a fresh incremental name (never overwritten).
  OVERWRITE=0
  if [[ -t 0 ]]; then
    read -r -p "   Overwrite ${TARGET} (old config is backed up)? [y/N] " ANS || ANS=""
    case "${ANS}" in
      [yY]*) OVERWRITE=1 ;;
    esac
  else
    echo "   -> no terminal to confirm overwriting; config left unchanged." >&2
  fi
  if [[ ${OVERWRITE} -eq 1 ]]; then
    BACKUP_DIR="${REPO_DIR}/config.backup"
    mkdir -p "${BACKUP_DIR}"
    N=1
    while [[ -e "${BACKUP_DIR}/config-${N}.yaml" ]]; do N=$((N + 1)); done
    BACKUP="${BACKUP_DIR}/config-${N}.yaml"
    cp "${TARGET}" "${BACKUP}"
    # The repo checkout belongs to the login user, not root -- keep it that way.
    chown --reference="${REPO_DIR}" "${BACKUP_DIR}" "${BACKUP}" 2>/dev/null || true
    echo "   -> previous config backed up to ${BACKUP}"
    cp "${SOURCE}" "${TARGET}"
    CONFIG_INSTALLED=1
    echo "   -> installed $(basename "${SOURCE}") as ${TARGET}."
  else
    echo "   -> keeping the existing ${TARGET}."
  fi
else
  echo "   -> ${TARGET} already exists, left unchanged."
fi

# Only a freshly written EXAMPLE config gets the interactive tweaks -- a
# prepared config is installed exactly as given.
if [[ ${CONFIG_INSTALLED} -eq 1 && -z "${CONFIG_SRC}" ]]; then
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
    sed -i -E "/^web:/,/^[^[:space:]]/ s/^([[:space:]]*enabled:[[:space:]]*).*/\1${WEB_ENABLED}/" "${TARGET}"
    echo "   -> web interface set to enabled=${WEB_ENABLED}."
  fi
  echo "   -> review/confirm the GPIO pins in ${TARGET} (see README)."
fi

# The WLAN access point is managed by the daemon at runtime via NetworkManager,
# so nothing is installed here -- but if it is enabled without nmcli present,
# flag it now instead of letting it silently fail to come up. NetworkManager is
# NOT auto-installed (it can clash with an existing dhcpcd/networkd setup).
if python3 -c "import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}; sys.exit(0 if (c.get('wifi_ap') or {}).get('enabled') else 1)" "${TARGET}" 2>/dev/null; then
  if command -v nmcli >/dev/null 2>&1; then
    echo "   -> wifi_ap enabled; NetworkManager present -- the daemon raises the AP on start."
  else
    echo "   -> NOTE: wifi_ap is enabled but 'nmcli' (NetworkManager) was not found." >&2
    echo "            Install NetworkManager to use the access point (see README)." >&2
  fi
fi

if [[ ${CONFIG_ONLY} -eq 0 ]]; then
  echo ">> Installing systemd service ..."
  cp "${REPO_DIR}/systemd/copystation.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable copystation.service
  systemctl restart copystation.service
elif [[ ${CONFIG_INSTALLED} -eq 1 ]]; then
  # Config-only mode installs no service -- but restart an existing one so the
  # new config takes effect.
  if systemctl list-unit-files copystation.service 2>/dev/null | grep -q '^copystation\.service'; then
    echo ">> Restarting copystation to apply the new config ..."
    systemctl restart copystation.service
  else
    echo ">> copystation.service not installed yet -- run the full install to set it up."
  fi
fi

echo ">> Done. Status:  systemctl status copystation"
echo ">> Logs:          journalctl -u copystation -f"
