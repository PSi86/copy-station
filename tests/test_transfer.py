import os

import pytest

from copystation.config import Config
from copystation.daemon import perform_transfer
from copystation.state import StationState, StatusHub
from copystation.status import StatusIndicator
from copystation.transfer import (
    InsufficientSpaceError,
    SourceVanishedError,
    TransferError,
    VerificationError,
    _describe_rsync_failure,
    check_free_space,
    cleanup_source,
    copy_tree,
    delete_files,
    dir_signature,
    total_size,
    verify,
)


def _make_dcim(root, files):
    """Create root/DCIM with the given {relpath: content}."""
    dcim = root / "DCIM"
    for rel, content in files.items():
        path = dcim / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return dcim


def test_dir_signature_counts_files_and_sizes(tmp_path):
    dcim = _make_dcim(tmp_path, {"100MEDIA/a.mp4": b"xxxx", "100MEDIA/b.jpg": b"yy"})
    sig = dir_signature(dcim)
    assert sig == {"100MEDIA/a.mp4": 4, "100MEDIA/b.jpg": 2}


def test_copy_and_verify_roundtrip(tmp_path):
    src = _make_dcim(tmp_path / "src", {"100MEDIA/a.mp4": b"hello", "x.jpg": b"hi"})
    dst = tmp_path / "dst"
    copy_tree(src, dst)
    verify(src, dst)  # must not raise
    assert (dst / "100MEDIA" / "a.mp4").read_bytes() == b"hello"


def test_verify_detects_missing_file(tmp_path):
    src = _make_dcim(tmp_path / "src", {"a.mp4": b"hello", "b.jpg": b"hi"})
    dst = tmp_path / "dst"
    copy_tree(src, dst)
    (dst / "b.jpg").unlink()
    with pytest.raises(VerificationError):
        verify(src, dst)


def test_verify_detects_size_mismatch(tmp_path):
    src = _make_dcim(tmp_path / "src", {"a.mp4": b"hello"})
    dst = tmp_path / "dst"
    copy_tree(src, dst)
    (dst / "a.mp4").write_bytes(b"hell")  # different size
    with pytest.raises(VerificationError):
        verify(src, dst)


def test_cleanup_keeps_dcim_folder(tmp_path):
    dcim = _make_dcim(tmp_path, {"100MEDIA/a.mp4": b"x", "b.jpg": b"y"})
    cleanup_source(dcim, keep_folder=True)
    assert dcim.is_dir()
    assert list(dcim.iterdir()) == []


def test_cleanup_removes_dcim_folder(tmp_path):
    dcim = _make_dcim(tmp_path, {"a.mp4": b"x"})
    cleanup_source(dcim, keep_folder=False)
    assert not dcim.exists()


def test_check_free_space_raises_when_too_small(tmp_path):
    # Huge requirement -> guaranteed to exceed free space.
    with pytest.raises(InsufficientSpaceError):
        check_free_space(tmp_path, required_bytes=10**18)


# ----- abort / friendly error handling -----------------------------------------


def test_describe_rsync_failure_io_error_is_disconnect():
    stderr = (
        'rsync: [sender] read errors mapping "/run/.../DJI_0011.MP4": '
        "Input/output error (5)\nrsync error: some files/attrs were not "
        "transferred (code 23) at main.c(1347)"
    )
    err = _describe_rsync_failure(23, stderr)
    assert isinstance(err, SourceVanishedError)
    assert "disconnected" in str(err).lower()


def test_describe_rsync_failure_no_space():
    err = _describe_rsync_failure(11, "rsync: write failed: No space left on device (28)")
    assert isinstance(err, InsufficientSpaceError)


def test_describe_rsync_failure_vanished_code24():
    assert isinstance(_describe_rsync_failure(24, ""), SourceVanishedError)


def test_describe_rsync_failure_generic_includes_code():
    err = _describe_rsync_failure(99, "something odd")
    assert isinstance(err, TransferError)
    assert "99" in str(err)


def test_copy_tree_aborts_when_source_vanishes(tmp_path):
    src = _make_dcim(tmp_path / "src", {"a.mp4": b"x" * 100, "b.mp4": b"y" * 100})
    dst = tmp_path / "dst"
    with pytest.raises(SourceVanishedError):
        copy_tree(src, dst, abort_check=lambda: True)


def test_perform_transfer_disconnect_keeps_source(tmp_path):
    src = tmp_path / "camera"
    _make_dcim(src, {"100MEDIA/clip.mp4": b"video-data"})
    target = tmp_path / "sd"
    target.mkdir()

    # A device node that does not exist -> abort_check fires immediately.
    with pytest.raises(SourceVanishedError, match="Source"):
        perform_transfer(
            src, target, "DJI_O4", _hub(), _config(), source_device="/dev/does-not-exist"
        )

    # Safety guarantee: the source media is untouched after a failed copy.
    assert (src / "DCIM" / "100MEDIA" / "clip.mp4").read_bytes() == b"video-data"


def test_perform_transfer_target_disconnect_keeps_source(tmp_path):
    src = tmp_path / "camera"
    _make_dcim(src, {"100MEDIA/clip.mp4": b"video-data"})
    target = tmp_path / "sd"
    target.mkdir()

    # Only the target node is missing -> abort with a clear "Target" message.
    with pytest.raises(SourceVanishedError, match="Target"):
        perform_transfer(
            src, target, "DJI_O4", _hub(), _config(), target_device="/dev/does-not-exist"
        )

    # The source media is still untouched (cleanup only happens after verify).
    assert (src / "DCIM" / "100MEDIA" / "clip.mp4").read_bytes() == b"video-data"


def test_copy_tree_abort_reason_propagates(tmp_path):
    src = _make_dcim(tmp_path / "src", {"a.mp4": b"x" * 100, "b.mp4": b"y" * 100})
    dst = tmp_path / "dst"
    with pytest.raises(SourceVanishedError, match="custom reason"):
        copy_tree(src, dst, abort_check=lambda: "custom reason")


def test_device_abort_check_labels_which_side(tmp_path):
    from copystation.daemon import _device_abort_check

    here = str(tmp_path)  # an existing path
    # Source present, target gone -> labelled "Target".
    msg = _device_abort_check(here, "/dev/does-not-exist")()
    assert msg and "Target" in msg
    # Target present, source gone -> labelled "Source".
    msg = _device_abort_check("/dev/does-not-exist", here)()
    assert msg and "Source" in msg
    # Both present -> no abort; nothing to watch -> no callback.
    assert _device_abort_check(here, here)() is None
    assert _device_abort_check(None, None) is None


def test_volume_alive_node_missing_is_dead():
    from copystation.transfer import volume_alive

    assert volume_alive("/dev/does-not-exist") is False
    assert volume_alive("/dev/does-not-exist", "/wherever") is False


def test_volume_alive_detects_dead_mount_via_statvfs(monkeypatch, tmp_path):
    # The device node still resolves, but the mount no longer answers I/O.
    import copystation.transfer as transfer

    def _eio(path):
        raise OSError("Input/output error")

    monkeypatch.setattr(transfer.os, "statvfs", _eio, raising=False)
    assert transfer.volume_alive(str(tmp_path), tmp_path) is False


def test_volume_alive_ok_when_node_and_fs_answer(monkeypatch, tmp_path):
    import copystation.transfer as transfer

    monkeypatch.setattr(transfer.os, "statvfs", lambda p: None, raising=False)
    assert transfer.volume_alive(str(tmp_path), tmp_path) is True


def test_disk_name_derivation():
    from copystation.transfer import _disk_name

    assert _disk_name("/dev/sdb1") == "sdb"
    assert _disk_name("/dev/sdc") == "sdc"          # whole disk, no partition
    assert _disk_name("/dev/mmcblk0p1") == "mmcblk0"
    assert _disk_name("/dev/nvme0n1p1") == "nvme0n1"


def _fake_sysblock(monkeypatch, tmp_path, sizes):
    """Build a fake /sys/block/<disk>/size tree and point transfer at it."""
    import copystation.transfer as transfer

    root = tmp_path / "sysblock"
    for disk, value in sizes.items():
        (root / disk).mkdir(parents=True)
        (root / disk / "size").write_text(value)
    monkeypatch.setattr(transfer, "_SYS_BLOCK", str(root))
    return transfer


def test_disk_capacity_zero_reads_whole_disk_size(monkeypatch, tmp_path):
    transfer = _fake_sysblock(monkeypatch, tmp_path, {"sdb": "0", "sdc": "249737216"})
    assert transfer._disk_capacity_zero("/dev/sdb1") is True    # partition -> sdb -> 0
    assert transfer._disk_capacity_zero("/dev/sdc") is False     # whole disk, nonzero
    assert transfer._disk_capacity_zero("/dev/sdd1") is False    # no sysfs entry -> unknown


def test_volume_alive_uses_the_capacity_check(monkeypatch, tmp_path):
    import copystation.transfer as transfer

    # Node exists; the kernel reports the backing disk's capacity as zero -> gone.
    monkeypatch.setattr(transfer, "_disk_capacity_zero", lambda node: True)
    assert transfer.volume_alive(str(tmp_path), tmp_path) is False
    monkeypatch.setattr(transfer, "_disk_capacity_zero", lambda node: False)
    assert transfer.volume_alive(str(tmp_path), tmp_path) is True


def test_abort_check_fires_on_zeroed_target_disk(monkeypatch, tmp_path):
    # Regression (Cubie + card-reader-is-a-hub): a pulled microSD whose node and
    # mount linger is caught because the kernel zeroes the backing disk's capacity.
    import copystation.transfer as transfer
    from copystation.daemon import _device_abort_check

    src = tmp_path / "src"
    src.mkdir()
    tgt = tmp_path / "tgt"
    tgt.mkdir()

    # Only the target's backing disk reads capacity 0.
    monkeypatch.setattr(
        transfer, "_disk_capacity_zero",
        lambda node: os.path.basename(str(node)) == "tgt",
    )
    msg = _device_abort_check(str(src), str(tgt), src, tgt)()
    assert msg and "Target" in msg


def test_perform_transfer_zeroed_target_keeps_source(monkeypatch, tmp_path):
    import copystation.transfer as transfer

    src = tmp_path / "camera"
    _make_dcim(src, {"100MEDIA/clip.mp4": b"video-data"})
    target = tmp_path / "sd"
    target.mkdir()

    # The target node still resolves, but the kernel zeroed its disk capacity.
    monkeypatch.setattr(
        transfer, "_disk_capacity_zero",
        lambda node: os.path.basename(str(node)) == "sd",
    )
    with pytest.raises(SourceVanishedError, match="Target"):
        perform_transfer(
            src, target, "DJI_O4", _hub(), _config(), target_device=str(target)
        )
    # Safety guarantee: the source media is untouched.
    assert (src / "DCIM" / "100MEDIA" / "clip.mp4").read_bytes() == b"video-data"


# ----- end-to-end via perform_transfer -----------------------------------------


def _config():
    return Config()  # defaults: media_dirname=DCIM, keep_dcim_folder=True


def _hub():
    return StatusHub(StationState(), StatusIndicator())


def test_perform_transfer_refreshes_devices(tmp_path):
    src = tmp_path / "camera"
    _make_dcim(src, {"100MEDIA/clip.mp4": b"video-data"})
    target = tmp_path / "sd"
    target.mkdir()

    calls = []
    perform_transfer(
        src, target, "Cam", _hub(), _config(),
        on_devices_refresh=lambda: calls.append(1),
    )
    # Refreshed at least after the copy and after clearing the source.
    assert len(calls) >= 1


def test_perform_transfer_full_cycle(tmp_path):
    src = tmp_path / "camera"
    _make_dcim(src, {"100MEDIA/clip.mp4": b"video-data", "100MEDIA/pic.jpg": b"img"})
    target = tmp_path / "sd"
    target.mkdir()

    dest = perform_transfer(src, target, "DJI_O4", _hub(), _config())

    # Target named and populated correctly.
    assert dest.name == "transfer_0001_DJI_O4"
    assert (dest / "100MEDIA" / "clip.mp4").read_bytes() == b"video-data"
    # Source cleared, DCIM folder kept.
    dcim = src / "DCIM"
    assert dcim.is_dir()
    assert list(dcim.iterdir()) == []


def test_perform_transfer_no_dcim_raises(tmp_path):
    src = tmp_path / "camera"
    src.mkdir()
    target = tmp_path / "sd"
    target.mkdir()
    with pytest.raises(TransferError):
        perform_transfer(src, target, "X", _hub(), _config())


def test_perform_transfer_second_run_increments(tmp_path):
    target = tmp_path / "sd"
    target.mkdir()

    src1 = tmp_path / "c1"
    _make_dcim(src1, {"a.mp4": b"one"})
    d1 = perform_transfer(src1, target, "Cam", _hub(), _config())

    src2 = tmp_path / "c2"
    _make_dcim(src2, {"b.mp4": b"two"})
    d2 = perform_transfer(src2, target, "Cam", _hub(), _config())

    assert d1.name == "transfer_0001_Cam"
    assert d2.name == "transfer_0002_Cam"


# ----- sources that record to the root of their storage (Walksnail) -----------

def _make_walksnail(root):
    """A Walksnail-like volume: recordings at the root, next to the unit's own
    files, an orphaned sidecar and filesystem bookkeeping."""
    files = {
        "VID0001.mp4": b"video-one",
        "VID0001.osd": b"osd-one",
        "VID0002.mp4": b"video-two!",
        "Avatar_version.txt": b"fw 1.0",
        "VID0009.osd": b"orphan",
        "System Volume Information/WPSettings.dat": b"sys",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _names(root):
    return sorted(p.name for p in root.iterdir())


RECORDINGS = ["VID0001.mp4", "VID0001.osd", "VID0002.mp4"]
LEFT_ON_THE_UNIT = ["Avatar_version.txt", "System Volume Information", "VID0009.osd"]


def test_copy_verify_and_delete_touch_only_the_listed_files(tmp_path):
    src = _make_walksnail(tmp_path / "walk")
    dst = tmp_path / "dst"
    assert total_size(src, RECORDINGS) == len(b"video-one") + len(b"osd-one") + len(b"video-two!")

    copy_tree(src, dst, files=RECORDINGS)
    assert _names(dst) == RECORDINGS
    verify(src, dst, files=RECORDINGS)  # must not raise

    delete_files(src, RECORDINGS)
    assert _names(src) == LEFT_ON_THE_UNIT
    assert (src / "System Volume Information" / "WPSettings.dat").is_file()


def test_verify_with_files_detects_a_missing_copy(tmp_path):
    src = _make_walksnail(tmp_path / "walk")
    dst = tmp_path / "dst"
    copy_tree(src, dst, files=RECORDINGS)
    (dst / "VID0001.osd").unlink()
    with pytest.raises(VerificationError):
        verify(src, dst, files=RECORDINGS)


def test_delete_files_never_removes_a_folder_or_leaves_the_root(tmp_path):
    src = tmp_path / "walk"
    (src / "VID0001.mp4").mkdir(parents=True)  # a folder named like a recording
    (src / "VID0001.mp4" / "keep.txt").write_bytes(b"k")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not on the unit")
    delete_files(src, ["VID0001.mp4", "../outside.txt", "missing.osd"])
    assert (src / "VID0001.mp4" / "keep.txt").is_file()
    assert outside.is_file()


def test_perform_transfer_root_media_full_cycle(tmp_path):
    src = _make_walksnail(tmp_path / "walk")
    target = tmp_path / "sd"
    target.mkdir()

    dest = perform_transfer(src, target, "Walksnail", _hub(), _config(), root_media=True)

    assert dest.name == "transfer_0001_Walksnail"
    assert _names(dest) == RECORDINGS
    assert (dest / "VID0002.mp4").read_bytes() == b"video-two!"
    # Only what was copied and verified left the unit; its own files stayed.
    assert _names(src) == LEFT_ON_THE_UNIT


def test_perform_transfer_root_media_without_recordings_keeps_everything(tmp_path):
    src = tmp_path / "walk"
    src.mkdir()
    (src / "Avatar_version.txt").write_bytes(b"fw 1.0")
    (src / "VID0009.osd").write_bytes(b"orphan")
    target = tmp_path / "sd"
    target.mkdir()
    with pytest.raises(TransferError):
        perform_transfer(src, target, "Walksnail", _hub(), _config(), root_media=True)
    assert _names(src) == ["Avatar_version.txt", "VID0009.osd"]
    assert _names(target) == []


def test_perform_transfer_root_media_ignores_a_dcim_folder(tmp_path):
    # A root-media source is copied from its root; a DCIM folder on it is not
    # its media location and is left alone.
    src = _make_walksnail(tmp_path / "walk")
    _make_dcim(src, {"100MEDIA/other.mp4": b"not a root recording"})
    target = tmp_path / "sd"
    target.mkdir()
    dest = perform_transfer(src, target, "Walksnail", _hub(), _config(), root_media=True)
    assert _names(dest) == RECORDINGS
    assert (src / "DCIM" / "100MEDIA" / "other.mp4").is_file()
