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
    pcm_dbfs,
    pcm_to_wav,
    sample_stats,
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
# the current identity on records without audible gaps between tracks.
MAX_LEARN_SECONDS_PER_PASSAGE = 600
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
OBSERVED_LENGTH_TOLERANCE_SECONDS = 3.0
DROPOUT_DBFS = SILENCE_FLOOR_DBFS + 1
# Stream dropouts: the Sendspin input sometimes delivers ~2 s of exact digital zeros in the middle
# of a track (5x on 2026-11-19 morning). A turntable never produces exact zeros (the needle and
# preamp hiss sit around -55..-80 dBFS), so such a run does not count as a gap: the passage
# continues. Only a longer run (the source stopped sending audio) becomes a gap.
DROPOUT_MAX_SECONDS = 10.0
# Published instead of a track when the shown track has clearly ended: a new passage plays and its
# first lookup did not identify it (user's wish 2026-11-19; during a gap the last track stays).
# The provider reports the source's own placeholder metadata for it.
PLACEHOLDER = "placeholder"
# A track recognized locally without a stored length gets one extra AudD request (from
# AUDD_FIRST_ATTEMPT_SECONDS into the passage, at most this many per passage) just to learn its
# length and the song position (user's choice, 2026-11-19: the extra requests are fine). Only a
# hit with the same track key (or alias) is used; another name is ignored (local wins).
MAX_DURATION_CHECKS_PER_PASSAGE = 2
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
# Stream-wide exponential backoff, only for real HTTP/network/API failures.
# When AudD is due but the latest local lookup already points at a known track (a candidate with
# >= POSTPONE_MIN_VOTES), wait one more lookup for local confirmation instead of spending an AudD
# call. At most MAX_AUDD_POSTPONES times per passage. Offline on real vinyl (Barry White replay):
# genuine candidates at 24 s had 3 votes, accidental ones up to 2, so 2 postponed for nothing.
POSTPONE_MIN_VOTES = 3
MAX_AUDD_POSTPONES = 2
# "Fast start": windows that end within the first fast_start_seconds of a passage are
# fingerprinted at 1/4 instead of 1/16 (a superset), when learning and when looking up, so a
# known track is recognized sooner from its start. Costs disk space; off unless configured.
FAST_START_DENSITY_DIVISOR = 4
AUDD_INITIAL_BACKOFF_SECONDS = 60
AUDD_MAX_BACKOFF_SECONDS = 900

# Local match acceptance. A strong candidate identifies at once; a credible one must be the
# same track on consecutive attempts. Random collisions give 0-1 votes.
STRONG_VOTES = 5
STRONG_RATIO = 0.60
CONFIRM_VOTES = 3
CONFIRM_RATIO = 0.60
CONFIRMATIONS_REQUIRED = 2
# Time-consistent evidence: on a new play of a record only 2-4 votes per window repeat (vinyl
# differs a little every play), so the votes of every lookup of a passage add up per (track,
# reference passage), but only those at the same absolute time shift between this passage and the
# earlier play. Genuine replays stay within +-0.15 s over minutes; accidental hits fall at random
# shifts. Offline on three real sessions (2026-11-18): genuine 6-21 votes after 18-24 s, accidental
# at most 3 over whole 4-5 minute passages (30-51 lookups). A hash counts once per cluster.
COHERENT_VOTES = 6
COHERENT_LOOKUPS = 2  # from at least this many lookups (a single window is judged above)
COHERENCE_BIN_SECONDS = 0.05
COHERENCE_BINS = 3  # a cluster spans +-3 bins: +-0.15 s (+ quantization)

# Keep the references of at most this many plays per track (the current one included), so a
# 5-minute track keeps as many plays as a 3-minute one (a reference cap of 60 left long tracks
# with a single play). Bounded by MAX_LEARN_SECONDS_PER_PASSAGE: ~1 MB per 3-minute track.
MAX_PLAYS_PER_TRACK = 2
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

    def add(
        self,
        query: Fingerprints,
        rows: list[MatchRow],
        query_start_seconds: float,
        *,
        new_lookup: bool = True,
    ) -> None:
        """Add one lookup (or, with new_lookup=False, another phase of the same lookup)."""
        if new_lookup or self.lookups == 0:
            self.lookups += 1
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

    def best(self, track_id: int | None = None) -> Coherence | None:
        """Return the strongest cluster (of one track if given), or None."""
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
                )
                if best is None or (candidate.votes, candidate.lookups) > (
                    best.votes,
                    best.lookups,
                ):
                    best = candidate
        return best

    def clear(self) -> None:
        """Drop all evidence (after an identification)."""
        self._bins.clear()


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
    evidence: TimelineEvidence = field(default_factory=TimelineEvidence)
    # passage time at which the identified song started (may be negative) and should end
    song_start_at: float | None = None
    track_end_at: float | None = None
    duration_checks_left: int = 0
    song_start_exact: bool = False  # song_start_at from an AudD timecode or a stored song start
    follows: ClosedPlay | None = None  # the identified play this passage followed without a gap
    settled: bool = False  # song start stored and the previous play corrected

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
            self._open_passage(f"audio resumed at {level:.1f} dBFS")
        elif (passage := self.passage) is not None and self._past_track_end(passage):
            self._end_at_track_end(passage)
        if self.passage is None:
            return  # inside a gap: no buffering, fingerprinting, matching or AudD
        self.passage.append(chunk, chunk_seconds)
        self._schedule(self.passage)

    def _gap_event(self, level: float, chunk_seconds: float) -> GapEvent | None:
        """Update the gap detector, treating a short run of digital zeros as a dropout."""
        if level <= DROPOUT_DBFS and not self.gap.in_gap:
            self.dropout_seconds += chunk_seconds
            if self.dropout_seconds < DROPOUT_MAX_SECONDS:
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
        self.stopped = True
        self.passage = None
        if self.task is not None and not self.task.done():
            self.task.cancel()

    # ---------------------------------------------------------------- passages

    def _open_passage(self, reason: str) -> Passage:
        passage = Passage(
            window_bytes=self.window_bytes, evidence=TimelineEvidence(self.frame_seconds)
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
            self._in_background(
                self._observe_length(passage.track_id, song_end - passage.song_start_at)
            )

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
            if passage.seconds > MAX_LEARN_SECONDS_PER_PASSAGE:
                passage.learning_capped = True
                self.logger.info(
                    "Passage %s: stopped learning after %ds",
                    passage.short_id,
                    MAX_LEARN_SECONDS_PER_PASSAGE,
                )
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
            local = await self._run_blocking(self._db.lookup, query, passage.id)
            if not self._is_current(passage):
                return
            passage.evidence.add(query, local.rows, window_end - SAMPLE_SECONDS)
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
            passage.evidence.add(query, found.rows, start, new_lookup=False)
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
        # Timing comes from this hit: the timecode is where this sample ended in the song.
        timecode = parse_timecode(hit.get("audd_timecode"))
        song_start_at = window_end - timecode * self._speed_factor if timecode is not None else None
        self.logger.info(
            "AudD HIT: %s (passage %s, attempt %d)",
            describe(metadata),
            passage.short_id,
            passage.audd_attempts,
        )
        self._identify(
            passage,
            track_id,
            metadata,
            via="AudD",
            song_start_at=song_start_at,
            song_start_exact=song_start_at is not None,
            duration=hit.get("duration_seconds") or metadata.get("duration_seconds"),
        )
        await self._settle(passage)
        # Learn every lookup window of this passage, including the one AudD identified.
        if not passage.early_windows or passage.early_windows[-1][0] != window_end:
            passage.early_windows.append((window_end, query))
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
                start, exact = _local_song_start(metadata, coherence)
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

    async def _judge_timeline(self, passage: Passage) -> bool:
        """Accept the strongest time-consistent cluster once it has enough evidence."""
        coherence = passage.evidence.best()
        if (
            coherence is None
            or coherence.votes < COHERENT_VOTES
            or coherence.lookups < COHERENT_LOOKUPS
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
        start, exact = _local_song_start(metadata, coherence)
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
        if latest is None or latest[1] < POSTPONE_MIN_VOTES:
            return False  # no promising candidate
        if self._audd_wait_reason(passage) is not None:
            return False  # AudD isn't due anyway
        passage.audd_postponed += 1
        coherence = passage.evidence.best(latest[0])
        votes = coherence.votes if coherence else latest[1]
        self.logger.info(
            "AudD postponed (%d/%d): local candidate track %d has %d votes so far (passage %s)",
            passage.audd_postponed,
            MAX_AUDD_POSTPONES,
            latest[0],
            votes,
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

    async def _try_audd(
        self, passage: Passage, pcm: bytes, window_end: float
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
        passage.audd_attempts += 1
        fast = self.audd_thorough or passage.audd_attempts < AUDD_FAST_ATTEMPTS
        passage.next_audd_at = passage.seconds + (
            AUDD_FAST_INTERVAL_SECONDS if fast else AUDD_SLOW_INTERVAL_SECONDS
        )
        self.last_audd_at = self.stream_seconds
        self.logger.info(
            "AudD attempt %d/%d on %ds sample (passage %s)",
            passage.audd_attempts,
            self._audd_max_attempts,
            SAMPLE_SECONDS,
            passage.short_id,
        )
        wav = pcm_to_wav(pcm, self.fmt, self.audd_speed_percent)
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
        return result

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
                "speed_correction_percent": self.audd_speed_percent,
                "source_format": f"{self.fmt.sample_rate}Hz/{self.fmt.channels}ch/"
                f"{self.fmt.content_type}",
                **sample_stats(pcm, self.fmt),
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
        passage.track_id = track_id
        passage.metadata = metadata
        passage.confirmer.reset()
        passage.evidence.clear()  # not needed once identified
        passage.next_learn_at = passage.seconds + LEARN_INTERVAL_SECONDS
        if via == "local" and not _positive(duration) and self._audd is not None:
            passage.duration_checks_left = MAX_DURATION_CHECKS_PER_PASSAGE
        self._set_track_timing(passage, song_start_at, duration)
        passage.song_start_exact = song_start_exact and song_start_at is not None
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
        self.shown_passage_id = None
        if self._publish({PLACEHOLDER: True}):
            self.logger.info(
                "No longer showing the previous track: passage %s is not identified yet",
                passage.short_id,
            )
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
                trimmed = await self._run_blocking(
                    self._db.trim_passage,
                    previous.track_id,
                    previous.passage_id,
                    real_end,
                    float(SAMPLE_SECONDS),
                )
                self.logger.info(
                    "%s started %.0fs before the boundary: removed %d windows of the previous "
                    "track learned past its real end (passage %s)",
                    describe(passage.metadata),
                    -passage.song_start_at,
                    trimmed,
                    previous.passage_id[:8],
                )
            if previous.song_start_at is not None and previous.song_start_exact:
                await self._observe_length(previous.track_id, real_end - previous.song_start_at)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Could not correct track timing (passage %s)", passage.short_id)

    async def _observe_length(self, track_id: int, played_seconds: float) -> None:
        """Record a length measured on this record (turntable seconds -> recording seconds)."""
        seconds = played_seconds / self._speed_factor
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
        end_at = song_start_at + duration * self._speed_factor
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
            await self._store_reference(passage, hashes, window_end)
            if self._duration_check_due(passage, window_end):
                await self._check_duration(passage, pcm, window_end)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Could not learn window (passage %s)", passage.short_id)

    def _duration_check_due(self, passage: Passage, window_end: float) -> bool:
        return (
            passage.duration_checks_left > 0
            and window_end >= AUDD_FIRST_ATTEMPT_SECONDS
            and self._is_current(passage)
            and self._audd_wait_reason(passage) is None
        )

    async def _check_duration(self, passage: Passage, pcm: bytes, window_end: float) -> None:
        """Ask AudD once for the length of a locally recognized track that has none stored."""
        passage.duration_checks_left -= 1
        self.logger.info(
            "Asking AudD for the length of %s (recognized locally) (passage %s)",
            describe(passage.metadata),
            passage.short_id,
        )
        hit = await self._try_audd(passage, pcm, window_end)
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
        timecode = parse_timecode(hit.get("audd_timecode"))
        song_start_at = (
            window_end - timecode * self._speed_factor
            if timecode is not None
            else passage.song_start_at
        )
        self._set_track_timing(passage, song_start_at, duration)
        if timecode is not None:
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

    async def _store_reference(
        self, passage: Passage, hashes: Fingerprints, window_end: float
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
        """Keep the track within MAX_PLAYS_PER_TRACK plays, dropping the oldest."""
        if not self._is_current(passage) or passage.track_id is None:
            return
        removed = await self._run_blocking(
            self._db.prune_track, passage.track_id, passage.id, MAX_PLAYS_PER_TRACK
        )
        if removed:
            self.logger.info(
                "Removed %d references of older plays of %s (keeping %d plays per track)",
                removed,
                describe(passage.metadata),
                MAX_PLAYS_PER_TRACK,
            )
