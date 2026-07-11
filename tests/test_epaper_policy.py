from copystation.status.epaper.model import DeviceView, StorageView, ViewModel
from copystation.status.epaper.policy import Decision, decide


def _vm(
    *,
    phase="copying",
    percent=50,
    src=(12, 32),
    tgt=(120, 256),
    devices=2,
    error="",
    ap=False,
):
    def storage(t):
        used, cap = t
        return StorageView(label="x", used=used, capacity=cap)

    devs = tuple(
        DeviceView(name=f"dev{i}", role="candidate", used=1, capacity=2)
        for i in range(devices)
    )
    return ViewModel(
        status_text=phase.title(),
        phase=phase,
        percent=percent,
        progress_fraction=percent / 100.0,
        show_progress=phase in ("copying", "success"),
        source=storage(src),
        target=storage(tgt),
        devices=devs,
        device_count=len(devs),
        speed_text="1 MB/s",
        eta_text="0:10",
        error_text=error,
        version="0.1.0",
        ap_active=ap,
    )


_KW = dict(
    partials_since_full=0,
    seconds_since_last=999.0,
    full_refresh_every=20,
    partial_min_interval=2.0,
)


def test_first_frame_is_full():
    assert decide(None, _vm(), **_KW) is Decision.FULL


def test_phase_change_is_full():
    prev = _vm(phase="detecting", percent=0)
    assert decide(prev, _vm(phase="copying", percent=1), **_KW) is Decision.FULL


def test_device_removed_is_full():
    prev = _vm(devices=2)
    assert decide(prev, _vm(devices=1), **_KW) is Decision.FULL


def test_device_appears_is_partial():
    # A freshly detected device drawn onto a blank area is additive -> partial.
    prev = _vm(phase="detecting", percent=0, devices=0)
    new = _vm(phase="detecting", percent=0, devices=1)
    assert decide(prev, new, **_KW) is Decision.PARTIAL


def test_progress_reset_is_full():
    prev = _vm(percent=80)
    assert decide(prev, _vm(percent=10), **_KW) is Decision.FULL


def test_storage_shrink_is_full():
    prev = _vm(src=(20, 32))
    assert decide(prev, _vm(src=(12, 32)), **_KW) is Decision.FULL


def test_no_change_is_skip():
    prev = _vm(percent=50)
    assert decide(prev, _vm(percent=50), **_KW) is Decision.SKIP


def test_additive_but_throttled_is_skip():
    prev = _vm(percent=50)
    kw = {**_KW, "seconds_since_last": 0.5}  # below the 2 s interval
    assert decide(prev, _vm(percent=55), **kw) is Decision.SKIP


def test_additive_after_interval_is_partial():
    prev = _vm(percent=50)
    assert decide(prev, _vm(percent=55), **_KW) is Decision.PARTIAL


def test_budget_exhausted_forces_full():
    prev = _vm(percent=50)
    kw = {**_KW, "partials_since_full": 20}
    assert decide(prev, _vm(percent=55), **kw) is Decision.FULL


def test_clear_beats_budget_and_throttle():
    # A required clear (device removed) is a full refresh even when throttled.
    prev = _vm(devices=2)
    kw = {**_KW, "seconds_since_last": 0.0}
    assert decide(prev, _vm(devices=1), **kw) is Decision.FULL


def test_ap_enabled_is_partial():
    # The WiFi badge appearing draws black onto white -> a clean partial.
    assert decide(_vm(ap=False), _vm(ap=True), **_KW) is Decision.PARTIAL


def test_ap_disabled_is_full():
    # The badge must go white again -> only a full refresh erases it cleanly.
    assert decide(_vm(ap=True), _vm(ap=False), **_KW) is Decision.FULL


def test_queue_advance_triggers_redraw():
    import dataclasses

    base = _vm(phase="transcoding", percent=50)
    prev = dataclasses.replace(base, transcode_active=True, transcode_queue_text="2/5")
    new = dataclasses.replace(base, transcode_active=True, transcode_queue_text="3/5")
    # Advancing to the next file changes the signature -> not skipped.
    assert decide(prev, new, **_KW) is not Decision.SKIP


def test_auto_transcode_badge_enable_partial_disable_full():
    import dataclasses

    base = _vm(phase="ready", percent=0)
    off = dataclasses.replace(base, auto_transcode_active=False)
    on = dataclasses.replace(base, auto_transcode_active=True)
    # Enabling only adds darker pixels -> a clean partial refresh.
    assert decide(off, on, **_KW) is Decision.PARTIAL
    # Disabling must erase the badge (black -> white) -> a full refresh.
    assert decide(on, off, **_KW) is Decision.FULL
