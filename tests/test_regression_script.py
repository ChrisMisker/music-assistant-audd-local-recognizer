"""The regression script's summary and comparison (no audio, no replay)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PREFIX = "2026-11-19 {} INFO (MainThread) [music_assistant.audd_local_recognizer] "


def _module() -> Any:
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("regression", REPO / "scripts" / "regression.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["regression"] = module
    spec.loader.exec_module(module)
    return module


def _lines(*entries: tuple[str, str]) -> list[str]:
    return [PREFIX.format(f"{when}.000") + text + "\n" for when, text in entries]


def test_summary_flags_a_wrong_local_hit_and_accidental_votes() -> None:
    regression = _module()
    lines = _lines(
        ("10:00:00", "Passage aaaaaaaa started (stream started)"),
        (
            "10:00:14",
            (
                "LOCAL CANDIDATE: X - Other votes=2 acc=2 ratio=1.000 shared=2 ref=1 offset=+1 "
                "ref_passage=bbbbbbbb at=14s -> rejected (passage aaaaaaaa)"
            ),
        ),
        (
            "10:00:20",
            (
                "LOCAL HIT: A - Song votes=6 acc=6 ratio=0.800 shared=7 ref=2 offset=+1 "
                "ref_passage=cccccccc at=20s -> accepted (passage aaaaaaaa)"
            ),
        ),
        ("10:00:20", "Identified A - Song via local after 20s (passage aaaaaaaa, AudD calls 0)"),
        ("10:03:00", "Track boundary: -50.0 dBFS for 2.0s; passage aaaaaaaa closed after 180s"),
    )

    def truth(_when: datetime) -> str:
        return "B - Real"

    [passage] = regression.summarize(lines, truth)
    assert (passage.track, passage.via, passage.after) == ("A - Song", "local", 20)
    assert passage.max_other_votes == 2
    assert passage.wrong
    folder = regression.FolderResult(passages=[passage])
    assert folder.totals()["wrong"] == 1
    report = "\n".join(regression.compare({"rec": folder}, None))
    assert "WRONG A - Song (live: B - Real)" in report


def test_compare_lists_changed_passages(tmp_path: Path) -> None:
    regression = _module()
    before = regression.PassageResult("10:00:00", 180, "A - Song", "AudD", 24, 1, 2, "A - Song")
    after = regression.PassageResult("10:00:00", 180, "A - Song", "local", 18, 0, 2, "A - Song")
    path = tmp_path / "run.json"
    regression.save(path, {"rec": regression.FolderResult(passages=[before])}, "abc")
    baseline = regression.load(path)
    report = "\n".join(
        regression.compare({"rec": regression.FolderResult(passages=[after])}, baseline)
    )
    assert "local 0→1" in report
    assert "audd_calls 1→0" in report
    assert "A - Song (AudD @24s, 1 AudD) → A - Song (local @18s, 0 AudD)" in report


def test_truth_uses_its_own_session_and_the_track_key_rules(tmp_path: Path) -> None:
    """An earlier session's open last passage and an edition suffix are no WRONG."""
    regression = _module()
    head = "INFO (MainThread) [music_assistant.audd_local_recognizer] "
    (tmp_path / "20261119-100000.log").write_text(
        f"2026-11-19 10:00:01.000 {head}Passage aaaaaaaa started (stream started)\n"
        f"2026-11-19 10:00:25.000 {head}Identified A - Old via AudD after 24s "
        "(passage aaaaaaaa, AudD calls 1)\n",
        encoding="utf-8",
    )
    (tmp_path / "20261119-103000.log").write_text(
        f"2026-11-19 10:30:01.000 {head}Passage bbbbbbbb started (stream started)\n"
        f"2026-11-19 10:30:25.000 {head}Identified B - New (Album Version) via AudD after 24s "
        "(passage bbbbbbbb, AudD calls 1)\n",
        encoding="utf-8",
    )
    truth = regression._truths(tmp_path)
    assert truth(datetime(2026, 11, 19, 10, 30, 40)) == "B - New (Album Version)"  # noqa: DTZ001
    same = regression._same_track(tmp_path)
    assert same("B - New (Album Version)", "B - New")
    assert not same("B - New", "A - Old")
