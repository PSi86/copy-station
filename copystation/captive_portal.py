"""Optional captive portal for the WLAN access point.

When the station hosts its own AP with no upstream internet, a client that joins
sees "no internet": phones then route around the AP (fall back to mobile data, so
even ``http://<ap-ip>:<port>/`` becomes unreachable), and nothing prompts the user
to open the web UI. A captive portal fixes both:

* **DNS hijack** -- NetworkManager's shared-mode dnsmasq is told, via a drop-in in
  ``/etc/NetworkManager/dnsmasq-shared.d/``, to resolve *every* name to the AP's
  own IP. So every request the client makes -- including the OS connectivity
  checks (Android ``generate_204``, Apple ``hotspot-detect``, Windows
  ``connecttest``) -- lands on this host.
* **Redirect responder** -- a tiny HTTP server on the AP address, port 80,
  answers those requests with a 302 to the web UI. The OS sees a non-success
  reply, flags a captive network ("Sign in to network") and opens the page
  automatically. It listens on the AP address only, so port 80 stays free on the
  LAN side and no LAN request is bounced to an AP-only address.

Opt-in via ``wifi_ap.captive_portal``. Needs port 80 and writes one file under
``dnsmasq-shared.d/``. Best-effort: any failure is logged and the AP still works,
just without the auto-redirect. Because all DNS is pointed at the AP, clients get
no general internet through it -- which is the expected trade-off for a field AP
whose only purpose is to serve this web UI.

The pure helpers (:func:`dnsmasq_hijack_content`, :func:`CaptivePortal.target`)
are unit-tested; the redirect server is exercised over a loopback socket.
"""

from __future__ import annotations

import errno
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("copystation.captive_portal")

# NetworkManager reads this directory for its shared-connection dnsmasq instance.
DNSMASQ_DIR = Path("/etc/NetworkManager/dnsmasq-shared.d")
DNSMASQ_CONF = DNSMASQ_DIR / "copystation-captive.conf"

# The AP address appears when NetworkManager finishes activating the profile,
# which can be a moment after `nmcli connection up` returns -- retry the bind
# for this long instead of losing the portal to a race.
BIND_TIMEOUT = 10.0


def dnsmasq_hijack_content(ap_ip: str) -> str:
    """dnsmasq drop-in that resolves every DNS name to ``ap_ip`` (wildcard)."""
    return (
        "# Managed by copy-station captive portal -- resolves all names to the AP\n"
        f"address=/#/{ap_ip}\n"
    )


def write_dnsmasq_hijack(ap_ip: str, path: Path = DNSMASQ_CONF) -> None:
    """Install the wildcard-DNS drop-in so NM's shared dnsmasq hijacks lookups."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dnsmasq_hijack_content(ap_ip), encoding="utf-8")
    _LOG.info("Captive portal: wrote DNS hijack -> %s (%s)", ap_ip, path)


def remove_dnsmasq_hijack(path: Path = DNSMASQ_CONF) -> None:
    """Remove the drop-in (used when the captive portal is disabled)."""
    try:
        path.unlink()
        _LOG.info("Captive portal: removed DNS hijack (%s)", path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.warning("Captive portal: could not remove %s: %s", path, exc)


class _RedirectHandler(BaseHTTPRequestHandler):
    """Redirect every request to the web UI (``target`` set on a subclass)."""

    target = "http://10.42.0.1:8080/"
    protocol_version = "HTTP/1.1"

    def _redirect(self) -> None:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<meta http-equiv=\"refresh\" content=\"0; url={self.target}\"></head>"
            f"<body><a href=\"{self.target}\">Open Copy_Station</a></body></html>"
        ).encode("utf-8")
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _redirect
    do_POST = _redirect

    def log_message(self, *args) -> None:  # keep the journal quiet
        pass


class CaptivePortal:
    """A port-80 redirect server pointing captive clients at the web UI.

    Bound to the **AP address only** by default: a wildcard bind would occupy
    port 80 on every interface and bounce requests that arrive over the LAN to an
    address only AP clients can reach. Because the AP address only exists while
    the AP is up, the portal is started/stopped along with it (see :meth:`sync`).
    """

    def __init__(self, ap_ip: str, web_port: int, listen_port: int = 80,
                 host: Optional[str] = None, bind_timeout: float = BIND_TIMEOUT) -> None:
        self._ap_ip = ap_ip
        self._web_port = int(web_port)
        self._listen_port = int(listen_port)
        self._host = ap_ip if host is None else host
        self._bind_timeout = float(bind_timeout)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def target(self) -> str:
        return f"http://{self._ap_ip}:{self._web_port}/"

    @property
    def port(self) -> int:
        """The actually bound port (useful when ``listen_port=0`` in tests)."""
        return self._server.server_address[1] if self._server else self._listen_port

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self, timeout: Optional[float] = None) -> None:
        """Bind and serve; idempotent. Raises OSError if the bind never works."""
        if self._server is not None:
            return
        handler = type("_CopystationRedirect", (_RedirectHandler,), {"target": self.target()})
        self._server = self._bind(handler, self._bind_timeout if timeout is None else timeout)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="copystation-captive", daemon=True
        )
        self._thread.start()
        _LOG.info("Captive portal redirecting %s:%d -> %s",
                  self._host, self.port, self.target())

    def _bind(self, handler, timeout: float) -> ThreadingHTTPServer:
        """Bind the listening socket, waiting out a not-yet-present AP address."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                return ThreadingHTTPServer((self._host, self._listen_port), handler)
            except OSError as exc:
                # EADDRNOTAVAIL = the address is not (yet) on this machine.
                if exc.errno != errno.EADDRNOTAVAIL or time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)

    def sync(self, active: bool) -> None:
        """Follow the AP: serve while it is up, stop when it goes down."""
        if not active:
            self.stop()
            return
        try:
            self.start()
        except OSError as exc:
            _LOG.warning("Captive portal could not bind %s:%d: %s",
                         self._host, self._listen_port, exc)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # pragma: no cover - best effort
                pass
            self._server = None
            self._thread = None
