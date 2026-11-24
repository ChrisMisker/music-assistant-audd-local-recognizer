"""Tests for the event-loop stall watcher (MA-independent)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from audd_local_recognizer import loopwatch
from audd_local_recognizer.loopwatch import LoopWatch

if TYPE_CHECKING:
    import pytest


async def test_a_blocked_loop_is_logged_with_the_work_in_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))
    watch.start()
    await asyncio.sleep(0.05)
    async with watch.busy("fingerprint 1/4"):
        time.sleep(0.12)  # noqa: ASYNC251 - block the loop, as work holding the GIL would
        await asyncio.sleep(0.05)
    watch.stop()
    stalls = [r.getMessage() for r in caplog.records if "Event loop stalled" in r.getMessage()]
    assert stalls
    assert "fingerprint 1/4" in stalls[0]
    assert any("Event loop stalls stopped: 1 of" in r.getMessage() for r in caplog.records)


async def test_a_quiet_loop_logs_no_stall(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))
    watch.start()
    await asyncio.sleep(0.15)
    watch.stop()
    assert not [r for r in caplog.records if "Event loop stalled" in r.getMessage()]


async def test_wrap_marks_calls_and_recent_work_is_named(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))

    async def lookup() -> int:
        return 7

    assert await watch.wrap("database", lookup)() == 7
    assert watch._describe(time.monotonic(), 0.1) == "database"  # ended just now
    monkeypatch.setattr(loopwatch, "INTERVAL_SECONDS", 0.0)
    assert watch._describe(time.monotonic() + 1, 0.1) == "nothing of ours"


async def test_timings_are_summarized_per_kind_of_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))
    watch.start()

    def lookup(value: int) -> int:
        time.sleep(0.01)
        return value

    assert await watch.run_blocking(lookup, 3) == 3
    assert await watch.run_blocking(lookup, 4) == 4
    with watch.timed("evidence best"):
        pass
    watch.stop()
    timings = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Timings:")]
    assert len(timings) == 1
    assert "lookup 2x median" in timings[0]
    assert "evidence best 1x" in timings[0]


async def test_garbage_collections_are_timed_and_named(caplog: pytest.LogCaptureFixture) -> None:
    import gc  # noqa: PLC0415

    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))
    watch.start()
    gc.collect()
    assert watch._describe(time.monotonic(), 0.1).startswith("gc gen2")
    watch.stop()
    assert watch._on_gc not in gc.callbacks
    timings = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Timings:")]
    assert "gc gen2 1x" in timings[0]


def test_warm_up_runs() -> None:
    from audd_local_recognizer.fingerprints import warm_up  # noqa: PLC0415

    warm_up()


def _hold_the_loop(seconds: float) -> None:
    time.sleep(seconds)


async def test_stall_stacks_name_the_code_that_held_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))
    watch.start(sample_stacks=True)
    await asyncio.sleep(0.05)
    _hold_the_loop(0.2)
    await asyncio.sleep(0.05)
    watch.stop()
    stacks = [r.getMessage() for r in caplog.records if "stall stack at" in r.getMessage()]
    assert stacks
    assert all("_hold_the_loop" in stack for stack in stacks)
    assert len(stacks) <= loopwatch.STACK_SAMPLES


async def test_no_stall_stacks_without_asking(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    watch = LoopWatch(logging.getLogger("test.loopwatch"))
    watch.start()
    await asyncio.sleep(0.05)
    _hold_the_loop(0.15)
    await asyncio.sleep(0.05)
    watch.stop()
    assert any("Event loop stalled" in r.getMessage() for r in caplog.records)
    assert not any("stall stack at" in r.getMessage() for r in caplog.records)
