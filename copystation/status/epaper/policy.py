"""Refresh policy: full vs partial vs skip -- the heart of the e-paper backend.

E-paper has two update modes with opposite trade-offs:

* **Full refresh** flashes the panel black/white for ~1-2 s and clears all
  ghosting. It must stay rare.
* **Partial refresh** is fast (~0.3 s) and flash-free but accumulates faint
  ghosts. Crucially, drawing *darker* pixels onto white (a growing bar, a device
  appearing in a blank area) is clean; *erasing* (black->white) is what ghosts.

So a partial update is right while content only grows, and a full refresh is
needed whenever a previously-filled region must go truly white again (a device
vanished, a bar shrank, the source was cleared) -- plus periodically to wipe the
ghosting that small re-rendered numbers leave behind.

:func:`decide` is a pure function of the previous and current view models plus a
few counters, so the whole policy is unit-testable without hardware.
"""

from __future__ import annotations

import enum

from .model import ViewModel

# A storage/progress fraction must drop by more than this to count as "erased"
# (guards against float noise from re-measuring the same volume).
_SHRINK_EPS = 0.005


class Decision(enum.Enum):
    SKIP = "skip"        # nothing worth drawing changed (or we are throttled)
    PARTIAL = "partial"  # additive change -> fast, flash-free partial update
    FULL = "full"        # structural change or anti-ghost maintenance -> full


def _signature(vm: ViewModel) -> tuple:
    """The visible content that warrants a redraw (volatile footer excluded).

    Speed/ETA are deliberately left out: they ride along with whatever partial a
    real content change triggers, so a stalled copy whose only ticking value is
    the ETA does not churn the panel on its own.
    """
    return (
        vm.status_text,
        vm.percent,
        round(vm.source.fraction, 2),
        vm.source.present,
        round(vm.target.fraction, 2),
        vm.target.present,
        # The detected-device rows (shown while detecting): a new/changed device
        # or a fill change must trigger a redraw.
        tuple((d.name, d.role, round(d.fraction, 2)) for d in vm.devices),
        vm.error_text,
        vm.show_progress,
        vm.ap_active,
        vm.auto_transcode_active,
        vm.transcode_active,
        vm.transcode_name,
        vm.transcode_encoder,
        # The queue position ("i/n"): advancing to the next file must redraw even
        # if the overall percent did not cross an integer.
        vm.transcode_queue_text,
    )


def _requires_clear(prev: ViewModel, new: ViewModel) -> bool:
    """True if anything that was drawn must now become white again."""
    if new.device_count < prev.device_count:
        return True
    if new.progress_fraction < prev.progress_fraction - _SHRINK_EPS:
        return True
    if prev.ap_active and not new.ap_active:
        return True  # the WiFi badge must go white again -> only a full erases it
    if prev.auto_transcode_active and not new.auto_transcode_active:
        return True  # the Auto badge must go white again -> only a full erases it
    for old, cur in ((prev.source, new.source), (prev.target, new.target)):
        if old.present and not cur.present:
            return True
        if cur.fraction < old.fraction - _SHRINK_EPS:
            return True
    return False


def decide(
    prev: ViewModel | None,
    new: ViewModel,
    *,
    partials_since_full: int,
    seconds_since_last: float,
    full_refresh_every: int,
    partial_min_interval: float,
) -> Decision:
    """Return whether to FULL-refresh, PARTIAL-refresh or SKIP this tick."""
    # First frame after start: there is nothing on the panel yet.
    if prev is None:
        return Decision.FULL
    # A phase change rewrites the big status word and the visible regions;
    # replacing text via partial ghosts badly, and phases change rarely.
    if new.phase != prev.phase:
        return Decision.FULL
    # A region must go white again (device removed, bar/storage shrank, source
    # cleared) -- only a full refresh erases cleanly. Not throttled: this matters.
    if _requires_clear(prev, new):
        return Decision.FULL

    # No visible change -> de-dup, just like the LED backends skip equal frames.
    if _signature(new) == _signature(prev):
        return Decision.SKIP
    # The WiFi AP was just switched on (a deliberate button press): show the badge
    # on the next tick rather than waiting out the partial cadence.
    if new.ap_active and not prev.ap_active:
        return Decision.PARTIAL
    # Additive change pending, but hold partials to the configured cadence.
    if seconds_since_last < partial_min_interval:
        return Decision.SKIP
    # Anti-ghost maintenance: after enough partials, clean the panel with a full.
    if partials_since_full >= full_refresh_every:
        return Decision.FULL
    return Decision.PARTIAL
