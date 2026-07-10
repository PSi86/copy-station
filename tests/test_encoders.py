"""Board-aware encoder selection and ffmpeg command building (pure, no hardware)."""

import pytest

from copystation.encoders import (
    Encoder,
    available_encoders,
    available_gst_elements,
    build_ffmpeg_cmd,
    build_gstreamer_cmd,
    cpu_encoder,
    decoder_scale_factor,
    default_bitrate,
    detect_board,
    family_of,
    gst_can_handle,
    gstreamer_output_height,
    select_encoders,
)


# --------------------------------------------------------------------------- #
# Board detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "model,expected",
    [
        ("Raspberry Pi 4 Model B Rev 1.4", "pi4"),
        ("Raspberry Pi 5 Model B Rev 1.0", "pi5"),
        ("Raspberry Pi 3 Model B", "pi"),
        ("Radxa Cubie A7S", "cubie"),
        ("Allwinner A733 board", "cubie"),
        ("Some Random x86 PC", "generic"),
        ("", "generic"),
    ],
)
def test_detect_board(model, expected):
    assert detect_board(model) == expected


def test_family_of():
    assert family_of("libx264") == "h264"
    assert family_of("h264_v4l2m2m") == "h264"
    assert family_of("libx265") == "hevc"
    assert family_of("hevc_v4l2m2m") == "hevc"
    assert family_of("") == "h264"


def test_available_encoders_parsing():
    sample = (
        "Encoders:\n"
        " V..... = Video\n"
        " ------\n"
        " V....D libx264              libx264 H.264\n"
        " V....D h264_v4l2m2m         V4L2 mem2mem H.264 encoder\n"
        " A..... aac                  AAC\n"
    )

    class _R:
        stdout = sample

    got = available_encoders(run=lambda *a, **k: _R())
    assert "libx264" in got
    assert "h264_v4l2m2m" in got
    assert "aac" not in got  # audio row ignored


def test_available_encoders_empty_on_error():
    def boom(*a, **k):
        raise FileNotFoundError("no ffmpeg")

    assert available_encoders(run=boom) == set()


def test_default_bitrate_scales_with_height():
    assert default_bitrate(1080) == "12M"
    assert default_bitrate(720) == "8M"
    assert default_bitrate(480) == "4M"
    assert default_bitrate(0) == "24M"   # unknown/original -> generous


# --------------------------------------------------------------------------- #
# Encoder selection (the fallback chain)
# --------------------------------------------------------------------------- #

H264 = {"id": "720p-h264", "height": 720, "vcodec": "libx264", "crf": 22}
H265 = {"id": "720p-h265", "height": 720, "vcodec": "libx265", "crf": 26}


def _names(encoders):
    return [e.name for e in encoders]


def test_auto_pi4_uses_hardware_h264_then_cpu():
    chain = select_encoders(H264, board="pi4", available={"h264_v4l2m2m", "libx264"})
    assert _names(chain) == ["h264_v4l2m2m", "cpu"]
    assert chain[0].is_hardware and chain[0].rate_mode == "bitrate"
    assert chain[1].kind == "sw" and chain[1].codec == "libx264"


def test_auto_pi5_has_no_hardware_encoder():
    # The Pi 5 dropped the HW encoder -> CPU only.
    chain = select_encoders(H264, board="pi5", available={"h264_v4l2m2m", "libx264"})
    assert _names(chain) == ["cpu"]


def test_auto_cubie_h264_uses_gstreamer_omx_then_cpu():
    # The A733's H.264 encoder is a GStreamer OMX element, not an ffmpeg encoder.
    chain = select_encoders(H264, board="cubie", available={"omxh264videoenc", "libx264"})
    assert _names(chain) == ["omxh264videoenc", "cpu"]
    assert chain[0].is_hardware and chain[0].is_gstreamer
    assert chain[0].rate_mode == "bitrate"
    assert chain[1].kind == "sw" and chain[1].codec == "libx264"


def test_auto_cubie_hevc_output_has_no_hardware_encoder():
    # The A733 exposes no H.265 hardware *encoder* -> H.265 output is CPU-only
    # (H.265 *input* is still hardware-decoded inside build_gstreamer_cmd).
    chain = select_encoders(H265, board="cubie", available={"omxh264videoenc", "libx265"})
    assert _names(chain) == ["cpu"]


def test_auto_skips_hardware_when_ffmpeg_lacks_it():
    # Board would prefer HW, but the installed ffmpeg has no such encoder.
    chain = select_encoders(H264, board="pi4", available={"libx264"})
    assert _names(chain) == ["cpu"]


def test_generic_board_stays_on_cpu():
    chain = select_encoders(H264, board="generic", available={"h264_v4l2m2m", "libx264"})
    assert _names(chain) == ["cpu"]


def test_acceleration_cpu_forces_software():
    chain = select_encoders(H264, board="pi4", available={"h264_v4l2m2m"}, acceleration="cpu")
    assert _names(chain) == ["cpu"]


def test_acceleration_explicit_encoder_matching_family():
    chain = select_encoders(
        H264, board="generic", available={"h264_v4l2m2m", "libx264"},
        acceleration="h264_v4l2m2m",
    )
    assert _names(chain) == ["h264_v4l2m2m", "cpu"]


def test_acceleration_explicit_encoder_wrong_family_uses_cpu():
    # Forcing an H.264 hardware encoder for an HEVC preset makes no sense -> CPU.
    chain = select_encoders(
        H265, board="generic", available={"h264_v4l2m2m", "libx265"},
        acceleration="h264_v4l2m2m",
    )
    assert _names(chain) == ["cpu"]


def test_no_fallback_keeps_only_hardware():
    chain = select_encoders(
        H264, board="pi4", available={"h264_v4l2m2m"}, fallback_to_cpu=False,
    )
    assert _names(chain) == ["h264_v4l2m2m"]


def test_no_fallback_still_yields_cpu_when_no_hardware():
    # Even with fallback off, a chain is never empty: CPU is the only option here.
    chain = select_encoders(H264, board="pi5", available=set(), fallback_to_cpu=False)
    assert _names(chain) == ["cpu"]


# --------------------------------------------------------------------------- #
# ffmpeg command building per encoder
# --------------------------------------------------------------------------- #

def test_build_cpu_cmd_uses_crf_and_preset():
    preset = {"height": 720, "vcodec": "libx264", "crf": 22, "preset": "slow"}
    cmd = build_ffmpeg_cmd(cpu_encoder("libx264"), preset, "/in/a.mp4", "/out/b.mp4")
    assert cmd[cmd.index("-i") + 1] == "/in/a.mp4"
    assert cmd[cmd.index("-vf") + 1] == "scale=-2:720"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "22"
    assert cmd[cmd.index("-preset") + 1] == "slow"
    assert "-b:v" not in cmd
    assert cmd[-1] == "/out/b.mp4"
    assert "-progress" in cmd


def test_build_hardware_cmd_uses_bitrate_not_crf():
    enc = select_encoders({"height": 1080, "vcodec": "libx264"}, board="pi4",
                          available={"h264_v4l2m2m", "libx264"})[0]
    cmd = build_ffmpeg_cmd(enc, {"height": 1080, "vcodec": "libx264"}, "a.mp4", "b.mp4")
    assert cmd[cmd.index("-c:v") + 1] == "h264_v4l2m2m"
    assert "-crf" not in cmd
    assert "-preset" not in cmd            # V4L2 M2M has no x264 speed preset
    assert cmd[cmd.index("-b:v") + 1] == "12M"  # height-based default bitrate
    assert "format=yuv420p" in cmd[cmd.index("-vf") + 1]


def test_build_hardware_cmd_honours_explicit_bitrate():
    enc = Encoder("h264_v4l2m2m", "h264_v4l2m2m", "hw", "bitrate", "format=yuv420p")
    cmd = build_ffmpeg_cmd(enc, {"height": 720, "vcodec": "libx264", "bitrate": "3M"}, "a", "b")
    assert cmd[cmd.index("-b:v") + 1] == "3M"


def test_build_keeps_resolution_when_height_zero():
    cmd = build_ffmpeg_cmd(cpu_encoder("libx265"), {"height": 0}, "a", "b")
    assert "-vf" not in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx265"


# --------------------------------------------------------------------------- #
# GStreamer (Allwinner OpenMAX) hardware path -- Cubie A7S
# --------------------------------------------------------------------------- #

def test_available_gst_elements_parsing():
    sample = (
        "omx:  omxh264videoenc: OpenMAX H.264 Video Encoder\n"
        "omx:  omxh264dec: OpenMAX H.264 Video Decoder\n"
        "omx:  omxhevcvideodec: OpenMAX H.265 Video Decoder\n"
        "coreelements:  queue: Queue\n"
        "\n"
        "Total count: 4 features\n"
    )

    class _R:
        stdout = sample

    got = available_gst_elements(run=lambda *a, **k: _R())
    assert {"omxh264videoenc", "omxh264dec", "omxhevcvideodec", "queue"} <= got
    assert not any(x.startswith("Total") for x in got)  # summary line ignored


def test_available_gst_elements_empty_on_error():
    def boom(*a, **k):
        raise FileNotFoundError("no gst-inspect")

    assert available_gst_elements(run=boom) == set()


# A 4K H.264 mp4 with no audio (the DJI drone case).
INFO_4K_H264 = {
    "vcodec": "h264", "width": 3840, "height": 2160,
    "has_audio": False, "acodec": None, "container": "mp4",
}


def _omx_encoder():
    return select_encoders(
        {"height": 1080, "vcodec": "libx264"}, board="cubie",
        available={"omxh264videoenc", "libx264"},
    )[0]


def test_gst_can_handle_accepts_h264_mp4_no_audio():
    assert gst_can_handle(INFO_4K_H264) is True


def test_gst_can_handle_accepts_hevc_and_aac_audio():
    assert gst_can_handle(dict(INFO_4K_H264, vcodec="hevc", has_audio=True, acodec="aac"))


def test_gst_can_handle_rejects_non_aac_audio():
    # Non-AAC audio can't be stream-copied here -> CPU path (which handles it).
    assert gst_can_handle(dict(INFO_4K_H264, has_audio=True, acodec="ac3")) is False


def test_gst_can_handle_rejects_unknown_codec_container_or_dims():
    assert gst_can_handle(dict(INFO_4K_H264, vcodec="vp9")) is False
    assert gst_can_handle(dict(INFO_4K_H264, container="avi")) is False
    assert gst_can_handle(dict(INFO_4K_H264, width=0)) is False


@pytest.mark.parametrize(
    "src,target,expected",
    [
        (2160, 1080, 1),   # 4K -> 1080p: exactly 1/2
        (2160, 720, 1),    # 4K -> 720p: 1/2 (1080) stays >= 720; 1/4 (540) would be below
        (2160, 540, 2),    # 4K -> 540p: exactly 1/4
        (1080, 720, 0),    # no clean 1/2-step >= 720 below 1080
        (2160, 1440, 0),   # 1/2 (1080) is below 1440 -> no decoder downscale
        (2160, 0, 0),      # keep source
        (0, 1080, 0),      # unknown source
        (1080, 1080, 0),   # target == source
    ],
)
def test_decoder_scale_factor(src, target, expected):
    assert decoder_scale_factor(src, target) == expected
    assert gstreamer_output_height(src, target) == (src >> expected if src else target)


def test_build_gstreamer_cmd_4k_to_1080p_uses_decoder_half_scale():
    preset = {"id": "1080p-h264", "height": 1080, "vcodec": "libx264"}
    cmd = build_gstreamer_cmd(_omx_encoder(), preset, "/in/a.mp4", "/out/b.mp4", INFO_4K_H264)
    assert cmd[0] == "gst-launch-1.0"
    assert "location=/in/a.mp4" in cmd
    assert "qtdemux" in cmd and "omxh264dec" in cmd and "omxh264videoenc" in cmd
    # Downscale happens in the DECODER (1/2), never the encoder (no output-* props).
    assert "scale=1" in cmd
    assert not any(x.startswith("output-width") or x.startswith("output-height") for x in cmd)
    assert "control-rate=variable" in cmd
    assert "target-bitrate=12000000" in cmd           # 1080p default 12M -> bits/s
    assert cmd[-1] == "location=/out/b.mp4"
    assert "aacparse" not in cmd and "name=d" not in cmd   # no audio -> linear pipeline


def test_build_gstreamer_cmd_scales_bitrate_for_60fps():
    # 60fps needs ~2x the 30fps default (capped at 1.8x): 12M -> 21.6M.
    info = dict(INFO_4K_H264, fps=59.94)
    cmd = build_gstreamer_cmd(_omx_encoder(), {"height": 1080, "vcodec": "libx264"}, "a", "b", info)
    assert f"target-bitrate={int(12_000_000 * 1.8)}" in cmd
    assert "scale=1" in cmd


def test_build_gstreamer_cmd_explicit_bitrate_wins_when_no_residual():
    # 4K -> 1080p is exact (out == target), so the explicit bitrate is honoured.
    info = dict(INFO_4K_H264, fps=59.94)
    preset = {"height": 1080, "vcodec": "libx264", "bitrate": "20M"}
    cmd = build_gstreamer_cmd(_omx_encoder(), preset, "a", "b", info)
    assert "target-bitrate=20000000" in cmd


def test_build_gstreamer_cmd_hevc_source_uses_hevc_decoder():
    # 4K HEVC -> 720p: decoder does 1/2 (to 1080), encoder stays H.264 and does NOT
    # scale (the 1080->720 finish is the transcoder's ffmpeg pass, not here).
    info = dict(INFO_4K_H264, vcodec="hevc")
    cmd = build_gstreamer_cmd(_omx_encoder(), {"height": 720, "vcodec": "libx264"}, "a", "b", info)
    assert "h265parse" in cmd and "omxhevcvideodec" in cmd and "scale=1" in cmd
    assert "omxh264videoenc" in cmd                   # output is still H.264
    assert not any(x.startswith("output-height") for x in cmd)
    assert "target-bitrate=12000000" in cmd           # sized to the 1080p HW stage


def test_build_gstreamer_cmd_carries_aac_audio_through_named_mux():
    info = dict(INFO_4K_H264, has_audio=True, acodec="aac")
    cmd = build_gstreamer_cmd(_omx_encoder(), {"height": 1080, "vcodec": "libx264"}, "a", "b", info)
    assert "name=d" in cmd and cmd.count("mux.") == 2  # video + audio into one muxer
    assert "aacparse" in cmd and "scale=1" in cmd


def test_build_gstreamer_cmd_keeps_resolution_when_height_zero():
    cmd = build_gstreamer_cmd(_omx_encoder(), {"height": 0, "vcodec": "libx264"}, "a", "b", INFO_4K_H264)
    assert not any(x.startswith("scale=") for x in cmd)   # no decoder downscale
    assert not any(x.startswith("output-width") or x.startswith("output-height") for x in cmd)
    assert "target-bitrate=24000000" in cmd           # default_bitrate(2160) = 24M
