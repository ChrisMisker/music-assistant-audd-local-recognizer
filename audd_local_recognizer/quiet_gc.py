"""
Run Python's full garbage collection in a silence, so it does not come in the music.

A full collection pauses every thread of Music Assistant, its event loop too: ~0.3 s on the HA
Blue (2026-11-25). Then Sendspin pads the live source with 100 ms of silence. Python starts one
by itself after a fixed number of smaller collections, wherever the music is; collecting when
all our sources are between tracks (or a session ends) starts that count again, so the automatic
one rarely falls in the music. In a silence the padding is inaudible. MA-independent.
"""

from __future__ import annotations

import gc
import math
import time

# at most one forced collection per this many seconds (short gaps follow each other on a side)
MIN_INTERVAL_SECONDS = 120.0
# collect once a gap has lasted this long (the passage is closed, the track has really ended)
SILENCE_SECONDS = 3.0


class QuietCollector:
    """Decide when a full collection may run, and run it (blocking: call in a worker thread)."""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS) -> None:
        """Start with no collection done yet."""
        self.min_interval = min_interval
        self._last = -math.inf
        self.running = False

    def due(self, *, quiet: bool, now: float | None = None) -> bool:
        """Return whether to collect now; quiet = no source of ours is in the music."""
        now = time.monotonic() if now is None else now
        return quiet and not self.running and now - self._last >= self.min_interval

    def collect(self, now: float | None = None) -> int:
        """Run a full collection; return the number of unreachable objects found."""
        self._last = time.monotonic() if now is None else now
        return gc.collect()
