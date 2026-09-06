"""Captive portal: DNS-hijack drop-in and the port-80 redirect responder."""

from http.client import HTTPConnection

from copystation.captive_portal import (
    CaptivePortal,
    dnsmasq_hijack_content,
    remove_dnsmasq_hijack,
    write_dnsmasq_hijack,
)


def test_dnsmasq_hijack_content_points_all_names_at_ap():
    text = dnsmasq_hijack_content("10.42.0.1")
    assert "address=/#/10.42.0.1" in text


def test_write_and_remove_dnsmasq_hijack(tmp_path):
    path = tmp_path / "sub" / "copystation-captive.conf"
    write_dnsmasq_hijack("10.42.0.1", path=path)
    assert path.read_text().strip().endswith("address=/#/10.42.0.1")
    remove_dnsmasq_hijack(path=path)
    assert not path.exists()
    remove_dnsmasq_hijack(path=path)  # idempotent -- no error when already gone


def test_target_url():
    assert CaptivePortal("10.42.0.1", 8080).target() == "http://10.42.0.1:8080/"
    assert CaptivePortal("10.42.0.1", 80).target() == "http://10.42.0.1:80/"


def test_daemon_captive_disabled_removes_dropin(monkeypatch):
    import copystation.captive_portal as cp
    from copystation.config import Config
    from copystation.daemon import _maybe_prepare_captive_portal

    removed = []
    monkeypatch.setattr(cp, "remove_dnsmasq_hijack", lambda *a, **k: removed.append(True))
    assert _maybe_prepare_captive_portal(Config()) is None  # default: off
    assert removed  # stale drop-in cleaned up


def test_daemon_captive_enabled_writes_hijack_but_does_not_bind_yet(monkeypatch):
    import copystation.captive_portal as cp
    from copystation.config import Config
    from copystation.daemon import _maybe_prepare_captive_portal

    wrote, started = [], []

    class _FakePortal:
        def __init__(self, ip, web_port, listen_port):
            self.args = (ip, web_port, listen_port)

        def start(self):
            started.append(self.args)

        def stop(self):
            pass

    monkeypatch.setattr(cp, "write_dnsmasq_hijack", lambda ip, **k: wrote.append(ip))
    monkeypatch.setattr(cp, "CaptivePortal", _FakePortal)

    cfg = Config()
    cfg.data["wifi_ap"]["captive_portal"] = True
    cfg.data["web"]["enabled"] = True
    portal = _maybe_prepare_captive_portal(cfg)
    assert isinstance(portal, _FakePortal)
    assert portal.args == ("10.42.0.1", 8080, 80)
    # The DNS drop-in must exist BEFORE the AP is raised (NM's dnsmasq reads it),
    # but the redirect server binds the AP address, which does not exist yet.
    assert wrote == ["10.42.0.1"]
    assert started == []


def _captive_without_web(monkeypatch, ap_enabled):
    import copystation.captive_portal as cp
    from copystation.config import Config
    from copystation.daemon import _maybe_prepare_captive_portal

    monkeypatch.setattr(cp, "remove_dnsmasq_hijack", lambda *a, **k: None)
    cfg = Config()
    cfg.data["wifi_ap"]["captive_portal"] = True
    cfg.data["wifi_ap"]["enabled"] = ap_enabled
    cfg.data["web"]["enabled"] = False
    assert _maybe_prepare_captive_portal(cfg) is None


def test_daemon_captive_without_web_warns_when_the_ap_is_usable(monkeypatch, caplog):
    with caplog.at_level("INFO"):
        _captive_without_web(monkeypatch, ap_enabled=True)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no page to redirect" in r.message for r in warnings)


def test_daemon_captive_without_web_is_only_a_note_without_an_ap(monkeypatch, caplog):
    # The portal ships enabled, so a station that never raises an AP must not be
    # warned about it on every single start -- that would be noise, not guidance.
    with caplog.at_level("INFO"):
        _captive_without_web(monkeypatch, ap_enabled=False)
    assert any("no page to redirect" in r.message for r in caplog.records)
    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_portal_binds_the_ap_address_by_default():
    # Not 0.0.0.0: a wildcard bind would occupy port 80 on the LAN side too and
    # bounce LAN requests to an address only AP clients can reach.
    portal = CaptivePortal("10.42.0.1", 8080)
    assert portal._host == "10.42.0.1"


def test_portal_sync_starts_and_stops_with_the_ap():
    portal = CaptivePortal("127.0.0.1", 8080, listen_port=0, host="127.0.0.1")
    try:
        portal.sync(True)
        assert portal.running
        port = portal.port
        portal.sync(True)  # idempotent: no second bind, same port
        assert portal.port == port
        portal.sync(False)
        assert not portal.running
    finally:
        portal.stop()


def test_portal_sync_survives_a_missing_ap_address(caplog):
    # The AP address is not on this machine -> the bind can never succeed. That
    # must be a logged warning, never an exception that takes the daemon down.
    portal = CaptivePortal("10.255.255.1", 8080, listen_port=0, bind_timeout=0)
    with caplog.at_level("WARNING"):
        portal.sync(True)
    assert not portal.running
    assert any("could not bind" in r.message for r in caplog.records)


def test_redirect_server_302s_to_web_ui():
    # Bind loopback + an OS-chosen free port so the test never needs port 80.
    portal = CaptivePortal("10.42.0.1", 8080, listen_port=0, host="127.0.0.1")
    portal.start()
    try:
        conn = HTTPConnection("127.0.0.1", portal.port, timeout=5)
        # A device connectivity check (any path) must be redirected to the web UI.
        conn.request("GET", "/generate_204")
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 302
        assert resp.getheader("Location") == "http://10.42.0.1:8080/"
        assert b"Copy_Station" in body  # fallback link in the body
        conn.close()
    finally:
        portal.stop()
