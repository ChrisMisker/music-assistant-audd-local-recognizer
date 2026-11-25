"""
Measure stalls of Music Assistant's event loop and the time our work takes (diagnostics).

Sendspin pads the live source with silence when the event loop falls behind (MA's own note:
the GIL serializes Python, so CPU work in a worker thread can starve the playback loop), which
shows up as 100 ms holes of exact zeros in every source at once (2026-11-24). This logs each
stall with what the provider was doing at that moment, so a stall can be tied to our own work
(fingerprinting, database, AudD) or ruled out. It also times every piece of work it is told about
and logs a summary (count, median, worst) every SUMMARY_SECONDS, so a version that makes something
heavier shows up at once. With sample_stacks, a helper thread also takes a few stack samples
of the loop's thread while a stall lasts, to name the code behind stalls that are not ours.
MA-independent: needs only asyncio and a logger.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import sys
import threading
import time
import traceback
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

# The loop is asked to wake up this often; a wake-up this much later counts as a stall.
INTERVAL_SECONDS = 0.02
STALL_SECONDS = 0.05
# A summary line this often while stalls happen, so long sessions stay readable.
SUMMARY_SECONDS = 300.0
# stack samples of the loop's thread during a stall: the first when the loop is this late, then
# every STACK_EVERY_SECONDS, at most STACK_SAMPLES per stall, each with its innermost frames
STACK_AFTER_SECONDS = 0.05
STACK_EVERY_SECONDS = 0.03
STACK_SAMPLES = 4
STACK_FRAMES = 14
# With the first stack sample of a stall, the other busy threads too (innermost frames only): a
# loop that waits at a trivial point is waiting for the GIL, and the thread holding it is among them
THREAD_FRAMES = 6
# innermost functions of a thread that is only waiting (a pool worker between jobs, a timer)
IDLE_FUNCTIONS = frozenset({"_worker", "wait", "_wait_for_tstate_lock", "get", "select", "_sample"})


def _format_stack(frame: Any) -> str:
    """Name the frames outermost first, file names shortened to the package path."""
    parts = []
    for entry in traceback.extract_stack(frame)[-STACK_FRAMES:]:
        name = entry.filename.replace("\\", "/")
        name = name.split("site-packages/")[-1] if "site-packages/" in name else name
        name = "/".join(name.split("/")[-3:])
        parts.append(f"{name}:{entry.lineno} {entry.name}")
    return " > ".join(parts)


def _busy_threads(skip: set[int | None]) -> str:
    """Name the threads that are doing something, each with its innermost frames."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():  # noqa: SLF001 - diagnostics
        if ident in skip or frame.f_code.co_name in IDLE_FUNCTIONS:
            continue
        stack = " > ".join(_format_stack(frame).split(" > ")[-THREAD_FRAMES:])
        parts.append(f"[{names.get(ident, ident)}] {stack}")
    return " | ".join(parts) or "none busy"


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
        self._durations: dict[str, list[float]] = {}  # label -> seconds, since the last summary
        self._gc_started = 0.0
        # stack sampling: the loop's ticks (count, time) and samples taken by the helper thread
        self._sample_stacks = False
        self._tick = 0
        self._tick_at = 0.0
        self._loop_thread: int | None = None
        self._samples: list[tuple[int, float, str]] = []
        self._thread_samples: list[tuple[int, float, str]] = []
        self._sampler: threading.Thread | None = None
        self._sampler_stop = threading.Event()

    @property
    def running(self) -> bool:
        """Return whether the watcher is measuring."""
        return self._task is not None and not self._task.done()

    def start(self, *, sample_stacks: bool = False) -> None:
        """Start measuring (no-op when already running); sample_stacks also samples stalls."""
        if not self.running:
            self._stalls, self._worst, self._since = 0, 0.0, time.monotonic()
            self._task = asyncio.get_running_loop().create_task(self._run())
            if self._on_gc not in gc.callbacks:
                gc.callbacks.append(self._on_gc)
        if sample_stacks and (self._sampler is None or not self._sampler.is_alive()):
            self._sample_stacks = True
            self._loop_thread = threading.get_ident()
            self._tick_at = time.monotonic()
            self._sampler_stop.clear()
            self._sampler = threading.Thread(
                target=self._sample, name="audd-local-stall-stacks", daemon=True
            )
            self._sampler.start()

    def stop(self) -> None:
        """Stop measuring and log the totals."""
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        if self._sampler is not None:
            self._sampler_stop.set()
            self._sampler = None
            self._sample_stacks = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
            self._summary("stopped")
            self._log_timings()

    @contextlib.asynccontextmanager
    async def busy(self, label: str) -> AsyncIterator[None]:
        """Mark provider work as in flight for the duration of the block."""
        self._busy[label] += 1
        started = time.monotonic()
        try:
            yield
        finally:
            self._busy[label] -= 1
            if self._busy[label] <= 0:
                del self._busy[label]
            self._recent[label] = time.monotonic()
            self._record(label, self._recent[label] - started)

    @contextlib.contextmanager
    def timed(self, label: str) -> Iterator[None]:
        """Time synchronous work on the event loop itself (it cannot overlap a stall check)."""
        started = time.monotonic()
        try:
            yield
        finally:
            self._record(label, time.monotonic() - started)

    def _on_gc(self, phase: str, info: dict[str, int]) -> None:
        """Time Python's garbage collections: a full one pauses every thread, the loop too."""
        if phase == "start":
            self._gc_started = time.monotonic()
        elif phase == "stop" and self._gc_started:
            label = f"gc gen{info.get('generation', '?')}"
            self._recent[label] = time.monotonic()
            self._record(label, self._recent[label] - self._gc_started)

    def _record(self, label: str, seconds: float) -> None:
        if self.running:
            self._durations.setdefault(label, []).append(seconds)

    def run_blocking(self, func: Callable[..., Any], *args: Any) -> Awaitable[Any]:
        """Run func in a worker thread, timed under its own name."""
        label = getattr(func, "__name__", "work")

        async def run() -> Any:
            async with self.busy(label):
                return await asyncio.to_thread(func, *args)

        return run()

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
        # a copy: garbage-collection callbacks may add to it from another thread
        for label, ended in list(self._recent.items()):
            if now - ended <= window and label not in labels:
                labels[label] = 0
        if not labels:
            return "nothing of ours"
        return ", ".join(
            f"{label} x{count}" if count > 1 else label for label, count in sorted(labels.items())
        )

    def _sample(self) -> None:
        """Take a few stack samples of the loop thread while it is late (helper thread)."""
        last_tick, taken, next_at = -1, 0, STACK_AFTER_SECONDS
        while not self._sampler_stop.wait(0.01):
            tick, ticked_at = self._tick, self._tick_at
            late = time.monotonic() - ticked_at - INTERVAL_SECONDS
            if tick != last_tick:
                last_tick, taken, next_at = tick, 0, STACK_AFTER_SECONDS
            if late < next_at or taken >= STACK_SAMPLES or self._loop_thread is None:
                continue
            frame = sys._current_frames().get(self._loop_thread)  # noqa: SLF001 - diagnostics
            if frame is not None:
                self._samples.append((tick, late, _format_stack(frame)))
            del frame
            if taken == 0:
                skip: set[int | None] = {self._loop_thread, threading.get_ident()}
                self._thread_samples.append((tick, late, _busy_threads(skip)))
            taken += 1
            next_at = late + STACK_EVERY_SECONDS

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        last_summary = time.monotonic()
        while True:
            before = loop.time()
            self._tick += 1
            self._tick_at = time.monotonic()
            tick = self._tick
            await asyncio.sleep(INTERVAL_SECONDS)
            lag = loop.time() - before - INTERVAL_SECONDS
            now = time.monotonic()
            samples, self._samples = self._samples, []
            thread_samples, self._thread_samples = self._thread_samples, []
            if lag >= STALL_SECONDS:
                self._stalls += 1
                self._worst = max(self._worst, lag)
                self._logger.info(
                    "Event loop stalled %d ms (ours in flight: %s)",
                    round(lag * 1000),
                    self._describe(now, lag),
                )
                for sampled_tick, late, stack in samples:
                    if sampled_tick == tick:
                        self._logger.info("  stall stack at +%d ms: %s", round(late * 1000), stack)
                for sampled_tick, late, threads in thread_samples:
                    if sampled_tick == tick:
                        self._logger.info(
                            "  other threads at +%d ms: %s", round(late * 1000), threads
                        )
            if now - last_summary >= SUMMARY_SECONDS:
                last_summary = now
                self._summary("so far")
                self._log_timings()

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

    def _log_timings(self) -> None:
        """Log count, median and worst time per kind of work since the last summary."""
        durations, self._durations = self._durations, {}  # swapped: other threads may record
        if not durations:
            return
        parts = []
        for label, seconds in sorted(durations.items(), key=lambda item: -sum(item[1])):
            ordered = sorted(seconds)
            parts.append(
                f"{label} {len(ordered)}x median {round(ordered[len(ordered) // 2] * 1000)} "
                f"max {round(ordered[-1] * 1000)} ms"
            )
        self._logger.info("Timings: %s", "; ".join(parts))
