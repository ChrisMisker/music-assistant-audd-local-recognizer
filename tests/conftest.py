"""
Shared test harness: a deterministic fake stream for the RecognitionEngine.

Audio is 100 Hz mono s16le. Every second of "music" is a constant sample value
code = 1000 * song + second (always loud enough to count as music), silence is zeros.
The fake fingerprinter decodes those codes, so tests know exactly which audio each
window contained, and produces one landmark per music second ("s<song>t<sec>" at its
position in the window), which gives exact offset votes in the real database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import wave
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from audd_local_recognizer.database import FingerprintDatabase
from audd_local_recognizer.engine import GapSettings, RecognitionEngine
from audd_local_recognizer.fingerprints import Fingerprints, PcmFormat

if TYPE_CHECKING:
    from pathlib import Path

# The server's `mass` fixture (a started server with this repository as its provider folder) is
# only available where Music Assistant is installed: in CI (`uv sync --all-groups`). Locally the
# tests that need it skip.
if importlib.util.find_spec("music_assistant") is not None:
    pytest_plugins = ["music_assistant.testing"]

RATE = 100
FMT = PcmFormat(sample_rate=RATE, channels=1, sample_width=2, content_type="s16le")


def music(song: int, second: int) -> bytes:
    """One second of 'music' for a song, identifiable by the fake fingerprinter."""
    return np.full(RATE, 1000 * song + second, dtype="<i2").tobytes()


def silence() -> bytes:
    """One second of digital silence."""
    return np.zeros(RATE, dtype="<i2").tobytes()


def decode_codes(pcm: bytes) -> list[int]:
    """Return the per-second codes (0 = silence) of an s16 mono 100 Hz buffer."""
    samples = np.frombuffer(pcm, dtype="<i2")
    return [int(samples[i]) for i in range(0, len(samples), RATE)]


def metadata_for(song: int) -> dict[str, Any]:
    """Return fake AudD metadata for a song."""
    return {
        "artist": f"Artist {song}",
        "title": f"Song {song}",
        "album": None,
        "artwork": None,
        "uri": None,
    }


@dataclass
class Harness:
    """A RecognitionEngine wired to fakes, plus everything it did."""

    db: FingerprintDatabase
    windows: list[list[int]] = field(default_factory=list)
    audd_calls: list[list[int]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    # songs AudD knows; None entries in audd_script force a no-match for that call
    audd_known: set[int] = field(default_factory=lambda: {1, 2, 3, 4})
    audd_script: list[Any] = field(default_factory=list)
    has_token: bool = True
    publish_result: bool = True
    fingerprint_gate: asyncio.Event | None = None
    # simulate a new play of vinyl: only every n-th second's landmark repeats (None = all)
    sparse_every: int | None = None
    audd_gate: asyncio.Event | None = None
    # holds the database lookup (find_best_match) until set: a slow database
    lookup_gate: asyncio.Event | None = None
    # collect AudD debug samples (None = the option is off)
    samples: list[tuple[bytes, dict[str, Any]]] | None = None
    audd_speed_percent: float = 0.0
    fast_start_seconds: float = 0.0
    divisors: list[int] = field(default_factory=list)
    engine: RecognitionEngine = field(init=False)

    def build(self, logger: logging.Logger) -> Harness:
        """Create the engine."""
        self.engine = RecognitionEngine(
            fmt=FMT,
            gap_settings=GapSettings(),
            fingerprint=self._fingerprint,
            db=self.db,
            audd=self._audd if self.has_token else None,
            publish=self._publish,
            spawn=lambda coro: asyncio.get_running_loop().create_task(coro),
            logger=logger,
            run_blocking=self._run_blocking,
            save_sample=self._save_sample if self.samples is not None else None,
            audd_speed_percent=self.audd_speed_percent,
            fast_start_seconds=self.fast_start_seconds,
        )
        return self

    async def _save_sample(self, wav: bytes, info: dict[str, Any]) -> None:
        assert self.samples is not None
        self.samples.append((wav, info))

    async def _run_blocking(self, func: Any, *args: Any) -> Any:
        if self.lookup_gate is not None and getattr(func, "__name__", "") == "find_best_match":
            await self.lookup_gate.wait()
        return func(*args)

    async def _fingerprint(self, pcm: bytes, divisor: int = 16) -> Fingerprints:
        self.divisors.append(divisor)
        codes = decode_codes(pcm)
        self.windows.append(codes)
        if self.fingerprint_gate is not None:
            await self.fingerprint_gate.wait()
        keep = self.sparse_every
        return {
            f"s{c // 1000}t{c % 1000}": [i]
            for i, c in enumerate(codes)
            if c and (keep is None or (c % 1000) % keep == 0)
        }

    async def _audd(self, wav: bytes) -> Any:
        with wave.open(io.BytesIO(wav)) as reader:
            frames = reader.readframes(reader.getnframes())
        codes = decode_codes(frames)  # s16 -> WAV round-trips exactly (rounded conversion)
        self.audd_calls.append(codes)
        if self.audd_gate is not None:
            await self.audd_gate.wait()
        if self.audd_script:
            scripted = self.audd_script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            if not isinstance(scripted, dict) and scripted is not None:
                return scripted  # e.g. an AudDNoMatch with a reason
            if scripted is None:
                return None
        songs = {c // 1000 for c in codes if c}
        song = max(songs, key=lambda s: sum(1 for c in codes if c // 1000 == s), default=0)
        return metadata_for(song) if song in self.audd_known else None

    def _publish(self, metadata: dict[str, Any]) -> bool:
        self.published.append(metadata)
        return self.publish_result

    async def settle(self) -> None:
        """Let the current task (if any) finish."""
        for _ in range(3):
            await asyncio.sleep(0)
        task = self.engine.task
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=5)

    async def play(self, song: int, seconds: int, start: int = 0) -> None:
        """Feed `seconds` of a song, letting each spawned task finish."""
        for second in range(start, start + seconds):
            self.engine.feed(music(song, second))
            await self.settle()

    async def quiet(self, seconds: int) -> None:
        """Feed silence."""
        for _ in range(seconds):
            self.engine.feed(silence())
            await self.settle()

    def references(self) -> list[tuple[int, str]]:
        """Return (track_id, passage_id) of every stored reference."""
        import sqlite3  # noqa: PLC0415

        with sqlite3.connect(self.db.path) as conn:
            return [
                (int(t), str(p))
                for t, p in conn.execute(
                    "SELECT track_id, passage_id FROM reference_windows ORDER BY id"
                )
            ]


async def _run_inline(func: Any, *args: Any) -> Any:
    return func(*args)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a fresh database path."""
    return tmp_path / "fingerprints.sqlite3"


@pytest.fixture
def logger() -> logging.Logger:
    """Return the test logger (captured by caplog)."""
    return logging.getLogger("test.audd_local_recognizer")


@pytest.fixture
def make_harness(db_path: Path, logger: logging.Logger) -> Any:
    """Return a factory for harnesses sharing one database file."""

    def _make(**kwargs: Any) -> Harness:
        return Harness(db=FingerprintDatabase(db_path), **kwargs).build(logger)

    return _make
