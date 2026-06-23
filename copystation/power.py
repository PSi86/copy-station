"""Optional GPIO shutdown button.

Polls a configured GPIO input; when the button is held for ``hold_seconds`` it
runs a clean ``systemctl poweroff`` (the daemon runs as root, so no sudo). This
is the safe way to power the station down -- cutting the supply while the OS card
is being written can corrupt it (see README).

The button is disabled unless ``power.shutdown_button.enabled`` is true in the
config. The press-detection logic (``evaluate``) is split from the polling thread
so it is unit-testable without GPIO hardware or a real poweroff.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable, Optional

_LOG = logging.getLogger("copystation.power")

# Reader returns True while the button is pressed; Action performs the shutdown.
PressReader = Callable[[], bool]
Action = Callable[[], None]


class ShutdownButton:
    """Debounced GPIO button that triggers a single action when held."""

    def __init__(
        self,
        reader: PressReader,
        action: Action,
        hold_seconds: float = 1.0,
        poll_interval: float = 0.05,
        release: Optional[Callable[[], None]] = None,
    ) -> None:
        self._reader = reader
        self._action = action
        self._hold = float(hold_seconds)
        self._poll = float(poll_interval)
        self._release = release
        self._press_started: Optional[float] = None
        self._fired = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----- pure decision logic (unit-testable) ---------------------------------

    def evaluate(self, pressed: bool, now: float) -> bool:
        """Return True exactly once, when the button has been held long enough.

        Releasing the button resets the state, so a brief glitch never triggers
        (debounce) and the action fires at most once per sustained press.
        """
        if not pressed:
            self._press_started = None
            self._fired = False
            return False
        if self._press_started is None:
            self._press_started = now
        if not self._fired and (now - self._press_started) >= self._hold:
            self._fired = True
            return True
        return False

    # ----- polling thread ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="shutdown-button", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pressed = self._reader()
            except Exception:  # pragma: no cover - never let a read crash the loop
                pressed = False
            if self.evaluate(pressed, time.monotonic()):
                _LOG.warning("Shutdown button held -- powering off")
                try:
                    self._action()
                except Exception as exc:  # pragma: no cover - defensive
                    _LOG.error("Shutdown action failed: %s", exc)
                return
            self._stop.wait(self._poll)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._release is not None:
            try:
                self._release()
            except Exception:  # pragma: no cover
                pass


def systemctl_action(verb: str = "poweroff") -> Action:
    """Action that runs ``systemctl poweroff`` (or ``reboot``)."""
    cmd = ["systemctl", "reboot" if verb == "reboot" else "poweroff"]

    def _run() -> None:
        subprocess.run(cmd, check=False)

    return _run


def build_shutdown_button(config, action: Optional[Action] = None) -> Optional[ShutdownButton]:
    """Build the shutdown button from config, or None if disabled/unconfigured.

    ``action`` is an injection point for tests; in production a ``systemctl``
    action is built from ``power.shutdown_button.action``.
    """
    cfg = (config.get("power", {}) or {}).get("shutdown_button", {}) or {}
    if not cfg.get("enabled"):
        return None

    line = cfg.get("line")
    if line is None:
        _LOG.warning("Shutdown button enabled but no 'line' configured -- skipping")
        return None

    if action is None:
        action = systemctl_action(str(cfg.get("action", "poweroff")))

    from .status.gpio import open_input_lines  # lazy: needs libgpiod on the board

    offset = int(line)
    lines = open_input_lines(
        cfg.get("gpiochip", "gpiochip0"),
        [offset],
        consumer="copystation-power",
        active_low=bool(cfg.get("active_low", True)),
        bias=str(cfg.get("bias", "pull_up")),
    )
    return ShutdownButton(
        reader=lambda: lines.get(offset),
        action=action,
        hold_seconds=float(cfg.get("hold_seconds", 1.0)),
        release=lines.release,
    )
