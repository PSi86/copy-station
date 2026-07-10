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


def test_wifi_ap_currently_active_reflects_real_state(monkeypatch):
    from copystation.daemon import _wifi_ap_currently_active

    # Feature usable via wifi_ap.enabled -> queries the real nmcli state.
    monkeypatch.setattr(ap, "is_active", lambda cfg: True)
    assert _wifi_ap_currently_active({"wifi_ap": {"enabled": True}}) is True
    monkeypatch.setattr(ap, "is_active", lambda cfg: False)
    assert _wifi_ap_currently_active({"wifi_ap": {"enabled": True}}) is False

    # Usable via a button binding even though enabled is false (the reported bug:
    # AP raised by a button before a restart must still be detected).
    checked = {}
    monkeypatch.setattr(ap, "is_active", lambda cfg: checked.setdefault("hit", True))
    cfg = {"wifi_ap": {"enabled": False},
           "buttons": {"u1": {"enabled": True, "actions": {"triple_click": "wifi_ap"}}}}
    assert _wifi_ap_currently_active(cfg) is True
    assert checked == {"hit": True}

    # Not usable at all -> never touches nmcli.
    checked.clear()
    assert _wifi_ap_currently_active({"wifi_ap": {"enabled": False}}) is False
    assert checked == {}


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
