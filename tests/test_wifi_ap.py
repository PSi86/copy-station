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


def test_button_wifi_ap_action_reports_to_hub(monkeypatch):
    from copystation.status import Event

    class _FakeHub:
        def __init__(self):
            self.ap = None
            self.signals = []

        def set_ap_active(self, active):
            self.ap = active

        def signal(self, event):
            self.signals.append(event)

    # AP came up -> hub told active + AP_ENABLED blink code.
    hub = _FakeHub()
    monkeypatch.setattr(ap, "toggle", lambda cfg: True)
    _resolve_action("b", "triple_click", "wifi_ap", {"wifi_ap": FULL}, hub)()
    assert hub.ap is True
    assert hub.signals == [Event.AP_ENABLED]

    # AP went down -> hub told inactive + AP_DISABLED blink code.
    hub2 = _FakeHub()
    monkeypatch.setattr(ap, "toggle", lambda cfg: False)
    _resolve_action("b", "triple_click", "wifi_ap", {"wifi_ap": FULL}, hub2)()
    assert hub2.ap is False
    assert hub2.signals == [Event.AP_DISABLED]
