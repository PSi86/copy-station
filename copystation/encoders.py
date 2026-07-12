"""Board-aware video encoder selection for transcoding.

The three target boards differ a lot in what they can encode in hardware:

* **Raspberry Pi 4** -- has an H.264 hardware encoder exposed by ffmpeg as
  ``h264_v4l2m2m`` (V4L2 mem2mem, ``/dev/video11``). No HEVC *encode*.
* **Raspberry Pi 5** -- the H.264 encoder block was **removed**; the Pi 5 has no
  hardware video encoder at all (HEVC decode only). So it must encode on the CPU.
* **Radxa Cubie A7S (Allwinner A733)** -- the Cedar VPU encodes H.264 in
  hardware, but **not through ffmpeg**: it is reachable only via GStreamer's
  Allwinner OpenMAX elements ``omxh264videoenc`` / ``omxhevcvideoenc`` (driving
  ``/dev/cedar_dev``) -- so **both H.264 and H.265 output** encode in hardware when
  the installed GStreamer exposes the element (older Radxa images shipped the HEVC
  encoder non-functional, so it is only used when present and falls back to the CPU
  otherwise). The VPU also *decodes* H.264 and H.265 in hardware (``omxh264dec`` /
  ``omxhevcvideodec``), which we use to offload the 4K decode. These encoders run as
  a **GStreamer pipeline** (see ``build_gstreamer_cmd``), not an ffmpeg command.

This module maps ``(board, codec-family) -> preferred hardware encoder``, probes
which encoders the installed ffmpeg (``-encoders``) and GStreamer
(``gst-inspect-1.0``) actually have, and produces an **ordered list of
candidates** to try (hardware first, then software) so the transcoder can fall
back automatically when a hardware encode fails at runtime. Each ``Encoder``
carries an ``engine`` (``ffmpeg`` | ``gstreamer``) telling the transcoder how to
build and run it.

Everything here is pure/deterministic given its inputs (board string, the set of
available encoder names), so it is fully unit-testable without any hardware.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

_LOG = logging.getLogger("copystation.encoders")


@dataclass(frozen=True)
class Encoder:
    """One concrete way to encode: an ffmpeg ``-c:v`` codec plus how to drive it."""

    name: str                        # shown in the UI / logs (e.g. "cpu", "h264_v4l2m2m")
    codec: str                       # ffmpeg -c:v value, or the GStreamer element name
    kind: str                        # "hw" | "sw"
    rate_mode: str                   # "crf" (software) | "bitrate" (hardware)
    format_filter: Optional[str] = None   # appended to -vf (e.g. "format=yuv420p")
    extra_args: tuple = field(default_factory=tuple)
    engine: str = "ffmpeg"           # "ffmpeg" | "gstreamer" -- how to build/run it
    # ffmpeg ``-hwaccel`` used to offload the SOURCE decode (e.g. "drm" for the
    # Pi 5's HEVC hardware decoder). Independent of the encoder: the encode still
    # runs on the CPU: this only moves the decode off it. ``None`` = software decode.
    decode_hwaccel: Optional[str] = None

    @property
    def is_hardware(self) -> bool:
        return self.kind == "hw"

    @property
    def is_gstreamer(self) -> bool:
        return self.engine == "gstreamer"


# board -> codec family -> preferred hardware encoder (None = no HW encode).
# Only well-known boards get a hardware default; an unknown board stays on the
# CPU unless the user forces an encoder via `transcode.acceleration`. The Pi
# encoders are ffmpeg V4L2 M2M; the Cubie's is a GStreamer OpenMAX element (see
# the module docstring) -- both are handled uniformly via the Encoder.engine.
_HW_ENCODERS = {
    "pi4": {"h264": "h264_v4l2m2m", "hevc": None},
    "pi": {"h264": "h264_v4l2m2m", "hevc": None},   # other/older Pi with the encoder
    "pi5": {"h264": None, "hevc": None},            # Pi 5 dropped the HW encoder
    # Allwinner A733: H.264 AND H.265 encode via GStreamer OMX (omxh264videoenc /
    # omxhevcvideoenc). Both are bitrate-controlled; the HEVC encoder is only used
    # when the installed GStreamer actually exposes it (older Radxa images shipped it
    # non-functional), and a runtime failure falls back to the CPU (libx265).
    "cubie": {"h264": "omxh264videoenc", "hevc": "omxhevcvideoenc"},
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
        # Encoder rows are "<flags> <name> <description>"; the 6-char flags token
        # starts with the media type V(ideo)/A(udio)/S(ubtitle). Skip the legend
        # lines ("V..... = Video", whose name field is "="): do NOT filter on the
        # flags value itself, or a real encoder with no capability flags -- e.g. the
        # Pi's ``h264_v4l2m2m`` / ``hevc_v4l2m2m``, listed as "V....." -- would be
        # wrongly dropped and its hardware never selected.
        if len(parts) >= 2 and parts[0][:1] == "V" and parts[1] != "=":
            names.add(parts[1])
    return names


# ``gst-inspect-1.0`` lists one element per line as ``plugin:  element: Description``.
_GST_ELEMENT_RE = re.compile(r"^\s*[\w.+-]+:\s+([\w.+-]+):\s")


def available_gst_elements(run=subprocess.run) -> Set[str]:
    """Names of the GStreamer elements installed (empty when gst-inspect is absent).

    Used to detect the Allwinner OpenMAX encoder/decoder elements (``omx*``) that
    expose the Cubie's hardware codec. ``run`` is injectable for tests.
    """
    try:
        out = run(
            ["gst-inspect-1.0"], capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    names: Set[str] = set()
    for line in out.splitlines():
        m = _GST_ELEMENT_RE.match(line)
        if m:
            names.add(m.group(1))
    return names


def available_hwaccels(run=subprocess.run) -> Set[str]:
    """Names of the ffmpeg hardware-acceleration methods (``ffmpeg -hwaccels``).

    Used to detect the ``drm`` method that reaches the Raspberry Pi 5's HEVC
    hardware decoder (see :func:`hevc_decode_hwaccel`). Empty when ffmpeg is
    missing. ``run`` is injectable for tests.
    """
    try:
        out = run(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    names: Set[str] = set()
    for line in out.splitlines():
        s = line.strip()
        # Skip blank lines and the "Hardware acceleration methods:" header (which
        # is the only line ending in a colon); every other line is one method.
        if not s or s.endswith(":"):
            continue
        names.add(s)
    return names


def family_of(vcodec: str) -> str:
    """Codec family ('h264' | 'hevc') of a software codec / encoder name."""
    v = (vcodec or "").lower()
    if "265" in v or "hevc" in v:
        return "hevc"
    return "h264"


def default_bitrate(height: int) -> str:
    """A sensible ~30fps target bitrate for bitrate-based (hardware) encoders.

    Hardware encoders are less efficient than x264/x265 at a given quality, so
    these are deliberately generous (a fixed low bitrate is what makes a hardware
    encode look far worse than a CRF software one). The GStreamer path scales
    these up further for high-framerate sources; a preset's explicit ``bitrate``
    overrides them entirely.
    """
    h = int(height or 0)
    if h <= 0 or h >= 2160:
        return "40M"
    if h >= 1440:
        return "24M"
    if h >= 1080:
        return "16M"
    if h >= 720:
        return "10M"
    if h >= 540:
        return "8M"
    if h >= 480:
        return "5M"
    return "4M"


def cpu_encoder(vcodec: str, rate_mode: str = "crf") -> Encoder:
    """A software encoder. ``rate_mode`` is ``crf`` (quality) or ``bitrate`` (ABR).

    ``bitrate`` is used for the GStreamer two-stage *finishing* pass, so the whole
    preset ladder stays bitrate-consistent (a 720p finish targets the preset's
    bitrate rather than a CRF, which otherwise made it smaller than a 540p hardware
    encode).
    """
    return Encoder(name="cpu", codec=str(vcodec or "libx264"), kind="sw", rate_mode=rate_mode)


def _hw_encoder(name: str) -> Encoder:
    # Allwinner OpenMAX elements are driven as a GStreamer pipeline (bitrate-based).
    if name.startswith("omx"):
        return Encoder(name=name, codec=name, kind="hw", rate_mode="bitrate",
                       engine="gstreamer")
    # V4L2 mem2mem encoders are bitrate-controlled and want a plain yuv420p input.
    if name.endswith("_v4l2m2m"):
        return Encoder(name=name, codec=name, kind="hw", rate_mode="bitrate",
                       format_filter="format=yuv420p")
    return Encoder(name=name, codec=name, kind="hw", rate_mode="bitrate")


# Board -> the ffmpeg ``-hwaccel`` that offloads HEVC *decode* to that board's
# video block. The Raspberry Pi 4 and Pi 5 both carry the V4L2-stateless HEVC
# decoder (the "rpivid" block on /dev/video19), reached through ffmpeg's DRM-PRIME
# path, so an HEVC source is hardware-decoded on both (measured ~2x the software
# decode rate for 4K). This frees the CPU for the encode; on the **Pi 4** it also
# pairs with the hardware H.264 *encoder* (``h264_v4l2m2m``) for a near-full-
# hardware HEVC->H.264 pass (see ``with_decode_offload``). The **Pi 5** has no
# hardware encoder, so the encode stays on the CPU. The Cubie hardware-decodes
# through its own GStreamer OMX pipeline instead, so it is deliberately not listed.
_HEVC_DECODE_HWACCEL = {"pi5": "drm", "pi4": "drm"}


def hevc_decode_hwaccel(board: str, src_vcodec: Any,
                        available: Iterable[str]) -> Optional[str]:
    """The ffmpeg ``-hwaccel`` to offload this source's decode, or ``None``.

    Returns the board's HEVC-decode hwaccel only when the source really is HEVC
    (the sole codec the wired hardware decodes) **and** the installed ffmpeg
    advertises that hwaccel (``ffmpeg -hwaccels``). Any other source codec --
    notably H.264, for which the Pi 5 has no hardware decoder -- or a board with no
    wired decoder returns ``None`` (plain software decode).
    """
    if family_of(str(src_vcodec or "")) != "hevc":
        return None
    hw = _HEVC_DECODE_HWACCEL.get(board)
    return hw if hw and hw in set(available) else None


def with_decode_offload(encoders: List[Encoder], board: str, src_vcodec: Any,
                        hwaccels: Iterable[str]) -> List[Encoder]:
    """Prepend a hardware-decode variant of the software encoder when it applies.

    On a board whose hardware can *decode* the source (the Pi 4 / Pi 5 HEVC
    decoder), a ``-hwaccel``-decorated copy of **each** ffmpeg encoder is inserted
    right in front of it, with the plain (software-decode) encoder kept right
    behind as the automatic fallback. Decorating the *hardware* encoder too matters
    on the Pi 4: an HEVC source can then be hardware-decoded **and** hardware-
    encoded in one pass (``h264_v4l2m2m`` with ``-hwaccel drm``), which is markedly
    faster than either half alone; the Pi 5 has only a CPU encoder, so this reduces
    to decorating that one encoder. A runtime hwaccel failure just drops to the
    plain software-decode candidate through the existing loop. GStreamer encoders
    (the Cubie, which does its own hardware decode) are never decorated. A no-op
    when no hardware decode applies, so H.264 sources and other boards are
    untouched.
    """
    hw = hevc_decode_hwaccel(board, src_vcodec, hwaccels)
    if not hw:
        return encoders
    out: List[Encoder] = []
    for e in encoders:
        if not e.is_gstreamer and not e.decode_hwaccel:
            out.append(replace(e, decode_hwaccel=hw))  # HW-decode variant first
        out.append(e)                                   # plain-decode fallback
    return out


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
    if accel in ("auto", "hw", "hardware"):
        # The board's preferred hardware encoder for this codec family (ffmpeg
        # V4L2 M2M or a GStreamer OMX element, decided by the board map).
        hw_name = _HW_ENCODERS.get(board, {}).get(family)
    elif accel == "v4l2m2m":
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
    emitted on stdout via ``-progress pipe:1``. ``encoder.decode_hwaccel`` (e.g.
    "drm" on the Pi 5) prepends ``-hwaccel`` so the source is hardware-decoded;
    ffmpeg then downloads the frames back to normal memory for the ``scale``
    filter and the CPU encode, so the rest of the command is unchanged.
    """
    height = int(preset.get("height", 0) or 0)
    cmd: List[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    if encoder.decode_hwaccel:  # hardware-decode the source (before -i)
        cmd += ["-hwaccel", encoder.decode_hwaccel]
    cmd += ["-i", str(src)]

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
    else:  # bitrate (hardware V4L2 M2M, or a software finishing pass)
        cmd += ["-b:v", str(preset.get("bitrate") or default_bitrate(height))]
    # Software encoders take an x264/x265 speed preset in either rate mode; the
    # hardware V4L2 encoders have none.
    if encoder.kind == "sw":
        cmd += ["-preset", str(preset.get("preset", "medium"))]
    cmd += list(encoder.extra_args)

    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


# --------------------------------------------------------------------------- #
# GStreamer (Allwinner OpenMAX) hardware path -- Cubie A7S
# --------------------------------------------------------------------------- #

# Container (by lowercase file extension) -> GStreamer demuxer. Only the
# well-tested ones; anything else routes to the CPU/ffmpeg path (which handles
# every container) via ``gst_can_handle`` returning False.
_GST_DEMUX = {
    "mp4": "qtdemux", "mov": "qtdemux", "m4v": "qtdemux",
    "mkv": "matroskademux", "webm": "matroskademux",
}

# Source video codecs the Cubie can hardware-*decode* (omxh264dec / omxhevcvideodec).
_GST_DECODERS = {
    "h264": ("h264parse", "omxh264dec"),
    "hevc": ("h265parse", "omxhevcvideodec"),
    "h265": ("h265parse", "omxhevcvideodec"),
}


def _bitrate_bps(value) -> int:
    """Bits/second from an ffmpeg-style bitrate ('8M', '2500k', 8000000)."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "").strip().lower()
    if not s:
        return 0
    mult = 1
    if s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("k"):
        mult, s = 1_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def gst_can_handle(info: dict) -> bool:
    """Whether the GStreamer OMX pipeline can transcode a source with this info.

    ``info`` is what ``transcode.probe_video_info`` returns. The pipeline needs a
    hardware-decodable video codec with known dimensions in a demuxable container;
    audio, if present, is stream-copied and so must already be AAC. Anything else
    falls back to the CPU/ffmpeg path (which handles it), rather than failing after
    a wasted encode.
    """
    vcodec = str(info.get("vcodec") or "").lower()
    if vcodec not in _GST_DECODERS:
        return False
    if not (int(info.get("width") or 0) > 0 and int(info.get("height") or 0) > 0):
        return False
    if str(info.get("container") or "").lower() not in _GST_DEMUX:
        return False
    if info.get("has_audio") and str(info.get("acodec") or "").lower() != "aac":
        return False
    return True


def decoder_scale_factor(src_h: int, target_h: int) -> int:
    """OMX decoder ``scale`` value (0=full, 1=1/2, 2=1/4) for a target height.

    Returns the largest power-of-two downscale whose result stays **>=**
    ``target_h`` (0 when no downscale applies). The Allwinner *decoder* scaler is
    artifact-free; the *encoder* scaler leaves a magenta line on the bottom row,
    so we always downscale in the decoder and never set the encoder's output size.
    Only 1/2 and 1/4 exist, so a target that is not a power-of-two fraction of the
    source is only reached to within one step here and finished on the CPU (see the
    transcoder's residual ffmpeg pass).
    """
    src_h, target_h = int(src_h or 0), int(target_h or 0)
    if src_h <= 0 or target_h <= 0 or target_h >= src_h:
        return 0
    k = 0
    while k < 2 and (src_h >> (k + 1)) >= target_h:
        k += 1
    return k


def gstreamer_output_height(src_h: int, target_h: int) -> int:
    """The height ``build_gstreamer_cmd`` actually produces (decoder-scaled)."""
    src_h = int(src_h or 0)
    if src_h <= 0:
        return int(target_h or 0)
    return src_h >> decoder_scale_factor(src_h, target_h)


def build_gstreamer_cmd(encoder: Encoder, preset: dict, src, dst, info: dict) -> List[str]:
    """``gst-launch-1.0`` argv for one hardware encode with an OMX ``encoder`` (pure).

    Builds a **HW-decode(+scale) -> HW-encode** pipeline: the source is demuxed and
    hardware-decoded, and the **decoder** downscales by 1/2 or 1/4 via its ``scale``
    property (artifact-free, unlike the encoder scaler, which leaves a magenta line
    on the bottom row). The encoder never scales. So the produced height is the
    source divided by 1, 2 or 4 -- whichever lands closest to, but not below,
    ``preset.height`` -- and the transcoder adds a light ffmpeg pass when that is
    still above the requested height. Present AAC audio is stream-copied through an
    ``mp4mux``; progress is emitted by ``progressreport``. Assumes ``gst_can_handle``.
    """
    src, dst = str(src), str(dst)
    src_h = int(info.get("height") or 0)
    target_h = int(preset.get("height", 0) or 0)
    scale = decoder_scale_factor(src_h, target_h)
    out_h = (src_h >> scale) if src_h > 0 else target_h

    # Bitrate is sized to the height THIS stage encodes (out_h), framerate-aware.
    # An explicit preset bitrate is honoured only when this stage already produces
    # the final height (no CPU finishing pass re-encodes it afterwards).
    if preset.get("bitrate") and out_h == target_h:
        bps = _bitrate_bps(preset.get("bitrate"))
    else:
        bps = _bitrate_bps(default_bitrate(out_h))
        fps = float(info.get("fps") or 0)
        if fps > 33:
            bps = int(bps * min(1.8, fps / 30.0))

    container = str(info.get("container") or "mp4").lower()
    demux = _GST_DEMUX.get(container, "qtdemux")
    vcodec = str(info.get("vcodec") or "h264").lower()
    parse, decoder = _GST_DECODERS.get(vcodec, ("h264parse", "omxh264dec"))
    dec: List[str] = [decoder] + ([] if scale == 0 else [f"scale={scale}"])
    enc: List[str] = [encoder.codec, "control-rate=variable", f"target-bitrate={bps}"]
    # Parse the ENCODER's output: H.265 when the OMX HEVC encoder is used, else H.264.
    out_parse = "h265parse" if encoder.codec == "omxhevcvideoenc" else "h264parse"
    has_audio = bool(info.get("has_audio")) and str(info.get("acodec") or "").lower() == "aac"

    cmd: List[str] = ["gst-launch-1.0", "-e", "filesrc", f"location={src}", "!"]
    if has_audio:
        # Named demux/mux so the audio can be carried alongside the re-encoded video.
        cmd += [demux, "name=d",
                "d.", "!", "queue", "!", parse, "!", *dec, "!", "queue", "!",
                *enc, "!", out_parse, "!", "progressreport", "update-freq=2",
                "!", "queue", "!", "mux.",
                "d.", "!", "queue", "!", "aacparse", "!", "queue", "!", "mux.",
                "mp4mux", "name=mux", "!", "filesink", f"location={dst}"]
    else:
        cmd += [demux, "!", parse, "!", *dec, "!", "queue", "!",
                *enc, "!", out_parse, "!", "progressreport", "update-freq=2",
                "!", "mp4mux", "!", "filesink", f"location={dst}"]
    return cmd
