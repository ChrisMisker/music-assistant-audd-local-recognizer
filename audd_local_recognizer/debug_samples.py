"""Saving AudD samples for troubleshooting (opt-in advanced setting; stays on the server)."""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

MAX_DEBUG_SAMPLES = 40


def write_debug_sample(folder: Path, wav: bytes, info: dict[str, Any]) -> Path:
    """
    Write one AudD sample (WAV + JSON) and keep only the newest MAX_DEBUG_SAMPLES.

    Returns the path of the WAV file. File names sort by time and say passage, attempt and
    outcome, e.g. ``20261118-073302_4b278839_a2_nomatch.wav``.
    """
    folder.mkdir(parents=True, exist_ok=True)
    kind = re.sub(r"[^a-z]", "", str(info.get("outcome", "")).split(":", 1)[0].lower())
    stem = (
        f"{time.strftime('%Y%m%d-%H%M%S')}_{str(info.get('passage', ''))[:8]}"
        f"_a{int(info.get('attempt', 0))}_{kind or 'unknown'}"
    )
    stem = re.sub(r"[^A-Za-z0-9_.-]", "", stem)
    wav_path = folder / f"{stem}.wav"
    suffix = 1
    while wav_path.exists():  # two samples within the same second
        suffix += 1
        wav_path = folder / f"{stem}-{suffix}.wav"
    wav_path.write_bytes(wav)
    info = {**info, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    wav_path.with_suffix(".json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    samples = sorted(folder.glob("*.wav"))
    for old in samples[: max(0, len(samples) - MAX_DEBUG_SAMPLES)]:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
    return wav_path
