import threading

from copystation.config import Config
from copystation.power import ShutdownButton, build_shutdown_button, systemctl_action


def _button(hold=1.0):
    return ShutdownButton(reader=lambda: True, action=lambda: None, hold_seconds=hold)


def test_evaluate_fires_once_after_hold():
    b = _button(hold=1.0)
    assert b.evaluate(True, 100.0) is False   # press starts
    assert b.evaluate(True, 100.5) is False   # not held long enough
    assert b.evaluate(True, 101.0) is True    # threshold reached -> fire
    assert b.evaluate(True, 101.5) is False   # already fired -> no repeat


def test_evaluate_resets_on_release():
    b = _button(hold=1.0)
    assert b.evaluate(True, 0.0) is False
    assert b.evaluate(False, 0.5) is False    # released -> state reset
    assert b.evaluate(True, 0.6) is False     # new press starts here
    assert b.evaluate(True, 1.0) is False     # only 0.4 s held
    assert b.evaluate(True, 1.6) is True      # 1.0 s after the new press


def test_short_press_never_fires():
    b = _button(hold=1.0)
    fired = [
        b.evaluate(True, 0.0),
        b.evaluate(True, 0.4),
        b.evaluate(False, 0.5),
        b.evaluate(True, 0.6),
        b.evaluate(False, 0.7),
    ]
    assert not any(fired)


def test_loop_triggers_action_when_held():
    done = threading.Event()
    # reader always pressed; with hold=0 the first poll crosses the threshold.
    b = ShutdownButton(
        reader=lambda: True, action=done.set, hold_seconds=0.0, poll_interval=0.01
    )
    b.start()
    assert done.wait(timeout=2.0), "action should have fired"
    b.close()


def test_build_disabled_by_default():
    assert build_shutdown_button(Config()) is None


def test_build_enabled_without_line_returns_none():
    cfg = Config({"power": {"shutdown_button": {"enabled": True}}})
    assert build_shutdown_button(cfg) is None


def test_systemctl_action_builds_command():
    # The action is a no-arg callable; we only check construction does not raise.
    assert callable(systemctl_action("poweroff"))
    assert callable(systemctl_action("reboot"))
