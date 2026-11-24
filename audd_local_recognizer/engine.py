"""
Recognition state machine for one live stream, independent of Music Assistant.

Model
-----
A *stream* (one MA analysis session) is a sequence of *passages*: continuous pieces of
music between confirmed silence gaps. An MA session is NOT a track; a whole record side
(or several) can play in one session.

    GAP  --audio resumes-->  PASSAGE(unidentified) --local hit / AudD hit--> PASSAGE(identified)
     ^                                  |                                          |
     +---------- confirmed gap ---------+------------------------------------------+

Invariants
----------
* A confirmed gap is a hard PCM boundary: no window ever mixes audio from two passages.
* A passage never matches references it learned itself (exclude_passage_id), but references
  from every earlier passage, including earlier in the same session, are eligible.
* An identified passage only learns; it does not keep matching. The lookup windows it
  computed while still unknown are stored too, so a track's start is learned as well.
* Every async task captures its Passage object and re-checks `self.passage is passage`
  before any state change, metadata report or DB write, so a late result from passage N
  can never touch passage N+1.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

from .audd_client import AudDError, AudDNoMatch, parse_timecode
from .database import ALIASES, DURATION_SOURCE, SONG_STARTS, track_key
from .fingerprints import (
    DENSITY_DIVISOR,
    PANAKO_FRAME_SECONDS,
    SILENCE_FLOOR_DBFS,
    Fingerprints,
    PcmFormat,
    QueryMatch,
    density_subset,
    pcm_dbfs,
    pcm_to_wav,
    sample_stats,
    stream_holes,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable, Callable, Coroutine

    from .database import FingerprintDatabase, LocalLookup, MatchRow

# Window and cadence (seconds of passage audio).
SAMPLE_SECONDS = 12
LOCAL_INTERVAL_SECONDS = 6
LEARN_INTERVAL_SECONDS = 6  # 12 s windows every 6 s: overlap is intentional (replay coverage)
# Stop learning a passage after this long. Guards against learning the next track under
# the current identity on records without audible gaps between tracks. When the song's start is
# exact and its length known, the end guard (below) stops learning at the right moment, so a long
# track (an 18-minute suite, 2026-11-19) is learned whole, up to MAX_LEARN_SECONDS_KNOWN_END.
MAX_LEARN_SECONDS_PER_PASSAGE = 600
MAX_LEARN_SECONDS_KNOWN_END = 1800
# Gapless guard: once a passage is identified, AudD's timecode (the song position at the end of
# the sample, 1 s resolution) or the time shift of a local match, plus the track's duration, tell
# when the track should end in passage time. Learning stops TRACK_END_GUARD_SECONDS before that
# moment, and if no silence gap came TRACK_END_GUARD_SECONDS after it, the passage is closed anyway
# (a soft boundary) and a new, unidentified one starts. The audio in between is never learned, so
# on records without gaps the next track is not learned under this one. Early lookup windows that
# start before the song did (a passage that began on the previous track) are not stored either;
# that check allows SONG_START_TOLERANCE_SECONDS for the timecode's 1 s resolution.
TRACK_END_GUARD_SECONDS = 5
SONG_START_TOLERANCE_SECONDS = 2
# Self-correction on gapless/live records (2026-11-19): AudD's length is often another edit (on a
# live LP 10-30 s too long). Once the session has had a soft boundary, learning stops this far
# before the expected end unless the length was measured on this record. When the next track's
# song start is known, the previous play is trimmed to its real end and that length is observed;
# a length seen twice (within the tolerance) replaces AudD's. A real silence gap also gives an
# observed length; a digital dropout (exact zeros, -120 dBFS) does not.
UNCERTAIN_END_GUARD_SECONDS = 20
# An unidentified passage after a soft boundary that ends in a real gap within this many seconds
# is taken as the rest of the previous track (see _soft_boundary_tail).
SOFT_TAIL_MAX_SECONDS = 30
OBSERVED_LENGTH_TOLERANCE_SECONDS = 3.0
# A lifted needle also ends in a real gap and so gives an observed length, but it is only noted:
# adopting needs two observations within the tolerance. A rule that ignored gaps far before the
# expected end (2026-11-19) blocked a real correction (Barry White, 188 s twice, AudD 280 s) in the
# overnight ablation, so it was removed again (2026-11-20).
DROPOUT_DBFS = SILENCE_FLOOR_DBFS + 1
# Stream dropouts: the Sendspin input sometimes delivers ~2 s of exact digital zeros in the middle
# of a track (5x on 2026-11-19 morning). A turntable never produces exact zeros (the needle and
# preamp hiss sit around -55..-80 dBFS), so such a run does not count as a gap: the passage
# continues. Only a longer run (the source stopped sending audio) becomes a gap.
DROPOUT_MAX_SECONDS = 10.0
# ...or this long inside an identified passage: when the source reconnects (Wi-Fi, 2026-11-20/21)
# MA delivers 10-26 s of zeros and the same song goes on; at 10 s every reconnect split the track
# into a new passage (Spaceman in 4 pieces). A source that really stopped closes 20 s later.
RECONNECT_MAX_SECONDS = 30.0
# Published instead of a track when the shown track has clearly ended: a new passage plays and its
# first lookup did not identify it (user's wish 2026-11-19; during a gap the last track stays).
# The provider reports the source's own placeholder metadata for it.
PLACEHOLDER = "placeholder"
# A silence this long (the side or the record has ended) also withdraws the shown track; shorter
# gaps between tracks keep it, so the title does not flicker.
WITHDRAW_AFTER_SILENCE_SECONDS = 30.0
# A track recognized locally without a stored length gets one extra AudD request (from
# AUDD_FIRST_ATTEMPT_SECONDS into the passage, at most this many per passage) just to learn its
# length and the song position (user's choice, 2026-11-19: the extra requests are fine). Only a
# hit with the same track key (or alias) is used; another name is ignored (local wins).
MAX_DURATION_CHECKS_PER_PASSAGE = 2
# An AudD hit whose timecode puts the song start far before a passage that began after a gap is
# usually right (a needle drop, another edit: 4 of 7 cases 2026-11-20..23) but can be another track
# that shares the audio: Coldplay's Always In My Head named "O" twice, whose hidden reprise has
# the same intro. Such a title is shown at once and asked once more a little later (at the speed
# of the hit); another track with a plausible song start then replaces it. About 4 % of AudD hits.
MAX_TITLE_CHECKS_PER_PASSAGE = 1
# The same far timecode is also a real needle drop (Olivia Dean, 2026-11-24: needle at 2:09, AudD
# right, but the next track played 2.5 min under the old name because no gap came). So AudD is
# asked once more when its own timecode says the song ended: the sample then starts this many
# seconds after that moment (a full window past it). Another title there is a soft boundary at
# the new song's start; the same title means the timecode was another edit's. One request.
END_CHECK_DELAY_SECONDS = 15
# Lookup fingerprints of a still-unknown passage are kept (at most this many windows, i.e.
# the first ~2 minutes) and stored as references once the passage is identified, so the
# start of a track is learned too even when identification only succeeds later.
MAX_EARLY_WINDOWS = 20

# AudD cadence per unknown passage. The first attempt waits for 24 s of audio: in real vinyl
# tests AudD missed the first 12 s (intro) of every track (6/6) and matched 30-42 s, while
# local lookups still start at 12 s. Then 30 s between the first attempts and 90 s after that,
# to spare AudD requests (the point of local fingerprints). The cap is high enough (20, ~27 min)
# that a long unidentified passage on a live or gapless record, which can hold several tracks,
# keeps a chance every 90 s (the old cap of 8 left the next tracks without AudD, 2026-11-19).
# "Thorough" (advanced setting, user's testing): every 30 s, cap 40.
AUDD_FIRST_ATTEMPT_SECONDS = 24
AUDD_FAST_ATTEMPTS = 3
AUDD_FAST_INTERVAL_SECONDS = 30
AUDD_SLOW_INTERVAL_SECONDS = 90
AUDD_MAX_ATTEMPTS_PER_PASSAGE = 20
AUDD_THOROUGH_MAX_ATTEMPTS = 40
# Stream-wide: never two AudD calls closer than this, whatever the passages do.
AUDD_MIN_INTERVAL_SECONDS = 30
# Speed correction per attempt (2026-11-20, Barry White singles in the AudD dashboard): AudD holds
# several uploads of one recording, mastered ~1-2 % apart in speed, and accepts a sample only within
# ~±0.5-1 % of the one it matches, so no single correction finds them all. Retries therefore go
# through these offsets (in AUDD_SPEED_STEP_PERCENT steps) from the configured correction: the
# setting itself every other attempt (a miss from a scratch or a stream hole gets a second chance),
# slower first (most hits so far were on the slow side). 0 % steps switch it off.
AUDD_SPEED_STEP_PERCENT = 1.0
AUDD_SPEED_PATTERN = (0, -1, 0, 1)
# Stream-wide exponential backoff, only for real HTTP/network/API failures.
# When AudD is due but the latest local lookup already points at a known track (a candidate with
# >= POSTPONE_MIN_VOTES), wait one more lookup for local confirmation instead of spending an AudD
# call. At most MAX_AUDD_POSTPONES times per passage. Offline on real vinyl (Barry White replay):
# genuine candidates at 24 s had 3 votes, accidental ones up to 2, so 2 postponed for nothing.
POSTPONE_MIN_VOTES = 3
MAX_AUDD_POSTPONES = 2
# Stream holes (runs of exact digital zeros: audio lost between source and MA) can defeat AudD. In
# 318 real AudD samples (2026-11-19..23) of tracks AudD knows, the hit rate was 64 % with no hole,
# 60 % with 1, 44-58 % with 2-4 and 25 % with 5 or more (1 of 4). A sample with at least
# AUDD_HOLES_TO_WAIT holes waits for the next window (6 s later), at most AUDD_MAX_HOLE_SKIPS times
# in a row. Waiting for any hole (first version, same day) cost 12 s on a third of the tracks in the
# regression and rarely found a clean window: holes come in bursts.
AUDD_HOLE_MIN_SECONDS = 0.002
AUDD_HOLES_TO_WAIT = 5
AUDD_MAX_HOLE_SKIPS = 1
# "Fast start": windows that end within the first fast_start_seconds of a passage are
# fingerprinted at 1/4 instead of 1/16 (a superset), when learning and when looking up, so a
# known track is recognized sooner from its start. Costs disk space; off unless configured.
FAST_START_DENSITY_DIVISOR = 4
AUDD_INITIAL_BACKOFF_SECONDS = 60
AUDD_MAX_BACKOFF_SECONDS = 900

# Local match acceptance. A strong candidate identifies at once; a credible one must be the
# same track on consecutive attempts. Random collisions mostly give 1-3 votes; the best wrong
# track of a lookup is the maximum over the whole database, so it grows (slowly) with every
# record learned: offline max 4 over all recent recordings, and on 2026-11-21 one live lookup gave
# a wrong track 5 votes (Percy Sledge shown as Kurt Vile). Strong needs 6: the 5-vote genuine hits
# (6 of ~70 offline) are then accepted a lookup later (the overnight ablation saw no difference).
STRONG_VOTES = 6
STRONG_RATIO = 0.60
CONFIRM_VOTES = 3
CONFIRM_RATIO = 0.60
CONFIRMATIONS_REQUIRED = 2
# ...and together those windows must hold this many distinct hashes at one time shift (the
# track's time-consistent cluster). A live false hit (2026-11-19: a Kurt Vile track identified as
# a Barry White track) had 3 votes on two lookups but a cluster of only 3. Genuine confirmed hits
# measured on all recordings: clusters of 6 and 7.
CONFIRM_COHERENT_VOTES = 5
# Time-consistent evidence: on a new play of a record only 2-4 votes per window repeat (vinyl
# differs a little every play), so the votes of every lookup of a passage add up per (track,
# reference passage), but only those at the same absolute time shift between this passage and the
# earlier play. Genuine replays stay within +-0.15 s over minutes; accidental hits fall at random
# shifts. Offline on three real sessions (2026-11-18): genuine 6-21 votes after 18-24 s, accidental
# at most 3 over whole 4-5 minute passages (30-51 lookups). A hash counts once per cluster.
# Every lookup is another chance for an accidental cluster, so the bar rises with the number of
# lookups: max(6, 5 + lookups/6) votes (6 up to 1 minute, 10 after 3 minutes). Measured
# 2026-11-19 on all 12 recordings replayed without AudD (no token): accidental clusters reached
# 6 votes after 30 lookups (a false "Easy Lover" that poisoned the DB) and 5 votes from 120 s on;
# this rule rejects all 217, keeps every genuine time-consistent hit up to 168 s (median 18 s),
# with at least 2.3 votes margin to the closest accidental cluster.
COHERENT_VOTES = 6
COHERENT_VOTES_BASE = 5
COHERENT_LOOKUPS_PER_EXTRA_VOTE = 6
COHERENT_LOOKUPS = 2  # from at least this many lookups (a single window is judged above)
# ...and at least this many of those votes must come from ordinary (1/16) fingerprints: the dense
# ones of fast start (1/4) make more accidental votes. Measured 2026-11-22 by looking up 318
# recorded tracks in a database without them: 7 false hits, all with 6-7 votes at 18-42 s and at
# most 3 ordinary ones; with 4 none, and no genuine hit lost (24 of 240 came 15 s later on average).
COHERENT_ORDINARY_VOTES = 4
# ...unless the cluster is this big anyway: the largest accidental cluster in all recordings had 8
# votes, genuine replays of a first play learned densely reach 11-12 at 26-32 s (Everly Brothers,
# 2026-11-23); with this, 0 false hits and 21 (not 24) of 240 genuine ones later than before.
COHERENT_LARGE_VOTES = 10
# A single lookup is enough for a cluster this big (with the ordinary votes above): Ink at 14 s had
# 58 time-consistent votes but a ratio of 0.557 and waited a lookup (2026-11-22). Simulated on 410
# passages (2026-11-23): 10 known tracks 6 s sooner, no extra false hit; the largest cluster of a
# really different track had 9 votes.
COHERENT_SINGLE_LOOKUP_VOTES = 20
COHERENCE_BIN_SECONDS = 0.05
COHERENCE_BINS = 3  # a cluster spans +-3 bins: +-0.15 s (+ quantization)

# Keep the references of at most this many plays per part of a track (the current one included),
# so a 5-minute track keeps as many plays as a 3-minute one (a reference cap of 60 left long tracks
# with a single play): ~1 MB per 3-minute track. Counted per song position, not per play: an older
# play goes only when newer plays cover all but MIN_UNIQUE_COVERAGE_SECONDS of it this many times,
# so a needle drop mid-song never evicts the play that holds the start (2026-11-19: two drops in
# Foreigner Suite removed both full plays).
MAX_PLAYS_PER_TRACK = 2
MIN_UNIQUE_COVERAGE_SECONDS = 30
# Where in the song a local match is. A loop-based track repeats itself, so a weak match can be
# right about the track and wrong about the position (2026-11-19, Hard Day: song start -195 s
# instead of +2 s, which gave a false soft boundary mid-song). A passage that began after a real
# gap normally begins with its song: a weak position (fewer than RELIABLE_POSITION_VOTES in its
# cluster) that puts the song start more than LOCAL_START_PRIOR_SECONDS before the passage start
# is not trusted; the song is assumed to start with the passage (not exact: nothing is stored
# or trimmed). Measured on 45 local hits of all recordings: genuine starts after a gap -9..+1 s.
LOCAL_START_PRIOR_SECONDS = 15
RELIABLE_POSITION_VOTES = 12
# Decide the position later (2026-11-20, needle drops in Foreigner Suite: all three positions were
# right but too weak to trust at the first hit): after a local identification without an exact
# song start, every learned window is also looked up for up to LATER_POSITION_LOOKUPS windows. The
# shared hashes with plays whose song start is stored add up per song start (all those plays vote
# together); a song start is adopted once its cluster has RELIABLE_POSITION_VOTES from at least
# CONFIRMATIONS_REQUIRED lookups and POSITION_LEAD_FACTOR times the votes of any cluster more than
# POSITION_RIVAL_SECONDS away (a song that repeats itself makes rival clusters: no decision).
LATER_POSITION_LOOKUPS = 20
# a passage after a gap whose AudD song start matches the play before the gap: the same song
CONTINUATION_TOLERANCE_SECONDS = 5
# A local hit after a gap that split this same song (Hard Day's quiet moment at 0:25, 2026-11-21):
# the song goes on where the play before the gap was, which a loop-based song can contradict with
# a stronger cluster elsewhere (decided at 3:43 instead of 0:25 live). The continuation is shown at
# once and adopted once positioned plays give it CONTINUATION_CONFIRM_VOTES; another position only
# after CONTINUATION_PATIENCE_LOOKUPS lookups without any support for it (a needle moved within
# the same song).
# A quiet moment inside a song resumes right after the gap is confirmed (Hard Day: 1 s after the
# close), a lifted needle later (the needle drops of 2026-11-20: ~3 s): only the former continues.
CONTINUATION_MAX_RESUME_SECONDS = 1.5
CONTINUATION_CONFIRM_VOTES = 3
CONTINUATION_PATIENCE_LOOKUPS = 5
POSITION_LEAD_FACTOR = 2
POSITION_RIVAL_SECONDS = 5
# A 12 s window at 6.25% density holds ~260 landmarks on real vinyl (9-390 measured). A window
# far above that means abnormal input (noise, a broken source); it is not stored, so such a
# source cannot blow up the database.
MAX_LANDMARKS_PER_REFERENCE = 2000


@dataclass(frozen=True)
class GapSettings:
    """Silence detection thresholds (validated on the real Sendspin turntable setup)."""

    silence_dbfs: float = -45.0
    resume_dbfs: float = -35.0
    min_gap_seconds: float = 2.0


class GapEvent(Enum):
    """Transition reported by GapDetector."""

    GAP = "gap"
    RESUME = "resume"


class GapDetector:
    """Hysteresis silence detector over chunk levels."""

    def __init__(self, settings: GapSettings) -> None:
        """Start outside a gap."""
        self.settings = settings
        self.in_gap = False
        self.silence_seconds = 0.0

    def update(self, level_dbfs: float, chunk_seconds: float) -> GapEvent | None:
        """Feed one chunk level; return a transition if one happened."""
        if level_dbfs <= self.settings.silence_dbfs:
            self.silence_seconds += chunk_seconds
            if not self.in_gap and self.silence_seconds >= self.settings.min_gap_seconds:
                self.in_gap = True
                return GapEvent.GAP
            return None
        if level_dbfs >= self.settings.resume_dbfs:
            self.silence_seconds = 0.0
            if self.in_gap:
                self.in_gap = False
                return GapEvent.RESUME
        elif not self.in_gap:
            # In-between level while playing: a quiet moment, not silence.
            self.silence_seconds = 0.0
        return None


class Decision(StrEnum):
    """Outcome of judging a local candidate."""

    ACCEPT_STRONG = "accepted (strong)"
    ACCEPT_CONFIRMED = "accepted (confirmed)"
    ACCEPT_COHERENT = "accepted (time-consistent)"
    PENDING = "pending confirmation"
    REJECT = "rejected"

    @property
    def accepted(self) -> bool:
        """Return whether this decision identifies the track."""
        return self in (
            Decision.ACCEPT_STRONG,
            Decision.ACCEPT_CONFIRMED,
            Decision.ACCEPT_COHERENT,
        )


@dataclass
class MatchConfirmer:
    """
    Judge the best single window of each lookup.

    Two ways to accept: one strong window, or the same track credible on consecutive lookups.
    Evidence spread over many lookups is judged by TimelineEvidence.
    """

    pending_track_id: int | None = None
    pending_count: int = 0
    # (track_id, votes) of the latest lookup's best candidate, None after a miss
    latest: tuple[int, int] | None = None

    def reset(self) -> None:
        """Forget everything (after an identification)."""
        self.pending_track_id = None
        self.pending_count = 0
        self.latest = None

    def record_miss(self) -> None:
        """Register a lookup without any candidate: it breaks a consecutive run."""
        self.pending_track_id = None
        self.pending_count = 0
        self.latest = None

    def consider(self, candidate: QueryMatch) -> Decision:
        """Judge one candidate."""
        self.latest = (candidate.track_id, candidate.best_votes)
        if candidate.best_votes >= STRONG_VOTES and candidate.ratio >= STRONG_RATIO:
            self.reset()
            return Decision.ACCEPT_STRONG
        if candidate.best_votes >= CONFIRM_VOTES and candidate.ratio >= CONFIRM_RATIO:
            if self.pending_track_id == candidate.track_id:
                self.pending_count += 1
            else:
                self.pending_track_id = candidate.track_id
                self.pending_count = 1
            if self.pending_count >= CONFIRMATIONS_REQUIRED:
                self.reset()
                return Decision.ACCEPT_CONFIRMED
            return Decision.PENDING
        self.pending_track_id = None
        self.pending_count = 0
        return Decision.REJECT


@dataclass(frozen=True)
class Coherence:
    """The strongest time-consistent cluster of evidence in a passage."""

    track_id: int
    reference_passage_id: str
    votes: int  # distinct hashes at (about) one time shift
    lookups: int  # lookups that contributed to them
    latest_votes: int  # of which found by the newest lookup
    shift_seconds: float  # reference passage time minus this passage's time
    ordinary_votes: int = 0  # of the votes, those from ordinary (not only dense) fingerprints


@dataclass(frozen=True)
class SongPosition:
    """Where the song started in this passage, from evidence against positioned plays."""

    start_seconds: float  # passage time of the song start
    votes: int  # distinct (play, hash) pairs at (about) this song start
    lookups: int
    rival_votes: int  # the strongest cluster more than POSITION_RIVAL_SECONDS away

    @property
    def decisive(self) -> bool:
        """Return whether this position is strong and clearly ahead of any other."""
        return (
            self.votes >= RELIABLE_POSITION_VOTES
            and self.lookups >= CONFIRMATIONS_REQUIRED
            and self.votes >= POSITION_LEAD_FACTOR * self.rival_votes
        )


class TimelineEvidence:
    """
    Add the shared hashes of every lookup of a passage up by (track, reference passage, shift).

    The shift of one shared hash is its time in the reference passage (window offset + stored
    position) minus its time in this passage (window start + query position). A real replay of
    the same audio keeps one shift; accidental collisions scatter.
    """

    def __init__(self, frame_seconds: float = PANAKO_FRAME_SECONDS) -> None:
        """Start empty; frame_seconds is the duration of one fingerprint position step."""
        self.frame_seconds = frame_seconds
        self.lookups = 0
        # (track_id, reference passage) -> shift bin -> hash -> lookups that saw it there
        self._bins: dict[tuple[int, str], dict[int, dict[str, set[int]]]] = {}
        # hashes only a dense (fast start) window holds, not an ordinary one
        self._dense_only: set[str] = set()

    def add(
        self,
        query: Fingerprints,
        rows: list[MatchRow],
        query_start_seconds: float,
        *,
        new_lookup: bool = True,
        dense: bool = False,
    ) -> None:
        """
        Add one lookup (or, with new_lookup=False, another phase of the same lookup).

        dense tells that the query was fingerprinted at fast start's higher density.
        """
        if new_lookup or self.lookups == 0:
            self.lookups += 1
        if dense:
            self._dense_only.update(query.keys() - density_subset(query, DENSITY_DIVISOR).keys())
        for row in rows:
            for query_position in query.get(row.fingerprint_hash, ()):
                shift = (
                    row.passage_offset_seconds
                    + (row.position - query_position) * self.frame_seconds
                    - query_start_seconds
                )
                bins = self._bins.setdefault((row.track_id, row.passage_id), {})
                seen = bins.setdefault(round(shift / COHERENCE_BIN_SECONDS), {})
                seen.setdefault(row.fingerprint_hash, set()).add(self.lookups)

    def best(
        self, track_id: int | None = None, min_ordinary: int = 0, large: int | None = None
    ) -> Coherence | None:
        """
        Return the strongest cluster (of one track if given), or None.

        With min_ordinary, a cluster needs that many ordinary votes, unless it has at least
        large votes in total.
        """
        best: Coherence | None = None
        for (track, passage_id), bins in sorted(self._bins.items()):
            if track_id is not None and track != track_id:
                continue
            for center in sorted(bins):
                hashes: dict[str, set[int]] = {}
                for index in range(center - COHERENCE_BINS, center + COHERENCE_BINS + 1):
                    for fp_hash, seen in bins.get(index, {}).items():
                        hashes.setdefault(fp_hash, set()).update(seen)
                lookups = set().union(*hashes.values())
                candidate = Coherence(
                    track,
                    passage_id,
                    len(hashes),
                    len(lookups),
                    sum(1 for seen in hashes.values() if self.lookups in seen),
                    center * COHERENCE_BIN_SECONDS,
                    sum(1 for fp_hash in hashes if fp_hash not in self._dense_only),
                )
                if candidate.ordinary_votes < min_ordinary and (
                    large is None or candidate.votes < large
                ):
                    continue
                if best is None or (candidate.votes, candidate.lookups) > (
                    best.votes,
                    best.lookups,
                ):
                    best = candidate
        return best

    def _song_start_clusters(
        self, track_id: int, song_starts: dict[str, Any]
    ) -> list[tuple[int, int, int]]:
        """Return (votes, lookups, song start bin) per song start of a track (positioned plays)."""
        merged: dict[int, dict[tuple[str, str], set[int]]] = {}
        for (track, passage_id), bins in self._bins.items():
            start = song_starts.get(passage_id)
            if track != track_id or isinstance(start, bool) or not isinstance(start, int | float):
                continue
            offset = round(float(start) / COHERENCE_BIN_SECONDS)
            for shift_bin, hashes in bins.items():
                target = merged.setdefault(offset - shift_bin, {})
                for fp_hash, seen in hashes.items():
                    target.setdefault((passage_id, fp_hash), set()).update(seen)
        clusters: list[tuple[int, int, int]] = []
        for center in sorted(merged):
            votes: dict[tuple[str, str], set[int]] = {}
            for index in range(center - COHERENCE_BINS, center + COHERENCE_BINS + 1):
                for key, seen in merged.get(index, {}).items():
                    votes.setdefault(key, set()).update(seen)
            clusters.append((len(votes), len(set().union(*votes.values())), center))
        return clusters

    def song_start(self, track_id: int, song_starts: dict[str, Any]) -> SongPosition | None:
        """
        Return the strongest song start of a track, from the plays with a stored song start.

        A shift maps a reference play onto this passage, so the song started at the reference
        play's song start minus the shift; plays without a stored start cannot say where the song
        is and are left out (their passage start is not the song start after a needle drop).
        """
        clusters = self._song_start_clusters(track_id, song_starts)
        if not clusters:
            return None
        votes_best, lookups, center = max(clusters)
        rival = max(
            (
                v
                for v, _, c in clusters
                if abs(c - center) * COHERENCE_BIN_SECONDS > POSITION_RIVAL_SECONDS
            ),
            default=0,
        )
        return SongPosition(center * COHERENCE_BIN_SECONDS, votes_best, lookups, rival)

    def votes_near(
        self, track_id: int, song_starts: dict[str, Any], start_seconds: float, tolerance: float
    ) -> int:
        """Return the votes of the strongest song start within tolerance of start_seconds."""
        return max(
            (
                v
                for v, _, c in self._song_start_clusters(track_id, song_starts)
                if abs(c * COHERENCE_BIN_SECONDS - start_seconds) <= tolerance
            ),
            default=0,
        )

    def clear(self) -> None:
        """Drop all evidence (after an identification)."""
        self._bins.clear()
        self._dense_only.clear()


@dataclass(frozen=True)
class ClosedPlay:
    """An identified passage that ended at a soft boundary (no gap): corrected later."""

    passage_id: str
    track_id: int
    song_start_at: float | None
    song_start_exact: bool
    closed_at: float  # passage seconds at the soft boundary


@dataclass(eq=False)
class Passage:
    """Active state of one continuous piece of music. Discarded at the next gap."""

    window_bytes: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    pcm: bytearray = field(default_factory=bytearray)
    seconds: float = 0.0  # fresh audio received in this passage
    track_id: int | None = None
    metadata: dict[str, Any] | None = None
    confirmer: MatchConfirmer = field(default_factory=MatchConfirmer)
    next_local_at: float = float(SAMPLE_SECONDS)
    next_learn_at: float = 0.0
    learning_capped: bool = False
    audd_attempts: int = 0
    audd_postponed: int = 0
    next_audd_at: float = float(AUDD_FIRST_ATTEMPT_SECONDS)
    early_windows: list[tuple[float, Fingerprints]] = field(default_factory=list)
    early_holes: dict[float, int] = field(default_factory=dict)  # stream holes per window end
    evidence: TimelineEvidence = field(default_factory=TimelineEvidence)
    # passage time at which the identified song started (may be negative) and should end
    song_start_at: float | None = None
    track_end_at: float | None = None
    duration_checks_left: int = 0
    song_start_exact: bool = False  # song_start_at from an AudD timecode or a stored song start
    follows: ClosedPlay | None = None  # the identified play this passage followed without a gap
    after_gap: bool = False  # opened by audio resuming after a real gap
    opened_at: float = 0.0  # stream seconds when the passage opened
    settled: bool = False  # song start stored and the previous play corrected
    position_lookups_left: int = 0  # deciding the song start later (see LATER_POSITION_*)
    audd_hit_speed_percent: float | None = None  # correction of the sample AudD identified
    continuation_start: float | None = None  # song start if it goes on from before the gap
    title_checks_left: int = 0  # AudD's title came with an implausible timecode: ask again
    # a distrusted AudD timecode: the song start it gave, and when to ask whether the song ended
    doubted_start: float | None = None
    end_check_at: float | None = None
    audd_hole_skips: int = 0  # AudD samples skipped in a row for stream holes

    @property
    def short_id(self) -> str:
        """Return a log-friendly prefix of the id."""
        return self.id[:8]

    @property
    def identified(self) -> bool:
        """Return whether this passage has been identified."""
        return self.track_id is not None

    @property
    def window_ready(self) -> bool:
        """Return whether a full window of this passage's own audio is buffered."""
        return len(self.pcm) >= self.window_bytes

    def append(self, chunk: bytes, chunk_seconds: float) -> None:
        """Append audio, keeping only the newest window."""
        self.seconds += chunk_seconds
        self.pcm.extend(chunk)
        if (excess := len(self.pcm) - self.window_bytes) > 0:
            del self.pcm[:excess]


def coherent_votes_required(lookups: int) -> float:
    """Return the votes a time-consistent cluster needs after this many lookups."""
    return max(COHERENT_VOTES, COHERENT_VOTES_BASE + lookups / COHERENT_LOOKUPS_PER_EXTRA_VOTE)


def _local_song_start(
    metadata: dict[str, Any], coherence: Coherence | None
) -> tuple[float | None, bool]:
    """
    Return (song start in this passage, exact?) for a local match.

    The time shift maps the reference play onto this passage; where the song started in that
    play is stored for plays learned with an exact start. Without it the reference play is
    assumed to have started with the song (true after a silence gap).
    """
    if coherence is None:
        return None, False
    starts = metadata.get(SONG_STARTS) or {}
    reference_start = starts.get(coherence.reference_passage_id)
    if isinstance(reference_start, int | float) and not isinstance(reference_start, bool):
        return float(reference_start) - coherence.shift_seconds, True
    return -coherence.shift_seconds, False


def _track_length(metadata: dict[str, Any], hit: dict[str, Any] | None = None) -> Any:
    """Return the track length to use: one measured on this record beats AudD's."""
    if metadata.get(DURATION_SOURCE) == "observed":
        return metadata.get("duration_seconds")
    return (hit or {}).get("duration_seconds") or metadata.get("duration_seconds")


def _positive(value: Any) -> bool:
    """Return whether value is a usable positive number (a duration)."""
    return not isinstance(value, bool) and isinstance(value, int | float) and value > 0


def describe(metadata: dict[str, Any] | None) -> str:
    """Return 'Artist - Title' for logs."""
    if not metadata:
        return "<unknown>"
    return f"{metadata.get('artist')} - {metadata.get('title')}"


class RecognitionEngine:
    """
    Gap/passage state machine plus recognition and learning for one stream.

    Dependencies are injected so the engine can be tested without Music Assistant:
    - fingerprint: async PCM window -> density-reduced hashes (CPU work offloaded)
    - db: FingerprintDatabase (called through run_blocking)
    - audd: async WAV -> metadata | None, raising AudDError on failure; None if no token
    - publish: report metadata to MA; False means stop recognizing this stream
    - spawn: start a background task
    """

    def __init__(  # noqa: PLR0913 - injected dependencies keep the engine testable
        self,
        *,
        fmt: PcmFormat,
        gap_settings: GapSettings,
        fingerprint: Callable[[bytes, int], Awaitable[Fingerprints]],
        db: FingerprintDatabase,
        audd: Callable[[bytes], Awaitable[dict[str, Any] | AudDNoMatch | None]] | None,
        publish: Callable[[dict[str, Any]], bool],
        spawn: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
        logger: logging.Logger,
        run_blocking: Callable[..., Awaitable[Any]] = asyncio.to_thread,
        save_sample: Callable[[bytes, dict[str, Any]], Awaitable[None]] | None = None,
        audd_speed_percent: float = 0.0,
        postpone_audd: bool = True,
        fast_start_seconds: float = 0.0,
        frame_seconds: float = PANAKO_FRAME_SECONDS,
        lookup_phases: int = 1,
        audd_thorough: bool = False,
    ) -> None:
        """Create the engine; the stream starts inside a passage."""
        self.fmt = fmt
        self.gap = GapDetector(gap_settings)
        self._fingerprint = fingerprint
        self._db = db
        self._audd = audd
        self._save_sample = save_sample
        self.audd_speed_percent = audd_speed_percent
        self.postpone_audd = postpone_audd
        self.fast_start_seconds = fast_start_seconds
        self.lookup_phases = max(1, lookup_phases)
        self.audd_thorough = audd_thorough
        self.soft_boundaries = 0
        # the last identified play that closed: (track id, song start in stream seconds)
        self.last_play: tuple[int, float] | None = None
        self.last_close_at = 0.0  # stream seconds when the last passage closed
        # a length measured at a gap, kept until the next passage shows the gap was not a quiet
        # moment inside the same song (Hard Day's 0:25 noted 22.6 and 18.8 s, 2026-11-21)
        self.pending_length: tuple[int, float] | None = None
        self.dropout_seconds = 0.0
        self.shown_passage_id: str | None = None  # passage whose track MA shows
        self.background: set[asyncio.Task[None]] = set()
        self.frame_seconds = frame_seconds
        self._publish = publish
        self._spawn = spawn
        self._run_blocking = run_blocking
        self.logger = logger
        self.window_bytes = fmt.bytes_per_second * SAMPLE_SECONDS
        self.stream_seconds = 0.0
        self.stopped = False
        self.task: asyncio.Task[None] | None = None
        self.last_audd_at: float | None = None  # stream clock
        self.audd_backoff_seconds = 0
        self.audd_blocked_until = 0.0
        self._warned_no_token = False
        self.passage: Passage | None = None
        # Start inside a passage so music already playing is recognized; an idle
        # source simply closes it after min_gap_seconds without ever fingerprinting.
        self._open_passage("stream started")

    # ---------------------------------------------------------------- stream input

    def feed(self, chunk: bytes) -> None:
        """Process one PCM chunk. Never blocks; heavy work runs in a task."""
        if self.stopped or not chunk:
            return
        chunk_seconds = len(chunk) / self.fmt.bytes_per_second
        self.stream_seconds += chunk_seconds
        # Classify the chunk BEFORE it is buffered: a gap must be a hard boundary.
        level = pcm_dbfs(chunk, self.fmt)
        event = self._gap_event(level, chunk_seconds)
        if event is GapEvent.GAP:
            self._close_passage(level, chunk_seconds)
        elif event is GapEvent.RESUME:
            self._open_passage(f"audio resumed at {level:.1f} dBFS").after_gap = True
        elif (passage := self.passage) is not None and self._past_track_end(passage):
            self._end_at_track_end(passage)
        if self.passage is None:
            if (
                self.pending_length is not None
                and self.stream_seconds - self.last_close_at > CONTINUATION_MAX_RESUME_SECONDS
            ):
                self._commit_pending_length()  # too long a silence for a quiet moment
            if (
                self.shown_passage_id is not None
                and self.gap.in_gap
                and self.gap.silence_seconds >= WITHDRAW_AFTER_SILENCE_SECONDS
            ):
                self._withdraw_shown_track(f"{WITHDRAW_AFTER_SILENCE_SECONDS:.0f}s of silence")
            return  # inside a gap: no buffering, fingerprinting, matching or AudD
        self.passage.append(chunk, chunk_seconds)
        self._schedule(self.passage)

    def _gap_event(self, level: float, chunk_seconds: float) -> GapEvent | None:
        """Update the gap detector, treating a short run of digital zeros as a dropout."""
        if level <= DROPOUT_DBFS and not self.gap.in_gap:
            self.dropout_seconds += chunk_seconds
            identified = self.passage is not None and self.passage.identified
            if self.dropout_seconds < (
                RECONNECT_MAX_SECONDS if identified else DROPOUT_MAX_SECONDS
            ):
                return None  # keep the passage (and its clock) running through the dropout
            # the source stopped sending audio: a real gap, counted from the first zero
            event = self.gap.update(level, self.dropout_seconds)
            self.dropout_seconds = 0.0
            return event
        if self.dropout_seconds:
            self.logger.info(
                "Stream dropout: %.1fs of digital silence ignored; passage %s continues",
                self.dropout_seconds,
                self.passage.short_id if self.passage else "-",
            )
            self.dropout_seconds = 0.0
        return self.gap.update(level, chunk_seconds)

    def close(self) -> None:
        """Stop the stream and cancel in-flight work."""
        self._commit_pending_length()
        self.stopped = True
        self.passage = None
        if self.task is not None and not self.task.done():
            self.task.cancel()

    # ---------------------------------------------------------------- passages

    def _open_passage(self, reason: str) -> Passage:
        passage = Passage(
            window_bytes=self.window_bytes,
            evidence=TimelineEvidence(self.frame_seconds),
            opened_at=self.stream_seconds,
        )
        self.passage = passage
        self.logger.info(
            "Passage %s started (%s); first lookup after %ds of fresh audio",
            passage.short_id,
            reason,
            SAMPLE_SECONDS,
        )
        return passage

    def _close_passage(self, level: float, chunk_seconds: float = 0.0) -> None:
        passage = self.passage
        self.passage = None  # in-flight tasks for it now discard their results
        if passage is None:
            return
        self._commit_pending_length()
        self.last_close_at = self.stream_seconds
        if passage.track_id is not None and passage.song_start_at is not None:
            self.last_play = (passage.track_id, passage.opened_at + passage.song_start_at)
        self.logger.info(
            "Track boundary: %.1f dBFS for %.1fs; passage %s closed after %.0fs (%s)",
            level,
            self.gap.silence_seconds,
            passage.short_id,
            passage.seconds,
            describe(passage.metadata) if passage.identified else "unidentified",
        )
        if (
            passage.track_id is not None
            and passage.song_start_at is not None
            and passage.song_start_exact
            and level > DROPOUT_DBFS
        ):
            # the chunk that confirmed the gap is not part of the passage
            song_end = passage.seconds - (self.gap.silence_seconds - chunk_seconds)
            self._commit_pending_length()
            self.pending_length = (passage.track_id, song_end - passage.song_start_at)
        elif (tail_of := self._soft_boundary_tail(passage, level)) is not None:
            # the previous track went on past its expected end and stopped here
            previous = tail_of
            assert previous.song_start_at is not None  # checked by _soft_boundary_tail
            tail_end = passage.seconds - (self.gap.silence_seconds - chunk_seconds)
            length = previous.closed_at + tail_end - previous.song_start_at
            self.logger.info(
                "Passage %s was the last %.0fs of track %d after the soft boundary: "
                "its length on this record is about %.0fs",
                passage.short_id,
                tail_end,
                previous.track_id,
                length,
            )
            self.pending_length = (previous.track_id, length)

    @staticmethod
    def _soft_boundary_tail(passage: Passage, level: float) -> ClosedPlay | None:
        """
        Return the play this passage most likely finished, or None.

        AudD's length is often another edit's: a soft boundary then cuts a track early (Magic,
        2026-11-22/23: AudD 258 s, the record ~278 s) and its last seconds become a short passage
        that nothing identifies, closed by the real gap at the end of the track. That gap gives
        the track's length on this record. Not when the tail looks like another track: a
        candidate for a different track, or longer than SOFT_TAIL_MAX_SECONDS.
        """
        previous = passage.follows
        if (
            previous is None
            or passage.identified
            or previous.song_start_at is None
            or not previous.song_start_exact
            or level <= DROPOUT_DBFS
            or passage.seconds > SOFT_TAIL_MAX_SECONDS
        ):
            return None
        other = passage.evidence.best()
        if (
            other is not None
            and other.track_id != previous.track_id
            and other.votes >= POSTPONE_MIN_VOTES
        ):
            return None
        return previous

    @staticmethod
    def _past_track_end(passage: Passage) -> bool:
        return (
            passage.track_end_at is not None
            and passage.seconds >= passage.track_end_at + TRACK_END_GUARD_SECONDS
        )

    def _end_at_track_end(self, passage: Passage) -> None:
        """Soft boundary: the track should have ended, but no gap came (a gapless record)."""
        self.passage = None  # in-flight tasks for it now discard their results
        self.logger.info(
            "Track boundary (no gap): %s should have ended at %.0fs; passage %s closed after %.0fs",
            describe(passage.metadata),
            passage.track_end_at,
            passage.short_id,
            passage.seconds,
        )
        self.soft_boundaries += 1
        following = self._open_passage("no gap after the expected end of the previous track")
        if passage.track_id is not None:
            following.follows = ClosedPlay(
                passage.id,
                passage.track_id,
                passage.song_start_at,
                passage.song_start_exact,
                passage.seconds,
            )

    def _is_current(self, passage: Passage) -> bool:
        return not self.stopped and self.passage is passage

    # ---------------------------------------------------------------- scheduling

    def _schedule(self, passage: Passage) -> None:
        if self.task is not None and not self.task.done():
            return
        if not passage.window_ready:
            return
        if passage.identified:
            if passage.seconds < passage.next_learn_at or passage.learning_capped:
                return
            if (
                passage.track_end_at is not None
                and passage.seconds > passage.track_end_at - self._learn_guard(passage)
            ):
                passage.learning_capped = True
                self.logger.info(
                    "Passage %s: stopped learning %ds before the expected end of %s (%.0fs)",
                    passage.short_id,
                    self._learn_guard(passage),
                    describe(passage.metadata),
                    passage.track_end_at,
                )
                return
            limit = self._learn_limit(passage)
            if passage.seconds > limit:
                passage.learning_capped = True
                self.logger.info("Passage %s: stopped learning after %ds", passage.short_id, limit)
                return
            passage.next_learn_at = passage.seconds + LEARN_INTERVAL_SECONDS
            self.task = self._spawn(self._learn(passage, bytes(passage.pcm), passage.seconds))
            return
        if passage.seconds >= passage.next_local_at:
            passage.next_local_at = passage.seconds + LOCAL_INTERVAL_SECONDS
            self.task = self._spawn(self._recognize(passage, bytes(passage.pcm), passage.seconds))

    # ---------------------------------------------------------------- recognition

    async def _recognize(self, passage: Passage, pcm: bytes, window_end: float) -> None:
        try:
            query = await self._fingerprint(pcm, self._divisor(window_end))
            if not self._is_current(passage):
                return
            if len(passage.early_windows) < MAX_EARLY_WINDOWS:
                passage.early_windows.append((window_end, query))
                passage.early_holes[window_end] = self._holes(pcm)
            local = await self._run_blocking(self._db.lookup, query, passage.id)
            if not self._is_current(passage):
                return
            dense = self._divisor(window_end) < DENSITY_DIVISOR
            passage.evidence.add(query, local.rows, window_end - SAMPLE_SECONDS, dense=dense)
            local = await self._other_phases(passage, pcm, window_end, local)
            if local is None:
                return
            if await self._judge_local(passage, local):
                await self._store_early_windows(passage)
                return
            if not self._postpone_audd(passage):
                await self._recognize_with_audd(passage, pcm, query, window_end)
            if self._is_current(passage) and not passage.identified:
                self._clear_stale_track(passage)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Recognition failed (passage %s)", passage.short_id)

    async def _other_phases(
        self, passage: Passage, pcm: bytes, window_end: float, local: LocalLookup
    ) -> LocalLookup | None:
        """
        Look up the same window at other sub-frame offsets; return the strongest result.

        Panako hashes depend on where the audio falls on the 2048-sample frame grid: half a
        frame of offset keeps ~1 % of the hashes of identical audio. Two plays of a record land
        at random offsets, so a lookup is repeated with the window shifted by k/phases of a frame
        (k = 1..phases-1). All phases count as one lookup for the evidence; references are learned
        from phase 0 only. Returns None if the passage changed meanwhile.
        """
        frame_bytes = self.fmt.channels * self.fmt.sample_width
        hop_frames = self.frame_seconds * self.fmt.sample_rate
        best = local
        for k in range(1, self.lookup_phases):
            skip = round(hop_frames * k / self.lookup_phases)
            query = await self._fingerprint(pcm[skip * frame_bytes :], self._divisor(window_end))
            if not self._is_current(passage):
                return None
            found = await self._run_blocking(self._db.lookup, query, passage.id)
            if not self._is_current(passage):
                return None
            start = window_end - SAMPLE_SECONDS + skip / self.fmt.sample_rate
            dense = self._divisor(window_end) < DENSITY_DIVISOR
            passage.evidence.add(query, found.rows, start, new_lookup=False, dense=dense)
            if found.best is not None and (
                best.best is None or found.best.best_votes > best.best.best_votes
            ):
                best = found
        return best

    async def _recognize_with_audd(
        self, passage: Passage, pcm: bytes, query: Fingerprints, window_end: float
    ) -> None:
        """Ask AudD (if allowed); on a hit identify the passage and learn its windows."""
        hit = await self._try_audd(passage, pcm, window_end)
        if hit is None or not self._is_current(passage):
            return
        track_id = await self._run_blocking(self._db.upsert_track, hit)
        # show what is stored: a known track keeps its first metadata (see upsert_track)
        metadata = await self._run_blocking(self._db.track_metadata, track_id) or hit
        if not self._is_current(passage):
            return
        self.logger.info(
            "AudD HIT: %s (passage %s, attempt %d)",
            describe(metadata),
            passage.short_id,
            passage.audd_attempts,
        )
        # Timing comes from this hit: the timecode is where this sample ended in the song.
        duration = _track_length(metadata, hit)
        song_start_at, exact = self._audd_song_start(
            passage,
            track_id,
            metadata,
            window_end,
            parse_timecode(hit.get("audd_timecode")),
            duration=duration,
        )
        self._identify(
            passage,
            track_id,
            metadata,
            via="AudD",
            song_start_at=song_start_at,
            song_start_exact=exact,
            duration=duration,
        )
        await self._settle(passage)
        # Learn every lookup window of this passage, including the one AudD identified.
        if not passage.early_windows or passage.early_windows[-1][0] != window_end:
            passage.early_windows.append((window_end, query))
            passage.early_holes[window_end] = self._holes(pcm)
        await self._store_early_windows(passage)

    async def _judge_local(self, passage: Passage, local: LocalLookup) -> bool:
        """Apply the acceptance rules; return True if the passage got identified."""
        candidate, metadata = local.best, local.metadata
        decision = Decision.REJECT
        if candidate is None or metadata is None:
            passage.confirmer.record_miss()
            self.logger.debug("LOCAL MISS: no candidate (passage %s)", passage.short_id)
        else:
            decision = passage.confirmer.consider(candidate)
            coherence = passage.evidence.best(candidate.track_id)
            if (
                decision is Decision.ACCEPT_CONFIRMED
                and CONFIRM_COHERENT_VOTES > 0  # 0 switches the rule off (experiments)
            ) and (
                coherence is None
                or coherence.votes < CONFIRM_COHERENT_VOTES
                or coherence.lookups < CONFIRMATIONS_REQUIRED
            ):
                # The windows agree on the track but not on the time shift: not yet one recording.
                decision = Decision.PENDING
                passage.confirmer.pending_track_id = candidate.track_id
                passage.confirmer.pending_count = CONFIRMATIONS_REQUIRED - 1
            self.logger.info(
                "LOCAL %s: %s votes=%d acc=%d ratio=%.3f shared=%d ref=%d offset=%+d "
                "ref_passage=%s at=%.0fs -> %s (passage %s)",
                "HIT" if decision.accepted else "CANDIDATE",
                describe(metadata),
                candidate.best_votes,
                coherence.votes if coherence else 0,
                candidate.ratio,
                candidate.shared_hashes,
                candidate.reference_id,
                candidate.best_offset,
                candidate.reference_passage_id[:8],
                passage.seconds,
                decision.value,
                passage.short_id,
            )
            if decision.accepted:
                start, exact = self._local_song_start(passage, metadata, coherence)
                self._identify(
                    passage,
                    candidate.track_id,
                    metadata,
                    via="local",
                    song_start_at=start,
                    song_start_exact=exact,
                    duration=metadata.get("duration_seconds"),
                )
                await self._settle(passage)
                return True
        return await self._judge_timeline(passage)

    def _continuation_start(self, passage: Passage, track_id: int | None) -> float | None:
        """Return the song start if this passage (after a gap) continues the same song, or None."""
        if not passage.after_gap or self.last_play is None or self.last_play[0] != track_id:
            return None
        if passage.opened_at - self.last_close_at > CONTINUATION_MAX_RESUME_SECONDS:
            return None  # a longer silence: the needle was lifted, not a quiet moment
        start = self.last_play[1] - passage.opened_at
        return start if start < 0 else None

    def _audd_song_start(  # noqa: PLR0913 - the length is optional and keyword-only
        self,
        passage: Passage,
        track_id: int | None,
        metadata: dict[str, Any],
        window_end: float,
        timecode: float | None,
        *,
        duration: Any = None,
    ) -> tuple[float | None, bool]:
        """
        Return (song start, exact?) from an AudD timecode, distrusting another edit's timecode.

        AudD often matches another edit of the recording (In The Air Tonight: a 7:18 upload for
        a 5:13 single, 2026-11-20); its timecode then puts the song start minutes before a
        passage that began after a real gap, which ended the passage mid-song and, stored as
        exact, misled every later local hit. Like a weak local position (LOCAL_START_PRIOR_*):
        assume the song started with the passage, not exact; deciding the position later from
        positioned plays can still correct it. Not when the gap split this same song (a quiet
        moment, Hard Day 2026-11-19): the play before the gap then puts the song start at the
        same moment, within CONTINUATION_TOLERANCE_SECONDS.
        """
        if timecode is None:
            return None, False
        start = window_end - timecode * self._passage_factor(passage)
        if passage.after_gap and start < -LOCAL_START_PRIOR_SECONDS:
            if (
                self.last_play is not None
                and self.last_play[0] == track_id
                and abs(passage.opened_at + start - self.last_play[1])
                <= CONTINUATION_TOLERANCE_SECONDS
            ):
                self.logger.info(
                    "%s continues the play before the gap (song started %.0fs before this "
                    "passage) (passage %s)",
                    describe(metadata),
                    -start,
                    passage.short_id,
                )
                return start, True
            self.logger.info(
                "AudD places %s %.0fs before this passage began, which began after a gap: "
                "probably another edit's timecode; assuming the song started with it (passage %s)",
                describe(metadata),
                -start,
                passage.short_id,
            )
            passage.title_checks_left = MAX_TITLE_CHECKS_PER_PASSAGE
            self._plan_end_check(passage, metadata, start, duration, window_end)
            return 0.0, False
        return start, True

    def _plan_end_check(
        self,
        passage: Passage,
        metadata: dict[str, Any],
        start: float,
        duration: Any,
        window_end: float,
    ) -> None:
        """Ask AudD again just after the end its own timecode implies (a needle drop?)."""
        if not _positive(duration):
            return
        implied_end = start + duration * self._passage_factor(passage)
        if implied_end <= window_end:
            return  # by AudD's timecode the song has ended already: it cannot be right
        passage.doubted_start = start
        passage.end_check_at = implied_end + END_CHECK_DELAY_SECONDS
        self.logger.info(
            "If it was a needle drop, %s ends at %.0fs: asking AudD again at %.0fs (passage %s)",
            describe(metadata),
            implied_end,
            passage.end_check_at,
            passage.short_id,
        )

    def _local_song_start(
        self, passage: Passage, metadata: dict[str, Any], coherence: Coherence | None
    ) -> tuple[float | None, bool]:
        """Return (song start, exact?) for a local match, distrusting weak far positions."""
        start, exact = _local_song_start(metadata, coherence)
        track_id = coherence.track_id if coherence is not None else None
        continuation = self._continuation_start(passage, track_id)
        if (
            continuation is not None
            and start is not None
            and abs(start - continuation) <= CONTINUATION_TOLERANCE_SECONDS
        ):
            self.logger.info(
                "%s continues the play before the gap (song started %.0fs before this "
                "passage) (passage %s)",
                describe(metadata),
                -start,
                passage.short_id,
            )
            return start, True
        if (
            start is not None
            and coherence is not None
            and passage.after_gap
            and start < -LOCAL_START_PRIOR_SECONDS
            and coherence.votes < RELIABLE_POSITION_VOTES
        ):
            if continuation is not None:
                # the gap split this song (a quiet moment): it goes on, whatever a loop says
                passage.continuation_start = continuation
                self.logger.info(
                    "Position of %s uncertain (song start %.0fs from %d votes) in a passage that "
                    "began after a gap; it probably continues the play before the gap (song start "
                    "%.0fs): to be confirmed (passage %s)",
                    describe(metadata),
                    start,
                    coherence.votes,
                    continuation,
                    passage.short_id,
                )
                return continuation, False
            self.logger.info(
                "Position of %s uncertain (song start %.0fs from %d votes) in a passage that "
                "began after a gap: assuming the song started with it (passage %s)",
                describe(metadata),
                start,
                coherence.votes,
                passage.short_id,
            )
            return 0.0, False
        return start, exact

    async def _judge_timeline(self, passage: Passage) -> bool:
        """Accept the strongest time-consistent cluster once it has enough evidence."""
        coherence = passage.evidence.best(
            min_ordinary=COHERENT_ORDINARY_VOTES, large=COHERENT_LARGE_VOTES
        )
        if coherence is None or coherence.votes < coherent_votes_required(passage.evidence.lookups):
            return False
        if coherence.lookups < COHERENT_LOOKUPS and not (
            coherence.votes >= COHERENT_SINGLE_LOOKUP_VOTES
            and coherence.ordinary_votes >= COHERENT_ORDINARY_VOTES
        ):
            return False
        metadata = await self._run_blocking(self._db.track_metadata, coherence.track_id)
        if metadata is None or not self._is_current(passage):
            return False
        passage.confirmer.reset()
        self.logger.info(
            "LOCAL HIT: %s votes=%d acc=%d ratio=1.000 lookups=%d shift=%+.2fs "
            "ref_passage=%s at=%.0fs -> %s (passage %s)",
            describe(metadata),
            coherence.latest_votes,
            coherence.votes,
            coherence.lookups,
            coherence.shift_seconds,
            coherence.reference_passage_id[:8],
            passage.seconds,
            Decision.ACCEPT_COHERENT.value,
            passage.short_id,
        )
        start, exact = self._local_song_start(passage, metadata, coherence)
        self._identify(
            passage,
            coherence.track_id,
            metadata,
            via="local",
            song_start_at=start,
            song_start_exact=exact,
            duration=metadata.get("duration_seconds"),
        )
        await self._settle(passage)
        return True

    def _divisor(self, window_end: float) -> int:
        """Fingerprint density for a window: denser at the start of a passage when enabled."""
        if self.fast_start_seconds and window_end <= self.fast_start_seconds:
            return FAST_START_DENSITY_DIVISOR
        return DENSITY_DIVISOR

    def _postpone_audd(self, passage: Passage) -> bool:
        """Return True (and log) when AudD is due but a local candidate looks promising."""
        if not self.postpone_audd or self._audd is None:
            return False
        if passage.audd_postponed >= MAX_AUDD_POSTPONES:
            return False
        latest = passage.confirmer.latest
        track_id = latest[0] if latest and latest[1] >= POSTPONE_MIN_VOTES else None
        coherence = passage.evidence.best(track_id)
        if track_id is None:
            # or a cluster that would be accepted but for its ordinary votes (a genuine one mostly
            # gets them a lookup later: regression 2026-11-22, 5 of 170 local hits went to AudD)
            if (
                coherence is None
                or coherence.votes < coherent_votes_required(passage.evidence.lookups)
                or coherence.lookups < COHERENT_LOOKUPS
            ):
                return False  # no promising candidate
            track_id = coherence.track_id
        if self._audd_wait_reason(passage) is not None:
            return False  # AudD isn't due anyway
        passage.audd_postponed += 1
        self.logger.info(
            "AudD postponed (%d/%d): local candidate track %d has %d votes so far (passage %s)",
            passage.audd_postponed,
            MAX_AUDD_POSTPONES,
            track_id,
            coherence.votes if coherence else (latest[1] if latest else 0),
            passage.short_id,
        )
        return True

    def _audd_wait_reason(self, passage: Passage) -> str | None:
        """Return why AudD may not be called now, or None if it may."""
        if passage.audd_attempts >= self._audd_max_attempts:
            return f"attempt limit ({self._audd_max_attempts}) reached for this passage"
        if passage.seconds < passage.next_audd_at:
            return f"next passage attempt at {passage.next_audd_at:.0f}s"
        if self.stream_seconds < self.audd_blocked_until:
            return f"error backoff ({self.audd_backoff_seconds}s)"
        if (
            self.last_audd_at is not None
            and self.stream_seconds - self.last_audd_at < AUDD_MIN_INTERVAL_SECONDS
        ):
            return "stream-wide minimum interval"
        return None

    async def _try_audd(  # noqa: PLR0911 - every refusal and outcome returns early
        self,
        passage: Passage,
        pcm: bytes,
        window_end: float,
        speed: float | None = None,
        *,
        holes_checked: bool = False,
    ) -> dict[str, Any] | None:
        if self._audd is None:
            if not self._warned_no_token:
                self._warned_no_token = True
                self.logger.warning(
                    "No AudD API token configured: unknown tracks cannot be identified "
                    "or learned; only previously learned tracks are recognized"
                )
            return None
        if (reason := self._audd_wait_reason(passage)) is not None:
            self.logger.debug("AudD skipped (passage %s): %s", passage.short_id, reason)
            return None
        if not holes_checked and self._wait_for_clean_sample(passage, pcm):
            return None
        passage.audd_attempts += 1
        fast = self.audd_thorough or passage.audd_attempts < AUDD_FAST_ATTEMPTS
        passage.next_audd_at = passage.seconds + (
            AUDD_FAST_INTERVAL_SECONDS if fast else AUDD_SLOW_INTERVAL_SECONDS
        )
        self.last_audd_at = self.stream_seconds
        if speed is None:
            speed = self._attempt_speed(passage.audd_attempts)
        self.logger.info(
            "AudD attempt %d/%d on %ds sample at %+.1f%% (passage %s)",
            passage.audd_attempts,
            self._audd_max_attempts,
            SAMPLE_SECONDS,
            speed,
            passage.short_id,
        )
        wav = pcm_to_wav(pcm, self.fmt, speed)
        try:
            result = await self._audd(wav)
        except asyncio.CancelledError:
            raise
        except AudDError as err:
            self._register_audd_failure(str(err))
            await self._keep_sample(wav, pcm, passage, window_end, f"error: {err}")
            return None
        except Exception as err:  # noqa: BLE001 - any failure is an AudD failure, never fatal
            self._register_audd_failure(f"{type(err).__name__}: {err}")
            await self._keep_sample(wav, pcm, passage, window_end, f"error: {type(err).__name__}")
            return None
        self.audd_backoff_seconds = 0
        self.audd_blocked_until = 0.0
        if result is None or isinstance(result, AudDNoMatch):
            reason = result.reason if isinstance(result, AudDNoMatch) else "no result"
            self.logger.info(
                "AudD: no match: %s (passage %s); retrying later", reason, passage.short_id
            )
            await self._keep_sample(wav, pcm, passage, window_end, f"no match: {reason}")
            return None
        await self._keep_sample(wav, pcm, passage, window_end, f"hit: {describe(result)}")
        passage.audd_hit_speed_percent = speed
        return result

    def _wait_for_clean_sample(self, passage: Passage, pcm: bytes) -> bool:
        """
        Return True if this AudD sample has many stream holes and a cleaner window should be awaited.

        Call only when AudD would be asked now. Waits at most AUDD_MAX_HOLE_SKIPS windows in a
        row; after that the sample goes anyway.
        """
        holes, longest = stream_holes(pcm, self.fmt, AUDD_HOLE_MIN_SECONDS)
        if holes >= AUDD_HOLES_TO_WAIT and passage.audd_hole_skips < AUDD_MAX_HOLE_SKIPS:
            passage.audd_hole_skips += 1
            self.logger.info(
                "AudD sample has %d stream hole(s) (longest %d ms): waiting for a clean window "
                "(%d/%d, passage %s)",
                holes,
                round(longest * 1000),
                passage.audd_hole_skips,
                AUDD_MAX_HOLE_SKIPS,
                passage.short_id,
            )
            return True
        if holes >= AUDD_HOLES_TO_WAIT:
            self.logger.info(
                "AudD sample has %d stream hole(s) (longest %d ms): no cleaner window, "
                "sending it anyway (passage %s)",
                holes,
                round(longest * 1000),
                passage.short_id,
            )
        passage.audd_hole_skips = 0
        return False

    def _attempt_speed(self, attempt: int) -> float:
        """Return the speed correction (%) for the n-th AudD attempt of a passage (1-based)."""
        offset = AUDD_SPEED_PATTERN[(attempt - 1) % len(AUDD_SPEED_PATTERN)]
        return self.audd_speed_percent + offset * AUDD_SPEED_STEP_PERCENT

    async def _keep_sample(
        self, wav: bytes, pcm: bytes, passage: Passage, window_end: float, outcome: str
    ) -> None:
        """Hand an AudD sample to the debug sink (if enabled). Never raises."""
        if self._save_sample is None:
            return
        try:
            info: dict[str, Any] = {
                "passage": passage.id,
                "attempt": passage.audd_attempts,
                "window_seconds": [round(max(0.0, window_end - SAMPLE_SECONDS), 1), window_end],
                "outcome": outcome,
                "speed_correction_percent": self._attempt_speed(passage.audd_attempts),
                "source_format": f"{self.fmt.sample_rate}Hz/{self.fmt.channels}ch/"
                f"{self.fmt.content_type}",
                **sample_stats(pcm, self.fmt),
                "stream_holes": self._holes(pcm),
            }
            await self._save_sample(wav, info)
        except Exception:
            self.logger.exception("Could not save the AudD debug sample")

    def _register_audd_failure(self, reason: str) -> None:
        self.audd_backoff_seconds = (
            AUDD_INITIAL_BACKOFF_SECONDS
            if self.audd_backoff_seconds <= 0
            else min(self.audd_backoff_seconds * 2, AUDD_MAX_BACKOFF_SECONDS)
        )
        self.audd_blocked_until = self.stream_seconds + self.audd_backoff_seconds
        self.logger.warning("AudD failed: %s; backing off %ds", reason, self.audd_backoff_seconds)

    @property
    def _audd_max_attempts(self) -> int:
        return AUDD_THOROUGH_MAX_ATTEMPTS if self.audd_thorough else AUDD_MAX_ATTEMPTS_PER_PASSAGE

    @property
    def _speed_factor(self) -> float:
        """Real (turntable) seconds per second of the original recording, from the correction."""
        return 1.0 + self.audd_speed_percent / 100.0

    def _passage_factor(self, passage: Passage) -> float:
        """Return the speed factor for AudD's timecode and length here (the hit's sample)."""
        if passage.audd_hit_speed_percent is None:
            return self._speed_factor
        return 1.0 + passage.audd_hit_speed_percent / 100.0

    def _identify(  # noqa: PLR0913 - timing is optional and keyword-only
        self,
        passage: Passage,
        track_id: int,
        metadata: dict[str, Any],
        *,
        via: str,
        song_start_at: float | None = None,
        song_start_exact: bool = False,
        duration: Any = None,
    ) -> None:
        """Mark the passage identified, report metadata and start learning."""
        self.logger.info(
            "Identified %s via %s after %.0fs (passage %s, AudD calls %d)",
            describe(metadata),
            via,
            passage.seconds,
            passage.short_id,
            passage.audd_attempts,
        )
        if self.pending_length is not None:
            if (
                self.pending_length[0] == track_id
                and passage.after_gap
                and passage.opened_at - self.last_close_at <= CONTINUATION_MAX_RESUME_SECONDS
            ):
                self.logger.info(
                    "The gap before passage %s was a quiet moment inside %s: no length observed",
                    passage.short_id,
                    describe(metadata),
                )
                self.pending_length = None
            else:
                self._commit_pending_length()
        passage.track_id = track_id
        passage.metadata = metadata
        passage.confirmer.reset()
        passage.next_learn_at = passage.seconds + LEARN_INTERVAL_SECONDS
        if via == "local" and not _positive(duration) and self._audd is not None:
            passage.duration_checks_left = MAX_DURATION_CHECKS_PER_PASSAGE
        self._set_track_timing(passage, song_start_at, duration)
        passage.song_start_exact = song_start_exact and song_start_at is not None
        if not passage.song_start_exact:
            passage.position_lookups_left = LATER_POSITION_LOOKUPS  # keep the evidence
        else:
            passage.evidence.clear()  # not needed once identified
        if self._publish(self._report(passage, metadata)):
            self.shown_passage_id = passage.id
            self.logger.info("Reported stream metadata: %s", describe(metadata))
        else:
            # MA declined: the source has metadata of its own, or the session is gone.
            self.logger.info("Music Assistant declined our metadata; recognition stopped")
            self.stopped = True

    def _clear_stale_track(self, passage: Passage) -> None:
        """Stop showing an earlier passage's track once this passage is clearly something else."""
        if self.shown_passage_id is None or self.shown_passage_id == passage.id:
            return
        self._withdraw_shown_track(f"passage {passage.short_id} is not identified yet")

    def _withdraw_shown_track(self, reason: str) -> None:
        """Report the source placeholder instead of the track shown so far."""
        self.shown_passage_id = None
        if self._publish({PLACEHOLDER: True}):
            self.logger.info("No longer showing the previous track: %s", reason)
        else:
            self.logger.info("Music Assistant declined our metadata; recognition stopped")
            self.stopped = True

    def _in_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run DB bookkeeping for an already closed passage without blocking the stream."""
        task = self._spawn(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def _settle(self, passage: Passage) -> None:
        """
        Store where the song started in this play and correct the play it followed.

        Only with an exact song start (AudD timecode or a stored start of the matched play).
        On a gapless record the previous track ended where this song started: its windows
        past that moment are trimmed and its length is observed.
        """
        if (
            passage.settled
            or passage.track_id is None
            or passage.song_start_at is None
            or not passage.song_start_exact
        ):
            return
        passage.settled = True
        try:
            await self._run_blocking(
                self._db.set_song_start, passage.track_id, passage.id, passage.song_start_at
            )
            previous = passage.follows
            if previous is None:
                return
            passage.follows = None
            real_end = previous.closed_at + passage.song_start_at
            if passage.song_start_at < 0:
                # The previous play's windows past its real end hold this song: move them here.
                moved, removed = await self._run_blocking(
                    self._db.move_passage_tail,
                    previous.track_id,
                    previous.passage_id,
                    real_end,
                    float(SAMPLE_SECONDS),
                    (passage.track_id, passage.id, previous.closed_at),
                )
                self.logger.info(
                    "%s started %.0fs before the boundary: of the previous track's windows "
                    "past its real end, moved %d to this song and removed %d that held both "
                    "(passage %s)",
                    describe(passage.metadata),
                    -passage.song_start_at,
                    moved,
                    removed,
                    previous.passage_id[:8],
                )
            if previous.song_start_at is not None and previous.song_start_exact:
                await self._observe_length(previous.track_id, real_end - previous.song_start_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Could not correct track timing (passage %s)", passage.short_id)

    def _commit_pending_length(self) -> None:
        """Record the length measured at the last gap (it was a real end, not a quiet moment)."""
        if self.pending_length is not None:
            track_id, seconds = self.pending_length
            self.pending_length = None
            self._in_background(self._observe_length(track_id, seconds))

    async def _observe_length(self, track_id: int, played_seconds: float) -> None:
        """
        Record a length measured on this record, in turntable seconds as measured.

        Not scaled by the AudD speed correction (until 2026-11-20 it was divided by it, so one
        track measured at -3 % and at -2 % gave lengths ~1 % apart that were never adopted).
        """
        seconds = played_seconds
        if seconds <= SAMPLE_SECONDS:
            return
        try:
            adopted = await self._run_blocking(
                self._db.record_observed_duration,
                track_id,
                seconds,
                OBSERVED_LENGTH_TOLERANCE_SECONDS,
            )
        except Exception:
            self.logger.exception("Could not record an observed length (track %d)", track_id)
            return
        if adopted is not None:
            self.logger.info("Track %d: observed length %.0fs adopted", track_id, adopted)
        else:
            self.logger.info("Track %d: observed length %.0fs noted", track_id, seconds)

    @staticmethod
    def _report(passage: Passage, metadata: dict[str, Any]) -> dict[str, Any]:
        """
        Return what to show in MA: the track's metadata plus, when known, the playback position.

        ``elapsed_seconds`` is the position in the song now (passage time since the song
        started), ``playback_duration_seconds`` the track length as it plays on this turntable
        (scaled by the speed correction, like the end guard). The stored metadata is not changed.
        """
        report = dict(metadata)
        if passage.song_start_at is not None:
            report["elapsed_seconds"] = max(0.0, passage.seconds - passage.song_start_at)
            if passage.track_end_at is not None:
                report["playback_duration_seconds"] = passage.track_end_at - passage.song_start_at
        return report

    def _set_track_timing(
        self, passage: Passage, song_start_at: float | None, duration: Any
    ) -> None:
        """
        Remember where the song started and should end in passage time (the gapless guard).

        A local match's time shift is relative to the reference passage, which is assumed to
        start with the song (true for records with gaps; a few seconds off otherwise).
        """
        if song_start_at is None:
            return
        passage.song_start_at = song_start_at
        if isinstance(duration, bool) or not isinstance(duration, int | float) or duration <= 0:
            self.logger.debug(
                "No track duration known: no end guard (passage %s)", passage.short_id
            )
            return
        metadata = passage.metadata or {}
        measured = metadata.get(DURATION_SOURCE) == "observed" and duration == metadata.get(
            "duration_seconds"
        )
        # AudD's length is in the recording's time; a length measured here is already turntable time
        end_at = song_start_at + duration * (1.0 if measured else self._passage_factor(passage))
        if passage.seconds > end_at + TRACK_END_GUARD_SECONDS:
            # The track already played longer than its duration says (another version or edit).
            self.logger.info(
                "%s is already playing longer than its known duration (%.0fs): "
                "no end guard for passage %s",
                describe(passage.metadata),
                duration,
                passage.short_id,
            )
            return
        passage.track_end_at = end_at
        self.logger.info(
            "Expected end of %s at %.0fs (song started at %.0fs, duration %.0fs) (passage %s)",
            describe(passage.metadata),
            end_at,
            song_start_at,
            duration,
            passage.short_id,
        )

    def _within_song(self, passage: Passage, window_end: float) -> bool:
        """Return whether a window lies within the identified song (with the tolerances)."""
        if (
            passage.song_start_at is not None
            and window_end - SAMPLE_SECONDS < passage.song_start_at - SONG_START_TOLERANCE_SECONDS
        ):
            return False
        return (
            passage.track_end_at is None
            or window_end <= passage.track_end_at - self._learn_guard(passage)
        )

    @staticmethod
    def _learn_limit(passage: Passage) -> int:
        """Seconds of a passage to learn at most: longer when the end guard knows the end."""
        if passage.track_end_at is not None and passage.song_start_exact:
            return MAX_LEARN_SECONDS_KNOWN_END
        return MAX_LEARN_SECONDS_PER_PASSAGE

    def _learn_guard(self, passage: Passage) -> int:
        """Seconds before the expected end at which learning stops."""
        measured = (passage.metadata or {}).get(DURATION_SOURCE) == "observed"
        if self.soft_boundaries and not measured:
            return UNCERTAIN_END_GUARD_SECONDS
        return TRACK_END_GUARD_SECONDS

    # ---------------------------------------------------------------- learning

    async def _learn(self, passage: Passage, pcm: bytes, window_end: float) -> None:
        try:
            hashes = await self._fingerprint(pcm, self._divisor(window_end))
            await self._store_reference(passage, hashes, window_end, self._holes(pcm))
            if passage.position_lookups_left > 0 and self._is_current(passage):
                await self._decide_position(passage, hashes, window_end)
            if self._duration_check_due(passage, window_end):
                await self._check_duration(passage, pcm, window_end)
            elif self._title_check_due(passage):
                await self._check_title(passage, pcm, window_end)
            elif self._end_check_due(passage, window_end):
                await self._check_end(passage, pcm, window_end)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Could not learn window (passage %s)", passage.short_id)

    async def _decide_position(
        self, passage: Passage, query: Fingerprints, window_end: float
    ) -> None:
        """Add a learned window to the position evidence; adopt the song start once decisive."""
        passage.position_lookups_left -= 1
        local = await self._run_blocking(self._db.lookup, query, passage.id)
        if not self._is_current(passage) or passage.track_id is None:
            return
        passage.evidence.add(query, local.rows, window_end - SAMPLE_SECONDS)
        metadata = passage.metadata or {}
        starts = metadata.get(SONG_STARTS) or {}
        position = passage.evidence.song_start(passage.track_id, starts)
        if passage.continuation_start is not None:
            support = passage.evidence.votes_near(
                passage.track_id,
                starts,
                passage.continuation_start,
                CONTINUATION_TOLERANCE_SECONDS,
            )
            if support >= CONTINUATION_CONFIRM_VOTES:
                position = SongPosition(passage.continuation_start, support, 0, 0)
                self.logger.info(
                    "Position of %s confirmed as the continuation of the play before the gap "
                    "(%d votes) (passage %s)",
                    describe(metadata),
                    support,
                    passage.short_id,
                )
            elif support > 0 or passage.evidence.lookups < CONTINUATION_PATIENCE_LOOKUPS:
                position = None  # wait: a loop elsewhere in the song must not win this early
        if position is None or (not position.decisive and position.lookups):
            if passage.position_lookups_left == 0:
                passage.evidence.clear()
                self.logger.info(
                    "Position of %s still undecided after %d lookups%s (passage %s)",
                    describe(metadata),
                    LATER_POSITION_LOOKUPS,
                    ""
                    if position is None
                    else f" (best {position.votes} votes, runner-up {position.rival_votes})",
                    passage.short_id,
                )
            return
        passage.position_lookups_left = 0
        passage.evidence.clear()
        if position.lookups:  # a confirmed continuation was logged above
            self.logger.info(
                "Position of %s decided later: song started at %.0fs (%d votes from %d lookups, "
                "runner-up %d) (passage %s)",
                describe(metadata),
                position.start_seconds,
                position.votes,
                position.lookups,
                position.rival_votes,
                passage.short_id,
            )
        passage.track_end_at = None
        self._set_track_timing(passage, position.start_seconds, metadata.get("duration_seconds"))
        passage.song_start_exact = True
        passage.settled = False
        await self._settle(passage)
        if not self._is_current(passage):
            return
        if not self._publish(self._report(passage, metadata)):
            self.logger.info("Music Assistant declined our metadata; recognition stopped")
            self.stopped = True

    def _duration_check_due(self, passage: Passage, window_end: float) -> bool:
        return (
            passage.duration_checks_left > 0
            and window_end >= AUDD_FIRST_ATTEMPT_SECONDS
            and self._is_current(passage)
            and self._audd_wait_reason(passage) is None
        )

    def _title_check_due(self, passage: Passage) -> bool:
        return (
            passage.title_checks_left > 0
            and self._is_current(passage)
            and self._audd_wait_reason(passage) is None
        )

    def _end_check_due(self, passage: Passage, window_end: float) -> bool:
        return (
            passage.end_check_at is not None
            and window_end >= passage.end_check_at
            and self._is_current(passage)
            and self._audd_wait_reason(passage) is None
        )

    async def _check_end(  # noqa: PLR0911 - each reason to stop returns early
        self, passage: Passage, pcm: bytes, window_end: float
    ) -> None:
        """
        Ask AudD whether the song ended where its first, distrusted timecode said.

        Another title that started after the passage began: the first timecode was right (a
        needle drop). The old play gets that song start, the passage closes at the new song's
        start (a soft boundary) and the new passage starts identified; its windows learned
        under the old name move to the new song, as after any soft boundary.
        """
        if self._wait_for_clean_sample(passage, pcm):
            return  # the check stays due for a cleaner window
        passage.end_check_at = None
        old_id, old_metadata, doubted = passage.track_id, passage.metadata, passage.doubted_start
        if old_id is None or old_metadata is None or doubted is None:
            return
        self.logger.info(
            "Asking AudD whether %s has ended (passage %s)",
            describe(old_metadata),
            passage.short_id,
        )
        hit = await self._try_audd(
            passage, pcm, window_end, speed=passage.audd_hit_speed_percent, holes_checked=True
        )
        if hit is None or not self._is_current(passage) or passage.track_id != old_id:
            return
        if track_key(hit) in {track_key(old_metadata), *old_metadata.get(ALIASES, [])}:
            self.logger.info(
                "AudD still names %s: its first timecode was another edit's (passage %s)",
                describe(old_metadata),
                passage.short_id,
            )
            return
        timecode = parse_timecode(hit.get("audd_timecode"))
        new_start = (
            None if timecode is None else window_end - timecode * self._passage_factor(passage)
        )
        if new_start is None or not 0 < new_start < window_end:
            self.logger.info(
                "AudD now names %s, but not at a plausible position: keeping %s (passage %s)",
                describe(hit),
                describe(old_metadata),
                passage.short_id,
            )
            return
        new_id = await self._run_blocking(self._db.upsert_track, hit)
        metadata = await self._run_blocking(self._db.track_metadata, new_id) or hit
        if not self._is_current(passage) or passage.track_id != old_id or new_id == old_id:
            return
        self.logger.info(
            "AudD names %s from %.0fs: %s was a needle drop and ended there (passage %s)",
            describe(metadata),
            new_start,
            describe(old_metadata),
            passage.short_id,
        )
        # the first timecode was right: this play started there
        passage.song_start_at, passage.song_start_exact = doubted, True
        passage.track_end_at = new_start
        await self._settle(passage)
        if not self._is_current(passage):
            return
        closed_at = passage.seconds
        self._end_at_track_end(passage)
        following = self.passage
        if following is None:
            return
        self._identify(
            following,
            new_id,
            metadata,
            via="AudD (end check)",
            song_start_at=new_start - closed_at,
            song_start_exact=True,
            duration=_track_length(metadata, hit),
        )
        await self._settle(following)

    async def _check_title(self, passage: Passage, pcm: bytes, window_end: float) -> None:
        """Ask AudD once more about a title that came with an implausible timecode."""
        if self._wait_for_clean_sample(passage, pcm):
            return  # the check stays due for a cleaner window
        passage.title_checks_left -= 1
        old_id, old_metadata = passage.track_id, passage.metadata
        if old_id is None or old_metadata is None:
            return
        self.logger.info(
            "Checking the title %s with AudD once more (passage %s)",
            describe(old_metadata),
            passage.short_id,
        )
        hit = await self._try_audd(
            passage, pcm, window_end, speed=passage.audd_hit_speed_percent, holes_checked=True
        )
        if hit is None or not self._is_current(passage) or passage.track_id != old_id:
            return
        if track_key(hit) in {track_key(old_metadata), *old_metadata.get(ALIASES, [])}:
            self.logger.info(
                "AudD confirms %s (passage %s)", describe(old_metadata), passage.short_id
            )
            return
        factor = self._passage_factor(passage)
        timecode = parse_timecode(hit.get("audd_timecode"))
        start = None if timecode is None else window_end - timecode * factor
        if start is None or start < -LOCAL_START_PRIOR_SECONDS:
            self.logger.info(
                "AudD now names it %s, but not at a plausible position either: keeping %s "
                "(passage %s)",
                describe(hit),
                describe(old_metadata),
                passage.short_id,
            )
            return
        new_id = await self._run_blocking(self._db.upsert_track, hit)
        metadata = await self._run_blocking(self._db.track_metadata, new_id) or hit
        if not self._is_current(passage) or passage.track_id != old_id or new_id == old_id:
            return
        moved = await self._run_blocking(self._db.reassign_passage, old_id, passage.id, new_id)
        self.logger.info(
            "AudD names it %s with a plausible position (song started at %.0fs): not %s; "
            "moved %d learned windows (passage %s)",
            describe(metadata),
            start,
            describe(old_metadata),
            moved,
            passage.short_id,
        )
        passage.track_end_at = None
        passage.settled = False
        passage.learning_capped = False
        self._identify(
            passage,
            new_id,
            metadata,
            via="AudD (title checked)",
            song_start_at=start,
            song_start_exact=True,
            duration=_track_length(metadata, hit),
        )
        await self._settle(passage)

    async def _check_duration(  # noqa: PLR0911 - each reason to stop returns early
        self, passage: Passage, pcm: bytes, window_end: float
    ) -> None:
        """Ask AudD once for the length of a locally recognized track that has none stored."""
        if self._wait_for_clean_sample(passage, pcm):
            return  # the check stays due for a cleaner window
        passage.duration_checks_left -= 1
        self.logger.info(
            "Asking AudD for the length of %s (recognized locally) (passage %s)",
            describe(passage.metadata),
            passage.short_id,
        )
        hit = await self._try_audd(passage, pcm, window_end, holes_checked=True)
        if hit is None:
            # The track is known already: retry soon, not at the slow cadence of later attempts
            # to identify an unknown passage (the stream-wide minimum interval still applies).
            passage.next_audd_at = min(
                passage.next_audd_at, passage.seconds + AUDD_FAST_INTERVAL_SECONDS
            )
            return
        if not self._is_current(passage) or passage.metadata is None:
            return
        passage.duration_checks_left = 0  # AudD answered: no further checks this passage
        known = {track_key(passage.metadata), *passage.metadata.get(ALIASES, [])}
        duration = hit.get("duration_seconds")
        if track_key(hit) not in known:
            self.logger.info(
                "AudD names it %s, not %s: keeping the local recognition, length not stored "
                "(passage %s)",
                describe(hit),
                describe(passage.metadata),
                passage.short_id,
            )
            return
        if not _positive(duration):
            self.logger.info("AudD reports no length for %s", describe(passage.metadata))
            return
        track_id = passage.track_id
        if track_id is None:
            return
        await self._run_blocking(self._db.upsert_track, hit)  # fills only empty fields
        metadata = await self._run_blocking(self._db.track_metadata, track_id)
        if metadata is None or not self._is_current(passage):
            return
        passage.metadata = metadata
        self.logger.info(
            "Learned the length of %s via AudD: %.0fs (passage %s)",
            describe(metadata),
            duration,
            passage.short_id,
        )
        hit_start, exact = self._audd_song_start(
            passage,
            track_id,
            metadata,
            window_end,
            parse_timecode(hit.get("audd_timecode")),
            duration=_track_length(metadata, hit),
        )
        song_start_at = hit_start if exact else passage.song_start_at
        passage.track_end_at = None
        self._set_track_timing(passage, song_start_at, _track_length(metadata, hit))
        if exact:
            passage.song_start_exact = True
            passage.settled = False
            await self._settle(passage)
        if not self._publish(self._report(passage, metadata)):
            self.logger.info("Music Assistant declined our metadata; recognition stopped")
            self.stopped = True

    def _storable(self, passage: Passage, hashes: Fingerprints) -> bool:
        """Return False (and log) for a window too dense to be real music."""
        landmarks = sum(len(positions) for positions in hashes.values())
        if landmarks <= MAX_LANDMARKS_PER_REFERENCE:
            return True
        self.logger.warning(
            "Not learning a window with %d landmarks (max %d; abnormal input?) (passage %s)",
            landmarks,
            MAX_LANDMARKS_PER_REFERENCE,
            passage.short_id,
        )
        return False

    async def _store_early_windows(self, passage: Passage) -> None:
        """Store the lookup windows of a just-identified passage as references."""
        windows, passage.early_windows = passage.early_windows, []
        holes, passage.early_holes = passage.early_holes, {}
        within = [(end, hashes) for end, hashes in windows if self._within_song(passage, end)]
        if len(within) < len(windows):
            self.logger.info(
                "Not learning %d early windows outside %s (passage %s)",
                len(windows) - len(within),
                describe(passage.metadata),
                passage.short_id,
            )
        windows = [(end, hashes) for end, hashes in within if self._storable(passage, hashes)]
        if not windows or not self._is_current(passage) or passage.track_id is None:
            return
        results = await self._run_blocking(
            self._db.add_references,
            passage.track_id,
            passage.id,
            [(max(0.0, end - SAMPLE_SECONDS), hashes) for end, hashes in windows],
            [holes.get(end) for end, _ in windows],
        )
        stored = [(ref, count) for ref, count in results if ref]
        if stored:
            self.logger.info(
                "Learned %d early references: %s passage=%s refs=%d-%d landmarks=%d "
                "covering %.0f-%.0fs",
                len(stored),
                describe(passage.metadata),
                passage.short_id,
                stored[0][0],
                stored[-1][0],
                sum(count for _, count in stored),
                max(0.0, windows[0][0] - SAMPLE_SECONDS),
                windows[-1][0],
            )
        await self._prune(passage)

    def _holes(self, pcm: bytes) -> int:
        """Return the stream holes (lost audio) in a window, as stored with its reference."""
        return stream_holes(pcm, self.fmt, AUDD_HOLE_MIN_SECONDS)[0]

    async def _store_reference(
        self, passage: Passage, hashes: Fingerprints, window_end: float, holes: int | None = None
    ) -> None:
        if not self._is_current(passage) or passage.track_id is None:
            return
        if not self._storable(passage, hashes):
            return
        reference_id, count = await self._run_blocking(
            self._db.add_reference,
            passage.track_id,
            passage.id,
            max(0.0, window_end - SAMPLE_SECONDS),
            hashes,
            holes,
        )
        if reference_id:
            self.logger.info(
                "Learned local reference: %s passage=%s ref=%d landmarks=%d at=%.0fs",
                describe(passage.metadata),
                passage.short_id,
                reference_id,
                count,
                window_end - SAMPLE_SECONDS,
            )
            await self._prune(passage)

    async def _prune(self, passage: Passage) -> None:
        """Keep MAX_PLAYS_PER_TRACK plays per part of the track, dropping covered older plays."""
        if not self._is_current(passage) or passage.track_id is None:
            return
        removed = await self._run_blocking(
            self._db.prune_track,
            passage.track_id,
            passage.id,
            MAX_PLAYS_PER_TRACK,
            float(LEARN_INTERVAL_SECONDS),
            MIN_UNIQUE_COVERAGE_SECONDS,
        )
        if removed:
            self.logger.info(
                "Removed %d references of older plays of %s (keeping %d plays per part of it)",
                removed,
                describe(passage.metadata),
                MAX_PLAYS_PER_TRACK,
            )
