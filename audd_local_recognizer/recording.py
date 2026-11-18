"""
Recording the live input for troubleshooting (opt-in advanced setting; stays on the server).

The recording holds exactly the audio the recognizer receives (before any output to players),
as 16-bit WAV at the source's rate and channel count, in segments of a few minutes. A hard cap
on the total recorded time, counted over all files in the folder, keeps the disk use bounded
across sessions and restarts. Files can be replayed offline through the recognizer
(`scripts/replay_recording.py`).
"""

from __future__ import annotations

import logging
import queue
import time
import wave
from logging.handlers import QueueHandler, QueueListener
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path

    from .fingerprints import PcmFormat

MAX_RECORDING_SECONDS = 3600  # default: one LP, both sides (a setting can raise it)
SEGMENT_SECONDS = 300
FLUSH_SECONDS = 10  # write to disk in blocks of this many seconds
# The same line format as the Music Assistant log, so the report and replay scripts read both.
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s (%(threadName)s) [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def pcm_to_s16_interleaved(pcm: bytes, fmt: PcmFormat) -> bytes:
    """Convert explicit little-endian PCM (s16le/f32le/s32le) to interleaved s16le."""
    content_type = fmt.content_type.removeprefix("pcm_")
    usable = len(pcm) - len(pcm) % (fmt.sample_width * fmt.channels)
    if content_type == "s16le" and fmt.sample_width == 2:
        return pcm[:usable]
    if content_type == "f32le" and fmt.sample_width == 4:
        data = np.nan_to_num(np.frombuffer(pcm[:usable], dtype="<f4"), nan=0.0)
        scaled = np.rint(data.astype(np.float64) * 32767.0)
    elif content_type == "s32le" and fmt.sample_width == 4:
        scaled = np.rint(np.frombuffer(pcm[:usable], dtype="<i4") / 65536.0)
    else:
        msg = f"Unsupported PCM format: {fmt.content_type} / {fmt.sample_width * 8}-bit"
        raise ValueError(msg)
    converted: bytes = np.clip(scaled, -32768, 32767).astype("<i2").tobytes()
    return converted


def recorded_seconds(folder: Path) -> float:
    """Return the total duration of the WAV recordings in a folder."""
    total = 0.0
    for path in folder.glob("*.wav"):
        try:
            with wave.open(str(path), "rb") as reader:
                total += reader.getnframes() / max(1, reader.getframerate())
        except wave.Error, EOFError, OSError:
            continue  # a segment that was cut off; ignore it
    return total


class InputRecorder:
    """
    Write the input of one analysis session to WAV segments.

    Blocking methods: call them off the event loop. ``add`` only buffers; ``flush`` writes.
    """

    def __init__(
        self,
        folder: Path,
        fmt: PcmFormat,
        *,
        max_seconds: float = MAX_RECORDING_SECONDS,
        segment_seconds: float = SEGMENT_SECONDS,
    ) -> None:
        """Prepare a recording; nothing is written until the first flush."""
        self.folder = folder
        self.fmt = fmt
        self.max_seconds = max_seconds
        self.segment_seconds = segment_seconds
        self.started = time.strftime("%Y%m%d-%H%M%S")
        self.buffer = bytearray()
        self.frame_bytes = 2 * fmt.channels
        self.bytes_per_second = fmt.sample_rate * self.frame_bytes
        self.segment = 0
        self.segment_written = 0.0
        self.writer: wave.Wave_write | None = None
        self.full = False
        self.budget = (
            max(0.0, max_seconds - recorded_seconds(folder)) if folder.exists() else max_seconds
        )

    @property
    def buffered_seconds(self) -> float:
        """Return how much audio waits to be written."""
        return len(self.buffer) / self.bytes_per_second

    def add(self, pcm: bytes) -> None:
        """Buffer a chunk of source PCM (converted to s16)."""
        if not self.full:
            self.buffer.extend(pcm_to_s16_interleaved(pcm, self.fmt))

    def flush(self) -> None:
        """Write buffered audio, starting new segments and stopping at the cap."""
        data, self.buffer = bytes(self.buffer), bytearray()
        while data and not self.full:
            if self.budget <= 0:
                self.full = True
                self.close()
                return
            if self.writer is None or self.segment_written >= self.segment_seconds:
                self._next_segment()
            room = min(self.segment_seconds - self.segment_written, self.budget)
            take = int(room * self.fmt.sample_rate) * self.frame_bytes
            part, data = data[:take], data[take:]
            assert self.writer is not None
            self.writer.writeframes(part)
            seconds = len(part) / self.bytes_per_second
            self.segment_written += seconds
            self.budget -= seconds

    def close(self) -> None:
        """Finish the current segment (writes the final WAV header)."""
        if self.writer is not None:
            self.writer.close()
            self.writer = None

    def _next_segment(self) -> None:
        self.close()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.segment += 1
        path = self.folder / f"{self.started}_{self.segment:02d}.wav"
        while path.exists():  # another session started in the same second: never overwrite
            self.segment += 1
            path = self.folder / f"{self.started}_{self.segment:02d}.wav"
        writer = wave.open(str(path), "wb")  # noqa: SIM115 - closed in close()
        writer.setnchannels(self.fmt.channels)
        writer.setsampwidth(2)
        writer.setframerate(self.fmt.sample_rate)
        self.writer = writer
        self.segment_written = 0.0


class SessionLog:
    """
    Copy the provider's INFO log lines to a text file next to a recording.

    The recording then carries its own ground truth (what was identified when), even after the
    Music Assistant container is recreated and its log is gone. The file is written by a
    background thread, never on the event loop. Call close() off the event loop.
    """

    def __init__(self, logger: logging.Logger, path: Path) -> None:
        """Start copying the logger's records to path (opened on the first record)."""
        self._logger = logger
        self._file = logging.FileHandler(path, encoding="utf-8", delay=True)
        self._file.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        records: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        self._handler = QueueHandler(records)
        self._handler.setLevel(logging.INFO)
        self._listener = QueueListener(records, self._file)
        self._listener.start()
        logger.addHandler(self._handler)

    def close(self) -> None:
        """Stop copying, write what is pending and close the file (blocking)."""
        self._logger.removeHandler(self._handler)
        self._listener.stop()
        self._file.close()
