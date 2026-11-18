"""Debug sample files: names, contents and the maximum kept."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from audd_local_recognizer import debug_samples
from audd_local_recognizer.debug_samples import write_debug_sample

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_sample_and_info_are_written(tmp_path: Path) -> None:
    info = {"passage": "4b278839abcdef", "attempt": 2, "outcome": "no match: no result"}
    wav = write_debug_sample(tmp_path / "debug", b"RIFFdata", info)
    assert wav.name.endswith("_4b278839_a2_nomatch.wav")
    assert wav.read_bytes() == b"RIFFdata"
    saved = json.loads(wav.with_suffix(".json").read_text())
    assert saved["outcome"] == "no match: no result"
    assert "saved_at" in saved


def test_only_the_newest_samples_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(debug_samples, "MAX_DEBUG_SAMPLES", 3)
    folder = tmp_path / "debug"
    for attempt in range(5):
        write_debug_sample(
            folder, b"RIFF", {"passage": "p", "attempt": attempt, "outcome": "hit: x"}
        )
    wavs = sorted(folder.glob("*.wav"))
    assert len(wavs) == 3
    assert len(list(folder.glob("*.json"))) == 3
    assert [w.stem.split("_a")[1].split("_")[0] for w in wavs] == ["2", "3", "4"]


def test_unsafe_characters_never_reach_the_file_name(tmp_path: Path) -> None:
    wav = write_debug_sample(
        tmp_path, b"RIFF", {"passage": "../../x y", "attempt": 1, "outcome": "hit: a/b"}
    )
    assert wav.parent == tmp_path
    assert "/" not in wav.name
