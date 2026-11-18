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
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

from .audd_client import AudDError, AudDNoMatch
from .fingerprints import (
    DENSITY_DIVISOR,
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

    from .database import FingerprintDatabase

# Window and cadence (seconds of passage audio).
SAMPLE_SECONDS = 12
LOCAL_INTERVAL_SECONDS = 6
LEARN_INTERVAL_SECONDS = 6  # 12 s windows every 6 s: overlap is intentional (replay coverage)
# Stop learning a passage after this long. Guards against learning the next track under
# the current identity on records without audible gaps between tracks.
MAX_LEARN_SECONDS_PER_PASSAGE = 600
# Lookup fingerprints of a still-unknown passage are kept (at most this many windows, i.e.
# the first ~2 minutes) and stored as references once the passage is identified, so the
# start of a track is learned too even when identification only succeeds later.
MAX_EARLY_WINDOWS = 20

# AudD cadence per unknown passage. The first attempt waits for 24 s of audio: in real vinyl
# tests AudD missed the first 12 s (intro) of every track (6/6) and matched 30-42 s, while
# local lookups still start at 12 s. Then 30 s between attempts, later 90 s, capped.
AUDD_FIRST_ATTEMPT_SECONDS = 24
AUDD_FAST_ATTEMPTS = 3
AUDD_FAST_INTERVAL_SECONDS = 30
AUDD_SLOW_INTERVAL_SECONDS = 90
AUDD_MAX_ATTEMPTS_PER_PASSAGE = 8
# Stream-wide: never two AudD calls closer than this, whatever the passages do.
AUDD_MIN_INTERVAL_SECONDS = 30
# Stream-wide exponential backoff, only for real HTTP/network/API failures.
# When AudD is due but the latest local lookup already points at a known track (a candidate with
# >= ACCUMULATION_MIN_VOTES; accidental candidates have 1), wait one more lookup for local
# confirmation instead of spending an AudD call. At most this many times per passage.
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
# Accumulated evidence: on a new play of a record only 2-4 votes per window repeat (vinyl differs
# a little every play), so weak but genuine windows add up per track over the last few lookups.
# Accidental collisions gave at most 1 vote in every real test, so they never count.
ACCUMULATION_LOOKUPS = 4  # ~24 s of lookups
ACCUMULATION_MIN_VOTES = 2  # a window counts from this many votes (and CONFIRM_RATIO)
ACCUMULATED_VOTES = 6  # total needed ...
ACCUMULATED_WINDOWS = 2  # ... from at least this many windows

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
    ACCEPT_ACCUMULATED = "accepted (accumulated)"
    PENDING = "pending confirmation"
    REJECT = "rejected"

    @property
    def accepted(self) -> bool:
        """Return whether this decision identifies the track."""
        return self in (
            Decision.ACCEPT_STRONG,
            Decision.ACCEPT_CONFIRMED,
            Decision.ACCEPT_ACCUMULATED,
        )


@dataclass
class MatchConfirmer:
    """
    Decide whether local candidates identify a track.

    Three ways to accept: one strong window; the same track credible on consecutive lookups;
    or weak-but-genuine windows of the same track adding up over the last few lookups.
    """

    pending_track_id: int | None = None
    pending_count: int = 0
    # (track_id, votes) of recent lookups whose best candidate counts as evidence, None otherwise
    history: deque[tuple[int, int] | None] = field(
        default_factory=lambda: deque(maxlen=ACCUMULATION_LOOKUPS)
    )

    def reset(self) -> None:
        """Forget everything (after an identification)."""
        self.pending_track_id = None
        self.pending_count = 0
        self.history.clear()

    def record_miss(self) -> None:
        """Register a lookup without any candidate: it breaks a consecutive run."""
        self.pending_track_id = None
        self.pending_count = 0
        self.history.append(None)

    def accumulated(self, track_id: int) -> tuple[int, int]:
        """Return (votes, windows) of the recent evidence for a track."""
        votes = [v for entry in self.history if entry and entry[0] == track_id for v in entry[1:]]
        return sum(votes), len(votes)

    def consider(self, candidate: QueryMatch) -> Decision:
        """Judge one candidate."""
        counts = candidate.best_votes >= ACCUMULATION_MIN_VOTES and candidate.ratio >= CONFIRM_RATIO
        self.history.append((candidate.track_id, candidate.best_votes) if counts else None)
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
        else:
            self.pending_track_id = None
            self.pending_count = 0
        if counts:
            votes, windows = self.accumulated(candidate.track_id)
            if votes >= ACCUMULATED_VOTES and windows >= ACCUMULATED_WINDOWS:
                self.reset()
                return Decision.ACCEPT_ACCUMULATED
            return Decision.PENDING
        return Decision.REJECT


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
        postpone_audd: bool = False,
        fast_start_seconds: float = 0.0,
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
        event = self.gap.update(level, chunk_seconds)
        if event is GapEvent.GAP:
            self._close_passage(level)
        elif event is GapEvent.RESUME:
            self._open_passage(f"audio resumed at {level:.1f} dBFS")
        if self.passage is None:
            return  # inside a gap: no buffering, fingerprinting, matching or AudD
        self.passage.append(chunk, chunk_seconds)
        self._schedule(self.passage)

    def close(self) -> None:
        """Stop the stream and cancel in-flight work."""
        self.stopped = True
        self.passage = None
        if self.task is not None and not self.task.done():
            self.task.cancel()

    # ---------------------------------------------------------------- passages

    def _open_passage(self, reason: str) -> None:
        self.passage = Passage(window_bytes=self.window_bytes)
        self.logger.info(
            "Passage %s started (%s); first lookup after %ds of fresh audio",
            self.passage.short_id,
            reason,
            SAMPLE_SECONDS,
        )

    def _close_passage(self, level: float) -> None:
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
            local = await self._run_blocking(self._db.find_best_match, query, passage.id)
            if not self._is_current(passage):
                return
            if self._judge_local(passage, local):
                await self._store_early_windows(passage)
                return
            if not self._postpone_audd(passage):
                await self._recognize_with_audd(passage, pcm, query, window_end)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Recognition failed (passage %s)", passage.short_id)

    async def _recognize_with_audd(
        self, passage: Passage, pcm: bytes, query: Fingerprints, window_end: float
    ) -> None:
        """Ask AudD (if allowed); on a hit identify the passage and learn its windows."""
        metadata = await self._try_audd(passage, pcm, window_end)
        if metadata is None or not self._is_current(passage):
            return
        track_id = await self._run_blocking(self._db.upsert_track, metadata)
        # show what is stored: a known track keeps its first metadata (see upsert_track)
        metadata = await self._run_blocking(self._db.track_metadata, track_id) or metadata
        if not self._is_current(passage):
            return
        self.logger.info(
            "AudD HIT: %s (passage %s, attempt %d)",
            describe(metadata),
            passage.short_id,
            passage.audd_attempts,
        )
        self._identify(passage, track_id, metadata, via="AudD")
        # Learn every lookup window of this passage, including the one AudD identified.
        if not passage.early_windows or passage.early_windows[-1][0] != window_end:
            passage.early_windows.append((window_end, query))
        await self._store_early_windows(passage)

    def _judge_local(
        self, passage: Passage, local: tuple[QueryMatch, dict[str, Any]] | None
    ) -> bool:
        """Apply the confirmation rules; return True if the passage got identified."""
        if local is None:
            passage.confirmer.record_miss()
            self.logger.debug("LOCAL MISS: no candidate (passage %s)", passage.short_id)
            return False
        candidate, metadata = local
        evidence_before, _ = passage.confirmer.accumulated(candidate.track_id)
        decision = passage.confirmer.consider(candidate)
        if decision.accepted:  # the confirmer resets its history on acceptance
            evidence = evidence_before + candidate.best_votes
        else:
            evidence, _ = passage.confirmer.accumulated(candidate.track_id)
        self.logger.info(
            "LOCAL %s: %s votes=%d acc=%d ratio=%.3f shared=%d ref=%d offset=%+d "
            "ref_passage=%s at=%.0fs -> %s (passage %s)",
            "HIT" if decision.accepted else "CANDIDATE",
            describe(metadata),
            candidate.best_votes,
            evidence,
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
            self._identify(passage, candidate.track_id, metadata, via="local")
            return True
        return False

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
        history = passage.confirmer.history
        latest = history[-1] if history else None
        if latest is None or self._audd_wait_reason(passage) is not None:
            return False  # no promising candidate, or AudD isn't due anyway
        passage.audd_postponed += 1
        votes, _ = passage.confirmer.accumulated(latest[0])
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
        if passage.audd_attempts >= AUDD_MAX_ATTEMPTS_PER_PASSAGE:
            return f"attempt limit ({AUDD_MAX_ATTEMPTS_PER_PASSAGE}) reached for this passage"
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
        interval = (
            AUDD_FAST_INTERVAL_SECONDS
            if passage.audd_attempts < AUDD_FAST_ATTEMPTS
            else AUDD_SLOW_INTERVAL_SECONDS
        )
        passage.next_audd_at = passage.seconds + interval
        self.last_audd_at = self.stream_seconds
        self.logger.info(
            "AudD attempt %d/%d on %ds sample (passage %s)",
            passage.audd_attempts,
            AUDD_MAX_ATTEMPTS_PER_PASSAGE,
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

    def _identify(
        self, passage: Passage, track_id: int, metadata: dict[str, Any], *, via: str
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
        passage.next_learn_at = passage.seconds + LEARN_INTERVAL_SECONDS
        if self._publish(metadata):
            self.logger.info("Reported stream metadata: %s", describe(metadata))
        else:
            # MA declined: the source has metadata of its own, or the session is gone.
            self.logger.info("Music Assistant declined our metadata; recognition stopped")
            self.stopped = True

    # ---------------------------------------------------------------- learning

    async def _learn(self, passage: Passage, pcm: bytes, window_end: float) -> None:
        try:
            hashes = await self._fingerprint(pcm, self._divisor(window_end))
            await self._store_reference(passage, hashes, window_end)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Could not learn window (passage %s)", passage.short_id)

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
        windows = [(end, hashes) for end, hashes in windows if self._storable(passage, hashes)]
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
