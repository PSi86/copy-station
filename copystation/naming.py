"""Naming of the target folders on the SD card.

Scheme: ``transfer_<NNNN>_<source_name>`` (e.g. ``transfer_0007_DJI_O4``).

The running number is persisted on the SD card itself so the numbering continues
per card and survives reboots or a missing system clock on the Cubie. As a
robustness fallback the highest already existing ``transfer_*`` number is also
taken into account.
"""

from __future__ import annotations

import re
from pathlib import Path

# Directory on the SD card where the counter is stored.
STATE_DIRNAME = ".copystation"
COUNTER_FILENAME = "counter"

# Matches our own target folders and extracts the running number.
_TRANSFER_RE = re.compile(r"^transfer_(\d+)_")

# Characters that are safe in folder names. Everything else is replaced.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Number of digits the running number is padded to.
_PAD = 4


def sanitize_name(raw: str | None) -> str:
    """Turn a plain-text device name into a filesystem-safe token.

    Empty/None input yields ``unknown``.
    """
    if not raw:
        return "unknown"
    cleaned = _SAFE_CHARS.sub("_", raw.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "unknown"


def _read_counter(state_dir: Path) -> int:
    counter_file = state_dir / COUNTER_FILENAME
    try:
        return int(counter_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_counter(state_dir: Path, value: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / COUNTER_FILENAME).write_text(f"{value}\n", encoding="utf-8")


def _highest_existing(target_root: Path) -> int:
    """Highest already used transfer_ number in the target root directory."""
    highest = 0
    if not target_root.is_dir():
        return 0
    for entry in target_root.iterdir():
        if not entry.is_dir():
            continue
        match = _TRANSFER_RE.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def next_transfer_dir(target_root: Path, source_name: str | None) -> Path:
    """Path of the next, guaranteed non-existing target folder.

    Does NOT create the folder -- that is left to the caller (or rsync). The
    persisted counter is written up to the assigned number so that no number is
    handed out twice even if the run is aborted.
    """
    target_root = Path(target_root)
    state_dir = target_root / STATE_DIRNAME

    # Start = max(persisted counter, highest existing folder number).
    number = max(_read_counter(state_dir), _highest_existing(target_root))

    name_token = sanitize_name(source_name)

    # Count up collision-safe until a free folder name is found.
    while True:
        number += 1
        candidate = target_root / f"transfer_{number:0{_PAD}d}_{name_token}"
        if not candidate.exists():
            _write_counter(state_dir, number)
            return candidate
