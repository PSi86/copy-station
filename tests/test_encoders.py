"""Board-aware encoder selection and ffmpeg command building (pure, no hardware)."""

import pytest

from copystation.encoders import (
    Encoder,
    available_encoders,
    build_ffmpeg_cmd,
    cpu_encoder,
    default_bitrate,
    detect_board,
    family_of,
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
    assert default_bitrate(1080) == "8M"
    assert default_bitrate(720) == "5M"
    assert default_bitrate(480) == "2500k"
    assert default_bitrate(0) == "16M"   # unknown/original -> generous


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


def test_auto_cubie_hevc_uses_hardware():
    chain = select_encoders(H265, board="cubie", available={"hevc_v4l2m2m", "libx265"})
    assert _names(chain) == ["hevc_v4l2m2m", "cpu"]


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
    assert cmd[cmd.index("-b:v") + 1] == "8M"   # height-based default bitrate
    assert "format=yuv420p" in cmd[cmd.index("-vf") + 1]


def test_build_hardware_cmd_honours_explicit_bitrate():
    enc = Encoder("h264_v4l2m2m", "h264_v4l2m2m", "hw", "bitrate", "format=yuv420p")
    cmd = build_ffmpeg_cmd(enc, {"height": 720, "vcodec": "libx264", "bitrate": "3M"}, "a", "b")
    assert cmd[cmd.index("-b:v") + 1] == "3M"


def test_build_keeps_resolution_when_height_zero():
    cmd = build_ffmpeg_cmd(cpu_encoder("libx265"), {"height": 0}, "a", "b")
    assert "-vf" not in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
