from pathlib import Path

import pytest

from copystation.devices import (
    InvalidLayoutError,
    NoSourceError,
    NoTargetError,
    Probe,
    select_roles,
)

GB = 1024**3
MIN_BYTES = 6 * GB


def _probe(name, capacity, has_dcim, matched_source=True, free=None):
    return Probe(
        sys_name=name,
        device_node=f"/dev/{name}",
        mountpoint=Path(f"/run/copystation/mnt/{name}"),
        has_dcim=has_dcim,
        matched_source=matched_source,
        capacity=capacity,
        free=capacity if free is None else free,
        name=name,
    )


def test_order_independent_source_target():
    cam = _probe("cam", 23 * GB, has_dcim=True)
    sd = _probe("sd", 256 * GB, has_dcim=False)

    src1, tgt1 = select_roles([cam, sd], MIN_BYTES)
    src2, tgt2 = select_roles([sd, cam], MIN_BYTES)

    assert src1.sys_name == src2.sys_name == "cam"
    assert tgt1.sys_name == tgt2.sys_name == "sd"


def test_both_have_dcim_smaller_is_source():
    cam = _probe("cam", 23 * GB, has_dcim=True)
    sd = _probe("sd", 256 * GB, has_dcim=True)  # SD also carries a DCIM folder

    src, tgt = select_roles([sd, cam], MIN_BYTES)
    assert src.sys_name == "cam"
    assert tgt.sys_name == "sd"


def test_partitions_below_min_size_ignored():
    tiny = _probe("tiny", 4 * GB, has_dcim=True)   # < 6 GB, must be ignored
    sd = _probe("sd", 256 * GB, has_dcim=False)
    # Only the SD remains eligible, and it has no DCIM -> no source.
    with pytest.raises(NoSourceError):
        select_roles([tiny, sd], MIN_BYTES)


def test_source_not_smaller_than_target_raises():
    big_cam = _probe("cam", 256 * GB, has_dcim=True)
    small_sd = _probe("sd", 23 * GB, has_dcim=False)
    with pytest.raises(InvalidLayoutError):
        select_roles([big_cam, small_sd], MIN_BYTES)


def test_require_smaller_can_be_disabled():
    big_cam = _probe("cam", 256 * GB, has_dcim=True)
    small_sd = _probe("sd", 23 * GB, has_dcim=False)
    src, tgt = select_roles([big_cam, small_sd], MIN_BYTES, require_source_smaller=False)
    assert src.sys_name == "cam"
    assert tgt.sys_name == "sd"


def test_no_source_when_no_dcim():
    a = _probe("a", 64 * GB, has_dcim=False)
    b = _probe("b", 128 * GB, has_dcim=False)
    with pytest.raises(NoSourceError):
        select_roles([a, b], MIN_BYTES)


def test_no_target_with_single_device():
    cam = _probe("cam", 23 * GB, has_dcim=True)
    with pytest.raises(NoTargetError):
        select_roles([cam], MIN_BYTES)


def test_vid_pid_mismatch_excludes_source():
    # A DCIM device that fails the VID/PID allowlist is not a source candidate.
    cam = _probe("cam", 23 * GB, has_dcim=True, matched_source=False)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    with pytest.raises(NoSourceError):
        select_roles([cam, sd], MIN_BYTES)
