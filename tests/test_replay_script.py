"""Smoke test: the offline replay script runs on a recording and reports passages."""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

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
