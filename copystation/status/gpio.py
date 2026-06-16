"""libgpiod compatibility shim (v1 and v2).

All GPIO output access in the status backends goes through this module so the
rest of the code never touches ``gpiod`` directly. Two libgpiod Python APIs exist
in the wild and we support both:

* **v1** (Debian Bullseye / Radxa, Raspberry Pi OS Bookworm apt `python3-libgpiod`):
  ``gpiod.Chip(name)`` -> ``chip.get_line(off)`` -> ``line.request(type=...)`` ->
  ``line.set_value(0/1)``.
* **v2** (PyPI ``gpiod`` >= 2, future distros): ``gpiod.request_lines("/dev/gpiochipN",
  config={off: LineSettings(...)})`` -> ``request.set_value(off, Value.ACTIVE)``.

The version is detected by feature-probing the imported module, so the same code
runs on the Cubie and on Raspberry Pi 4/5.
"""

from __future__ import annotations

from typing import Iterable, Optional


def _chip_name(chip: str) -> str:
    """Normalise a chip identifier to a bare name (e.g. ``gpiochip0``) for v1."""
    c = str(chip)
    if c.startswith("/dev/"):
        c = c[len("/dev/"):]
    if c.isdigit():
        c = f"gpiochip{c}"
    return c


def _chip_path(chip: str) -> str:
    """Normalise a chip identifier to a device path (``/dev/gpiochipN``) for v2."""
    c = str(chip)
    if c.startswith("/dev/"):
        return c
    if c.isdigit():
        return f"/dev/gpiochip{c}"
    return f"/dev/{c}"


class OutputLines:
    """Uniform handle for a set of GPIO output lines."""

    def set(self, offset: int, high: bool) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def release(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _V1OutputLines(OutputLines):
    def __init__(self, mod, chip: str, offsets: Iterable[int], consumer: str) -> None:
        self._chip = mod.Chip(_chip_name(chip))
        self._lines: dict[int, object] = {}
        for off in offsets:
            line = self._chip.get_line(int(off))
            line.request(consumer=consumer, type=mod.LINE_REQ_DIR_OUT)
            self._lines[int(off)] = line

    def set(self, offset: int, high: bool) -> None:
        self._lines[int(offset)].set_value(1 if high else 0)

    def release(self) -> None:
        for line in self._lines.values():
            try:
                line.set_value(0)
                line.release()
            except Exception:  # pragma: no cover
                pass
        try:
            self._chip.close()
        except Exception:  # pragma: no cover
            pass


class _V2OutputLines(OutputLines):
    def __init__(self, mod, chip: str, offsets: Iterable[int], consumer: str) -> None:
        direction = mod.line.Direction
        self._value = mod.line.Value
        config = {
            int(off): mod.LineSettings(
                direction=direction.OUTPUT,
                output_value=self._value.INACTIVE,
            )
            for off in offsets
        }
        self._req = mod.request_lines(_chip_path(chip), consumer=consumer, config=config)

    def set(self, offset: int, high: bool) -> None:
        self._req.set_value(
            int(offset), self._value.ACTIVE if high else self._value.INACTIVE
        )

    def release(self) -> None:
        try:
            self._req.release()
        except Exception:  # pragma: no cover
            pass


def _select_impl(mod) -> type[OutputLines]:
    """Pick the v1 or v2 implementation by probing the gpiod module."""
    if hasattr(mod, "LineSettings") and hasattr(mod, "request_lines"):
        return _V2OutputLines
    if hasattr(mod, "LINE_REQ_DIR_OUT"):
        return _V1OutputLines
    raise RuntimeError("Unsupported gpiod: neither v1 nor v2 API detected")


def _import_gpiod():
    import gpiod

    try:  # ensure the v2 submodule is attached as an attribute
        import gpiod.line  # noqa: F401
    except Exception:  # pragma: no cover - v1 has no submodule
        pass
    return gpiod


def open_output_lines(
    chip: str,
    offsets: Iterable[int],
    consumer: str,
    gpiod_module=None,
) -> OutputLines:
    """Request the given GPIO line offsets as outputs, version-agnostically.

    ``gpiod_module`` is an injection point for tests; in production the real
    ``gpiod`` is imported lazily.
    """
    mod = gpiod_module if gpiod_module is not None else _import_gpiod()
    impl = _select_impl(mod)
    return impl(mod, chip, list(offsets), consumer)
