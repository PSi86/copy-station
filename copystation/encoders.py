"""Board-aware video encoder selection for transcoding.

The three target boards differ a lot in what they can encode in hardware:

* **Raspberry Pi 4** -- has an H.264 hardware encoder exposed by ffmpeg as
  ``h264_v4l2m2m`` (V4L2 mem2mem, ``/dev/video11``). No HEVC *encode*.
* **Raspberry Pi 5** -- the H.264 encoder block was **removed**; the Pi 5 has no
  hardware video encoder at all (HEVC decode only). So it must encode on the CPU.
* **Radxa Cubie A7S (Allwinner A733)** -- the Cedar VPU can encode H.264 (and,
  depending on the ffmpeg build, HEVC) via ``h264_v4l2m2m`` / ``hevc_v4l2m2m``.

This module maps ``(board, codec-family) -> preferred hardware encoder``, probes
which encoders the installed ffmpeg actually has, and produces an **ordered list
of candidates** to try (hardware first, then software) so the transcoder can
fall back automatically when a hardware encode fails at runtime.

Everything here is pure/deterministic given its inputs (board string, the set of
available encoder names), so it is fully unit-testable without any hardware.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Set

_LOG = logging.getLogger("copystation.encoders")


@dataclass(frozen=True)
class Encoder:
    """One concrete way to encode: an ffmpeg ``-c:v`` codec plus how to drive it."""

    name: str                        # shown in the UI / logs (e.g. "cpu", "h264_v4l2m2m")
    codec: str                       # ffmpeg -c:v value
    kind: str                        # "hw" | "sw"
    rate_mode: str                   # "crf" (software) | "bitrate" (V4L2 M2M hardware)
    format_filter: Optional[str] = None   # appended to -vf (e.g. "format=yuv420p")
    extra_args: tuple = field(default_factory=tuple)

    @property
    def is_hardware(self) -> bool:
        return self.kind == "hw"


# board -> codec family -> preferred hardware encoder (None = no HW encode).
# Only well-known boards get a hardware default; an unknown board stays on the
# CPU unless the user forces an encoder via `transcode.acceleration`.
_HW_ENCODERS = {
    "pi4": {"h264": "h264_v4l2m2m", "hevc": None},
    "pi": {"h264": "h264_v4l2m2m", "hevc": None},   # other/older Pi with the encoder
    "pi5": {"h264": None, "hevc": None},            # Pi 5 dropped the HW encoder
    "cubie": {"h264": "h264_v4l2m2m", "hevc": "hevc_v4l2m2m"},
    "generic": {"h264": None, "hevc": None},
}


def _read_model() -> str:
    """The board model string from the device tree ('' when unavailable)."""
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            return Path(path).read_bytes().decode("utf-8", "ignore").replace("\x00", "").strip()
        except OSError:
            continue
    return ""


def detect_board(model: Optional[str] = None) -> str:
    """Normalise the board into one of the keys of ``_HW_ENCODERS``.

    ``model`` is exposed for testing; by default it is read from the device tree.
    """
    text = (model if model is not None else _read_model()).lower()
    if "raspberry pi 5" in text:
        return "pi5"
    if "raspberry pi 4" in text:
        return "pi4"
    if "raspberry pi" in text:
        return "pi"
    if any(k in text for k in ("cubie", "radxa", "a733", "a7s", "allwinner", "sun")):
        return "cubie"
    return "generic"


def available_encoders(run=subprocess.run) -> Set[str]:
    """Names of the video encoders the installed ffmpeg supports (empty on error).

    ``run`` is injectable for tests; the parsed output is ``ffmpeg -encoders``.
    """
    try:
        out = run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    names: Set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        # Encoder rows start with a flags token whose first char is the media
        # type: V(ideo)/A(udio)/S(ubtitle); the second token is the codec name.
        if len(parts) >= 2 and parts[0] and parts[0][0] == "V" and parts[0] != "V.....":
            names.add(parts[1])
    return names


def family_of(vcodec: str) -> str:
    """Codec family ('h264' | 'hevc') of a software codec / encoder name."""
    v = (vcodec or "").lower()
    if "265" in v or "hevc" in v:
        return "hevc"
    return "h264"


def default_bitrate(height: int) -> str:
    """A sensible target bitrate for bitrate-based (hardware) encoders by height."""
    h = int(height or 0)
    if h <= 0 or h >= 2160:
        return "16M"
    if h >= 1440:
        return "12M"
    if h >= 1080:
        return "8M"
    if h >= 720:
        return "5M"
    if h >= 480:
        return "2500k"
    return "1500k"


def cpu_encoder(vcodec: str) -> Encoder:
    return Encoder(name="cpu", codec=str(vcodec or "libx264"), kind="sw", rate_mode="crf")


def _hw_encoder(name: str) -> Encoder:
    # V4L2 mem2mem encoders are bitrate-controlled and want a plain yuv420p input.
    if name.endswith("_v4l2m2m"):
        return Encoder(name=name, codec=name, kind="hw", rate_mode="bitrate",
                       format_filter="format=yuv420p")
    return Encoder(name=name, codec=name, kind="hw", rate_mode="bitrate")


def select_encoders(
    preset: dict,
    board: str,
    available: Iterable[str],
    acceleration: str = "auto",
    fallback_to_cpu: bool = True,
) -> List[Encoder]:
    """Ordered encoders to try for ``preset`` (hardware first, then software).

    * ``acceleration`` = ``auto`` -> the board's preferred HW encoder for the
      preset's codec family (if the installed ffmpeg has it), then CPU.
    * ``cpu`` / ``software`` / ``none`` -> CPU only.
    * ``v4l2m2m`` / ``hw`` / ``hardware`` -> the family's V4L2 M2M encoder, then CPU.
    * any other string -> that exact ffmpeg encoder name (only if it matches the
      preset's codec family), then CPU.

    The software encoder is always appended when ``fallback_to_cpu`` is set (and
    always kept as the sole option when no hardware encoder applies), so a job can
    never end up with an empty candidate list.
    """
    available = set(available)
    vcodec = str(preset.get("vcodec", "libx264"))
    family = family_of(vcodec)
    cpu = cpu_encoder(vcodec)
    accel = (acceleration or "auto").strip().lower()

    if accel in ("cpu", "software", "none"):
        return [cpu]

    hw_name: Optional[str] = None
    if accel == "auto":
        hw_name = _HW_ENCODERS.get(board, {}).get(family)
    elif accel in ("v4l2m2m", "hw", "hardware"):
        hw_name = {"h264": "h264_v4l2m2m", "hevc": "hevc_v4l2m2m"}.get(family)
    else:
        # Explicit ffmpeg encoder name -- only honoured if it matches the family.
        if family_of(accel) == family:
            hw_name = accel
        else:
            _LOG.warning(
                "acceleration %r does not match preset codec family %r -- using CPU",
                acceleration, family,
            )

    chain: List[Encoder] = []
    if hw_name:
        if hw_name in available:
            chain.append(_hw_encoder(hw_name))
        else:
            _LOG.info(
                "Hardware encoder %s not available in ffmpeg -- using software.", hw_name
            )

    if not chain or fallback_to_cpu:
        chain.append(cpu)
    return chain


def build_ffmpeg_cmd(encoder: Encoder, preset: dict, src, dst) -> List[str]:
    """ffmpeg argument list for one encode with a specific ``encoder`` (pure).

    ``preset.height`` downscales to that height keeping the aspect ratio (width
    auto, forced even via ``scale=-2:H``); ``0``/absent keeps the source size.
    Software encoders use ``-crf``/``-preset``; hardware (V4L2 M2M) encoders use a
    target bitrate (``preset.bitrate`` or a height-based default). Progress is
    emitted on stdout via ``-progress pipe:1``.
    """
    height = int(preset.get("height", 0) or 0)
    cmd: List[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src)]

    filters: List[str] = []
    if height > 0:
        filters.append(f"scale=-2:{height}")
    if encoder.format_filter:
        filters.append(encoder.format_filter)
    if filters:
        cmd += ["-vf", ",".join(filters)]

    cmd += ["-c:v", encoder.codec]
    if encoder.rate_mode == "crf":
        cmd += ["-crf", str(preset.get("crf", 23))]
        if encoder.kind == "sw":
            cmd += ["-preset", str(preset.get("preset", "medium"))]
    else:  # bitrate (hardware)
        bitrate = str(preset.get("bitrate") or default_bitrate(height))
        cmd += ["-b:v", bitrate]
    cmd += list(encoder.extra_args)

    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
    return cmd
