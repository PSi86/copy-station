"""Status backend that writes state transitions to the log.

Default backend: runs everywhere (including Windows dev) without hardware and is
also useful alongside real backends, because journald then keeps the full
history of states.
"""

from __future__ import annotations

import logging

from . import State, StatusIndicator

_LOG = logging.getLogger("copystation.status")


class LogBackend(StatusIndicator):
    def __init__(self) -> None:
        self._last: State | None = None

    def set_state(self, state: State) -> None:
        # Only log actual transitions to avoid flooding the journal.
        if state is self._last:
            return
        self._last = state
        _LOG.info("Status: %s", state.value.upper())
