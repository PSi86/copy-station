import os
from pathlib import Path

import pytest

from copystation.devices import (
    DeviceWatcher,
    InvalidLayoutError,
    NoSourceError,
    NoTargetError,
    Probe,
    device_views,
    fill_fraction_for_display,
    has_empty_source,
    has_source,
    order_for_display,
    select_roles,
)
from copystation.status import Event

GB = 1024**3
MIN_BYTES = 6 * GB


def _probe(name, capacity, has_dcim, matched_source=True, free=None,
           has_media=True, is_empty=None, has_label=False):
    # Mirror the production rule: empty = no media AND (has a media folder OR the
    # medium is blank). Tests for a blank medium (no DCIM) pass is_empty=True.
    if is_empty is None:
        is_empty = (not has_media) and has_dcim
    return Probe(
        sys_name=name,
        device_node=f"/dev/{name}",
        mountpoint=Path(f"/run/copystation/mnt/{name}"),
        has_dcim=has_dcim,
        matched_source=matched_source,
        capacity=capacity,
        free=capacity if free is None else free,
        name=name,
        has_media=has_media,
        is_empty=is_empty,
        has_label=has_label,
    )


def test_order_independent_source_target():
    cam = _probe("cam", 23 * GB, has_dcim=True)
    sd = _probe("sd", 256 * GB, has_dcim=False)

    src1, tgt1 = select_roles([cam, sd], MIN_BYTES)
    src2, tgt2 = select_roles([sd, cam], MIN_BYTES)

    assert src1.sys_name == src2.sys_name == "cam"
    assert tgt1.sys_name == tgt2.sys_name == "sd"


def test_empty_dcim_is_not_a_source():
    # A device whose DCIM folder is empty must not be picked as source.
    empty_cam = _probe("cam", 23 * GB, has_dcim=True, has_media=False)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    with pytest.raises(NoSourceError):
        select_roles([empty_cam, sd], MIN_BYTES)


def test_empty_dcim_source_skipped_for_a_nonempty_one():
    empty = _probe("empty", 23 * GB, has_dcim=True, has_media=False)
    full = _probe("full", 64 * GB, has_dcim=True, has_media=True)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    src, tgt = select_roles([empty, full, sd], MIN_BYTES)
    assert src.sys_name == "full"   # the empty DCIM device is never the source
    assert tgt.sys_name == "sd"


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


def test_has_empty_source():
    empty = _probe("cam", 23 * GB, has_dcim=True, has_media=False)
    full = _probe("cam2", 23 * GB, has_dcim=True, has_media=True)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    assert has_empty_source([empty, sd]) is True
    assert has_empty_source([full, sd]) is False       # a source with media exists
    assert has_empty_source([empty, full, sd]) is False  # one full source is enough
    assert has_empty_source([sd]) is False              # no source-shaped volume
    # A DCIM device that fails the VID/PID allowlist is not "source-shaped".
    foreign = _probe("x", 23 * GB, has_dcim=True, has_media=False, matched_source=False)
    assert has_empty_source([foreign, sd]) is False


def test_has_source():
    full = _probe("cam", 23 * GB, has_dcim=True, has_media=True)
    empty = _probe("cam2", 23 * GB, has_dcim=True, has_media=False)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    assert has_source([full, sd]) is True            # a usable source exists
    assert has_source([empty, sd]) is False          # source-shaped but empty
    assert has_source([sd]) is False                 # no source-shaped volume
    # Two completely blank cards -> no source at all -> wait, don't error.
    blank_a = _probe("a", 64 * GB, has_dcim=False, has_media=False, is_empty=True)
    blank_b = _probe("b", 256 * GB, has_dcim=False, has_media=False, is_empty=True)
    assert has_source([blank_a, blank_b]) is False
    # A DCIM device that fails the VID/PID allowlist is not a source.
    foreign = _probe("x", 23 * GB, has_dcim=True, has_media=True, matched_source=False)
    assert has_source([foreign, sd]) is False


def test_order_for_display_priority():
    # Prio 1: configured label. Prio 2: source-shaped (DCIM + media). Prio 3:
    # everything else by descending size. Ignored (< min) last.
    labelled = _probe("o4", 30 * GB, has_dcim=True, has_media=True, has_label=True)
    source = _probe("cam", 23 * GB, has_dcim=True, has_media=True)
    big_blank = _probe("bigsd", 256 * GB, has_dcim=False, has_media=False, is_empty=True)
    small_blank = _probe("smallsd", 64 * GB, has_dcim=False, has_media=False, is_empty=True)
    tiny = _probe("boot", 1 * GB, has_dcim=True, has_media=True)  # below min -> last

    order = [p.sys_name for p in order_for_display(
        [small_blank, tiny, source, big_blank, labelled], MIN_BYTES)]
    assert order == ["o4", "cam", "bigsd", "smallsd", "boot"]


def test_order_for_display_label_beats_source_and_size():
    # A configured label outranks a source-shaped device even if larger ones exist.
    label_small = _probe("o4", 8 * GB, has_dcim=False, has_media=False,
                         is_empty=True, has_label=True)
    source_big = _probe("cam", 200 * GB, has_dcim=True, has_media=True)
    assert [p.sys_name for p in order_for_display([source_big, label_small], MIN_BYTES)] \
        == ["o4", "cam"]


def test_device_views_are_ordered_by_priority():
    # The web/e-paper list comes out ordered (best candidates first).
    blank = _probe("sd", 256 * GB, has_dcim=False, has_media=False, is_empty=True)
    source = _probe("cam", 23 * GB, has_dcim=True, has_media=True)
    names = [v["name"] for v in device_views([blank, source], MIN_BYTES)]
    assert names == ["cam", "sd"]   # the source-shaped volume is shown first


def test_fill_fraction_prefers_source_and_reflects_usage():
    # 23 GB source, 8 GB free -> 15/23 used.
    cam = _probe("cam", 23 * GB, has_dcim=True, free=8 * GB)
    sd = _probe("sd", 256 * GB, has_dcim=False, free=200 * GB)
    frac = fill_fraction_for_display([cam, sd])
    assert abs(frac - (23 - 8) / 23) < 1e-9   # the source's fill, not the SD's


def test_fill_fraction_single_device_and_empty():
    sd = _probe("sd", 256 * GB, has_dcim=False, free=64 * GB)
    assert abs(fill_fraction_for_display([sd]) - (256 - 64) / 256) < 1e-9
    assert fill_fraction_for_display([]) is None


def test_fill_fraction_clamps_and_handles_zero_capacity():
    full = _probe("cam", 10 * GB, has_dcim=True, free=0)
    assert fill_fraction_for_display([full]) == 1.0
    zero = Probe(
        sys_name="x", device_node="/dev/x", mountpoint=Path("/x"),
        has_dcim=True, matched_source=True, capacity=0, free=0, name="x", has_media=True,
    )
    assert fill_fraction_for_display([zero]) == 0.0


def _probe_with_node(name, node):
    # Mount on the node's parent (a real, existing dir in the tests) so the
    # liveness checks in _both_present don't spuriously fail.
    return Probe(
        sys_name=name, device_node=str(node), mountpoint=Path(node).parent,
        has_dcim=True, matched_source=True, capacity=GB, free=0, name=name, has_media=True,
    )


def test_hold_before_copy_returns_source_size_when_present(tmp_path, monkeypatch):
    import copystation.devices as dev

    monkeypatch.setattr(dev, "FILL_GAUGE_SECONDS", 0.05)  # keep the test quick
    src = tmp_path / "sdc"; src.write_bytes(b"")
    tgt = tmp_path / "sdd"; tgt.write_bytes(b"")
    media = tmp_path / "DCIM"; media.mkdir()
    (media / "clip.mp4").write_bytes(b"x" * 16)  # the size scanned during the hold
    w = _watcher()
    assert w._hold_before_copy(
        _probe_with_node("cam", src), _probe_with_node("sd", tgt), media
    ) == 16


def test_hold_before_copy_bails_when_a_device_is_gone(tmp_path, monkeypatch):
    import copystation.devices as dev

    monkeypatch.setattr(dev, "FILL_GAUGE_SECONDS", 5.0)  # long, but must bail at once
    missing = tmp_path / "sdc"            # never created
    tgt = tmp_path / "sdd"; tgt.write_bytes(b"")
    media = tmp_path / "DCIM"; media.mkdir()
    (media / "clip.mp4").write_bytes(b"x")
    w = _watcher()
    assert w._hold_before_copy(
        _probe_with_node("cam", missing), _probe_with_node("sd", tgt), media
    ) is None


class _RecordingHub:
    """Captures log_event / signal calls for the detection-flow tests."""

    def __init__(self):
        self.events = []
        self.signals = []

    def log_event(self, message, level="info"):
        self.events.append((message, level))

    def signal(self, event):
        self.signals.append(event)


def test_detected_devices_emit_one_signal_each():
    w = _watcher()
    w._hub = _RecordingHub()
    w._prev_nodes = set()
    w._node_names = {}

    cam = _probe("cam", 23 * GB, has_dcim=True)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    added, removed = w._log_device_changes([cam, sd])

    assert added and not removed
    # One green "detected" blink per newly recognised volume.
    assert w._hub.signals.count(Event.DEVICE_DETECTED) == 2

    # Re-evaluating the same set emits nothing new (no spurious re-blink).
    w._hub.signals.clear()
    w._log_device_changes([cam, sd])
    assert w._hub.signals == []


class _FakeDevice:
    """Minimal stand-in for a pyudev device (.get / .sys_name / .find_parent)."""

    def __init__(self, sys_name, parent=None, **props):
        self.sys_name = sys_name
        self._props = props
        self._parent = parent

    def get(self, key, default=None):
        return self._props.get(key, default)

    def find_parent(self, subsystem, device_type=None):
        return self._parent


def _watcher(root_dev="mmcblk1"):
    # Bypass __init__ (which imports pyudev) -- _is_candidate only needs _root_dev.
    watcher = DeviceWatcher.__new__(DeviceWatcher)
    watcher._root_dev = root_dev
    return watcher


def test_candidate_accepts_usb_partition():
    w = _watcher()
    dev = _FakeDevice("sda1", DEVTYPE="partition", ID_BUS="usb", ID_FS_TYPE="exfat")
    assert w._is_candidate(dev) is True


def test_candidate_accepts_partitionless_usb_disk():
    # DJI O4 Air Unit: whole-disk filesystem, no partition table (sdc, no sdc1).
    w = _watcher()
    dev = _FakeDevice("sdc", DEVTYPE="disk", ID_BUS="usb", ID_FS_TYPE="vfat")
    assert w._is_candidate(dev) is True


def test_candidate_rejects_partitioned_disk_node():
    # A disk that carries a partition table is handled via its partitions, not
    # the whole-disk node.
    w = _watcher()
    dev = _FakeDevice("sdb", DEVTYPE="disk", ID_BUS="usb", ID_PART_TABLE_TYPE="dos")
    assert w._is_candidate(dev) is False


def test_candidate_rejects_disk_without_filesystem():
    w = _watcher()
    dev = _FakeDevice("sdd", DEVTYPE="disk", ID_BUS="usb")
    assert w._is_candidate(dev) is False


def test_candidate_rejects_non_usb():
    w = _watcher()
    dev = _FakeDevice("sdc", DEVTYPE="disk", ID_BUS="ata", ID_FS_TYPE="vfat")
    assert w._is_candidate(dev) is False


def test_candidate_rejects_root_disk():
    w = _watcher(root_dev="sdc")
    dev = _FakeDevice("sdc", DEVTYPE="disk", ID_BUS="usb", ID_FS_TYPE="vfat")
    assert w._is_candidate(dev) is False


def test_device_views_reflect_actual_decision():
    cam = _probe("cam", 23 * GB, has_dcim=True)
    sd = _probe("sd", 256 * GB, has_dcim=True)  # both carry DCIM
    tiny = _probe("boot", 1 * GB, has_dcim=False)

    # The roles come from select_roles, not from per-device guessing.
    source, target = select_roles([cam, sd], MIN_BYTES)
    views = {v["name"]: v for v in device_views([cam, sd, tiny], MIN_BYTES, source, target)}

    assert views["cam"]["role"] == "source"  # smaller DCIM volume
    assert views["sd"]["role"] == "target"   # larger, even though it has DCIM
    assert views["boot"]["role"] == "ignored"
    assert views["boot"]["eligible"] is False


def test_device_views_expose_capacity_and_free():
    # The web UI derives used storage as capacity - free, so both must be present.
    dev = _probe("cam", 23 * GB, has_dcim=True, free=8 * GB)
    [view] = device_views([dev], MIN_BYTES)
    assert view["capacity"] == 23 * GB
    assert view["free"] == 8 * GB


def test_device_views_marks_empty_source():
    # A source-shaped device with an empty DCIM shows as "empty" (no copy).
    empty = _probe("cam", 23 * GB, has_dcim=True, has_media=False)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    views = {v["name"]: v for v in device_views([empty, sd], MIN_BYTES)}
    assert views["cam"]["role"] == "empty"
    assert views["sd"]["role"] == "candidate"


def test_device_views_source_flips_to_empty_when_cleared():
    tgt = _probe("sd", 256 * GB, has_dcim=False)
    # During the copy the chosen source still has media -> "source".
    src = _probe("cam", 23 * GB, has_dcim=True, has_media=True)
    v = {x["name"]: x for x in device_views([src, tgt], MIN_BYTES, src, tgt)}
    assert v["cam"]["role"] == "source"
    assert v["sd"]["role"] == "target"
    # After cleanup the same chosen source is empty -> "empty" (overrides source).
    cleared = _probe("cam", 23 * GB, has_dcim=True, has_media=False)
    v2 = {x["name"]: x for x in device_views([cleared, tgt], MIN_BYTES, cleared, tgt)}
    assert v2["cam"]["role"] == "empty"
    assert v2["sd"]["role"] == "target"


def test_device_views_target_with_empty_dcim_stays_target():
    # An empty-DCIM device used as TARGET must remain "target", never "empty".
    src = _probe("cam", 23 * GB, has_dcim=True, has_media=True)
    empty_target = _probe("big", 256 * GB, has_dcim=True, has_media=False)
    v = {x["name"]: x for x in device_views([src, empty_target], MIN_BYTES, src, empty_target)}
    assert v["big"]["role"] == "target"
    assert v["cam"]["role"] == "source"


@pytest.mark.skipif(not hasattr(os, "statvfs"), reason="statvfs is Linux-only")
def test_restat_reflects_free_and_media(tmp_path):
    from copystation.config import Config

    w = _watcher()
    w._config = Config()
    mp = tmp_path / "mnt"
    (mp / "DCIM").mkdir(parents=True)
    (mp / "DCIM" / "clip.mp4").write_bytes(b"x" * 16)
    probe = Probe(
        sys_name="sdb1", device_node="/dev/sdb1", mountpoint=mp,
        has_dcim=False, matched_source=True, capacity=0, free=0, name="x", has_media=False,
    )
    r = w._restat(probe)
    assert r.has_dcim is True and r.has_media is True
    assert r.capacity > 0 and r.free > 0
    # Clearing the DCIM contents -> empty.
    (mp / "DCIM" / "clip.mp4").unlink()
    assert w._restat(probe).has_media is False


def test_dcim_has_media(tmp_path):
    from copystation.devices import _dcim_has_media

    dcim = tmp_path / "DCIM"
    dcim.mkdir()
    assert _dcim_has_media(dcim) is False          # empty folder
    (dcim / "100MEDIA").mkdir()
    assert _dcim_has_media(dcim) is False           # empty subfolder, still no file
    (dcim / "100MEDIA" / "clip.mp4").write_bytes(b"x")
    assert _dcim_has_media(dcim) is True            # a media file exists


def test_device_views_without_decision_are_candidates():
    # While only one volume is present no source/target split is known yet.
    cam = _probe("cam", 23 * GB, has_dcim=True)
    [view] = device_views([cam], MIN_BYTES)
    assert view["role"] == "candidate"


def test_device_views_extra_eligible_is_unused():
    cam = _probe("cam", 23 * GB, has_dcim=True)
    sd = _probe("sd", 256 * GB, has_dcim=False)
    other = _probe("other", 64 * GB, has_dcim=False)
    source, target = select_roles([cam, sd, other], MIN_BYTES)
    views = {v["name"]: v for v in device_views([cam, sd, other], MIN_BYTES, source, target)}
    assert views["other"]["role"] == "unused"


class _FakeMonitor:
    """Returns queued events on poll(), then None (a quiet bus)."""

    def __init__(self, events):
        self._events = list(events)
        self.polls = 0

    def poll(self, timeout=None):
        self.polls += 1
        return self._events.pop(0) if self._events else None


def test_settle_drains_burst_then_returns():
    w = _watcher()
    w._config = {"settle_seconds": 2.0, "settle_quiet_seconds": 0.3}
    mon = _FakeMonitor([object(), object()])  # two follow-up events, then quiet
    w._settle(mon)
    # Two events consumed, plus one quiet poll that returned None.
    assert mon.polls == 3


def test_settle_returns_immediately_when_quiet():
    w = _watcher()
    w._config = {"settle_seconds": 2.0, "settle_quiet_seconds": 0.3}
    mon = _FakeMonitor([])  # bus already quiet
    w._settle(mon)
    assert mon.polls == 1


# ----- Theme 2: junk-robust media / blank-medium / empty flag -------------------


def test_dcim_has_media_ignores_junk(tmp_path):
    from copystation.devices import _dcim_has_media

    dcim = tmp_path / "DCIM"
    (dcim / "100MEDIA").mkdir(parents=True)
    (dcim / ".DS_Store").write_bytes(b"x")
    (dcim / "100MEDIA" / "Thumbs.db").write_bytes(b"x")
    assert _dcim_has_media(dcim) is False           # only junk -> nothing to copy
    (dcim / "100MEDIA" / "clip.mp4").write_bytes(b"x")
    assert _dcim_has_media(dcim) is True             # a real media file


def test_medium_is_blank_ignores_junk(tmp_path):
    from copystation.devices import _medium_is_blank

    mp = tmp_path / "mnt"
    mp.mkdir()
    assert _medium_is_blank(mp) is True              # nothing at all
    (mp / "System Volume Information").mkdir()
    (mp / ".Trashes").mkdir()
    assert _medium_is_blank(mp) is True              # only junk
    (mp / "Photos").mkdir()
    assert _medium_is_blank(mp) is False             # a real folder = content


def test_device_views_blank_medium_is_empty():
    # A completely blank card (no media folder, no real content) -> "empty",
    # so it is never picked as a source.
    blank = _probe("sd", 256 * GB, has_dcim=False, has_media=False, is_empty=True)
    [view] = device_views([blank], MIN_BYTES)
    assert view["role"] == "empty"


def test_device_views_card_with_content_but_no_dcim_is_candidate():
    # Content present but no media folder -> not "empty", a plain candidate.
    card = _probe("sd", 256 * GB, has_dcim=False, has_media=False, is_empty=False)
    [view] = device_views([card], MIN_BYTES)
    assert view["role"] == "candidate"


# ----- Theme 1: the event set that drives re-evaluation -------------------------


class _Event:
    def __init__(self, action, **props):
        self.action = action
        self._props = props

    def get(self, key, default=None):
        return self._props.get(key, default)


def _polls_until_return(events):
    w = _watcher()
    mon = _FakeMonitor(events)
    w._wait_for_change(mon)
    return mon.polls


def test_wait_for_change_triggers_on_the_right_events():
    assert _polls_until_return([_Event("add")]) == 1
    assert _polls_until_return([_Event("remove")]) == 1
    # A pulled card in a reader shows up as a media-change, not a prompt remove.
    assert _polls_until_return([_Event("change", DISK_MEDIA_CHANGE="1")]) == 1
    # A benign change is skipped; the following remove is what returns.
    assert _polls_until_return([_Event("change"), _Event("remove")]) == 2


def test_current_partitions_filters_dead_lingering_nodes(monkeypatch):
    import copystation.devices as dev

    w = _watcher()
    live = _FakeDevice("sdb1", DEVTYPE="partition", ID_BUS="usb", ID_FS_TYPE="exfat")
    live.device_node = "/dev/sdb1"
    dead = _FakeDevice("sdc1", DEVTYPE="partition", ID_BUS="usb", ID_FS_TYPE="exfat")
    dead.device_node = "/dev/sdc1"  # node lingers, but the kernel zeroed it

    class _Ctx:
        def list_devices(self, **kw):
            return [live, dead]

    w._context = _Ctx()
    monkeypatch.setattr(dev, "volume_alive", lambda node, *a, **k: node != "/dev/sdc1")
    assert [d.sys_name for d in w._current_partitions()] == ["sdb1"]


def test_configured_label_matches_vid_pid():
    w = _watcher()
    w._config = {
        "identify": {
            "device_labels": [
                {"vid": "2ca3", "pid": "0020", "name": "O4 Lite"},
                {"vid": "2ca3", "pid": "0021", "name": "O4 Pro"},
            ]
        }
    }
    lite = _FakeDevice("sdc", ID_VENDOR_ID="2ca3", ID_MODEL_ID="0020")
    pro = _FakeDevice("sdc", ID_VENDOR_ID="2ca3", ID_MODEL_ID="0021")
    other = _FakeDevice("sdc", ID_VENDOR_ID="abcd", ID_MODEL_ID="1234", ID_FS_LABEL="MYCARD")

    assert w._volume_name(lite) == "O4 Lite"
    assert w._volume_name(pro) == "O4 Pro"
    # No mapping match -> fall back to the filesystem label.
    assert w._volume_name(other) == "MYCARD"


def test_usb_ids_fall_back_to_usb_parent():
    # The whole-disk node carries no VID/PID; the USB ancestor does.
    parent = _FakeDevice("usb1", ID_VENDOR_ID="2ca3", ID_MODEL_ID="0020")
    disk = _FakeDevice("sdc", ID_FS_LABEL="InternalSto", parent=parent)
    assert _watcher()._usb_ids(disk) == ("2ca3", "0020")


def test_default_config_labels_o4_lite():
    # A fresh install (default config) must name the O4 Lite, not "InternalSto".
    from copystation.config import Config

    w = _watcher()
    w._config = Config()  # defaults only
    o4 = _FakeDevice("sdc", ID_VENDOR_ID="2ca3", ID_MODEL_ID="0020", ID_FS_LABEL="InternalSto")
    assert w._volume_name(o4) == "O4 Lite"

    # ... and via the USB parent when the disk node lacks the IDs.
    parent = _FakeDevice("usb1", ID_VENDOR_ID="2ca3", ID_MODEL_ID="0020")
    o4_disk = _FakeDevice("sdc", ID_FS_LABEL="InternalSto", parent=parent)
    assert w._volume_name(o4_disk) == "O4 Lite"


def test_detecting_after_success_clears_stale_role_storage(monkeypatch):
    # After a successful copy the SUCCESS view keeps source/target figures.
    # Pulling the source must not leave them standing next to the DETECTING
    # phase -- the panel would otherwise render the "Done" panes forever.
    from copystation.config import Config
    from copystation.state import StationState, StatusHub, StorageInfo
    from copystation.status import State, StatusIndicator

    state = StationState()
    hub = StatusHub(state, StatusIndicator())
    w = _watcher()
    w._hub = hub
    w._config = Config()
    w._errored = False
    w._armed = False                      # the copy for this set already ran
    w._prev_nodes = {"/dev/cam", "/dev/sd"}
    w._node_names = {"/dev/cam": "cam", "/dev/sd": "sd"}

    hub.set_storage(
        StorageInfo("cam", 23 * GB, 10 * GB, 13 * GB),
        StorageInfo("sd", 256 * GB, 40 * GB, 216 * GB),
    )
    state.set_phase(State.SUCCESS)

    sd = _probe("sd", 256 * GB, has_dcim=False)   # only the target remains
    monkeypatch.setattr(DeviceWatcher, "_current_partitions", lambda self: [object()])
    monkeypatch.setattr(DeviceWatcher, "_probe_device", lambda self, dev, base: sd)

    w._evaluate()

    snap = state.snapshot()
    assert snap["phase"] == "detecting"
    assert snap["source"]["capacity"] == 0        # stale roles are gone
    assert snap["target"]["capacity"] == 0
    assert [d["name"] for d in snap["devices"]] == ["sd"]
