"""
Measure stalls of Music Assistant's event loop while live sessions run (diagnostics).

Sendspin pads the live source with silence when the event loop falls behind (MA's own note:
the GIL serializes Python, so CPU work in a worker thread can starve the playback loop), which
shows up as 100 ms holes of exact zeros in every source at once (2026-11-24). This logs each
stall with what the provider was doing at that moment, so a stall can be tied to our own work
(fingerprinting, database, AudD) or ruled out. MA-independent: needs only asyncio and a logger.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

# The loop is asked to wake up this often; a wake-up this much later counts as a stall.
INTERVAL_SECONDS = 0.02
STALL_SECONDS = 0.05
# A summary line this often while stalls happen, so long sessions stay readable.
SUMMARY_SECONDS = 300.0


class LoopWatch:
    """Watch the running event loop for stalls and name the provider work in flight."""

    def __init__(self, logger: logging.Logger) -> None:
        """Create an idle watcher; start() begins measuring."""
        self._logger = logger
        self._busy: Counter[str] = Counter()
        self._recent: dict[str, float] = {}  # label -> monotonic time it last ended
        self._task: asyncio.Task[None] | None = None
        self._stalls = 0
        self._worst = 0.0
        self._since = time.monotonic()

    @property
    def running(self) -> bool:
        """Return whether the watcher is measuring."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start measuring (no-op when already running)."""
        if not self.running:
            self._stalls, self._worst, self._since = 0, 0.0, time.monotonic()
            self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        """Stop measuring and log the totals."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
            self._summary("stopped")

    @contextlib.asynccontextmanager
    async def busy(self, label: str) -> AsyncIterator[None]:
        """Mark provider work as in flight for the duration of the block."""
        self._busy[label] += 1
        try:
            yield
        finally:
            self._busy[label] -= 1
            if self._busy[label] <= 0:
                del self._busy[label]
            self._recent[label] = time.monotonic()

    def wrap(
        self, label: str, func: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        """Return func with its calls marked as in flight under label."""

        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            async with self.busy(label):
                return await func(*args, **kwargs)

        return wrapped

    def _describe(self, now: float, lag: float) -> str:
        """Name what ran during the stall: in flight now, or ended within it."""
        window = lag + INTERVAL_SECONDS
        labels = dict(self._busy)
        for label, ended in self._recent.items():
            if now - ended <= window and label not in labels:
                labels[label] = 0
        if not labels:
            return "nothing of ours"
        return ", ".join(
            f"{label} x{count}" if count > 1 else label for label, count in sorted(labels.items())
        )

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last_summary = time.monotonic()
        while True:
            before = loop.time()
            await asyncio.sleep(INTERVAL_SECONDS)
            lag = loop.time() - before - INTERVAL_SECONDS
            now = time.monotonic()
            if lag >= STALL_SECONDS:
                self._stalls += 1
                self._worst = max(self._worst, lag)
                self._logger.info(
                    "Event loop stalled %d ms (ours in flight: %s)",
                    round(lag * 1000),
                    self._describe(now, lag),
                )
            if now - last_summary >= SUMMARY_SECONDS:
                last_summary = now
                self._summary("so far")

    def _summary(self, when: str) -> None:
        minutes = (time.monotonic() - self._since) / 60
        self._logger.info(
            "Event loop stalls %s: %d of >= %d ms in %.0f min (worst %d ms)",
            when,
            self._stalls,
            round(STALL_SECONDS * 1000),
            minutes,
            round(self._worst * 1000),
        )
