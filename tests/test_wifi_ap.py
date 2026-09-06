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


def test_down_also_deletes_the_profile(monkeypatch):
    """A profile left behind is auto-activated by NetworkManager at the next boot.

    It carries ``autoconnect yes``, so NM raises the AP seconds into the boot --
    broadcasting and serving DHCP for ~20 s before the daemon can reconcile it
    away, although it is persisted off. So 'down' must also drop the profile.
    """
    calls = []
    monkeypatch.setattr(ap, "_run", lambda cmd, check=True: calls.append(cmd))
    assert ap.down(FULL) is True
    assert calls == [
        ["nmcli", "connection", "down", "copystation-ap"],
        ["nmcli", "connection", "delete", "copystation-ap"],
    ]


def test_down_deletes_the_profile_even_when_the_down_fails(monkeypatch):
    """An orphaned profile is precisely what must not survive a failed 'down'."""
    import subprocess

    calls = []

    def fake_run(cmd, check=True):
        calls.append(cmd)
        if cmd[2] == "down":
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(ap, "_run", fake_run)
    assert ap.down(FULL) is False
    assert calls[-1] == ["nmcli", "connection", "delete", "copystation-ap"]


def test_down_is_idempotent_when_there_is_no_profile(monkeypatch, caplog):
    """Switching off an AP that is already off is not a failure.

    Because switching off deletes the profile, a second 'off' -- or the startup
    reconcile on a station that was already down -- finds nothing to bring down.
    nmcli calls that exit 10 ("does not exist"), which must not surface as a
    warning: it is the state we are asking for.
    """
    import logging
    import subprocess

    def fake_run(cmd, check=True):
        if cmd[2] == "down":
            raise subprocess.CalledProcessError(ap._NMCLI_NOT_FOUND, cmd)

    monkeypatch.setattr(ap, "_run", fake_run)
    with caplog.at_level(logging.WARNING, logger="copystation.wifi_ap"):
        assert ap.down(FULL) is True
    assert caplog.records == []


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

    # want_up=False and already down -> no 'down', but the profile is still
    # dropped: a leftover with autoconnect is what raises the AP at the NEXT
    # boot, before this reconcile ever runs.
    calls.clear()
    monkeypatch.setattr(ap, "is_active", lambda cfg: False)
    monkeypatch.setattr(ap, "forget", lambda cfg: calls.append("forget"))
    assert _apply_wifi_ap_state({"wifi_ap": {}}, False) is False
    assert calls == ["forget"]


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


# ----- web.host vs the AP address --------------------------------------------


def test_web_bind_covers_wildcard_and_exact_address():
    from copystation.daemon import web_bind_covers

    for wildcard in ("0.0.0.0", "", "::", "*"):
        assert web_bind_covers(wildcard, "10.42.0.1") is True
    assert web_bind_covers("10.42.0.1", "10.42.0.1") is True
    assert web_bind_covers("192.168.1.50", "10.42.0.1") is False


def test_check_ap_web_reachability_warns_about_concrete_web_host(caplog):
    from copystation.config import Config
    from copystation.daemon import _check_ap_web_reachability

    cfg = Config()
    cfg.data["wifi_ap"]["enabled"] = True
    cfg.data["web"]["enabled"] = True
    cfg.data["web"]["host"] = "192.168.1.50"  # LAN address only -> refused over the AP
    with caplog.at_level("INFO"):
        _check_ap_web_reachability(cfg, web_up=True)
    assert any("web.host" in r.message and "refused over the AP" in r.message
               for r in caplog.records)
    # ... and the reachable-URL line must NOT claim an address nobody can reach.
    assert not any("Web interface over the AP" in r.message for r in caplog.records)


def test_check_ap_web_reachability_logs_url_when_bind_covers_the_ap(caplog):
    from copystation.config import Config
    from copystation.daemon import _check_ap_web_reachability

    cfg = Config()
    cfg.data["wifi_ap"]["enabled"] = True
    cfg.data["web"]["enabled"] = True
    with caplog.at_level("INFO"):
        _check_ap_web_reachability(cfg, web_up=True)
    assert any("Web interface over the AP: http://10.42.0.1:8080/" in r.message
               for r in caplog.records)


# ----- AP subnet vs the networks the station is already on --------------------


IP_ADDR_OUTPUT = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global dynamic eth0\\"
    "       valid_lft 42sec\n"
)


def test_parse_addr_show():
    assert ap.parse_addr_show(IP_ADDR_OUTPUT) == [
        ("lo", "127.0.0.1/8"),
        ("eth0", "192.168.1.50/24"),
    ]
    assert ap.parse_addr_show("") == []
    assert ap.parse_addr_show("garbage line without inet") == []


def test_address_conflicts_none_for_a_separate_subnet():
    assert ap.address_conflicts("10.42.0.1/24", ap.parse_addr_show(IP_ADDR_OUTPUT)) == []


def test_address_conflicts_flags_overlapping_lan_subnet():
    # The classic breakage: the AP subnet is the LAN subnet, so the station gets
    # a second route into it and answers LAN hosts over Wi-Fi.
    messages = ap.address_conflicts("192.168.1.1/24", ap.parse_addr_show(IP_ADDR_OUTPUT))
    assert len(messages) == 1
    assert "eth0" in messages[0] and "OVERLAPS" in messages[0]


def test_address_conflicts_flags_duplicate_address():
    messages = ap.address_conflicts("192.168.1.50/24", ap.parse_addr_show(IP_ADDR_OUTPUT))
    assert len(messages) == 1 and "DUPLICATE" in messages[0]


def test_address_conflicts_ignores_loopback_and_the_ap_interface():
    existing = [("lo", "127.0.0.1/8"), ("wlan0", "10.42.0.1/24")]
    assert ap.address_conflicts("127.0.0.1/8", existing) == []      # lo never counts
    assert ap.address_conflicts("10.42.0.1/24", existing, skip=("wlan0",)) == []
    # Without the skip the AP's own leftover address would look like a duplicate.
    assert ap.address_conflicts("10.42.0.1/24", existing) != []


def test_start_ap_warns_about_a_conflicting_subnet(monkeypatch, caplog):
    def fake_run(cmd, check=True):
        class R:
            stdout = IP_ADDR_OUTPUT if cmd[0] == "ip" else ""

        return R()

    monkeypatch.setattr(ap, "_run", fake_run)
    cfg = dict(FULL, ipv4_address="192.168.1.1/24", ifname="")
    with caplog.at_level("WARNING"):
        assert ap.start_ap(cfg) is True  # advisory only -- the AP still comes up
    assert any("address conflict" in r.message for r in caplog.records)


NMCLI_DEVICE_OUTPUT = """end0:ethernet:connected:Wired connection 1
lo:loopback:connected (externally):lo
wlan0:wifi:disconnected:
p2p-dev-wlan0:wifi-p2p:disconnected:
"""


def test_parse_wifi_ifnames_picks_only_wifi_devices():
    # wifi-p2p is a different type and must not be mistaken for the AP device.
    assert ap.parse_wifi_ifnames(NMCLI_DEVICE_OUTPUT) == ["wlan0"]
    assert ap.parse_wifi_ifnames("") == []


def test_ap_ifnames_prefers_the_configured_interface(monkeypatch):
    def fail(cmd, check=True):  # pragma: no cover - must not be reached
        raise AssertionError("nmcli must not be asked when ifname is configured")

    monkeypatch.setattr(ap, "_run", fail)
    assert ap.ap_ifnames({"ifname": "wlan1"}) == ["wlan1"]


def test_ap_ifnames_falls_back_to_the_wifi_devices(monkeypatch):
    monkeypatch.setattr(ap, "_run", lambda cmd, check=True: type(
        "R", (), {"stdout": NMCLI_DEVICE_OUTPUT})())
    assert ap.ap_ifnames({"ifname": ""}) == ["wlan0"]


def test_ap_ifnames_survives_a_missing_nmcli(monkeypatch):
    def boom(cmd, check=True):
        raise OSError("no nmcli")

    monkeypatch.setattr(ap, "_run", boom)
    assert ap.ap_ifnames({"ifname": ""}) == []


def test_no_conflict_warning_when_the_ap_is_already_up(monkeypatch, caplog):
    """Re-raising a running AP must not report its own address as a duplicate.

    NetworkManager drops the old address asynchronously, so at the moment of the
    check the AP address is still on wlan0 -- which used to produce a scary
    "SSH will break" warning on every daemon restart with the AP enabled.
    """
    already_up = IP_ADDR_OUTPUT + """3: wlan0    inet 10.42.0.1/24 scope global wlan0
"""

    def fake_run(cmd, check=True):
        stdout = already_up if cmd[0] == "ip" else NMCLI_DEVICE_OUTPUT
        return type("R", (), {"stdout": stdout})()

    monkeypatch.setattr(ap, "_run", fake_run)
    # ifname empty, exactly as the shipped configs have it.
    cfg = dict(FULL, ipv4_address="10.42.0.1/24", ifname="")
    with caplog.at_level("WARNING"):
        assert ap.log_address_conflicts(cfg) == []
    assert not any("address conflict" in r.message for r in caplog.records)


def test_a_real_lan_clash_is_still_reported_with_an_empty_ifname(monkeypatch, caplog):
    """The skip must not swallow the case the check exists for."""
    def fake_run(cmd, check=True):
        stdout = IP_ADDR_OUTPUT if cmd[0] == "ip" else NMCLI_DEVICE_OUTPUT
        return type("R", (), {"stdout": stdout})()

    monkeypatch.setattr(ap, "_run", fake_run)
    cfg = dict(FULL, ipv4_address="192.168.1.1/24", ifname="")
    with caplog.at_level("WARNING"):
        conflicts = ap.log_address_conflicts(cfg)
    assert conflicts and "eth0" in conflicts[0]


# ----- the shared runtime controller (button / web / CLI) --------------------


def test_ap_controller_applies_persists_and_syncs_the_portal(monkeypatch, tmp_path):
    from copystation.config import Config

    class _Portal:
        def __init__(self):
            self.states = []

        def sync(self, active):
            self.states.append(active)

    monkeypatch.setattr(ap, "set_active", lambda cfg, desired: desired)
    hub, store, portal = _ApHub(), _ap_section(tmp_path), _Portal()
    control = ap.ApController(config=Config(), hub=hub, settings=store, portal=portal)

    assert control.apply(True) is True
    assert hub.state.ap_active is True and store.get("enabled") is True
    assert portal.states == [True]

    assert control.flip() is False  # flips the last known state
    assert hub.state.ap_active is False and store.get("enabled") is False
    assert portal.states == [True, False]


def test_ap_controller_reconciles_a_failed_bringup(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "set_active", lambda cfg, desired: False)
    hub, store = _ApHub(), _ap_section(tmp_path)
    control = ap.ApController(hub=hub, settings=store)
    assert control.apply(True) is False
    assert hub.state.ap_active is False and store.get("enabled") is False


def test_ap_controller_without_hub_asks_nmcli_for_the_direction(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ap, "toggle", lambda cfg: seen.append(cfg) or True)
    store = _ap_section(tmp_path)
    assert ap.ApController(settings=store).flip() is True
    assert seen == [{}] and store.get("enabled") is True


def test_daemon_ap_controller_only_when_the_ap_can_come_up():
    from copystation.config import Config
    from copystation.daemon import _build_ap_controller

    cfg = Config()
    assert _build_ap_controller(cfg) is None  # no password -> AP can never be up
    cfg.data["wifi_ap"]["password"] = "supersecret"
    assert _build_ap_controller(cfg) is not None


def test_cli_wifi_ap_switches_and_persists(monkeypatch, tmp_path, capsys):
    from copystation.config import Config
    from copystation.daemon import run_wifi_ap
    from copystation.settings_store import SettingsStore

    monkeypatch.setattr(ap, "set_active", lambda cfg, desired: desired)
    cfg = Config()
    cfg.data["user_settings_file"] = str(tmp_path / "user-settings.json")
    cfg.data["wifi_ap"]["password"] = "supersecret"

    assert run_wifi_ap(cfg, "on") == 0
    assert "up" in capsys.readouterr().out
    store = SettingsStore(str(tmp_path / "user-settings.json")).section("wifi_ap")
    assert store.get("enabled") is True  # survives a restart

    assert run_wifi_ap(cfg, "off") == 0
    store = SettingsStore(str(tmp_path / "user-settings.json")).section("wifi_ap")
    assert store.get("enabled") is False


def test_cli_wifi_ap_on_reports_a_failed_bringup(monkeypatch, tmp_path):
    from copystation.config import Config
    from copystation.daemon import run_wifi_ap

    monkeypatch.setattr(ap, "set_active", lambda cfg, desired: False)
    cfg = Config()
    cfg.data["user_settings_file"] = str(tmp_path / "user-settings.json")
    assert run_wifi_ap(cfg, "on") == 1   # non-zero: the caller sees it failed
    assert run_wifi_ap(cfg, "off") == 0  # "off" that stays off is a success
