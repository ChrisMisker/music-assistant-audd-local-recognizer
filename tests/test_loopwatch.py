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
