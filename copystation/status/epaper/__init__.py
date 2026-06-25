"""E-paper status backend.

A black/white SPI e-paper panel that renders the live status frame: the transfer
progress bar, the used/free storage of source and target, and the current phase.
Unlike the LED backends (which only need a progress scalar), the panel mirrors the
whole picture, so it *reads the shared StationState snapshot* -- the same source
of truth the web UI polls -- rather than receiving pushed values.

The render thread samples the snapshot ~2x/s, asks :func:`policy.decide` whether a
full / partial / no refresh is warranted (see ``policy`` for the e-paper-specific
full-vs-partial reasoning) and drives the controller via a parameterised driver.
On shutdown it leaves a clean "powered off" frame and deep-sleeps the panel.

Without the panel (or Pillow/spidev/libgpiod) the constructor raises, and the
factory in ``status/__init__`` skips the backend -- exactly like the other
hardware backends, so the service still runs on the dev machine.
"""

from __future__ import annotations

import threading
import time

from .. import State, StatusIndicator
from .drivers import open_driver
from .layout import render, render_stopped
from .model import build_view
from .policy import Decision, decide
from .presets import display_size, resolve_panel


class EpaperBackend(StatusIndicator):
    def __init__(self, cfg: dict, state=None, start: bool = True, driver_factory=None) -> None:
        # Fail fast (so the factory skips us with a clear warning) if the panel is
        # under-specified or Pillow is missing -- rather than silently never
        # drawing from inside the render thread.
        panel = resolve_panel(cfg)
        import PIL  # noqa: F401

        from copystation import __version__

        self._state = state
        self._version = __version__
        self._native_w = int(panel["width"])
        self._native_h = int(panel["height"])
        self._rotation = int(panel["rotation"])
        self._mirror = bool(panel.get("mirror", False))
        self._disp_w, self._disp_h = display_size(
            self._native_w, self._native_h, self._rotation
        )
        self._full_every = int(panel["full_refresh_every"])
        self._min_interval = float(panel["partial_min_interval"])

        factory = driver_factory or open_driver
        self._driver = factory(panel)

        self._prev_view = None
        self._partials_since_full = 0
        self._last_draw = 0.0
        self._initialized = False

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # ``start=False`` (the ``leds-off`` path) opens the hardware without the
        # render loop; close() then draws the off frame and sleeps the panel.
        if start:
            self._driver.init()
            self._initialized = True
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    # ----- StatusIndicator interface -------------------------------------------
    #
    # The panel reads StationState on its own cadence, so these pushes are no-ops;
    # they exist only to satisfy the interface (and never throttle the transfer).

    def set_state(self, state: State) -> None:
        pass

    def set_progress(self, fraction: float) -> None:
        pass

    def set_fill(self, fraction: float, sticky: bool = False) -> None:
        pass

    def signal(self, event) -> None:
        pass

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            if not self._initialized:
                self._driver.init()
                self._initialized = True
            img = render_stopped(self._version, self._disp_w, self._disp_h)
            self._driver.display_full(self._pack(img))
            self._driver.sleep()
        except Exception:  # pragma: no cover - indication must never crash
            pass
        try:
            self._driver.power_off_panel()
        except Exception:  # pragma: no cover
            pass
        try:
            self._driver.close()
        except Exception:  # pragma: no cover
            pass

    # ----- render loop ---------------------------------------------------------

    def _run(self) -> None:
        # Tick ~2x/s; the policy's throttle/budget governs the actual refresh rate.
        while not self._stop.wait(0.5):
            try:
                self._tick()
            except Exception:  # pragma: no cover - never let a frame crash the loop
                pass

    def _tick(self) -> None:
        if self._state is None:
            return
        view = build_view(self._state.snapshot(), self._version)
        now = time.monotonic()
        decision = decide(
            self._prev_view,
            view,
            partials_since_full=self._partials_since_full,
            seconds_since_last=now - self._last_draw,
            full_refresh_every=self._full_every,
            partial_min_interval=self._min_interval,
        )
        if decision is Decision.SKIP:
            return
        buf = self._pack(render(view, self._disp_w, self._disp_h))
        if decision is Decision.FULL:
            self._driver.display_full(buf)
            self._partials_since_full = 0
        else:
            self._driver.display_partial(buf)
            self._partials_since_full += 1
        self._prev_view = view
        self._last_draw = now

    # ----- output --------------------------------------------------------------

    def _pack(self, img) -> list[int]:
        """Mirror/rotate the viewer-facing image to the panel's native grid and
        pack it MSB-first (1 = white) into the controller's RAM byte layout."""
        from PIL import ImageOps

        if self._mirror:
            img = ImageOps.mirror(img)
        if self._rotation:
            img = img.rotate(self._rotation, expand=True)
        return list(img.convert("1").tobytes())
