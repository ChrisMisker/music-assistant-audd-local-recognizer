"""State-machine tests for RecognitionEngine (simulated acceptance tests A-H and more)."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Any

import pytest

from audd_local_recognizer import engine as eng
from audd_local_recognizer.audd_client import AudDError, AudDNoMatch
from audd_local_recognizer.engine import (
    Decision,
    GapDetector,
    GapEvent,
    GapSettings,
    MatchConfirmer,
)
from audd_local_recognizer.fingerprints import QueryMatch
from tests.conftest import music, silence

GAP = 3  # seconds of silence between tracks (min gap is 2 s)


def current_passage_id(h: Any) -> str:
    """Return the id of the open passage (a call, so type narrowing from earlier asserts ends)."""
    passage = h.engine.passage
    assert passage is not None
    return str(passage.id)


def songs_in(window: list[int]) -> set[int]:
    return {c // 1000 for c in window if c}


def local_hits(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("LOCAL HIT")]


# ---------------------------------------------------------------- pure pieces


def test_gap_detector_hysteresis() -> None:
    gap = GapDetector(GapSettings(silence_dbfs=-45, resume_dbfs=-35, min_gap_seconds=2))
    assert gap.update(-60, 1) is None
    assert gap.update(-60, 1) is GapEvent.GAP
    assert gap.update(-60, 1) is None  # already in gap
    assert gap.update(-40, 1) is None  # between thresholds: still a gap
    assert gap.in_gap
    assert gap.update(-30, 1) is GapEvent.RESUME
    # a short dip below the threshold is not a gap
    assert gap.update(-60, 1) is None
    assert gap.update(-40, 1) is None  # quiet moment resets the silence counter
    assert gap.update(-60, 1) is None
    assert not gap.in_gap


def _candidate(track: int, votes: int, ratio: float) -> QueryMatch:
    return QueryMatch(track, 1, "p" * 32, votes, 0, votes, ratio)


def test_match_confirmer_rules() -> None:
    confirmer = MatchConfirmer()
    assert confirmer.consider(_candidate(1, 5, 0.6)) is Decision.ACCEPT_STRONG
    assert confirmer.consider(_candidate(1, 3, 0.6)) is Decision.PENDING
    assert confirmer.consider(_candidate(1, 3, 0.6)) is Decision.ACCEPT_CONFIRMED
    # a different track restarts the consecutive run
    assert confirmer.consider(_candidate(1, 3, 0.6)) is Decision.PENDING
    assert confirmer.consider(_candidate(2, 3, 0.6)) is Decision.PENDING
    # random collisions (0-1 votes) never identify
    assert confirmer.consider(_candidate(2, 1, 1.0)) is Decision.REJECT
    # low ratio is rejected even with many votes
    assert confirmer.consider(_candidate(3, 50, 0.3)) is Decision.REJECT


def test_weak_windows_of_one_track_accumulate() -> None:
    """The real replay pattern 2, <=1, 2, 4 (v0.5.3 re-test) identifies at the 4th lookup."""
    confirmer = MatchConfirmer()
    assert confirmer.consider(_candidate(1, 2, 1.0)) is Decision.PENDING
    assert confirmer.consider(_candidate(1, 1, 1.0)) is Decision.REJECT
    assert confirmer.consider(_candidate(1, 2, 1.0)) is Decision.PENDING
    assert confirmer.consider(_candidate(1, 4, 1.0)) is Decision.ACCEPT_ACCUMULATED


def test_accumulation_needs_two_windows_and_the_same_track() -> None:
    confirmer = MatchConfirmer()
    assert confirmer.consider(_candidate(1, 4, 1.0)) is Decision.PENDING
    assert confirmer.consider(_candidate(2, 4, 1.0)) is Decision.PENDING  # other track: no sum
    assert confirmer.consider(_candidate(1, 2, 1.0)) is Decision.ACCEPT_ACCUMULATED  # 4 + 2


def test_one_vote_windows_never_accumulate() -> None:
    confirmer = MatchConfirmer()
    for _ in range(20):
        assert confirmer.consider(_candidate(1, 1, 1.0)) is Decision.REJECT


def test_old_evidence_expires() -> None:
    confirmer = MatchConfirmer()
    assert confirmer.consider(_candidate(1, 2, 1.0)) is Decision.PENDING
    for _ in range(eng.ACCUMULATION_LOOKUPS - 1):
        confirmer.record_miss()
    # the 2-vote window is now older than the last ACCUMULATION_LOOKUPS lookups
    assert confirmer.consider(_candidate(1, 2, 1.0)) is Decision.PENDING
    assert confirmer.consider(_candidate(1, 2, 1.0)) is Decision.PENDING  # 2 + 2 = 4 < 6


def test_miss_breaks_consecutive_run_but_not_accumulation() -> None:
    confirmer = MatchConfirmer()
    assert confirmer.consider(_candidate(1, 3, 1.0)) is Decision.PENDING
    confirmer.record_miss()
    # not consecutive any more, but 3 + 3 within the last lookups adds up
    assert confirmer.consider(_candidate(1, 3, 1.0)) is Decision.ACCEPT_ACCUMULATED


# ---------------------------------------------------------------- acceptance A-H


async def test_a_first_unknown_track_learns_without_self_recognition(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    assert len(h.audd_calls) == 1
    assert [m["title"] for m in h.published] == ["Song 1"]
    refs = h.references()
    assert len(refs) >= 8  # early lookup windows + one every 6 s
    assert {p for _, p in refs} == {h.engine.passage.id}
    assert local_hits(caplog) == []
    # identified passages only learn: one lookup window, every other window was learned
    assert len(h.windows) == len(refs)


async def test_b_track_transition_is_a_hard_boundary(make_harness: Any) -> None:
    h = make_harness()
    await h.play(1, 30)
    first_passage = h.engine.passage
    await h.quiet(GAP)
    assert h.engine.passage is None
    await h.play(2, 30)
    assert h.engine.passage is not first_passage
    assert [m["title"] for m in h.published] == ["Song 1", "Song 2"]
    for window in h.windows:
        assert len(songs_in(window)) == 1, window  # never two songs in one window
    for window in h.windows:
        if songs_in(window) == {2}:
            assert 0 not in window  # no pre-boundary silence either
    # the AudD sample for song 2 is pure song 2
    assert songs_in(h.audd_calls[-1]) == {2}
    # song 2 is learned under its own track, in its own passage
    refs = h.references()
    assert {t for t, p in refs if p == current_passage_id(h)} == {2}


async def test_c_audd_miss_then_retry_in_same_passage(make_harness: Any) -> None:
    h = make_harness(audd_script=[None, None])
    await h.play(1, 90)
    # attempts at 24 s, 54 s (both scripted misses) and 84 s (hit)
    assert len(h.audd_calls) == 3
    assert [m["title"] for m in h.published] == ["Song 1"]


async def test_c_previous_identity_never_blocks_next_track(make_harness: Any) -> None:
    """Regression: an identified track A must not suppress AudD for track B (stale last_key)."""
    h = make_harness()
    await h.play(1, 40)
    await h.quiet(GAP)
    h.audd_script = [None]
    await h.play(2, 80)
    assert [m["title"] for m in h.published] == ["Song 1", "Song 2"]
    assert len(h.audd_calls) == 3  # A, B miss, B hit


async def test_d_replay_in_same_stream_matches_earlier_passage(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    passage_a = h.engine.passage.id
    await h.quiet(GAP)
    await h.play(2, 40)
    await h.quiet(GAP)
    await h.play(1, 40)
    assert len(h.audd_calls) == 2  # song 1 and song 2 once each; the replay is local
    assert [m["title"] for m in h.published] == ["Song 1", "Song 2", "Song 1"]
    hits = local_hits(caplog)
    assert len(hits) == 1
    assert f"ref_passage={passage_a[:8]}" in hits[0]


async def test_e_persistence_across_restart(make_harness: Any) -> None:
    first = make_harness()
    await first.play(1, 60)
    first.engine.close()
    # new engine + new database object on the same file = MA restart
    second = make_harness()
    await second.play(1, 30)
    assert second.audd_calls == []
    assert [m["title"] for m in second.published] == ["Song 1"]


async def test_f_idle_source_does_nothing(make_harness: Any) -> None:
    h = make_harness()
    await h.quiet(120)
    assert h.windows == []
    assert h.audd_calls == []
    assert h.published == []
    assert h.engine.passage is None


async def test_g_late_fingerprint_result_is_discarded(make_harness: Any) -> None:
    gate = asyncio.Event()
    h = make_harness(fingerprint_gate=gate)
    for second in range(12):
        h.engine.feed(music(1, second))
    late = h.engine.task
    assert late is not None
    assert not late.done()
    for _ in range(GAP):
        h.engine.feed(silence())
    for second in range(5):
        h.engine.feed(music(2, second))
    new_passage = h.engine.passage
    gate.set()
    await asyncio.wait_for(late, 5)
    assert h.audd_calls == []
    assert h.published == []
    assert h.references() == []
    assert h.engine.passage is new_passage
    assert not new_passage.identified


async def test_g_late_audd_result_is_discarded(make_harness: Any) -> None:
    gate = asyncio.Event()
    h = make_harness(audd_gate=gate)
    await h.play(1, 23)  # local-only lookups at 12 s and 18 s
    h.engine.feed(music(1, 23))  # 24 s: first AudD attempt, held at the gate
    late = h.engine.task
    for _ in range(3):
        await asyncio.sleep(0)
    assert len(h.audd_calls) == 1  # waiting on AudD
    for _ in range(GAP):
        h.engine.feed(silence())
    h.engine.feed(music(2, 0))
    gate.set()
    await asyncio.wait_for(late, 5)
    assert h.published == []
    assert h.references() == []
    assert h.db.stats().tracks == 0
    assert not h.engine.passage.identified


async def test_h_different_track_is_not_a_false_local_hit(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    await h.play(3, 30)
    assert local_hits(caplog) == []
    assert [m["title"] for m in h.published] == ["Song 1", "Song 3"]
    assert len(h.audd_calls) == 2


# ---------------------------------------------------------------- AudD scheduling


async def test_audd_cadence_and_cap_for_unknown_passage(make_harness: Any) -> None:
    h = make_harness(audd_known=set())
    await h.play(9, 1000)
    # 24, 54, 84 (30 s apart), then every 90 s, capped at 8 attempts per passage
    assert len(h.audd_calls) == eng.AUDD_MAX_ATTEMPTS_PER_PASSAGE
    window_starts = [c[0] % 1000 for c in h.audd_calls]
    assert window_starts[:4] == [12, 42, 72, 162]  # never the 0-12 s intro


async def test_audd_cadence_resets_for_new_passage(make_harness: Any) -> None:
    h = make_harness(audd_known=set())
    await h.play(9, 200)  # 4 attempts, now on the slow cadence
    await h.quiet(GAP)
    h.audd_known = {2}
    calls_before = len(h.audd_calls)
    await h.play(2, 30)
    assert len(h.audd_calls) == calls_before + 1
    assert h.published[-1]["title"] == "Song 2"


async def test_error_300_style_no_match_does_not_back_off(make_harness: Any) -> None:
    h = make_harness(audd_known=set())
    await h.play(9, 60)
    assert len(h.audd_calls) == 2
    assert h.engine.audd_backoff_seconds == 0


async def test_real_audd_failure_backs_off(make_harness: Any) -> None:
    h = make_harness(audd_script=[AudDError("AudD HTTP 500")] * 3)
    await h.play(1, 100)
    # 24 s fails -> blocked 60 s; 84 s fails -> blocked 120 s
    assert len(h.audd_calls) == 2
    assert h.engine.audd_backoff_seconds == 120
    assert h.published == []


async def test_no_token_recognizes_known_tracks_only(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    learner = make_harness()
    await learner.play(1, 60)
    learner.engine.close()
    caplog.set_level(logging.WARNING)
    h = make_harness(has_token=False)
    await h.play(3, 30)  # unknown: cannot be identified, warned once
    await h.quiet(GAP)
    await h.play(1, 20)  # known: recognized locally
    assert h.audd_calls == []
    assert [m["title"] for m in h.published] == ["Song 1"]
    assert sum("No AudD API token" in r.getMessage() for r in caplog.records) == 1


async def test_declined_metadata_stops_recognition(make_harness: Any) -> None:
    h = make_harness(publish_result=False)
    await h.play(1, 60)
    assert h.engine.stopped
    assert len(h.published) == 1
    assert h.references() == []


async def test_learning_stops_after_cap(make_harness: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(eng, "MAX_LEARN_SECONDS_PER_PASSAGE", 40)
    h = make_harness()
    await h.play(1, 120)
    # early lookup windows (ending 12, 18, 24 s) + learns at 30, 36 s; none after 40 s
    assert len(h.references()) == 5


async def test_close_cancels_running_task(make_harness: Any) -> None:
    h = make_harness(fingerprint_gate=asyncio.Event())
    for second in range(12):
        h.engine.feed(music(1, second))
    task = h.engine.task
    h.engine.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    h.engine.feed(music(1, 12))  # ignored after close
    assert h.engine.passage is None


# ---------------------------------------------------------------- early windows


def _offsets(h: Any) -> list[float]:
    import sqlite3  # noqa: PLC0415

    with sqlite3.connect(h.db.path) as conn:
        return [
            float(o)
            for (o,) in conn.execute(
                "SELECT passage_offset_seconds FROM reference_windows ORDER BY id"
            )
        ]


async def test_early_lookup_windows_are_learned_after_identification(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(audd_script=[None])  # AudD misses at 24 s, hits at 54 s
    await h.play(1, 60)
    offsets = _offsets(h)
    # every lookup window from the very start (0-12 s) up to the AudD window (42-54 s)
    assert offsets[:8] == [0, 6, 12, 18, 24, 30, 36, 42]
    assert any("Learned 8 early references" in r.getMessage() for r in caplog.records)


async def test_replay_matches_locally_from_the_start(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(audd_script=[None])  # identified late (54 s) on first play
    await h.play(1, 60)
    await h.quiet(GAP)
    await h.play(1, 12)  # replay: identified at the very first lookup
    assert len(h.audd_calls) == 2
    assert h.engine.passage.identified
    assert len(local_hits(caplog)) == 1


async def test_local_hit_also_stores_early_windows(make_harness: Any) -> None:
    h = make_harness()
    await h.play(1, 40)
    await h.quiet(GAP)
    await h.play(1, 13)
    replay = h.engine.passage.id
    assert [p for _, p in h.references()].count(replay) >= 1


async def test_early_windows_are_capped(make_harness: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(eng, "MAX_EARLY_WINDOWS", 3)
    h = make_harness(audd_script=[None, None])  # hit at 84 s after 13 lookups
    await h.play(1, 85)
    # 3 kept early windows + the AudD window itself
    assert _offsets(h)[:4] == [0, 6, 12, 72]


async def test_early_windows_of_a_closed_passage_are_never_stored(make_harness: Any) -> None:
    h = make_harness(audd_known=set())
    await h.play(9, 30)
    await h.quiet(GAP)
    h.audd_known = {2}
    await h.play(2, 30)
    assert {t for t, _ in h.references()} == {1}  # only song 2's track (id 1)
    assert all(songs_in(w) for w in h.windows)


# ---------------------------------------------------------------- v0.7: accumulation and cap


async def test_sparse_replay_is_identified_by_accumulated_evidence(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A replay whose windows each repeat only 2 landmarks is still recognized locally."""
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    h.sparse_every = 6  # every 12 s window now shares exactly 2 landmarks with the references
    await h.play(1, 40)
    assert len(h.audd_calls) == 1  # only the first play; the replay never needed AudD
    hits = local_hits(caplog)
    assert len(hits) == 1
    assert "accepted (accumulated)" in hits[0]
    assert any("Identified Artist 1 - Song 1 via local" in r.getMessage() for r in caplog.records)


async def test_plays_per_track_are_capped(make_harness: Any) -> None:
    h = make_harness()
    passages = []
    for _ in range(3):
        await h.play(1, 60)
        assert h.engine.passage is not None
        passages.append(h.engine.passage.id)
        await h.quiet(GAP)
    plays = {p for _, p in h.references()}
    # the two most recent plays stay complete; the oldest went whole
    assert plays == {passages[1], passages[2]}


async def test_audd_hit_on_a_known_track_shows_the_stored_metadata(make_harness: Any) -> None:
    """What MA shows equals what is stored, so the album does not jump between plays."""
    h = make_harness()
    await h.play(1, 30)
    first_album = h.db.track_metadata(1)
    await h.quiet(GAP)
    h.db.prune_track(1, "none", 0)  # forget the fingerprints, keep the track: AudD again
    await h.play(1, 30)
    assert len(h.audd_calls) == 2
    assert h.published[-1] == first_album


async def test_abnormally_dense_windows_are_not_stored(
    make_harness: Any, monkeypatch: Any, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(eng, "MAX_LANDMARKS_PER_REFERENCE", 5)  # the fake windows hold ~12
    caplog.set_level(logging.WARNING)
    h = make_harness()
    await h.play(1, 60)
    assert [m["title"] for m in h.published] == ["Song 1"]  # recognition itself is unaffected
    assert h.references() == []
    assert any("Not learning a window" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- stale work, more stages


async def test_g_late_database_lookup_is_discarded(make_harness: Any) -> None:
    """A lookup of passage N that returns a strong hit after N closed changes nothing in N+1."""
    learner = make_harness()
    await learner.play(1, 60)  # song 1 is known: the late lookup would be a strong local hit
    learner.engine.close()
    gate = asyncio.Event()
    h = make_harness(lookup_gate=gate)
    for second in range(12):
        h.engine.feed(music(1, second))
        await asyncio.sleep(0)
    late = h.engine.task
    assert late is not None
    assert not late.done()  # waiting on the database
    for _ in range(GAP):
        h.engine.feed(silence())
    for second in range(5):
        h.engine.feed(music(2, second))
    new_passage = h.engine.passage
    assert new_passage is not None
    references_before = h.references()
    gate.set()
    await asyncio.wait_for(late, 5)
    assert h.published == []  # no stale metadata
    assert not new_passage.identified  # no track activated in N+1
    assert h.references() == references_before  # nothing written for N or N+1
    assert new_passage.confirmer.pending_track_id is None  # pending state untouched
    assert list(new_passage.confirmer.history) == []
    assert new_passage.early_windows == []


async def test_g_late_result_leaves_pending_evidence_of_new_passage_alone(
    make_harness: Any,
) -> None:
    """Evidence collected by passage N+1 survives a stale task of N finishing."""
    learner = make_harness()
    await learner.play(1, 60)
    await learner.quiet(GAP)
    await learner.play(2, 60)
    learner.engine.close()
    gate = asyncio.Event()
    h = make_harness(lookup_gate=gate, sparse_every=6)
    for second in range(12):
        h.engine.feed(music(1, second))
        await asyncio.sleep(0)
    late = h.engine.task
    assert late is not None
    for _ in range(GAP):
        h.engine.feed(silence())
    h.engine.feed(music(2, 0))  # audio resumes: passage N+1 opens
    new_passage = h.engine.passage
    assert new_passage is not None
    new_passage.confirmer.consider(_candidate(2, 2, 1.0))  # N+1 already holds evidence
    history_before = list(new_passage.confirmer.history)
    gate.set()
    await asyncio.wait_for(late, 5)
    assert list(new_passage.confirmer.history) == history_before
    assert h.published == []


# ---------------------------------------------------------------- debug samples


async def test_audd_samples_are_kept_with_outcome_and_measurements(make_harness: Any) -> None:
    h = make_harness(samples=[], audd_script=[None, AudDError("AudD HTTP 500")])
    await h.play(1, 140)  # 24 s no match, 54 s error -> backoff, 114 s hit
    assert h.samples is not None
    outcomes = [info["outcome"] for _, info in h.samples]
    assert outcomes[0] == "no match: no result"
    assert outcomes[1].startswith("error: AudD HTTP 500")
    assert outcomes[-1] == "hit: Artist 1 - Song 1"
    wav, info = h.samples[0]
    assert wav.startswith(b"RIFF")
    assert info["attempt"] == 1
    assert info["window_seconds"] == [12.0, 24.0]
    assert {"rms_dbfs", "peak_dbfs", "clipped_ratio", "lr_correlation"} <= set(info)


async def test_no_samples_kept_when_the_option_is_off(make_harness: Any) -> None:
    h = make_harness()
    await h.play(1, 30)
    assert h.samples is None
    assert h.engine._save_sample is None


async def test_no_match_reason_is_logged(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(audd_script=[AudDNoMatch("error 300 (no fingerprint from the sample)")])
    await h.play(1, 30)
    assert any(
        "AudD: no match: error 300 (no fingerprint from the sample)" in r.getMessage()
        for r in caplog.records
    )


async def test_audd_sample_uses_the_speed_correction(make_harness: Any) -> None:
    h = make_harness(samples=[], audd_speed_percent=-2.0)
    await h.play(1, 30)
    assert h.samples is not None
    wav, info = h.samples[0]
    assert info["speed_correction_percent"] == -2.0
    with wave.open(io.BytesIO(wav)) as reader:
        assert reader.getnframes() > 12 * 100  # 12 s at 100 Hz, made ~2 % longer
    assert h.published  # the fake AudD still identifies the (slightly slower) sample


async def test_fast_start_fingerprints_the_start_of_a_passage_densely(make_harness: Any) -> None:
    h = make_harness(fast_start_seconds=30)
    await h.play(1, 60)
    # lookups/learns whose window ends at <= 30 s use 1/4, later ones 1/16
    assert h.divisors[:4] == [4, 4, 4, 4]
    assert h.divisors[-1] == 16
    plain = make_harness()
    await plain.play(2, 30)
    assert set(plain.divisors) == {16}


def _due_passage(h: Any) -> Any:
    """Return the open passage with AudD due (24 s of audio, no attempts yet)."""
    passage = h.engine.passage
    assert passage is not None
    passage.seconds = 24.0
    return passage


async def test_audd_is_postponed_only_for_a_promising_candidate(make_harness: Any) -> None:
    h = make_harness()
    h.engine.feed(music(1, 0))
    passage = _due_passage(h)
    assert not h.engine._postpone_audd(passage)  # option off
    h.engine.postpone_audd = True
    assert not h.engine._postpone_audd(passage)  # no candidate yet
    passage.confirmer.consider(_candidate(7, 1, 1.0))  # an accidental 1-vote candidate
    assert not h.engine._postpone_audd(passage)
    passage.confirmer.consider(_candidate(7, 2, 1.0))  # 2 votes: accidental ones reach that
    assert not h.engine._postpone_audd(passage)
    passage.confirmer.consider(_candidate(7, 3, 1.0))  # a promising candidate
    assert h.engine._postpone_audd(passage)
    assert h.engine._postpone_audd(passage)
    assert not h.engine._postpone_audd(passage)  # at most MAX_AUDD_POSTPONES per passage
    assert passage.audd_postponed == eng.MAX_AUDD_POSTPONES


async def test_audd_is_not_postponed_when_it_is_not_due(make_harness: Any) -> None:
    h = make_harness()
    h.engine.postpone_audd = True
    h.engine.feed(music(1, 0))
    passage = _due_passage(h)
    passage.seconds = 18.0  # before the first AudD attempt
    passage.confirmer.consider(_candidate(7, 3, 1.0))
    assert not h.engine._postpone_audd(passage)
    assert passage.audd_postponed == 0


async def test_without_token_nothing_is_postponed(make_harness: Any) -> None:
    h = make_harness(has_token=False)
    h.engine.postpone_audd = True
    h.engine.feed(music(1, 0))
    passage = _due_passage(h)
    passage.confirmer.consider(_candidate(7, 3, 1.0))
    assert not h.engine._postpone_audd(passage)
