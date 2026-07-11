"""WiFi access point: nmcli command builders and control flow.

Pure/argument-level tests -- no nmcli is executed. The real subprocess calls are
field-validated on the device.
"""

import copystation.wifi_ap as ap
from copystation.buttons import _resolve_action


FULL = {
    "con_name": "copystation-ap",
    "ssid": "CS",
    "ifname": "wlan0",
    "autoconnect": True,
    "band": "bg",
    "channel": 6,
    "ipv4_address": "10.42.0.1/24",
    "password": "supersecret",
}


def test_add_cmd_full():
    assert ap.add_cmd(FULL) == [
        "nmcli", "connection", "add", "type", "wifi",
        "con-name", "copystation-ap",
        "ifname", "wlan0",
        "ssid", "CS",
        "autoconnect", "yes",
        "802-11-wireless.mode", "ap",
        "ipv4.method", "shared",
        "802-11-wireless.band", "bg",
        "802-11-wireless.channel", "6",
        "ipv4.addresses", "10.42.0.1/24",
        "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "supersecret",
    ]


def test_add_cmd_defaults_ifname_any_and_no_security_without_password():
    cfg = {"ssid": "Open", "password": "", "autoconnect": False, "band": "", "channel": None,
           "ipv4_address": ""}
    cmd = ap.add_cmd(cfg)
    assert cmd[:5] == ["nmcli", "connection", "add", "type", "wifi"]
    assert "ifname" in cmd and cmd[cmd.index("ifname") + 1] == "*"
    assert cmd[cmd.index("autoconnect") + 1] == "no"
    assert "wifi-sec.key-mgmt" not in cmd  # no password -> open profile args
    assert "802-11-wireless.band" not in cmd
    assert "ipv4.addresses" not in cmd


def test_simple_cmd_builders():
    assert ap.con_name(FULL) == "copystation-ap"
    assert ap.delete_cmd(FULL) == ["nmcli", "connection", "delete", "copystation-ap"]
    assert ap.up_cmd(FULL) == ["nmcli", "connection", "up", "copystation-ap"]
    assert ap.down_cmd(FULL) == ["nmcli", "connection", "down", "copystation-ap"]
    assert ap.con_name({}) == "copystation-ap"  # default name


def test_start_ap_refuses_short_password(monkeypatch):
    calls = []
    monkeypatch.setattr(ap, "_run", lambda cmd, check=True: calls.append(cmd))
    assert ap.start_ap({"password": "short"}) is False  # < 8 chars
    assert calls == []  # nmcli never invoked


def test_start_ap_creates_and_brings_up(monkeypatch):
    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)

        class R:
            stdout = ""

        return R()

    monkeypatch.setattr(ap, "_run", fake_run)
    assert ap.start_ap(FULL) is True
    verbs = [c[:3] for c in calls]
    assert ["nmcli", "connection", "delete"] in verbs
    assert ["nmcli", "connection", "add"] in verbs
    assert ["nmcli", "connection", "up"] in verbs


def test_toggle_down_when_active_returns_false(monkeypatch):
    monkeypatch.setattr(ap, "is_active", lambda cfg: True)
    seen = {}
    monkeypatch.setattr(ap, "down", lambda cfg: seen.setdefault("down", True))
    monkeypatch.setattr(ap, "start_ap", lambda cfg: seen.setdefault("up", True))
    assert ap.toggle(FULL) is False  # now down
    assert seen == {"down": True}


def test_toggle_up_when_inactive_returns_true(monkeypatch):
    monkeypatch.setattr(ap, "is_active", lambda cfg: False)
    seen = {}
    monkeypatch.setattr(ap, "down", lambda cfg: seen.setdefault("down", True))
    monkeypatch.setattr(ap, "start_ap", lambda cfg: seen.setdefault("up", True) or True)
    assert ap.toggle(FULL) is True  # now up
    assert seen == {"up": True}


def test_button_wifi_ap_action_toggles(monkeypatch):
    seen = {}
    monkeypatch.setattr(ap, "toggle", lambda cfg: seen.setdefault("cfg", cfg) or True)
    action = _resolve_action("b", "triple_click", "wifi_ap", {"wifi_ap": FULL})
    assert callable(action)
    action()
    assert seen["cfg"] == FULL


def test_set_active_up_and_down(monkeypatch):
    seen = {}
    monkeypatch.setattr(ap, "start_ap", lambda cfg: True)
    monkeypatch.setattr(ap, "down", lambda cfg: seen.setdefault("down", True))
    assert ap.set_active(FULL, True) is True
    assert ap.set_active(FULL, False) is False
    assert seen == {"down": True}


import types  # noqa: E402


class _FakeHub:
    def __init__(self, ap_active=False):
        self.ap = ap_active
        self.signals = []
        self.events = []
        self.state = types.SimpleNamespace(ap_active=ap_active)

    def set_ap_active(self, active):
        self.ap = active
        self.state.ap_active = active
        self.events.append(("display", active))

    def signal(self, event):
        self.signals.append(event)
        self.events.append(("led", event))


def test_button_wifi_ap_feedback_precedes_slow_nmcli(monkeypatch):
    from copystation.status import Event

    hub = _FakeHub(ap_active=False)
    monkeypatch.setattr(ap, "set_active",
                        lambda cfg, active: hub.events.append(("nmcli", active)) or active)
    _resolve_action("b", "triple_click", "wifi_ap", {"wifi_ap": FULL}, hub)()

    assert hub.ap is True
    assert hub.signals == [Event.AP_ENABLED]
    # The display badge and the LED code are applied BEFORE the slow nmcli call.
    assert hub.events == [("display", True), ("led", Event.AP_ENABLED), ("nmcli", True)]


def test_button_wifi_ap_flips_off_when_active(monkeypatch):
    from copystation.status import Event

    hub = _FakeHub(ap_active=True)  # AP currently up
    monkeypatch.setattr(ap, "set_active", lambda cfg, active: active)
    _resolve_action("b", "triple_click", "wifi_ap", {"wifi_ap": FULL}, hub)()
    assert hub.ap is False
    assert hub.signals == [Event.AP_DISABLED]


def test_button_wifi_ap_reconciles_when_bringup_fails(monkeypatch):
    from copystation.status import Event

    hub = _FakeHub(ap_active=False)
    monkeypatch.setattr(ap, "set_active", lambda cfg, active: False)  # bring-up fails
    _resolve_action("b", "triple_click", "wifi_ap", {"wifi_ap": FULL}, hub)()
    # Optimistically shown on, then reconciled back to off; the press was still
    # acknowledged with the enable blink.
    assert hub.ap is False
    assert hub.signals == [Event.AP_ENABLED]


def test_wifi_ap_bound_to_button_detection():
    from copystation.daemon import _wifi_ap_bound_to_button

    on = {"buttons": {"u1": {"enabled": True, "actions": {"triple_click": "wifi_ap"}}}}
    off = {"buttons": {"u1": {"enabled": False, "actions": {"triple_click": "wifi_ap"}}}}
    other = {"buttons": {"u1": {"enabled": True, "actions": {"hold": "poweroff"}}}}
    assert _wifi_ap_bound_to_button(on) is True
    assert _wifi_ap_bound_to_button(off) is False
    assert _wifi_ap_bound_to_button(other) is False
    assert _wifi_ap_bound_to_button({}) is False


def _ap_section(tmp_path):
    from copystation.settings_store import SettingsStore

    return SettingsStore(str(tmp_path / "user-settings.json")).section("wifi_ap")


def test_effective_ap_enabled_overlay_wins_over_config(tmp_path):
    from copystation.config import Config
    from copystation.daemon import _effective_ap_enabled

    store = _ap_section(tmp_path)

    # No overlay yet -> the config value applies.
    cfg_on = Config({"wifi_ap": {"enabled": True}})
    cfg_off = Config({"wifi_ap": {"enabled": False}})
    assert _effective_ap_enabled(cfg_on, store) is True
    assert _effective_ap_enabled(cfg_off, store) is False

    # A persisted overlay wins over the config either way (a runtime toggle
    # survives a restart independent of wifi_ap.enabled).
    store.update(enabled=False)
    assert _effective_ap_enabled(cfg_on, store) is False
    store.update(enabled=True)
    assert _effective_ap_enabled(cfg_off, store) is True


class _ApHub:
    """Minimal hub for the wifi_ap button action tests."""

    class _State:
        ap_active = False

    def __init__(self):
        self.state = self._State()
        self.events = []

    def set_ap_active(self, value):
        self.state.ap_active = bool(value)

    def signal(self, event):
        self.events.append(event)


def test_wifi_ap_button_persists_state(monkeypatch, tmp_path):
    from copystation.buttons import wifi_ap_toggle_action

    hub = _ApHub()
    store = _ap_section(tmp_path)
    monkeypatch.setattr(ap, "set_active", lambda cfg, desired: desired)  # bring-up ok

    action = wifi_ap_toggle_action({"wifi_ap": {}}, hub, store)
    action()  # off -> on: persisted so it survives a restart
    assert hub.state.ap_active is True and store.get("enabled") is True
    action()  # on -> off
    assert hub.state.ap_active is False and store.get("enabled") is False


def test_wifi_ap_button_persists_corrected_state_on_failed_bringup(monkeypatch, tmp_path):
    from copystation.buttons import wifi_ap_toggle_action

    hub = _ApHub()
    store = _ap_section(tmp_path)
    # The bring-up fails (e.g. no valid PSK): set_active reports the AP still down.
    monkeypatch.setattr(ap, "set_active", lambda cfg, desired: False)

    wifi_ap_toggle_action({"wifi_ap": {}}, hub, store)()  # tries on, fails
    # Both the display and the persisted state are corrected to the real (off) state.
    assert hub.state.ap_active is False and store.get("enabled") is False


def test_apply_wifi_ap_state_starts_or_reconciles(monkeypatch):
    from copystation.daemon import _apply_wifi_ap_state

    # want_up=True -> start_ap; return its result.
    monkeypatch.setattr(ap, "start_ap", lambda cfg: True)
    assert _apply_wifi_ap_state({"wifi_ap": {}}, True) is True

    # want_up=False while the AP is active (stale autoconnect) -> bring it down.
    calls = []
    monkeypatch.setattr(ap, "is_active", lambda cfg: True)
    monkeypatch.setattr(ap, "down", lambda cfg: calls.append("down") or True)
    assert _apply_wifi_ap_state({"wifi_ap": {}}, False) is False
    assert calls == ["down"]

    # want_up=False and already down -> nothing to do, no nmcli 'down'.
    calls.clear()
    monkeypatch.setattr(ap, "is_active", lambda cfg: False)
    assert _apply_wifi_ap_state({"wifi_ap": {}}, False) is False
    assert calls == []


def test_check_ap_web_reachability_warns_when_web_disabled(caplog):
    from copystation.config import Config
    from copystation.daemon import _check_ap_web_reachability

    cfg = Config()
    cfg.data["wifi_ap"]["enabled"] = True
    cfg.data["web"]["enabled"] = False
    with caplog.at_level("WARNING"):
        _check_ap_web_reachability(cfg, web_up=False)
    assert any("web.enabled is false" in r.message for r in caplog.records)


def test_check_ap_web_reachability_quiet_when_web_enabled(caplog):
    from copystation.config import Config
    from copystation.daemon import _check_ap_web_reachability

    cfg = Config()
    cfg.data["wifi_ap"]["enabled"] = True
    cfg.data["web"]["enabled"] = True
    with caplog.at_level("WARNING"):
        _check_ap_web_reachability(cfg, web_up=True)
    assert not any("web.enabled is false" in r.message for r in caplog.records)
