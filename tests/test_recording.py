"""Input recording: conversion, segments and the cap on the total recorded time."""

from __future__ import annotations

import logging
import re
import wave
from logging.handlers import QueueHandler
from typing import TYPE_CHECKING

import numpy as np
import pytest

from audd_local_recognizer.fingerprints import PcmFormat
from audd_local_recognizer.recording import (
    InputRecorder,
    SessionLog,
    pcm_to_s16_interleaved,
    recorded_seconds,
)

if TYPE_CHECKING:
    from pathlib import Path

RATE = 1000  # small rate keeps the tests fast
F32 = PcmFormat(RATE, 2, 4, "f32le")


def _seconds_of_f32(seconds: float, value: float = 0.5) -> bytes:
    return np.full(int(seconds * RATE) * 2, value, dtype="<f4").tobytes()


def _durations(folder: Path) -> list[float]:
    out = []
    for path in sorted(folder.glob("*.wav")):
        with wave.open(str(path), "rb") as reader:
            assert reader.getnchannels() == 2
            assert reader.getsampwidth() == 2
            assert reader.getframerate() == RATE
            out.append(reader.getnframes() / RATE)
    return out


def test_conversion_keeps_channels_and_scales() -> None:
    f32 = np.array([0.5, -0.5, 1.5, -1.5], dtype="<f4").tobytes()
    assert np.frombuffer(pcm_to_s16_interleaved(f32, F32), dtype="<i2").tolist() == [
        16384,
        -16384,
        32767,
        -32768,
    ]
    s16 = np.array([1, 2, 3, 4], dtype="<i2").tobytes()
    assert pcm_to_s16_interleaved(s16, PcmFormat(RATE, 2, 2, "s16le")) == s16
    s32 = np.array([2**30, -(2**30)], dtype="<i4").tobytes()
    assert np.frombuffer(
        pcm_to_s16_interleaved(s32, PcmFormat(RATE, 2, 4, "s32le")), dtype="<i2"
    ).tolist() == [16384, -16384]
    with pytest.raises(ValueError, match="Unsupported"):
        pcm_to_s16_interleaved(b"\0" * 8, PcmFormat(RATE, 2, 4, "unknown"))


def test_segments_and_valid_wav_files(tmp_path: Path) -> None:
    recorder = InputRecorder(tmp_path, F32, max_seconds=100, segment_seconds=10)
    for _ in range(25):
        recorder.add(_seconds_of_f32(1))
    recorder.flush()
    recorder.close()
    assert _durations(tmp_path) == [10.0, 10.0, 5.0]
    assert recorded_seconds(tmp_path) == 25.0


def test_recording_stops_at_the_cap_also_across_sessions(tmp_path: Path) -> None:
    first = InputRecorder(tmp_path, F32, max_seconds=30, segment_seconds=10)
    first.add(_seconds_of_f32(20))
    first.flush()
    first.close()
    second = InputRecorder(tmp_path, F32, max_seconds=30, segment_seconds=10)
    assert second.budget == 10.0
    second.add(_seconds_of_f32(25))
    second.flush()
    second.add(_seconds_of_f32(5))
    second.flush()
    second.close()
    assert second.full
    assert recorded_seconds(tmp_path) == 30.0
    third = InputRecorder(tmp_path, F32, max_seconds=30, segment_seconds=10)
    assert third.budget == 0.0


def test_nothing_is_written_before_the_first_flush(tmp_path: Path) -> None:
    recorder = InputRecorder(tmp_path / "rec", F32)
    recorder.add(_seconds_of_f32(2))
    assert recorder.buffered_seconds == pytest.approx(2.0)
    assert not (tmp_path / "rec").exists()


def test_session_log_copies_info_lines_in_the_ma_format(tmp_path: Path) -> None:
    logger = logging.getLogger("test.session_log")
    logger.setLevel(logging.DEBUG)
    path = tmp_path / "20261118-121907.log"
    session_log = SessionLog(logger, path)
    logger.debug("not copied")
    logger.info("Passage %s started (stream started)", "abcdef12")
    session_log.close()
    logger.info("after close: not copied")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert re.match(
        r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} INFO \(\w+\) \[test\.session_log\] "
        r"Passage abcdef12 started \(stream started\)$",
        lines[0],
    )
    assert not any(isinstance(h, QueueHandler) for h in logger.handlers)
