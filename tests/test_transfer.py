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
    dir_signature,
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


def test_abort_check_fires_on_dead_target_mount(monkeypatch, tmp_path):
    # Regression: a pulled target whose device node lingers (card-in-reader /
    # stale mount) must still be caught -- the mount stops answering even while
    # rsync buffers writes into the page cache.
    import os

    import copystation.transfer as transfer
    from copystation.daemon import _device_abort_check

    src = tmp_path / "src"
    src.mkdir()
    tgt = tmp_path / "tgt"
    tgt.mkdir()

    def _statvfs(path):
        if os.fspath(path) == os.fspath(tgt):
            raise OSError("Input/output error")
        return None

    monkeypatch.setattr(transfer.os, "statvfs", _statvfs, raising=False)
    # Both device nodes still resolve (real dirs); only the target's fs is dead.
    msg = _device_abort_check(str(src), str(tgt), src, tgt)()
    assert msg and "Target" in msg


def test_perform_transfer_dead_target_mount_keeps_source(monkeypatch, tmp_path):
    import os

    import copystation.transfer as transfer

    src = tmp_path / "camera"
    _make_dcim(src, {"100MEDIA/clip.mp4": b"video-data"})
    target = tmp_path / "sd"
    target.mkdir()

    def _statvfs(path):
        if os.fspath(path) == os.fspath(target):
            raise OSError("Input/output error")
        return None

    monkeypatch.setattr(transfer.os, "statvfs", _statvfs, raising=False)
    # The target device node still exists (real dir), but its filesystem is gone.
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
