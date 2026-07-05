import logging
import threading

from copystation.buttons import (
    ClickPatternEngine,
    UserButton,
    build_buttons,
    command_action,
    systemctl_action,
    _resolve_action,
)
from copystation.config import Config

STEP = 0.05  # sampling step, mirrors the production poll interval


def feed(engine, timeline, tail=2.0, start=0.0):
    """Sample a (pressed, duration) timeline through the engine.

    Appends ``tail`` seconds of released samples so gap timeouts fire.
    Returns the list of emitted events in order.
    """
    events = []
    t = start
    for pressed, duration in timeline:
        for _ in range(round(duration / STEP)):
            ev = engine.evaluate(pressed, t)
            if ev is not None:
                events.append(ev)
            t += STEP
    for _ in range(round(tail / STEP)):
        ev = engine.evaluate(False, t)
        if ev is not None:
            events.append(ev)
        t += STEP
    return events


# Gesture building blocks: activation click + valid gap (never counted).
ACTIVATION = [(True, 0.3), (False, 0.5)]
CLICK = [(True, 0.3), (False, 0.5)]


# ----- engine: valid gestures -------------------------------------------------


def test_dji_shutdown_fires_hold_exactly_once():
    events = feed(ClickPatternEngine(), ACTIVATION + [(True, 4.0)])
    assert events == ["hold"]


def test_single_click():
    events = feed(ClickPatternEngine(), ACTIVATION + [(True, 0.3)])
    assert events == ["click_1"]


def test_double_click():
    events = feed(ClickPatternEngine(), ACTIVATION + CLICK + [(True, 0.3)])
    assert events == ["click_2"]


def test_triple_click():
    events = feed(ClickPatternEngine(), ACTIVATION + CLICK + CLICK + [(True, 0.3)])
    assert events == ["click_3"]


def test_quad_click_emitted_but_unmapped():
    # The engine emits click_4; the dispatcher simply has no action bound.
    events = feed(
        ClickPatternEngine(), ACTIVATION + CLICK + CLICK + CLICK + [(True, 0.3)]
    )
    assert events == ["click_4"]


# ----- engine: gestures that must do nothing ----------------------------------


def test_plain_long_hold_never_fires():
    engine = ClickPatternEngine()
    assert feed(engine, [(True, 10.0)]) == []
    # The engine recovers: a proper gesture afterwards still works.
    assert feed(engine, ACTIVATION + [(True, 0.3)], start=12.0) == ["click_1"]


def test_activation_only_times_out_silently():
    engine = ClickPatternEngine()
    assert feed(engine, [(True, 0.3)]) == []
    assert feed(engine, ACTIVATION + [(True, 4.0)], start=10.0) == ["hold"]


def test_too_long_activation_press_invalidates():
    # 1 s "activation" is not a click, so the following hold must not fire.
    assert feed(ClickPatternEngine(), [(True, 1.0), (False, 0.5), (True, 4.0)]) == []


def test_armed_press_released_between_click_and_hold_is_silent():
    assert feed(ClickPatternEngine(), ACTIVATION + [(True, 1.5)]) == []


def test_click_then_long_press_is_invalid():
    # Hold is only valid as the first press after activation (strict gesture).
    assert feed(ClickPatternEngine(), ACTIVATION + CLICK + [(True, 4.0)]) == []


def test_gap_too_long_ends_sequence_before_late_press():
    # The 1.5 s pause ends the sequence as a single click; the late press is a
    # fresh activation click that then times out silently.
    events = feed(
        ClickPatternEngine(), ACTIVATION + [(True, 0.3), (False, 1.5), (True, 0.3)]
    )
    assert events == ["click_1"]


# ----- engine: contact bounce -------------------------------------------------


def test_bounce_glitch_does_not_abort_hold():
    # One 0.05 s release sample in the middle of the 3 s hold must merge.
    timeline = ACTIVATION + [(True, 1.5), (False, STEP), (True, 2.5)]
    assert feed(ClickPatternEngine(), timeline) == ["hold"]


def test_bounce_in_gap_does_not_double_count_click():
    # click, 0.05 s glitch, short re-press: still exactly one click in total.
    timeline = ACTIVATION + [(True, 0.3), (False, STEP), (True, 0.2)]
    assert feed(ClickPatternEngine(), timeline) == ["click_1"]


# ----- engine: exact boundaries (direct calls, no sampling jitter) -------------


def test_release_at_exactly_max_click_is_not_a_click():
    e = ClickPatternEngine()
    assert e.evaluate(True, 0.0) is None    # activation press
    assert e.evaluate(False, 0.3) is None   # valid activation click
    assert e.evaluate(True, 1.0) is None    # armed press starts
    assert e.evaluate(False, 1.6) is None   # released at exactly max_click
    assert e.evaluate(False, 3.0) is None   # no click_1 after max_gap either


def test_repress_at_exactly_min_gap_is_valid():
    e = ClickPatternEngine()
    assert e.evaluate(True, 0.0) is None
    assert e.evaluate(False, 0.3) is None
    assert e.evaluate(True, 0.5) is None    # gap of exactly min_gap -> armed
    assert e.evaluate(False, 0.8) is None   # click pending
    assert e.evaluate(False, 1.8) == "click_1"  # timeout at exactly max_gap


def test_hold_fires_at_exact_threshold_while_pressed():
    e = ClickPatternEngine()
    assert e.evaluate(True, 0.0) is None
    assert e.evaluate(False, 0.3) is None
    assert e.evaluate(True, 0.8) is None
    assert e.evaluate(True, 3.75) is None       # just below the threshold
    assert e.evaluate(True, 3.8) == "hold"      # exactly t_press + hold
    assert e.evaluate(True, 4.5) is None        # no repeat while held
    assert e.evaluate(False, 5.0) is None       # release resets silently


# ----- UserButton loop ---------------------------------------------------------


class _StubEngine:
    """Engine stand-in that replays a fixed event sequence."""

    def __init__(self, events):
        self._events = list(events)

    def evaluate(self, pressed, now):
        return self._events.pop(0) if self._events else None


def test_loop_dispatches_repeatedly():
    # Two events must yield two action runs: the loop must not exit after one.
    fired = []
    done = threading.Event()

    def action():
        fired.append(1)
        if len(fired) == 2:
            done.set()

    button = UserButton(
        name="test",
        reader=lambda: False,
        actions={"click_1": action},
        engine=_StubEngine(["click_1", "click_1"]),
        poll_interval=0.01,
    )
    button.start()
    assert done.wait(timeout=2.0), "both actions should have fired"
    button.close()


def test_reader_exception_does_not_kill_loop():
    calls = []
    seen_two = threading.Event()

    def reader():
        calls.append(1)
        if len(calls) >= 2:
            seen_two.set()
        raise RuntimeError("gpio gone")

    button = UserButton(
        name="test", reader=reader, actions={}, poll_interval=0.01
    )
    button.start()
    assert seen_two.wait(timeout=2.0), "loop should survive reader errors"
    button.close()


def test_close_calls_release():
    released = []
    button = UserButton(
        name="test",
        reader=lambda: False,
        actions={},
        poll_interval=0.01,
        release=lambda: released.append(1),
    )
    button.start()
    button.close()
    assert released == [1]


# ----- factory / config --------------------------------------------------------


def test_build_disabled_by_default():
    assert build_buttons(Config()) == []


def test_build_enabled_without_line_returns_empty(caplog):
    cfg = Config({"buttons": {"userbutton_1": {"enabled": True}}})
    with caplog.at_level(logging.WARNING, logger="copystation.buttons"):
        assert build_buttons(cfg) == []
    assert "no 'line'" in caplog.text


def test_legacy_power_key_warns_and_is_ignored(caplog):
    cfg = Config({"power": {"shutdown_button": {"enabled": True, "line": 3}}})
    with caplog.at_level(logging.WARNING, logger="copystation.buttons"):
        assert build_buttons(cfg) == []
    assert "power.shutdown_button" in caplog.text
    assert "buttons.userbutton_1" in caplog.text


def test_all_actions_none_skips_before_gpio(caplog):
    # Resolution happens before the GPIO line is claimed, so this must not
    # try to import libgpiod even with enabled: true and a line set.
    cfg = Config(
        {
            "buttons": {
                "userbutton_1": {
                    "enabled": True,
                    "line": 3,
                    "actions": {
                        "hold": "none",
                        "single_click": "none",
                        "double_click": "none",
                        "triple_click": "none",
                    },
                }
            }
        }
    )
    with caplog.at_level(logging.WARNING, logger="copystation.buttons"):
        assert build_buttons(cfg) == []
    assert "all actions are 'none'" in caplog.text


def test_action_resolution():
    assert callable(_resolve_action("b", "hold", "poweroff"))
    assert callable(_resolve_action("b", "hold", "reboot"))
    assert callable(_resolve_action("b", "hold", {"command": "echo hi"}))
    assert _resolve_action("b", "hold", "none") is None
    assert _resolve_action("b", "hold", None) is None


def test_unknown_action_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="copystation.buttons"):
        assert _resolve_action("b", "hold", "explode") is None
    assert "unknown action" in caplog.text


def test_action_factories_build_without_running():
    # The actions are no-arg callables; only check construction does not raise.
    assert callable(systemctl_action("poweroff"))
    assert callable(systemctl_action("reboot"))
    assert callable(command_action("echo hi"))
