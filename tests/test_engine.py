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
from audd_local_recognizer.database import MatchRow
from audd_local_recognizer.engine import (
    Decision,
    GapDetector,
    GapEvent,
    GapSettings,
    MatchConfirmer,
    TimelineEvidence,
)
from audd_local_recognizer.fingerprints import QueryMatch
from tests.conftest import PLAYING, metadata_for, music, silence

GAP = 3  # seconds of silence between tracks (min gap is 2 s)
LIFT = 6  # seconds without music when the needle is lifted and put down again
QUIET_MOMENT = 2  # a quiet moment inside a song: the gap is confirmed and the music is back


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
    # 5 votes in one window is not enough at once (a wrong track reached 5 live, 2026-11-21)
    assert MatchConfirmer().consider(_candidate(1, 5, 0.9)) is Decision.PENDING
    confirmer = MatchConfirmer()
    assert confirmer.consider(_candidate(1, 6, 0.6)) is Decision.ACCEPT_STRONG
    assert confirmer.consider(_candidate(1, 3, 0.6)) is Decision.PENDING
    assert confirmer.consider(_candidate(1, 3, 0.6)) is Decision.ACCEPT_CONFIRMED
    # a different track restarts the consecutive run
    assert confirmer.consider(_candidate(1, 3, 0.6)) is Decision.PENDING
    assert confirmer.consider(_candidate(2, 3, 0.6)) is Decision.PENDING
    # random collisions (0-1 votes) never identify
    assert confirmer.consider(_candidate(2, 1, 1.0)) is Decision.REJECT
    # low ratio is rejected even with many votes
    assert confirmer.consider(_candidate(3, 50, 0.3)) is Decision.REJECT


def _rows(track: int, passage: str, shifts: list[tuple[str, float]]) -> list[MatchRow]:
    """Return stored rows for hashes that each sit at a given shift (query position 0)."""
    return [MatchRow(h, round(shift), track, passage, 0.0) for h, shift in shifts]


def test_time_consistent_evidence_adds_up_across_lookups() -> None:
    """The real replay pattern 2, 2, 2: three weak windows at one shift make 6 votes."""
    evidence = TimelineEvidence(frame_seconds=1.0)
    for lookup in range(3):
        hashes = {f"h{lookup}a": [0], f"h{lookup}b": [0]}
        evidence.add(hashes, _rows(1, "ref", [(h, 30.0) for h in hashes]), 0.0)
    best = evidence.best()
    assert best is not None
    assert (best.track_id, best.reference_passage_id, best.votes, best.lookups) == (1, "ref", 6, 3)
    assert best.latest_votes == 2
    assert best.shift_seconds == pytest.approx(30.0)


def test_the_time_consistent_bar_rises_with_the_number_of_lookups() -> None:
    """Every lookup is another chance for an accidental cluster (measured 2026-11-19)."""
    assert eng.coherent_votes_required(2) == 6
    assert eng.coherent_votes_required(6) == 6  # the first minute: as before
    # the false "Easy Lover": 6 accidental votes after 30 lookups is no longer enough
    assert eng.coherent_votes_required(30) == 10
    assert eng.coherent_votes_required(27) < 10  # a genuine live-LP hit: 10 votes at 27 lookups


def _ordinary(n: int) -> str:
    """Return a hash an ordinary (1/16) window holds."""
    return f"{16 * n:016x}"


def _dense_only(n: int) -> str:
    """Return a hash only a dense (1/4, fast start) window holds."""
    return f"{16 * n + 4:016x}"


def _evidence_of(hashes: list[str], *, dense: bool) -> TimelineEvidence:
    """Spread the hashes over two lookups at one time shift."""
    evidence = TimelineEvidence(frame_seconds=1.0)
    for half in (hashes[: len(hashes) // 2], hashes[len(hashes) // 2 :]):
        evidence.add(
            {h: [0] for h in half},
            _rows(1, "ref", [(h, 30.0) for h in half]),
            0.0,
            dense=dense,
        )
    return evidence


def test_dense_votes_alone_do_not_make_a_time_consistent_match() -> None:
    """Of 7 votes in fast-start windows, 3 ordinary ones are not enough evidence; 4 are."""
    few = [_ordinary(n) for n in range(1, 4)] + [_dense_only(n) for n in range(4)]
    evidence = _evidence_of(few, dense=True)
    best = evidence.best()
    assert best is not None
    assert (best.votes, best.ordinary_votes) == (7, 3)
    assert evidence.best(min_ordinary=eng.COHERENT_ORDINARY_VOTES) is None
    enough = [_ordinary(n) for n in range(1, 5)] + [_dense_only(n) for n in range(3)]
    accepted = _evidence_of(enough, dense=True).best(min_ordinary=eng.COHERENT_ORDINARY_VOTES)
    assert accepted is not None
    assert accepted.ordinary_votes == 4


def test_a_large_dense_cluster_needs_no_ordinary_votes() -> None:
    """A genuine replay of a densely learned start: 11 votes, none ordinary, is accepted."""
    evidence = _evidence_of([_dense_only(n) for n in range(11)], dense=True)
    rule = {"min_ordinary": eng.COHERENT_ORDINARY_VOTES, "large": eng.COHERENT_LARGE_VOTES}
    assert evidence.best(**rule) is not None
    small = _evidence_of([_dense_only(n) for n in range(eng.COHERENT_LARGE_VOTES - 1)], dense=True)
    assert small.best(**rule) is None


def test_without_fast_start_every_vote_is_ordinary() -> None:
    best = _evidence_of([_dense_only(n) for n in range(6)], dense=False).best(min_ordinary=4)
    assert best is not None
    assert (best.votes, best.ordinary_votes) == (6, 6)


async def test_a_dense_cluster_is_not_identified_until_it_has_ordinary_votes(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The live false hits of 2026-11-22 (6-7 dense votes): no identification."""
    caplog.set_level(logging.INFO)
    h = make_harness(fast_start_seconds=30)
    await h.play(1, 60)  # track 1 exists now
    await h.quiet(GAP)
    await h.play(9, 1)
    passage = h.engine.passage
    passage.evidence = _evidence_of([_dense_only(n) for n in range(7)], dense=True)
    passage.evidence.lookups = 3
    assert not await h.engine._judge_timeline(passage)
    for n in range(1, 5):
        passage.evidence.add({_ordinary(n): [0]}, _rows(1, "ref", [(_ordinary(n), 30.0)]), 0.0)
    assert await h.engine._judge_timeline(passage)


def test_evidence_at_random_shifts_never_adds_up() -> None:
    """Accidental 2-vote windows over a whole long passage stay at 2 coherent votes."""
    evidence = TimelineEvidence(frame_seconds=1.0)
    for lookup in range(50):
        shift = 7.0 * lookup  # every lookup at another shift
        hashes = {f"x{lookup}a": [0], f"x{lookup}b": [0]}
        evidence.add(hashes, _rows(2, "ref", [(h, shift) for h in hashes]), 0.0)
    best = evidence.best()
    assert best is not None
    assert best.votes == 2


def test_a_hash_counts_once_per_cluster() -> None:
    """Overlapping windows (and overlapping references) see the same hash again: one vote."""
    evidence = TimelineEvidence(frame_seconds=1.0)
    for query_start in (0.0, 6.0):  # the hash at passage time 8 s is in both windows
        position = int(8 - query_start)
        rows = [MatchRow("same", 8, 1, "ref", 0.0), MatchRow("same", 2, 1, "ref", 6.0)]
        evidence.add({"same": [position]}, rows, query_start)
    best = evidence.best()
    assert best is not None
    assert (best.votes, best.lookups) == (1, 2)


def test_evidence_is_kept_apart_per_reference_passage_and_track() -> None:
    evidence = TimelineEvidence(frame_seconds=1.0)
    evidence.add({"a": [0], "b": [0]}, _rows(1, "play-1", [("a", 5.0), ("b", 5.0)]), 0.0)
    evidence.add({"c": [0], "d": [0]}, _rows(1, "play-2", [("c", 5.0), ("d", 5.0)]), 0.0)
    evidence.add({"e": [0]}, _rows(2, "play-1", [("e", 5.0)]), 0.0)
    best = evidence.best()
    assert best is not None
    assert best.votes == 2
    track_2 = evidence.best(track_id=2)
    assert track_2 is not None
    assert track_2.votes == 1
    evidence.clear()
    assert evidence.best() is None


def test_shift_tolerance_is_about_015_seconds() -> None:
    evidence = TimelineEvidence(frame_seconds=0.01)
    # positions in 10 ms steps: 0.10 s apart still agree, 0.40 s apart do not
    evidence.add({"a": [0]}, [MatchRow("a", 1000, 1, "ref", 0.0)], 0.0)
    evidence.add({"b": [0]}, [MatchRow("b", 1010, 1, "ref", 0.0)], 0.0)
    evidence.add({"c": [0]}, [MatchRow("c", 1040, 1, "ref", 0.0)], 0.0)
    best = evidence.best()
    assert best is not None
    assert best.votes == 2


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


# lengths AudD reports in tests that are not about the gapless guard (longer than any play)
LONG = {1: 600, 2: 600, 3: 600}


async def test_d_replay_in_same_stream_matches_earlier_passage(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations=LONG)  # AudD reports lengths, so a replay needs no AudD at all
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
    first = make_harness(durations=LONG)
    await first.play(1, 60)
    first.engine.close()
    # new engine + new database object on the same file = MA restart
    second = make_harness(durations=LONG)
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


async def test_audd_cadence_and_cap_for_unknown_passage(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eng, "AUDD_MAX_ATTEMPTS_PER_PASSAGE", 6)
    monkeypatch.setattr(eng, "AUDD_SPEED_STEP_PERCENT", 0.0)  # exact sample codes
    h = make_harness(audd_known=set())
    await h.play(9, 900)
    # 24, 54, 84 (30 s apart), then every 90 s to spare AudD requests, capped per passage
    assert len(h.audd_calls) == 6
    window_starts = [c[0] % 1000 for c in h.audd_calls]
    assert window_starts == [12, 42, 72, 162, 252, 342]  # never the 0-12 s intro


async def test_a_long_unidentified_passage_keeps_a_chance(make_harness: Any) -> None:
    """On a live record one passage holds several tracks: past the old cap of 8, AudD goes on."""
    h = make_harness(audd_known=set())
    await h.play(9, 990)
    assert len(h.audd_calls) == 13  # 24, 54, 84, 174, ... 984 s


async def test_thorough_audd_asks_every_30s(make_harness: Any) -> None:
    h = make_harness(audd_known=set(), audd_thorough=True)
    await h.play(9, 300)
    assert len(h.audd_calls) == 10  # 24 s … 294 s


async def test_audd_cadence_resets_for_new_passage(make_harness: Any) -> None:
    h = make_harness(audd_known=set())
    await h.play(9, 200)  # several attempts in a long unknown passage
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


async def test_a_known_end_lifts_the_learning_cap(make_harness: Any, monkeypatch: Any) -> None:
    """A long track with an exact start and a known length is learned until its end guard."""
    monkeypatch.setattr(eng, "MAX_LEARN_SECONDS_PER_PASSAGE", 40)
    h = make_harness(durations={1: 100})
    await h.play(1, 120)
    seconds = {second for song, second in learned(h)[1]}
    assert max(seconds) > 60  # past the 40 s cap
    assert max(seconds) <= 95  # stops before the end guard (100 - 5 s)


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


# ---------------------------------------------------------------- time-consistent evidence, cap


async def test_sparse_replay_is_identified_by_time_consistent_evidence(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A replay whose windows each repeat only 2 landmarks is still recognized locally."""
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    h.engine.close()
    # a new stream without AudD: every 12 s window shares exactly 2 landmarks (every 6th second)
    # with the first play, and overlapping windows share one of them, so distinct votes grow
    # 2, 3, 4, 5, 6: identified at the 5th lookup (36 s)
    replay = make_harness(has_token=False, sparse_every=6)
    await replay.play(1, 40)
    hits = local_hits(caplog)
    assert len(hits) == 1
    assert "accepted (time-consistent)" in hits[0]
    assert "acc=6" in hits[0]
    assert "lookups=5" in hits[0]
    assert "shift=+0.00s" in hits[0]
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
    assert new_passage.confirmer.latest is None
    assert new_passage.evidence.lookups == 0
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
    new_passage.evidence.add({"h": [0]}, [MatchRow("h", 3, 2, "ref", 0.0)], 0.0)
    gate.set()
    await asyncio.wait_for(late, 5)
    assert new_passage.confirmer.latest == (2, 2)
    assert new_passage.evidence.lookups == 1
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


async def test_audd_retries_alternate_the_speed_correction(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AudD's uploads differ in speed: retries try 1 % slower and faster, the setting in between."""
    caplog.set_level(logging.INFO)
    h = make_harness(samples=[], audd_speed_percent=-3.0, audd_known=set(), audd_thorough=True)
    await h.play(9, 200)
    assert h.samples is not None
    speeds = [info["speed_correction_percent"] for _, info in h.samples]
    assert speeds[:6] == [-3.0, -4.0, -3.0, -2.0, -3.0, -4.0]
    assert messages(caplog, "AudD attempt 2/40 on 12s sample at -4.0%")


async def test_the_timecode_uses_the_speed_of_the_identified_sample(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A hit on a retry at another speed converts AudD's timecode and length with that speed."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 1000}, audd_speed_percent=-2.0)
    h.audd_script = [None]  # the first attempt (at -2 %) misses; the retry is at -3 %
    await h.play(1, 70)
    # the hit's window ends at 54 s with timecode ~54-55: the song started at ~1 s and ends at
    # ~1 + 1000 * 0.97 = ~971 s (with the setting's 0.98 it would be ~981 s)
    assert messages(caplog, "AudD attempt 2/20 on 12s sample at -3.0%")
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 97")


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
    assert not await h.engine._postpone_audd(passage)  # option off
    h.engine.postpone_audd = True
    assert not await h.engine._postpone_audd(passage)  # no candidate yet
    passage.confirmer.consider(_candidate(7, 1, 1.0))  # an accidental 1-vote candidate
    assert not await h.engine._postpone_audd(passage)
    passage.confirmer.consider(_candidate(7, 2, 1.0))  # 2 votes: accidental ones reach that
    assert not await h.engine._postpone_audd(passage)
    passage.confirmer.consider(_candidate(7, 3, 1.0))  # a promising candidate
    assert await h.engine._postpone_audd(passage)
    assert await h.engine._postpone_audd(passage)
    assert not await h.engine._postpone_audd(passage)  # at most MAX_AUDD_POSTPONES per passage
    assert passage.audd_postponed == eng.MAX_AUDD_POSTPONES


async def test_audd_waits_for_the_ordinary_votes_of_a_cluster(make_harness: Any) -> None:
    """A cluster with the votes but not yet the ordinary ones gets one more lookup (max 2)."""
    h = make_harness()
    h.engine.postpone_audd = True
    h.engine.feed(music(1, 0))
    passage = _due_passage(h)
    passage.evidence = _evidence_of([_dense_only(n) for n in range(5)], dense=True)
    assert not await h.engine._postpone_audd(passage)  # 5 votes: not enough
    passage.evidence = _evidence_of([_dense_only(n) for n in range(6)], dense=True)
    assert await h.engine._postpone_audd(passage)
    assert await h.engine._postpone_audd(passage)
    assert not await h.engine._postpone_audd(passage)


async def test_audd_is_not_postponed_when_it_is_not_due(make_harness: Any) -> None:
    h = make_harness()
    h.engine.postpone_audd = True
    h.engine.feed(music(1, 0))
    passage = _due_passage(h)
    passage.seconds = 18.0  # before the first AudD attempt
    passage.confirmer.consider(_candidate(7, 3, 1.0))
    assert not await h.engine._postpone_audd(passage)
    assert passage.audd_postponed == 0


async def test_without_token_nothing_is_postponed(make_harness: Any) -> None:
    h = make_harness(has_token=False)
    h.engine.postpone_audd = True
    h.engine.feed(music(1, 0))
    passage = _due_passage(h)
    passage.confirmer.consider(_candidate(7, 3, 1.0))
    assert not await h.engine._postpone_audd(passage)


# ---------------------------------------------------------------- gapless guard


def learned(h: Any) -> dict[int, set[tuple[int, int]]]:
    """Return per track id the (song, second) of every stored landmark."""
    import sqlite3  # noqa: PLC0415

    with sqlite3.connect(h.db.path) as conn:
        rows = conn.execute(
            "SELECT r.track_id, l.fingerprint_hash FROM landmarks l "
            "JOIN reference_windows r ON r.id = l.reference_id"
        ).fetchall()
    result: dict[int, set[tuple[int, int]]] = {}
    for track_id, fp_hash in rows:
        song, second = str(fp_hash)[1:].split("t")
        result.setdefault(int(track_id), set()).add((int(song), int(second)))
    return result


def messages(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


async def test_gapless_next_track_is_not_learned_under_the_previous_one(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 60, 2: 50})
    await h.play(1, 60)
    await h.play(2, 60)  # no silence in between
    assert [m["title"] for m in h.published] == ["Song 1", "Song 2"]
    songs = {track: {song for song, _ in seconds} for track, seconds in learned(h).items()}
    assert songs == {1: {1}, 2: {2}}
    # learning of song 1 stopped TRACK_END_GUARD_SECONDS before its end
    assert max(second for _, second in learned(h)[1]) < 60 - eng.TRACK_END_GUARD_SECONDS
    assert len(messages(caplog, "Track boundary (no gap)")) == 2  # after song 1 and song 2
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 60s")


async def test_without_a_duration_there_is_no_soft_boundary(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness()  # AudD reports no timecode or duration
    await h.play(1, 60)
    await h.play(2, 30)
    assert messages(caplog, "Track boundary (no gap)") == []
    assert [m["title"] for m in h.published] == ["Song 1"]


async def test_early_windows_from_before_the_song_are_not_learned(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={2: 120}, audd_known={2})
    await h.play(1, 10, start=170)  # the stream starts on the tail of another track
    await h.play(2, 40)  # without a gap
    assert [m["title"] for m in h.published] == ["Song 2"]
    assert {song for seconds in learned(h).values() for song, _ in seconds} == {2}
    assert messages(caplog, "Not learning 2 early windows outside Artist 2 - Song 2")


async def test_a_longer_version_than_the_duration_is_relearned_without_a_guard(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 40})  # AudD knows a shorter edit than the record plays
    await h.play(1, 100)
    assert len(messages(caplog, "Track boundary (no gap)")) == 1
    assert messages(caplog, "already playing longer than its known duration")
    assert [m["title"] for m in h.published] == ["Song 1", "Song 1"]
    assert set(learned(h)) == {1}
    # the second passage keeps learning to the end of the record's (longer) version
    assert max(second for _, second in learned(h)[1]) >= 80


async def test_a_local_hit_uses_the_stored_duration(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 60, 2: 50})
    await h.play(1, 60)
    await h.play(2, 50)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(1, 60)
    await h.play(2, 20)
    assert len(h.audd_calls) == calls  # the replay of song 1 was recognized locally
    assert messages(caplog, "Identified Artist 1 - Song 1 via local")
    assert len(messages(caplog, "Expected end of Artist 1 - Song 1 at 60s")) == 2
    songs = {track: {song for song, _ in seconds} for track, seconds in learned(h).items()}
    assert songs == {1: {1}, 2: {2}}


async def test_the_guard_scales_with_the_speed_correction(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # a turntable 2 % fast: 100 s of the recording takes 98 s
    h = make_harness(durations={1: 100}, audd_speed_percent=-2.0)
    await h.play(1, 30)
    # timecode 24 at 24 s: song started at 24 - 24 * 0.98 = 0.48 s; ends at 0.48 + 98 = 98.5 s
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 98s")


async def test_the_report_carries_position_and_length(make_harness: Any) -> None:
    """MA gets the position in the song and the track length, so it can show a progress bar."""
    h = make_harness(durations={1: 60})
    await h.play(1, 30)
    report = h.published[0]
    # identified by AudD at 24 s with timecode 00:24: the song started with the passage
    assert report["elapsed_seconds"] == pytest.approx(24, abs=1)
    assert report["playback_duration_seconds"] == pytest.approx(60)
    # the position belongs to this play only: it is not stored with the track
    assert "elapsed_seconds" not in h.db.track_metadata(1)


async def test_a_local_hit_reports_the_position_too(make_harness: Any) -> None:
    h = make_harness(durations={1: 60})
    await h.play(1, 60)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(1, 40)
    assert len(h.audd_calls) == calls
    report = h.published[-1]
    assert 0 < report["elapsed_seconds"] <= 40
    assert report["playback_duration_seconds"] == pytest.approx(60, abs=1)


async def test_without_timing_the_report_has_no_position(make_harness: Any) -> None:
    h = make_harness()  # AudD reports no timecode or duration
    await h.play(1, 30)
    assert "elapsed_seconds" not in h.published[0]
    assert "playback_duration_seconds" not in h.published[0]


async def test_the_reported_length_follows_the_speed_correction(make_harness: Any) -> None:
    h = make_harness(durations={1: 100}, audd_speed_percent=-2.0)
    await h.play(1, 30)
    assert h.published[0]["playback_duration_seconds"] == pytest.approx(98)


async def test_a_local_hit_without_length_asks_audd_once_for_it(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A track learned before lengths were stored gets its length from one extra AudD call."""
    caplog.set_level(logging.INFO)
    h = make_harness()  # first play: AudD reports no length
    await h.play(1, 60)
    await h.quiet(GAP)
    assert "duration_seconds" not in (h.db.track_metadata(1) or {})
    h.durations[1] = 100  # from now on AudD knows the length
    calls = len(h.audd_calls)
    await h.play(1, 60)
    assert messages(caplog, "Identified Artist 1 - Song 1 via local")
    assert len(h.audd_calls) == calls + 1  # exactly one length request
    assert messages(caplog, "Learned the length of Artist 1 - Song 1 via AudD: 100s")
    assert (h.db.track_metadata(1) or {})["duration_seconds"] == 100
    # MA got the metadata again, now with the length
    assert h.published[-1]["title"] == "Song 1"
    assert h.published[-1]["playback_duration_seconds"] == pytest.approx(100, abs=1)
    # a later replay knows the length: no AudD at all
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(1, 40)
    assert len(h.audd_calls) == calls


async def test_the_length_check_ignores_another_name(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """If AudD names another track, the local recognition stands and nothing is stored."""
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    h.audd_script = [{"artist": "Someone", "title": "Else", "duration_seconds": 99}] * 2
    await h.play(1, 60)
    assert len(h.audd_calls) == calls + 1  # AudD answered: no second check
    assert messages(caplog, "AudD names it Someone - Else, not Artist 1 - Song 1")
    assert "duration_seconds" not in (h.db.track_metadata(1) or {})
    assert [m["title"] for m in h.published] == ["Song 1", "Song 1"]


async def test_a_missed_length_request_is_retried_after_30s(make_harness: Any) -> None:
    """A length request that AudD misses is tried again 30 s later."""
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    h.durations[1] = 300
    h.audd_script = [None]  # the first length request misses
    calls = len(h.audd_calls)
    await h.play(1, 80)
    assert len(h.audd_calls) == calls + 2  # the miss and a retry ~30 s later
    assert (h.db.track_metadata(1) or {})["duration_seconds"] == 300


# ---------------------------------------------------------------- self-correcting track ends


async def test_the_next_track_trims_the_previous_one_to_its_real_end(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """AudD's length is too long (another edit): the next track's start corrects the learning."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 80, 2: 600})  # song 1 really lasts 60 s on this record
    await h.play(1, 60)
    await h.play(2, 60)  # no gap: song 2 starts at 60 s, the soft boundary comes at 85 s
    assert messages(caplog, "Track boundary (no gap)")
    assert messages(caplog, "Artist 2 - Song 2 started 25s before the boundary")
    songs = {track: {song for song, _ in seconds} for track, seconds in learned(h).items()}
    assert songs[1] == {1}  # nothing of song 2 is left under song 1
    assert messages(caplog, "Track 1: observed length 60s noted")


async def test_an_observed_length_seen_twice_replaces_audds(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 80, 2: 600})
    for _ in range(2):
        await h.play(1, 60)
        await h.play(2, 60)
        await h.quiet(GAP, level=3)
    assert messages(caplog, "Track 1: observed length 60s adopted")
    metadata = h.db.track_metadata(1) or {}
    assert metadata["duration_seconds"] == pytest.approx(60, abs=1)
    assert metadata["duration_source"] == "observed"


async def test_a_real_gap_gives_an_observed_length_but_a_dropout_does_not(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(1, 30)
    await h.quiet(2, level=0)  # 2 s of digital zeros mid-song: a stream dropout
    await h.play(1, 28, start=32)
    assert messages(caplog, "Stream dropout: 2.0s of digital silence ignored")
    assert messages(caplog, "Track boundary") == []
    await h.quiet(LIFT)  # quiet hiss: a real gap after 60 s of song
    assert messages(caplog, "Track 1: observed length 60s noted")


async def test_a_lifted_needle_is_only_noted(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A gap mid-song gives one observation; it takes two that agree to change the length."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(1, 60)
    await h.quiet(LIFT)  # the needle is lifted 240 s before the end
    assert messages(caplog, "Track 1: observed length 60s noted")
    assert (h.db.track_metadata(1) or {})["duration_seconds"] == 300


async def test_a_long_digital_silence_is_a_gap(make_harness: Any) -> None:
    """The source stopped sending audio: after the dropout limit the passage closes."""
    h = make_harness()
    await h.play(1, 30)  # identified at 24 s
    await h.quiet(int(eng.RECONNECT_MAX_SECONDS) + 1, level=0)
    assert h.engine.passage is None


async def test_digital_silence_closes_an_unidentified_passage_sooner(make_harness: Any) -> None:
    h = make_harness(audd_known=set())
    await h.play(1, 30)
    await h.quiet(int(eng.DROPOUT_MAX_SECONDS) + 1, level=0)
    assert h.engine.passage is None


async def test_a_source_reconnect_does_not_split_an_identified_track(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Wi-Fi, 2026-11-21): 10-26 s of zeros per reconnect split Spaceman in 4."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 120})
    await h.play(1, 40)
    passage = h.engine.passage
    await h.quiet(20, level=0)  # MA fills the reconnect with digital zeros
    await h.play(1, 40, start=60)  # the song went on meanwhile
    assert h.engine.passage is passage
    assert messages(caplog, "Stream dropout: 20.0s of digital silence ignored")
    assert messages(caplog, "Track boundary") == []
    assert len(h.audd_calls) == 1


async def test_a_local_hit_uses_the_stored_song_start_of_its_reference(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A reference play that began mid-song no longer shifts the expected end of a replay."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 40, 2: 100})
    await h.play(1, 40)
    await h.play(2, 60)  # gapless: the passage of song 2 begins 5 s into it (soft boundary)
    assert messages(caplog, "Expected end of Artist 2 - Song 2 at 95s (song started at -5s")
    await h.quiet(GAP, level=3)
    await h.play(2, 40)  # replay of song 2 from its start: recognized locally
    assert messages(caplog, "Identified Artist 2 - Song 2 via local")
    # without the stored start of the reference play this would say "song started at 5s"
    assert messages(caplog, "Expected end of Artist 2 - Song 2 at 100s (song started at 0s")


# ---------------------------------------------------------------- withdrawing the shown track


async def test_an_unknown_next_track_withdraws_the_shown_one(make_harness: Any) -> None:
    """After a track, an unidentified passage replaces it with the source placeholder."""
    h = make_harness(audd_known={1})
    await h.play(1, 40)
    await h.quiet(GAP)
    assert h.cleared == 0  # during the gap the last track stays
    await h.play(9, 30)  # unknown: the first lookup at 12 s does not identify it
    assert h.cleared == 1
    assert [m["title"] for m in h.published] == ["Song 1"]


async def test_a_quickly_recognized_next_track_switches_directly(make_harness: Any) -> None:
    h = make_harness(durations={1: 600, 2: 600})
    await h.play(1, 40)
    await h.quiet(GAP)
    await h.play(2, 40)
    await h.quiet(GAP)
    await h.play(1, 30)  # known: recognized at the first lookup, no placeholder in between
    assert h.cleared == 1  # only once: when song 2 was still unknown (first play)
    assert [m["title"] for m in h.published] == ["Song 1", "Song 2", "Song 1"]


async def test_a_long_silence_withdraws_the_shown_track(make_harness: Any) -> None:
    """The record has ended: after 30 s of silence MA shows the source again."""
    h = make_harness()
    await h.play(1, 40)
    await h.quiet(GAP)
    assert h.cleared == 0  # a short gap between tracks keeps the title
    await h.quiet(int(eng.WITHDRAW_AFTER_SILENCE_SECONDS))
    assert h.cleared == 1
    await h.quiet(60)
    assert h.cleared == 1  # withdrawn once


# ---------------------------------------------------------------- trusting a local position


async def test_the_previous_plays_tail_moves_to_the_song_that_followed(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A misnamed or too-long play gives its windows of the next song to that song, not the bin."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 80, 2: 600})  # song 1 really lasts 60 s on this record
    await h.play(1, 60)
    await h.play(2, 60)  # no gap: song 2 starts at 60 s, the soft boundary comes at 85 s
    assert messages(caplog, "Artist 2 - Song 2 started 25s before the boundary")
    moved = messages(caplog, "moved ")
    assert moved
    assert "moved 0 " not in moved[0]
    songs = {track: {song for song, _ in seconds} for track, seconds in learned(h).items()}
    assert songs[1] == {1}
    # song 2's first seconds were only heard before the soft boundary: they came with the move
    assert min(second for song, second in learned(h)[2] if song == 2) < 25


async def test_confirmed_needs_time_consistent_evidence(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Two lookups with the same three hashes of a track are not yet one recording."""
    caplog.set_level(logging.INFO)
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(9, 6)  # unknown audio...
    await h.play(1, 3, start=10)  # ...with three seconds of song 1 inside both lookups
    await h.play(9, 9, start=6)
    await h.quiet(GAP)
    assert messages(caplog, "LOCAL CANDIDATE: Artist 1 - Song 1 votes=3")
    assert not [m for m in messages(caplog, "accepted (confirmed)") if "Song 1" in m]
    assert len(h.audd_calls) == calls  # the passage ended before AudD was due


async def test_a_weak_far_position_after_a_gap_is_not_trusted(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A passage after a gap starts with its song unless a strong match says otherwise."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300}, sparse_every=3)  # a new play: few votes per window
    await h.play(1, 60, start=100)  # the stream starts mid-song: AudD puts the start at -100 s
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 200s (song started at -100s")
    await h.quiet(LIFT)
    await h.play(1, 30, start=100)  # the same stretch after a gap: a weak local position
    assert messages(caplog, "Position of Artist 1 - Song 1 uncertain")
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 300s (song started at 0s")


async def test_a_weak_position_is_decided_later(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Needle drop: the position is too weak at the hit, but adds up while the record plays."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300}, sparse_every=3)
    await h.play(1, 90, start=100)  # first play from mid-song: AudD stores the song start
    await h.quiet(GAP)
    published = len(h.published)
    await h.play(1, 80, start=100)  # the same stretch again after a gap
    assert messages(caplog, "Position of Artist 1 - Song 1 uncertain")
    assert messages(caplog, "Position of Artist 1 - Song 1 decided later: song started at -100s")
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 200s (song started at -100s")
    report = h.published[-1]
    assert len(h.published) > published + 1  # the hit, then the corrected position
    assert report["elapsed_seconds"] > 100  # the progress bar now shows the song position
    starts = (h.db.track_metadata(1) or {})["song_starts"]
    assert sorted(starts.values()) == [-100.0, -100.0]  # this play is positioned now too


def test_a_song_start_with_an_equal_rival_is_not_decisive() -> None:
    """A song that repeats itself: two song starts with the same evidence decide nothing."""
    evidence = eng.TimelineEvidence(frame_seconds=1.0)
    rows = []
    query: dict[str, list[int]] = {}
    for i in range(15):
        for start, prefix in ((0.0, "a"), (60.0, "b")):  # the same audio at two places
            fp_hash = f"{prefix}{i}"
            query[fp_hash] = [i]
            rows.append(MatchRow(fp_hash, i, 1, "ref", start))
    evidence.add(query, rows, 0.0)
    evidence.add(query, rows, 0.0)
    position = evidence.song_start(1, {"ref": 0.0})
    assert position is not None
    assert position.votes == 15
    assert position.rival_votes == 15
    assert not position.decisive
    rows = [r for r in rows if r.fingerprint_hash.startswith("a")]
    evidence = eng.TimelineEvidence(frame_seconds=1.0)
    evidence.add(query, rows, 0.0)
    evidence.add(query, rows, 0.0)
    position = evidence.song_start(1, {"ref": 0.0})
    assert position is not None
    assert position.decisive
    assert evidence.song_start(1, {}) is None  # a play without a stored start says nothing


async def test_an_audd_timecode_far_before_a_passage_after_a_gap_is_not_trusted(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (In The Air Tonight, 2026-11-20): another edit's timecode ended the passage."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(2, 30)
    await h.quiet(GAP)
    # after the gap the passage holds song 1 from 100 s on: AudD's timecode says -100 s
    await h.play(1, 80, start=100)
    assert messages(caplog, "probably another edit's timecode")
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 300s (song started at 0s")
    assert messages(caplog, "Track boundary (no gap)") == []
    assert "song_starts" not in (h.db.track_metadata(1) or {})  # nothing stored as exact


async def test_a_wrong_title_with_a_far_timecode_is_corrected_by_a_second_check(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Coldplay, 2026-11-22/23): Always In My Head named "O" at 6:16, twice."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 210})
    await h.play(2, 30)
    await h.quiet(GAP)
    wrong = {**metadata_for(5), "audd_timecode": "06:16", "duration_seconds": 466}
    h.audd_script = [wrong]  # the first answer after the gap; later ones are the real song
    calls = len(h.audd_calls)
    await h.play(1, 90)
    assert messages(caplog, "AudD places Artist 5 - Song 5 352s before this passage")
    assert messages(caplog, "Checking the title Artist 5 - Song 5 with AudD once more")
    assert messages(caplog, "AudD names it Artist 1 - Song 1 with a plausible position")
    assert len(h.audd_calls) == calls + 2  # one extra request
    assert h.published[-1]["title"] == "Song 1"
    tracks = {(h.db.track_metadata(t) or {}).get("title"): t for t in learned(h)}
    assert set(tracks) == {"Song 1", "Song 2"}  # the wrong track is gone
    assert {song for song, _ in learned(h)[tracks["Song 1"]]} == {1}


async def test_a_needle_drop_title_is_confirmed_by_the_check(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A real needle drop mid-song: the second answer names the same track, nothing changes."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(2, 30)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(1, 90, start=100)
    assert messages(caplog, "AudD confirms Artist 1 - Song 1")
    assert messages(caplog, "AudD names it") == []
    assert len(h.audd_calls) == calls + 2


async def test_a_needle_drop_ending_without_a_gap_is_caught_at_audds_implied_end(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Olivia Dean, 2026-11-24): the next track played 2.5 min under the old name."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 200, 3: 200})
    await h.play(2, 30)
    await h.quiet(GAP)
    await h.play(1, 100, start=100)  # the needle drop: the last 100 s of song 1
    await h.play(3, 60)  # the next song follows without a gap
    assert messages(caplog, "probably another edit's timecode")
    assert messages(caplog, "If it was a needle drop, Artist 1 - Song 1 ends at 100s")
    assert messages(caplog, "AudD names Artist 3 - Song 3 from 100s")
    assert messages(caplog, "Track boundary (no gap): Artist 1 - Song 1")
    assert h.published[-1]["title"] == "Song 3"
    windows = learned(h)
    tracks = {(h.db.track_metadata(t) or {}).get("title"): t for t in windows}
    assert {song for song, _ in windows[tracks["Song 1"]]} == {1}  # no song 3 under song 1
    assert {song for song, _ in windows[tracks["Song 3"]]} == {3}
    starts = (h.db.track_metadata(tracks["Song 1"]) or {})["song_starts"]
    assert sorted(starts.values()) == [-100.0]  # the needle drop's start, now stored


async def test_the_end_check_tries_again_when_audd_misses(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Olivia Dean (2026-11-24): the one end check got no match from AudD."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 200, 3: 200})
    await h.play(2, 30)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    h.audd_script = [PLAYING, PLAYING, None]  # the hit, the title check, a missed end check
    await h.play(1, 100, start=100)
    await h.play(3, 90)
    assert len(messages(caplog, "Asking AudD whether Artist 1 - Song 1 has ended")) == 2
    assert messages(caplog, "AudD names Artist 3 - Song 3 from 100s")
    assert h.published[-1]["title"] == "Song 3"
    assert len(h.audd_calls) == calls + 4


async def test_a_gap_at_audds_implied_end_confirms_the_needle_drop(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Tom Odell (2026-11-24): AudD's end 97 s, the gap at 93 s. Store the start, observe the length."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 200})
    await h.play(2, 30)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(1, 100, start=100)  # the needle drop: the last 100 s of song 1, then a gap
    await h.quiet(GAP)
    await h.settle()
    assert messages(caplog, "The gap at 100s fits the end AudD's timecode implied (100s)")
    assert messages(caplog, "Asking AudD whether") == []  # no request needed
    assert len(h.audd_calls) == calls + 2  # the hit and the title check
    starts = (h.db.track_metadata(2) or {}).get("song_starts", {})
    assert sorted(starts.values()) == [-100.0]


async def test_another_edits_timecode_is_kept_distrusted_after_the_end_check(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """In The Air Tonight: at AudD's implied end the same song still plays."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(2, 30)
    await h.quiet(GAP)
    wrong = {**metadata_for(1), "audd_timecode": "02:04", "duration_seconds": 200}
    h.audd_script = [wrong]  # AudD's first answer puts the song start 100 s before the passage
    await h.play(1, 160)
    assert messages(caplog, "If it was a needle drop, Artist 1 - Song 1 ends at 100s")
    assert messages(caplog, "AudD still names Artist 1 - Song 1")
    assert messages(caplog, "Track boundary (no gap)") == []
    assert h.published[-1]["title"] == "Song 1"
    assert "song_starts" not in (h.db.track_metadata(1) or {})


async def test_a_plausible_timecode_needs_no_title_check(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(2, 30)
    await h.quiet(GAP)
    calls = len(h.audd_calls)
    await h.play(1, 90)
    assert messages(caplog, "Checking the title") == []
    assert len(h.audd_calls) == calls + 1


async def test_a_gap_inside_a_song_keeps_audds_position(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Hard Day, offline 2026-11-20): a quiet moment split the song after 25 s."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(1, 30)  # identified by AudD at 24 s: the song started with the passage
    await h.quiet(QUIET_MOMENT)
    await h.play(1, 80, start=30 + QUIET_MOMENT)  # the same song goes on after the quiet moment
    assert messages(caplog, "probably another edit's timecode") == []
    assert messages(caplog, "continues the play before the gap")
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 268s (song started at -32s")


async def test_a_quiet_moment_inside_a_song_continues_its_position(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Hard Day live, 2026-11-21): a loop put the continued song at 3:43, not 0:25."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300}, sparse_every=3)
    await h.play(1, 60, start=100)  # learned with an exact start (AudD timecode)
    await h.quiet(LIFT)
    await h.play(2, 40)
    await h.quiet(LIFT)
    await h.play(1, 30)  # a new play from the start: exact, stored
    await h.quiet(
        QUIET_MOMENT
    )  # a quiet moment (the music resumes right after the gap is confirmed)
    await h.play(1, 80, start=30 + QUIET_MOMENT)
    found = messages(caplog, "continues the play before the gap") + messages(
        caplog, "confirmed as the continuation"
    )
    assert found
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 268s (song started at -32s")


async def test_a_quiet_moment_inside_a_song_is_not_its_length(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Hard Day, 2026-11-21): the quiet moment at 0:25 noted lengths of 19-23 s."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(1, 30)
    await h.quiet(QUIET_MOMENT)  # the music resumes right after the gap is confirmed
    await h.play(1, 60, start=30 + QUIET_MOMENT)
    assert messages(caplog, "was a quiet moment inside Artist 1 - Song 1: no length observed")
    assert messages(caplog, "Track 1: observed length") == []


async def test_a_stream_that_starts_mid_song_keeps_audds_position(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a gap before it (the stream started mid-song) AudD's timecode is trusted."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(1, 60, start=100)
    assert messages(caplog, "probably another edit's timecode") == []
    assert messages(caplog, "Expected end of Artist 1 - Song 1 at 200s (song started at -100s")


async def test_a_measured_length_is_not_scaled_by_the_speed_correction(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Sussudio 260 vs 255 s, 2026-11-20): lengths depended on the AudD setting."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 80, 2: 600}, audd_speed_percent=-2.0)
    for _ in range(2):
        await h.play(1, 60)
        await h.quiet(LIFT, level=3)
    assert messages(caplog, "Track 1: observed length 60s adopted")
    metadata = h.db.track_metadata(1) or {}
    assert metadata["duration_seconds"] == pytest.approx(60, abs=1)  # as measured, not 61.2
    calls = len(h.audd_calls)
    await h.play(1, 30)
    assert len(h.audd_calls) == calls  # local
    # the measured 60 s is turntable time already: the end is not scaled by 0.98 again
    import re  # noqa: PLC0415

    last = messages(caplog, "Expected end of Artist 1 - Song 1")[-1]
    found = re.search(r"at (-?\d+)s \(song started at (-?\d+)s, duration 60s\)", last)
    assert found is not None, last
    assert int(found.group(1)) - int(found.group(2)) == pytest.approx(60, abs=1)


async def test_a_strong_far_position_after_a_gap_is_trusted(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A needle dropped mid-track: a strong local match keeps its position."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 300})
    await h.play(1, 60, start=100)
    await h.quiet(GAP)
    await h.play(1, 30, start=100)
    assert not messages(caplog, "uncertain")
    assert (
        len(messages(caplog, "Expected end of Artist 1 - Song 1 at 200s (song started at -100s"))
        == 2
    )


async def play_with_holes(h: Any, song: int, seconds: int, holes: set[int], start: int = 0) -> None:
    """Play a song where the stream lost the given song seconds (digital zeros instead)."""
    for second in range(start, start + seconds):
        h.engine.feed(silence(0) if second in holes else music(song, second))
        await h.settle()


async def test_a_few_stream_holes_do_not_delay_audd(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """In real samples AudD hit as often with 1-4 holes as without (2026-11-23)."""
    caplog.set_level(logging.INFO)
    h = make_harness()
    await play_with_holes(h, 1, 30, holes={14, 18})
    assert messages(caplog, "stream hole") == []
    assert h.published[-1]["title"] == "Song 1"
    assert len(h.audd_calls) == 1


async def test_audd_waits_for_a_window_without_stream_holes(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (side D, 2026-11-22): holes every ~6 s made AudD miss audio it knows."""
    caplog.set_level(logging.INFO)
    h = make_harness()
    # 5 holes in the first AudD sample (12-24 s), 2 in the next one (18-30 s)
    await play_with_holes(h, 1, 40, holes={13, 15, 17, 19, 21})
    assert messages(caplog, "waiting for a clean window (1/1")
    assert len(h.audd_calls) == 1
    assert h.audd_calls[0].count(0) == 2  # the sample AudD got was the cleaner one
    assert h.engine.passage is not None
    assert h.engine.passage.audd_attempts == 1  # a skipped sample is not an attempt
    assert h.published[-1]["title"] == "Song 1"


async def test_audd_sends_a_sample_with_holes_when_no_window_is_clean(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness()
    await play_with_holes(h, 1, 40, holes=set(range(2, 40, 2)))
    assert len(messages(caplog, "waiting for a clean window")) == eng.AUDD_MAX_HOLE_SKIPS
    assert messages(caplog, "no cleaner window, sending it anyway")
    assert len(h.audd_calls) == 1
    assert h.published[-1]["title"] == "Song 1"


async def test_a_title_check_keeps_its_chance_while_the_stream_has_holes(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 210})
    await h.play(2, 30)
    await h.quiet(GAP)
    wrong = {**metadata_for(5), "audd_timecode": "06:16", "duration_seconds": 466}
    h.audd_script = [wrong]
    calls = len(h.audd_calls)
    await play_with_holes(h, 1, 100, holes={43, 45, 47, 49, 51})  # check due at 54 s
    assert messages(caplog, "waiting for a clean window")
    assert messages(caplog, "AudD names it Artist 1 - Song 1 with a plausible position")
    assert len(h.audd_calls) == calls + 2
    assert h.published[-1]["title"] == "Song 1"


async def test_the_tail_after_a_soft_boundary_gives_the_records_length(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (Magic, 2026-11-22/23): AudD's length 258 s, the record ~278 s, cut early."""
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 30})  # AudD's edit is 30 s; this record plays 45 s
    for _ in range(2):
        await h.play(1, 45)  # soft boundary at 35 s, the last 10 s an unidentified passage
        await h.quiet(LIFT)
    assert len(messages(caplog, "was the last 10s of track 1 after the soft boundary")) == 2
    assert messages(caplog, "Track 1: observed length 45s adopted")
    caplog.clear()
    await h.play(1, 45)
    assert messages(caplog, "Track boundary (no gap)") == []  # no early cut any more


async def test_a_short_new_track_after_a_soft_boundary_is_not_a_tail(
    make_harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = make_harness(durations={1: 30}, audd_known={1})  # AudD can't name song 2
    await h.play(1, 35)  # the soft boundary comes at 35 s
    await h.play(2, 40)  # then another, longer track without a gap: unidentified
    await h.quiet(LIFT)
    assert messages(caplog, "after the soft boundary") == []


@pytest.mark.parametrize(("single", "at"), [(10, 12), (1000, 18)])
async def test_a_big_cluster_needs_only_one_lookup(
    make_harness: Any, caplog: pytest.LogCaptureFixture, monkeypatch: Any, single: int, at: int
) -> None:
    """Regression (Ink, 2026-11-22): 58 time-consistent votes waited a lookup for a low ratio."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(eng, "STRONG_VOTES", 1000)  # the single-window rule never fires here
    monkeypatch.setattr(eng, "COHERENT_SINGLE_LOOKUP_VOTES", single)
    h = make_harness()
    await h.play(1, 60)
    await h.quiet(GAP)
    h.engine.close()
    replay = make_harness(has_token=False)
    await replay.play(1, 30)  # each 12 s window shares 12 votes with the first play
    assert messages(caplog, f"Identified Artist 1 - Song 1 via local after {at}s")


async def test_learned_windows_record_their_stream_holes(make_harness: Any, db_path: Any) -> None:
    import sqlite3  # noqa: PLC0415
    from contextlib import closing  # noqa: PLC0415

    h = make_harness()
    await play_with_holes(h, 1, 50, holes={40})
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT passage_offset_seconds, stream_holes FROM reference_windows ORDER BY 1"
        ).fetchall()
    holes = dict(rows)
    assert holes[0.0] == 0  # an early (lookup) window, stored after the AudD hit
    assert holes[max(holes)] == 1  # the newest learned window holds the lost second
