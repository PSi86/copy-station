"""Render a :class:`ViewModel` into a 1-bit image with Pillow.

Pure and hardware-free: given a view model and the viewer-facing panel size, it
produces a black/white ``PIL.Image`` (mode ``"1"``). The backend rotates and
packs it for the controller. Two designs are chosen by aspect ratio -- a portrait
stack for the squarish 1.54" panel and a two-column landscape for the wide 2.9"/
3.7" panels.

Storage bars are drawn the honest 1-bit way: the used part is solid black, the
free part is white inside a 1px frame -- exactly how the panel will show it.

Pillow is imported lazily so the rest of the project still runs without it; only
this module (and its tests) need it.
"""

from __future__ import annotations

from typing import Any

from .model import ViewModel

_WHITE = 1
_BLACK = 0

# Candidate scalable fonts, tried in order. DejaVu ships on Debian (fonts-dejavu-
# core, pulled in by the installer); Arial covers the Windows dev machine. If
# none is found we fall back to Pillow's built-in bitmap font (small but valid),
# so headless tests still render.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans.ttf",
    "arial.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
)

_font_cache: dict[int, Any] = {}
_font_path: str | None = None
_font_path_resolved = False


def _resolve_font_path() -> str | None:
    global _font_path, _font_path_resolved
    if _font_path_resolved:
        return _font_path
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        try:
            ImageFont.truetype(candidate, 12)
            _font_path = candidate
            break
        except Exception:
            continue
    _font_path_resolved = True
    return _font_path


def _font(size: int):
    """A font at ``size`` px (scalable if available, else the bitmap default)."""
    from PIL import ImageFont

    if size in _font_cache:
        return _font_cache[size]
    path = _resolve_font_path()
    font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
    _font_cache[size] = font
    return font


def _text_width(draw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except Exception:  # pragma: no cover - very old Pillow
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]


def _text(draw, x: int, y: int, text: str, font, anchor_right: int | None = None) -> None:
    if anchor_right is not None:
        x = anchor_right - _text_width(draw, text, font)
    draw.text((x, y), text, font=font, fill=_BLACK)


def _bar(draw, x: int, y: int, w: int, h: int, fraction: float) -> None:
    """A storage/progress bar: 1px black frame, used part filled solid black."""
    draw.rectangle([x, y, x + w - 1, y + h - 1], outline=_BLACK, fill=_WHITE)
    frac = max(0.0, min(1.0, fraction))
    fill_w = int(round(frac * (w - 2)))
    if fill_w > 0:
        draw.rectangle([x + 1, y + 1, x + fill_w, y + h - 2], fill=_BLACK)


def render(view: ViewModel, width: int, height: int):
    """Render ``view`` to a mode-``"1"`` image of the given viewer-facing size."""
    landscape = width >= height * 1.3
    if landscape:
        return _render_landscape(view, width, height)
    return _render_portrait(view, width, height)


def _new_image(width: int, height: int):
    from PIL import Image, ImageDraw

    img = Image.new("1", (width, height), _WHITE)
    return img, ImageDraw.Draw(img)


def _render_portrait(view: ViewModel, width: int, height: int):
    img, draw = _new_image(width, height)
    m = max(4, width // 25)
    right = width - m

    title_f = _font(max(11, height // 16))
    status_f = _font(max(18, height // 8))
    label_f = _font(max(11, height // 17))
    small_f = _font(max(10, height // 20))

    _text(draw, m, m, "Copy_Station", title_f)
    if view.version:
        _text(draw, 0, m, f"v{view.version}", small_f, anchor_right=right)
    line_y = m + _line_height(title_f) + 2
    draw.line([m, line_y, right, line_y], fill=_BLACK)

    y = line_y + 4
    _text(draw, m, y, view.status_text, status_f)
    y += _line_height(status_f) + 4

    if view.phase == "error" and view.error_text:
        y = _draw_wrapped(draw, m, y, right, view.error_text, label_f)

    if view.show_progress:
        _text(draw, m, y, "Transfer", label_f)
        _text(draw, 0, y, f"{view.percent}%", label_f, anchor_right=right)
        y += _line_height(label_f) + 2
        _bar(draw, m, y, width - 2 * m, max(10, height // 18), view.progress_fraction)
        y += max(10, height // 18) + 8

    y = _draw_storage(draw, m, right, y, view.source, "Source", view, label_f, small_f)
    y = _draw_storage(draw, m, right, y, view.target, "Target", view, label_f, small_f)

    if view.show_progress and (view.speed_text or view.eta_text != "--"):
        foot_y = height - m - _line_height(small_f)
        if view.speed_text:
            _text(draw, m, foot_y, view.speed_text, small_f)
        if view.eta_text and view.eta_text != "--":
            _text(draw, 0, foot_y, f"ETA {view.eta_text}", small_f, anchor_right=right)
    return img


def _render_landscape(view: ViewModel, width: int, height: int):
    img, draw = _new_image(width, height)
    m = max(4, height // 16)
    col = int(width * 0.42)
    left_w = col - 2 * m

    title_f = _font(max(10, height // 13))
    label_f = _font(max(10, height // 12))
    small_f = _font(max(10, height // 13))

    _text(draw, m, m, "Copy_Station", title_f)
    ly = m + _line_height(title_f) + 1
    draw.line([m, ly, col - m, ly], fill=_BLACK)

    status_f = _fit_font(draw, view.status_text, left_w, max(16, height // 5))
    sy = ly + 3
    _text(draw, m, sy, view.status_text, status_f)
    sy += _line_height(status_f) + 2
    if view.show_progress:
        big_f = _fit_font(draw, f"{view.percent}%", left_w, max(14, height // 6))
        _text(draw, m, sy, f"{view.percent}%", big_f)
        sy += _line_height(big_f) + 1
        if view.speed_text:
            _text(draw, m, sy, view.speed_text, small_f)
            sy += _line_height(small_f)
        if view.eta_text and view.eta_text != "--":
            _text(draw, m, sy, f"ETA {view.eta_text}", small_f)
    elif view.phase == "error" and view.error_text:
        _draw_wrapped(draw, m, sy, col - m, view.error_text, small_f)

    draw.line([col, m, col, height - m], fill=_BLACK)

    rx = col + m
    x1 = width - m
    y = m
    if view.show_progress:
        _text(draw, rx, y, "Transfer", label_f)
        _text(draw, 0, y, f"{view.percent}%", label_f, anchor_right=x1)
        y += _line_height(label_f) + 2
        _bar(draw, rx, y, x1 - rx, max(9, height // 11), view.progress_fraction)
        y += max(9, height // 11) + 8
    y = _draw_storage(draw, rx, x1, y, view.source, "Source", view, label_f, small_f)
    _draw_storage(draw, rx, x1, y, view.target, "Target", view, label_f, small_f)
    return img


def _draw_storage(draw, x0, x1, y, storage, title, view, label_f, small_f):
    """Draw one storage row (label + used/total + bar) between x0..x1; skip if
    not present. The device name is dropped when it would collide with the value
    in a narrow column."""
    if not storage.present:
        return y
    label = f"{title} · {storage.label}" if storage.label else title
    value = view.storage_line(storage)
    gap = 8
    if _text_width(draw, label, label_f) + _text_width(draw, value, small_f) + gap > (x1 - x0):
        label = title
    _text(draw, x0, y, label, label_f)
    _text(draw, 0, y, value, small_f, anchor_right=x1)
    y += _line_height(label_f) + 2
    _bar(draw, x0, y, x1 - x0, 9, storage.fraction)
    y += 9 + 8
    return y


def _fit_font(draw, text: str, max_w: int, start_size: int, min_size: int = 11):
    """The largest font (down to ``min_size``) at which ``text`` fits ``max_w``."""
    size = start_size
    while size > min_size:
        font = _font(size)
        if _text_width(draw, text, font) <= max_w:
            return font
        size -= 1
    return _font(min_size)


def _line_height(font) -> int:
    try:
        ascent, descent = font.getmetrics()
        return ascent + descent
    except Exception:  # pragma: no cover - bitmap default font
        return 11


def _draw_wrapped(draw, x, y, right, text, font):
    """Word-wrap ``text`` within ``right`` and draw it; return the new y."""
    words = text.split()
    line = ""
    lh = _line_height(font) + 1
    for word in words:
        trial = f"{line} {word}".strip()
        if _text_width(draw, trial, font) > (right - x) and line:
            _text(draw, x, y, line, font)
            y += lh
            line = word
        else:
            line = trial
    if line:
        _text(draw, x, y, line, font)
        y += lh
    return y


def render_stopped(version: str, width: int, height: int):
    """The clean 'powered off' frame left on the panel after the service stops."""
    landscape = width >= height * 1.3
    img, draw = _new_image(width, height)
    m = max(4, min(width, height) // 12)
    title_f = _font(max(18, (height if not landscape else width) // 9))
    body_f = _font(max(12, (height if not landscape else width) // 16))

    cx = width // 2
    y = height // 2 - _line_height(title_f)
    _center(draw, cx, y, "Copy_Station", title_f)
    y += _line_height(title_f) + 4
    if version:
        _center(draw, cx, y, f"v{version}", body_f)
        y += _line_height(body_f) + 2
    _center(draw, cx, y, "Power off", body_f)
    draw.rectangle([2, 2, width - 3, height - 3], outline=_BLACK)
    return img


def _center(draw, cx: int, y: int, text: str, font) -> None:
    w = _text_width(draw, text, font)
    draw.text((cx - w // 2, y), text, font=font, fill=_BLACK)
