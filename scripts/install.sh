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

  # On the Allwinner A733 (Cubie A7S) the hardware H.264 encoder + H.264/H.265
  # decoders are GStreamer OpenMAX elements (omxh264videoenc / omxh264dec /
  # omxhevcvideodec). The vendor OMX plugin already ships in the Radxa image, so
  # we do NOT install gstreamer1.0-omx (that would pull a conflicting generic
  # one); we only add the standard tools + plugins our pipeline needs
  # (gst-launch/gst-inspect, qtdemux/mp4mux/matroskademux/aacparse from -good,
  # h264parse/h265parse from -bad). Cubie-only and best-effort -- the Pi uses
  # ffmpeg, and a missing element just makes the transcoder fall back to the CPU.
  # "sun" is in the pattern because that is what the board actually reports: an
  # A733 Cubie A7S has model "sun60iw2" (the Allwinner sunxi SoC name), not any
  # marketing name -- without it this whole branch is skipped on the very board
  # it exists for, and hardware transcoding silently falls back to the CPU. Keep
  # the list in step with copystation/encoders.py:detect_board().
  MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
  if printf '%s' "${MODEL}" | grep -qiE "cubie|radxa|a733|a7s|allwinner|sun"; then
    echo ">> Installing GStreamer for A733 hardware transcoding ..."
    apt-get install -y gstreamer1.0-tools gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      || echo "   -> GStreamer incomplete; hardware transcoding will fall back to the CPU."
  fi

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
  # Make `copystation` importable for the venv interpreter itself. The systemd
  # unit sets PYTHONPATH, but a shell does not -- and the CLI we point people at
  # (`venv/bin/python -m copystation.daemon ... wifi-ap on`) is run from a shell,
  # where it would otherwise fail with ModuleNotFoundError.
  PURELIB="$("${VENV_DIR}/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  echo "${INSTALL_DIR}" > "${PURELIB}/copystation.pth"
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
  elif printf '%s' "${MODEL}" | grep -qiE "cubie|radxa|a733|a7s|allwinner|sun"; then
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
    # Two network stacks fighting over the same interface is the classic reason
    # "the LAN/SSH dies when the AP comes up" -- name it here, before it happens.
    for other in dhcpcd systemd-networkd; do
      if systemctl is-active --quiet "${other}" 2>/dev/null; then
        echo "   -> WARNING: ${other} is running alongside NetworkManager. Both manage" >&2
        echo "               the same interfaces; raising the AP can then knock out the" >&2
        echo "               wired connection (SSH included). See the README section" >&2
        echo "               'Running the AP while the station is on a LAN'." >&2
      fi
    done
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

# How to actually reach the station -- the values one would otherwise have to dig
# out of the YAML on first setup. The WLAN password is only echoed while it is
# still the shipped default (which is public anyway); a password the user chose
# stays out of the terminal scrollback and the install log.
python3 - "${TARGET}" "${VENV_DIR}/bin/python" <<'PY' || true
import sys

import yaml

SHIPPED_PASSWORD = "copystation"  # the default in the example configs

config_path, python_bin = sys.argv[1], sys.argv[2]
cfg = yaml.safe_load(open(config_path, encoding="utf-8")) or {}
ap = cfg.get("wifi_ap") or {}
web = cfg.get("web") or {}
auth = web.get("auth") or {}
port = web.get("port", 8080)


def shown(value):
    """Echo the shipped default; keep a self-chosen password to yourself."""
    return repr(value) if value == SHIPPED_PASSWORD else "<as configured in the config file>"


if web.get("enabled"):
    print(f">> Web interface:  http://<device-ip>:{port}/")
    if auth.get("enabled"):
        print(f">>   login:        {auth.get('username', 'admin')} / {shown(auth.get('password', ''))}")
    else:
        print(">>   no login required (set web.auth.enabled: true to add one)")

password = str(ap.get("password") or "")
if password:
    ip = str(ap.get("ipv4_address", "10.42.0.1/24")).split("/")[0]
    state = "raised on start" if ap.get("enabled") else "off until switched on"
    print(f">> WiFi AP:        SSID {ap.get('ssid', 'Copy_Station')!r} / {shown(password)} ({state})")
    if web.get("enabled"):
        print(f">>   over the AP:  http://{ip}:{port}/"
              + ("  (opens by itself when you join)" if ap.get("captive_portal") else ""))
    print(">>   switch it:    from the web interface, a user button, or the shell:")
    print(f">>                 sudo {python_bin} -m copystation.daemon "
          f"--config {config_path} wifi-ap on|off")
PY

echo ">> Done. Status:  systemctl status copystation"
echo ">> Logs:          journalctl -u copystation -f"
