"""Audio Recognizer: AudD + Local Fingerprints v0.5.0 — Panako-first recognition for live MA AudioSources."""
from __future__ import annotations

import asyncio
import io
import math
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

from .audd_client import AudDClient
from .database import FingerprintDatabase
from .fingerprints import DENSITY_DIVISOR, QueryMatch, density_subset, panako_fingerprint, pcm_to_mono_s16

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

SAMPLE_SECONDS = 12
LEARN_INTERVAL_SECONDS = 6
LOCAL_INTERVAL_SECONDS = 6
AUDD_MIN_INTERVAL_SECONDS = 30
AUDD_INITIAL_BACKOFF_SECONDS = 60
AUDD_MAX_BACKOFF_SECONDS = 900

CONF_ALL_AUDIO_SOURCES = "all_audio_sources"
CONF_AUDIO_SOURCES = "audio_sources"
CONF_AUDD_API_TOKEN = "audd_api_token"
CONF_SILENCE_DBFS = "silence_threshold_dbfs"
CONF_RESUME_DBFS = "resume_threshold_dbfs"
CONF_MIN_GAP_SECONDS = "minimum_gap_seconds"
CONF_GAP_DELAY_SECONDS = "recognition_delay_seconds"

DEFAULT_SILENCE_DBFS = -45
DEFAULT_RESUME_DBFS = -35
DEFAULT_MIN_GAP_SECONDS = 2
DEFAULT_GAP_DELAY_SECONDS = 8

STRONG_VOTES = 5
STRONG_RATIO = 0.60
CONFIRM_VOTES = 3
CONFIRM_RATIO = 0.60


@dataclass
class RecognitionSession:
    streamdetails: Any
    sample_rate: int
    channels: int
    sample_width: int
    content_type: str
    silence_dbfs: float
    resume_dbfs: float
    min_gap_seconds: int
    gap_delay_seconds: int
    pcm: bytearray = field(default_factory=bytearray)
    audio_seconds_seen: float = 0.0
    next_local_at: float = float(SAMPLE_SECONDS)
    next_learn_at: float = 0.0
    recognition_task: asyncio.Task[None] | None = None
    last_key: tuple[str, str] | None = None
    current_track_id: int | None = None
    current_metadata: dict[str, Any] | None = None
    learning_paused: bool = True
    silence_seconds: float = 0.0
    gap_detected: bool = False
    gap_recognition_at: float | None = None
    last_audd_at: float | None = None
    audd_backoff_seconds: int = 0
    audd_blocked_until: float = 0.0
    pending_track_id: int | None = None
    pending_count: int = 0

    @property
    def target_bytes(self) -> int:
        return self.sample_rate * self.channels * self.sample_width * SAMPLE_SECONDS

    def append_pcm(self, chunk: bytes) -> None:
        self.audio_seconds_seen += len(chunk) / (self.sample_rate * self.channels * self.sample_width)
        self.pcm.extend(chunk)
        excess = len(self.pcm) - self.target_bytes
        if excess > 0:
            del self.pcm[:excess]


class AudDLocalRecognizerProvider(AudioAnalysisProvider):
    """Recognize selected live AudioSources with local Panako and AudD fallback."""

    analysis_version = 2
    media_types = frozenset({MediaType.AUDIO_SOURCE})

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        super().__init__(mass, manifest, config, supported_features)
        self._recognition_sessions: dict[str, RecognitionSession] = {}
        storage_dir = Path(self.mass.storage_path) / "audd_local_recognizer"
        storage_dir.mkdir(parents=True, exist_ok=True)
        self._db = FingerprintDatabase(storage_dir / "fingerprints_v05.sqlite3")

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        source_options: list[ConfigValueOption] = []
        for provider in list(self.mass.providers):
            try:
                if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
                    continue
                get_sources = getattr(provider, "get_audio_sources", None)
                if get_sources is None:
                    continue
                for source in await get_sources():
                    source_options.append(
                        ConfigValueOption(
                            f"{provider.instance_id}|{source.item_id}",
                            title=f"{provider.name}: {source.name}",
                        )
                    )
            except Exception:
                self.logger.debug("Could not enumerate AudioSources", exc_info=True)
        source_options.sort(key=lambda option: str(option.title or option.value).casefold())
        return (
            ConfigEntry(
                key=CONF_AUDD_API_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                label="AudD API token",
            ),
            ConfigEntry(
                key=CONF_ALL_AUDIO_SOURCES,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
                label="Recognize all live audio sources",
            ),
            ConfigEntry(
                key=CONF_AUDIO_SOURCES,
                type=ConfigEntryType.STRING,
                default_value=[],
                options=source_options,
                multi_value=True,
                required=False,
                depends_on=CONF_ALL_AUDIO_SOURCES,
                depends_on_value=False,
                label="Audio sources to recognize",
            ),
            ConfigEntry(
                key=CONF_SILENCE_DBFS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SILENCE_DBFS,
                required=False,
                advanced=True,
                label="Silence threshold (dBFS)",
            ),
            ConfigEntry(
                key=CONF_RESUME_DBFS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_RESUME_DBFS,
                required=False,
                advanced=True,
                label="Resume threshold (dBFS)",
            ),
            ConfigEntry(
                key=CONF_MIN_GAP_SECONDS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_MIN_GAP_SECONDS,
                required=False,
                advanced=True,
                label="Minimum track gap (seconds)",
            ),
            ConfigEntry(
                key=CONF_GAP_DELAY_SECONDS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_GAP_DELAY_SECONDS,
                required=False,
                advanced=True,
                label="Recognition delay after gap (seconds)",
            ),
        )

    def _setting_int(self, key: str, default: int) -> int:
        value = self.config.get_value(key)
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        if streamdetails.media_type != MediaType.AUDIO_SOURCE or not self._source_enabled(streamdetails):
            return False
        channels = int(audio_format.channels or 0)
        if channels not in (1, 2):
            self.logger.warning("Skipping %s: unsupported channels=%s", session_id, channels)
            return False
        bit_depth = int(audio_format.bit_depth or 16)
        sample_width = max(1, bit_depth // 8)
        content_type = str(audio_format.content_type.value)
        if sample_width not in (2, 4):
            self.logger.warning("Skipping %s: unsupported bit depth=%s", session_id, bit_depth)
            return False
        silence = self._setting_int(CONF_SILENCE_DBFS, DEFAULT_SILENCE_DBFS)
        resume = self._setting_int(CONF_RESUME_DBFS, DEFAULT_RESUME_DBFS)
        gap = min(10, max(1, self._setting_int(CONF_MIN_GAP_SECONDS, DEFAULT_MIN_GAP_SECONDS)))
        delay = min(30, max(0, self._setting_int(CONF_GAP_DELAY_SECONDS, DEFAULT_GAP_DELAY_SECONDS)))
        state = RecognitionSession(
            streamdetails=streamdetails,
            sample_rate=int(audio_format.sample_rate),
            channels=channels,
            sample_width=sample_width,
            content_type=content_type,
            silence_dbfs=float(silence),
            resume_dbfs=float(resume),
            min_gap_seconds=gap,
            gap_delay_seconds=delay,
        )
        self._recognition_sessions[session_id] = state
        self.logger.info(
            "Audio Recognizer: AudD + Local Fingerprints v0.5.0 started: session=%s source=%s|%s format=%sHz/%sch/%s-bit Panako=%.2f%%",
            session_id,
            streamdetails.provider,
            streamdetails.item_id,
            state.sample_rate,
            state.channels,
            bit_depth,
            100 / DENSITY_DIVISOR,
        )
        return True

    def _source_enabled(self, streamdetails: StreamDetails) -> bool:
        if bool(self.config.get_value(CONF_ALL_AUDIO_SOURCES)):
            return True
        selected = self.config.get_value(CONF_AUDIO_SOURCES) or []
        if isinstance(selected, str):
            selected = [selected]
        return f"{streamdetails.provider}|{streamdetails.item_id}" in selected

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        state = self._recognition_sessions.get(session_id)
        if state is None:
            return
        chunk_seconds = len(pcm_chunk) / (state.sample_rate * state.channels * state.sample_width)
        state.append_pcm(pcm_chunk)
        self._update_gap_state(state, pcm_chunk, chunk_seconds)
        if len(state.pcm) < state.target_bytes:
            return
        if state.recognition_task is not None and not state.recognition_task.done():
            return

        # Keep learning a confirmed track every six seconds, but never across a suspected gap.
        if (
            state.current_track_id is not None
            and not state.learning_paused
            and state.audio_seconds_seen >= state.next_learn_at
        ):
            sample = bytes(state.pcm)
            state.next_learn_at = state.audio_seconds_seen + LEARN_INTERVAL_SECONDS
            state.recognition_task = self.mass.create_task(
                self._learn_window(session_id, sample),
                task_id=f"audd_local_recognizer_learn_{self.instance_id}_{session_id}",
            )
            return

        # Do not spend local/AudD recognition work on a known silent gap.
        # Once audio resumes, the gap detector schedules a fresh recognition.
        if state.gap_detected:
            return

        trigger: str | None = None

        # A resumed track has an explicit recognition deadline. Respect it
        # before the normal initial/periodic schedule so the configured
        # post-gap delay is actually honored.
        if state.gap_recognition_at is not None:
            if state.audio_seconds_seen < state.gap_recognition_at:
                return
            trigger = "track-gap"
            state.gap_recognition_at = None
        elif state.last_key is None and state.audio_seconds_seen >= SAMPLE_SECONDS:
            trigger = "initial"
        elif state.audio_seconds_seen >= state.next_local_at:
            trigger = "periodic-local"
        if trigger is None:
            return
        state.next_local_at = state.audio_seconds_seen + LOCAL_INTERVAL_SECONDS
        state.recognition_task = self.mass.create_task(
            self._recognize(session_id, bytes(state.pcm), trigger),
            task_id=f"audd_local_recognizer_match_{self.instance_id}_{session_id}",
        )

    def _update_gap_state(self, state: RecognitionSession, pcm_chunk: bytes, chunk_seconds: float) -> None:
        level_dbfs = self._pcm_dbfs(
            pcm_chunk,
            state.sample_width,
            state.channels,
            state.content_type,
        )
        if level_dbfs <= state.silence_dbfs:
            state.silence_seconds += chunk_seconds
            if not state.gap_detected and state.silence_seconds >= state.min_gap_seconds:
                state.gap_detected = True
                state.learning_paused = True
                self.logger.info("Possible track gap: %.1f dBFS for %.2fs", level_dbfs, state.silence_seconds)
            return
        if level_dbfs >= state.resume_dbfs:
            if state.gap_detected:
                state.gap_recognition_at = state.audio_seconds_seen + state.gap_delay_seconds
                self.logger.info("Audio resumed; recognition scheduled in %.1fs", state.gap_delay_seconds)
            state.silence_seconds = 0.0
            state.gap_detected = False
        elif not state.gap_detected:
            state.silence_seconds = 0.0

    async def _fingerprint(self, state: RecognitionSession, pcm: bytes) -> dict[str, list[int]]:
        hashes = await asyncio.to_thread(
            panako_fingerprint,
            pcm,
            state.sample_rate,
            state.channels,
            state.sample_width,
            state.content_type
        )
        return density_subset(hashes)

    async def _recognize(self, session_id: str, pcm: bytes, trigger: str) -> None:
        state = self._recognition_sessions.get(session_id)
        if state is None:
            return
        try:
            query = await self._fingerprint(state, pcm)
            local = await asyncio.to_thread(self._db.find_best_match, query)
            if local is not None:
                candidate, metadata = local
                if self._accept_local(state, candidate):
                    self.logger.info(
                        "LOCAL HIT: %s - %s votes=%d ratio=%.3f ref=%d offset=%+d (%s)",
                        metadata["artist"], metadata["title"], candidate.best_votes,
                        candidate.ratio, candidate.reference_id, candidate.best_offset, trigger,
                    )
                    self._activate_track(state, candidate.track_id, metadata)
                    return
            if trigger == "periodic-local" and state.last_key is not None:
                return
            match = await self._recognize_with_audd(state, pcm, trigger)
            if match is None:
                return
            track_id = await asyncio.to_thread(self._db.upsert_track, match)
            self.logger.info("Recognized via AudD: %s - %s", match["artist"], match["title"])
            self._activate_track(state, track_id, match)
            # Learn the exact window that AudD just identified.
            await asyncio.to_thread(self._db.add_reference, track_id, state.audio_seconds_seen, query)
            state.next_learn_at = state.audio_seconds_seen + LEARN_INTERVAL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Recognition failed for session %s", session_id)

    def _accept_local(self, state: RecognitionSession, candidate: QueryMatch) -> bool:
        if candidate.best_votes >= STRONG_VOTES and candidate.ratio >= STRONG_RATIO:
            state.pending_track_id = None
            state.pending_count = 0
            return True
        if candidate.best_votes >= CONFIRM_VOTES and candidate.ratio >= CONFIRM_RATIO:
            if state.pending_track_id == candidate.track_id:
                state.pending_count += 1
            else:
                state.pending_track_id = candidate.track_id
                state.pending_count = 1
            if state.pending_count >= 2:
                state.pending_track_id = None
                state.pending_count = 0
                return True
        else:
            state.pending_track_id = None
            state.pending_count = 0
        return False

    def _activate_track(self, state: RecognitionSession, track_id: int, metadata: dict[str, Any]) -> None:
        key = (metadata["artist"].strip().casefold(), metadata["title"].strip().casefold())
        changed = key != state.last_key
        state.last_key = key
        state.current_track_id = track_id
        state.current_metadata = metadata
        state.learning_paused = False
        state.next_learn_at = max(state.next_learn_at, state.audio_seconds_seen + LEARN_INTERVAL_SECONDS)
        if changed:
            self._publish_live_metadata(state, metadata)

    async def _learn_window(self, session_id: str, pcm: bytes) -> None:
        state = self._recognition_sessions.get(session_id)
        if state is None or state.current_track_id is None or state.learning_paused:
            return
        track_id = state.current_track_id
        try:
            hashes = await self._fingerprint(state, pcm)
            if state.current_track_id != track_id or state.learning_paused:
                return
            reference_id, count = await asyncio.to_thread(
                self._db.add_reference, track_id, state.audio_seconds_seen, hashes
            )
            if reference_id:
                self.logger.debug("Learned Panako reference=%d landmarks=%d", reference_id, count)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Could not learn Panako window")

    async def _recognize_with_audd(
        self, state: RecognitionSession, pcm: bytes, trigger: str
    ) -> dict[str, Any] | None:
        now = state.audio_seconds_seen
        if now < state.audd_blocked_until:
            return None
        if state.last_audd_at is not None and now - state.last_audd_at < AUDD_MIN_INTERVAL_SECONDS:
            return None
        token = str(self.config.get_value(CONF_AUDD_API_TOKEN) or "").strip()
        if not token:
            self.logger.warning("Local miss but no AudD API token is configured")
            return None
        state.last_audd_at = now
        self.logger.info("LOCAL MISS: running AudD on %ss sample (%s)", SAMPLE_SECONDS, trigger)
        try:
            wav = self._pcm_to_wav(
                pcm,
                state.sample_rate,
                state.channels,
                state.sample_width,
                state.content_type,
            )
            match = await AudDClient(self.mass.http_session, token).recognize(wav)
        except asyncio.CancelledError:
            raise
        except Exception:
            state.audd_backoff_seconds = (
                AUDD_INITIAL_BACKOFF_SECONDS
                if state.audd_backoff_seconds <= 0
                else min(state.audd_backoff_seconds * 2, AUDD_MAX_BACKOFF_SECONDS)
            )
            state.audd_blocked_until = now + state.audd_backoff_seconds
            self.logger.exception("AudD recognition failed; backoff=%ds", state.audd_backoff_seconds)
            return None
        state.audd_backoff_seconds = 0
        state.audd_blocked_until = 0.0
        if match is None:
            self.logger.info("No AudD match")
        return match

    @staticmethod
    def _pcm_dbfs(
        pcm: bytes,
        sample_width: int,
        channels: int,
        content_type: str,
    ) -> float:
        if not pcm:
            return -120.0
        samples = pcm_to_mono_s16(pcm, sample_width, channels, content_type).astype(np.float32)
        if not samples.size:
            return -120.0
        rms = float(np.sqrt(np.mean(np.square(samples))))
        if rms <= 0:
            return -120.0
        return 20.0 * math.log10(rms / 32767.0)

    @staticmethod
    def _pcm_to_wav(
        pcm: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
        content_type: str,
    ) -> bytes:
        mono = pcm_to_mono_s16(pcm, sample_width, channels, content_type)
        out = io.BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(mono.tobytes())
        return out.getvalue()

    def _publish_live_metadata(
        self,
        state: RecognitionSession,
        match: dict[str, Any],
    ) -> None:
        """Report recognized metadata through the official MA analysis API."""
        session_id = next(
            (
                session_id
                for session_id, session_state
                in self._recognition_sessions.items()
                if session_state is state
            ),
            None,
        )
        if session_id is None:
            self.logger.warning(
                "Could not report metadata: analysis session not found"
            )
            return

        self.report_stream_metadata(
            session_id,
            StreamMetadata(
                title=match["title"],
                artist=match.get("artist"),
                album=match.get("album"),
                image_url=match.get("artwork"),
                uri=match.get("uri"),
            ),
        )

        self.logger.info(
            "Reported stream metadata: %s - %s",
            match["artist"],
            match["title"],
        )

    async def cancel(self, session_id: str) -> None:
        state = self._recognition_sessions.pop(session_id, None)
        if state and state.recognition_task and not state.recognition_task.done():
            state.recognition_task.cancel()
        await super().cancel(session_id)

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        self._recognition_sessions.pop(session_id, None)
        return None
