"""Generic GPIO user buttons with DJI-style click patterns.

Every gesture starts with a short *activation click* (intent check) that is
never counted: press shorter than ``max_click_seconds``, then release for
``min_gap_seconds``..``max_gap_seconds``. Only then is the button armed:

* press and hold for ``hold_seconds``        -> ``hold`` event (default: poweroff)
* 1-3 further short clicks, then silence     -> ``click_1``..``click_3`` events

A plain long hold without the activation click does nothing, so the station
cannot be shut down by accident (e.g. the button squeezed in a bag). Buttons
live under ``buttons.<name>`` in the config -- ``userbutton_1`` today, more
possible later. A clean ``systemctl poweroff`` is the safe way to power the
station down: cutting the supply while the OS card is written can corrupt it.

The press-detection logic (``ClickPatternEngine.evaluate``) is split from the
polling thread so it is unit-testable without GPIO hardware. A release shorter
than ``min_gap_seconds`` is treated as contact bounce and merged into the
ongoing press; there is no minimum *press* duration -- with idle-high wiring
(``bias: pull_up``) and 50 ms sampling, spurious press glitches are unlikely.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable, Dict, Optional

_LOG = logging.getLogger("copystation.buttons")

# Reader returns True while the button is pressed; Action runs on an event.
PressReader = Callable[[], bool]
Action = Callable[[], None]

# Engine event -> config key under ``actions``.
EVENT_CONFIG_KEYS = {
    "hold": "hold",
    "click_1": "single_click",
    "click_2": "double_click",
    "click_3": "triple_click",
}

_TIMING_DEFAULTS = {
    "max_click_seconds": 0.6,
    "min_gap_seconds": 0.2,
    "max_gap_seconds": 1.0,
    "hold_seconds": 3.0,
}


class ClickPatternEngine:
    """Pure click-pattern state machine (no threads, no GPIO).

    Feed samples via ``evaluate``; it returns at most one event per call:
    ``"hold"`` or ``"click_<n>"``. Timeout checks run before the pressed
    sample, and every threshold uses ``>=`` for "reached". ``hold`` is only
    valid as the *first* press after the activation click -- a long press
    after counted clicks invalidates the whole sequence (poweroff is
    destructive, so the exact gesture is required).
    """

    def __init__(
        self,
        max_click: float = 0.6,
        min_gap: float = 0.2,
        max_gap: float = 1.0,
        hold: float = 3.0,
    ) -> None:
        self._max_click = float(max_click)
        self._min_gap = float(min_gap)
        self._max_gap = float(max_gap)
        self._hold = float(hold)
        self._state = "idle"
        self._clicks = 0  # confirmed clicks after the activation click
        self._t_press = 0.0  # start of the current (or bounce-merged) press
        self._t_release = 0.0  # start of the current gap

    def evaluate(self, pressed: bool, now: float) -> Optional[str]:
        state = self._state

        if state == "idle":
            if pressed:
                self._state = "activation_press"
                self._t_press = now
            return None

        if state == "activation_press":
            if pressed:
                if now - self._t_press >= self._max_click:
                    # Plain long hold: the intent check failed.
                    self._state = "wait_release"
                return None
            self._state = "activation_gap"
            self._t_release = now
            return None

        if state == "activation_gap":
            if now - self._t_release >= self._max_gap:
                self._state = "idle"  # activation-only: silent timeout
                return None
            if pressed:
                if now - self._t_release < self._min_gap:
                    # Contact bounce: the release never happened.
                    self._state = "activation_press"
                else:
                    self._state = "armed_press"
                    self._clicks = 0
                    self._t_press = now
            return None

        if state == "armed_press":
            held = now - self._t_press
            if pressed:
                if self._clicks == 0 and held >= self._hold:
                    self._state = "wait_release"
                    return "hold"
                if self._clicks > 0 and held >= self._max_click:
                    # Click-then-long-press: not a valid gesture.
                    self._state = "wait_release"
                return None
            if held >= self._max_click:
                # Released during a hold attempt: real release aborts silently,
                # but a bounce may still merge back into the press.
                self._state = "hold_gap"
                self._t_release = now
                return None
            self._state = "armed_gap"  # click pending, counted on gap validation
            self._t_release = now
            return None

        if state == "armed_gap":
            if now - self._t_release >= self._max_gap:
                self._state = "idle"
                return f"click_{self._clicks + 1}"
            if pressed:
                if now - self._t_release < self._min_gap:
                    # Bounce: the pending click dissolves back into the press.
                    self._state = "armed_press"
                else:
                    self._clicks += 1  # pending click confirmed by a valid gap
                    self._state = "armed_press"
                    self._t_press = now
            return None

        if state == "hold_gap":
            if now - self._t_release >= self._min_gap:
                self._state = "idle"  # aborted hold: silent
                return None
            if pressed:
                self._state = "armed_press"  # bounce merge, press time kept
            return None

        # wait_release: swallow everything until the button is let go.
        if not pressed:
            self._state = "idle"
        return None


class UserButton:
    """Polls a GPIO line and dispatches click-pattern events to actions."""

    def __init__(
        self,
        name: str,
        reader: PressReader,
        actions: Dict[str, Action],
        engine: Optional[ClickPatternEngine] = None,
        poll_interval: float = 0.05,
        release: Optional[Callable[[], None]] = None,
    ) -> None:
        self._name = name
        self._reader = reader
        self._actions = dict(actions)
        self._engine = engine if engine is not None else ClickPatternEngine()
        self._poll = float(poll_interval)
        self._release = release
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"button-{self._name}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        # Never exits on an event: click actions must be repeatable.
        while not self._stop.is_set():
            try:
                pressed = self._reader()
            except Exception:  # pragma: no cover - never let a read crash the loop
                pressed = False
            event = self._engine.evaluate(pressed, time.monotonic())
            if event is not None:
                self._dispatch(event)
            self._stop.wait(self._poll)

    def _dispatch(self, event: str) -> None:
        action = self._actions.get(event)
        if action is None:
            _LOG.debug("Button %s: %s (no action bound)", self._name, event)
            return
        log = _LOG.warning if event == "hold" else _LOG.info
        log("Button %s: %s -- running action", self._name, event)
        try:
            action()
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.error("Button %s: action for %s failed: %s", self._name, event, exc)

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


def command_action(command: str) -> Action:
    """Action that runs an arbitrary shell command."""

    def _run() -> None:
        subprocess.run(command, shell=True, check=False)

    return _run


def wifi_ap_toggle_action(config=None) -> Action:
    """Action that toggles the WLAN access point (see :mod:`copystation.wifi_ap`)."""

    def _run() -> None:
        from .wifi_ap import toggle

        ap_cfg = (config.get("wifi_ap") if config is not None else None) or {}
        toggle(ap_cfg)

    return _run


def _resolve_action(button: str, key: str, raw, config=None) -> Optional[Action]:
    if raw is None or raw == "none":
        return None
    if raw in ("poweroff", "reboot"):
        return systemctl_action(raw)
    if raw == "wifi_ap":
        return wifi_ap_toggle_action(config)
    if isinstance(raw, dict) and isinstance(raw.get("command"), str):
        return command_action(raw["command"])
    _LOG.warning("Button %s: unknown action %r for %s -- ignored", button, raw, key)
    return None


def build_buttons(config, action_overrides: Optional[Dict[str, Action]] = None) -> list:
    """Build all configured user buttons; disabled/misconfigured ones are skipped.

    ``action_overrides`` is an injection point for tests: a ready-made
    event->Action mapping used instead of resolving ``actions`` from config.
    """
    if (config.get("power") or {}).get("shutdown_button"):
        _LOG.warning(
            "Config key power.shutdown_button is no longer supported -- migrate to "
            "buttons.userbutton_1 (the gesture changed, see README 'User button')"
        )

    buttons = []
    entries = config.get("buttons") or {}
    for name in sorted(entries):
        cfg = entries[name] or {}
        if not cfg.get("enabled"):
            continue
        line = cfg.get("line")
        if line is None:
            _LOG.warning("Button %s enabled but no 'line' configured -- skipping", name)
            continue

        if action_overrides is not None:
            actions = dict(action_overrides)
        else:
            raw = cfg.get("actions") or {}
            actions = {}
            for event, key in EVENT_CONFIG_KEYS.items():
                action = _resolve_action(name, key, raw.get(key), config)
                if action is not None:
                    actions[event] = action
        if not actions:
            _LOG.warning(
                "Button %s enabled but all actions are 'none' -- skipping", name
            )
            continue

        # The deep-merge only fills defaults for buttons present in DEFAULTS
        # (userbutton_1), so every field carries its own default here.
        timing = cfg.get("timing") or {}
        engine = ClickPatternEngine(
            max_click=float(timing.get("max_click_seconds", _TIMING_DEFAULTS["max_click_seconds"])),
            min_gap=float(timing.get("min_gap_seconds", _TIMING_DEFAULTS["min_gap_seconds"])),
            max_gap=float(timing.get("max_gap_seconds", _TIMING_DEFAULTS["max_gap_seconds"])),
            hold=float(timing.get("hold_seconds", _TIMING_DEFAULTS["hold_seconds"])),
        )

        from .status.gpio import open_input_lines  # lazy: needs libgpiod on the board

        offset = int(line)
        lines = open_input_lines(
            cfg.get("gpiochip", "gpiochip0"),
            [offset],
            consumer=f"copystation-{name}",
            active_low=bool(cfg.get("active_low", True)),
            bias=str(cfg.get("bias", "pull_up")),
        )
        buttons.append(
            UserButton(
                name=name,
                reader=lambda lines=lines, offset=offset: lines.get(offset),
                actions=actions,
                engine=engine,
                release=lines.release,
            )
        )
    return buttons
