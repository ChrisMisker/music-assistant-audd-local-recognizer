"""Smoke test: the offline replay script runs on a recording and reports passages."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import soundfile

REPO = Path(__file__).resolve().parents[1]


def _music(seconds: int, rate: int = 44100) -> np.ndarray:
    t = np.arange(seconds * rate) / rate
    tones = np.zeros_like(t)
    for f, m in [(220, 0.3), (440, 0.7), (880, 1.1), (1320, 0.2)]:
        tones += np.sin(2 * np.pi * f * t) * (0.5 + 0.5 * np.sin(2 * np.pi * m * t))
    music: np.ndarray = (0.1 * tones * 32767).astype("<i2")
    return music


def test_replay_runs_and_reports(tmp_path: Path) -> None:
    signal = np.concatenate([_music(20), np.zeros(3 * 44100, dtype="<i2"), _music(15)])
    with wave.open(str(tmp_path / "20261118-080000_01.wav"), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(np.repeat(signal, 2).tobytes())
    result = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [sys.executable, str(REPO / "scripts" / "replay_recording.py"), str(tmp_path), "--report"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert "passage" in result.stdout
    assert "Summary" in result.stdout
    assert "08:00:" in result.stdout  # log times follow the recording's clock


def test_replay_reads_flac_segments(tmp_path: Path) -> None:
    signal = np.concatenate([_music(20), np.zeros(3 * 44100, dtype="<i2"), _music(15)])
    stereo = np.repeat(signal, 2).reshape(-1, 2)
    soundfile.write(str(tmp_path / "20261118-080000_01.flac"), stereo, 44100, subtype="PCM_16")
    result = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [sys.executable, str(REPO / "scripts" / "replay_recording.py"), str(tmp_path), "--report"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert "Replayed 1 segments" in result.stderr
    assert "Summary" in result.stdout


def _replay_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "replay_recording", REPO / "scripts" / "replay_recording.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_oracle_answers_with_timecode_and_duration_from_a_session_log(tmp_path: Path) -> None:
    log = tmp_path / "20261118-080000.log"
    prefix = "INFO (MainThread) [music_assistant.audd_local_recognizer]"
    log.write_text(
        f"2026-11-18 08:00:00.000 {prefix} Passage abcdef12 started (stream started)\n"
        f"2026-11-18 08:00:24.000 {prefix} Identified A - T via AudD after 24s "
        "(passage abcdef12, AudD calls 1)\n"
        f"2026-11-18 08:00:24.001 {prefix} Expected end of A - T at 200s (song started at 0s, "
        "duration 200s) (passage abcdef12)\n",
        encoding="utf-8",
    )
    oracle = _replay_module().Oracle(log)
    answer = oracle.answer(datetime(2026, 11, 18, 8, 1, 30))  # noqa: DTZ001 - log clock
    assert answer is not None
    assert (answer["artist"], answer["title"]) == ("A", "T")
    assert answer["audd_timecode"] == "01:30"
    assert answer["duration_seconds"] == 200.0
    assert oracle.answer(datetime(2026, 11, 18, 7, 0, 0)) is None  # noqa: DTZ001


def test_oracle_uses_the_logged_song_start_and_the_stream_start(tmp_path: Path) -> None:
    """A song that began before its passage, and a passage started before the log did."""
    log = tmp_path / "20261119-111840.log"
    prefix = "INFO (MainThread) [music_assistant.audd_local_recognizer]"
    log.write_text(
        f"2026-11-19 11:18:40.000 {prefix} Recording the input of session s to /data "
        "(300 of 300 minutes left); provider v0.9, source 48000Hz/2ch/f32le, "
        "speed correction -2.0%, fast start on\n"
        f"2026-11-19 11:19:10.000 {prefix} Identified A - One via AudD after 29s "
        "(passage aaaaaaaa, AudD calls 1)\n"
        f"2026-11-19 11:22:00.000 {prefix} Track boundary (no gap): A - One should have ended "
        "at 175s; passage aaaaaaaa closed after 180s\n"
        f"2026-11-19 11:22:00.000 {prefix} Passage bbbbbbbb started (no gap)\n"
        f"2026-11-19 11:22:27.000 {prefix} Identified A - Two via AudD after 27s "
        "(passage bbbbbbbb, AudD calls 1)\n"
        f"2026-11-19 11:22:27.001 {prefix} Expected end of A - Two at 271s (song started at "
        "-98s, duration 557s) (passage bbbbbbbb)\n",
        encoding="utf-8",
    )
    oracle = _replay_module().Oracle(log)
    first = oracle.answer(datetime(2026, 11, 19, 11, 19, 10))  # noqa: DTZ001 - log clock
    assert first is not None
    assert first["title"] == "One"
    assert first["audd_timecode"] == "00:31"  # 30 s since the log began, / 0.98
    second = oracle.answer(datetime(2026, 11, 19, 11, 22, 27))  # noqa: DTZ001
    assert second is not None
    assert second["title"] == "Two"
    assert second["audd_timecode"] == "02:08"  # (27 + 98) s / 0.98 = 127.6 s
    assert second["duration_seconds"] == 557.0
