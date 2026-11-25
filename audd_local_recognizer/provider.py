"""
Music Assistant glue for Audio Recognizer: AudD + Local Fingerprints.

The recognition logic lives in engine.py (MA-independent). This module only maps MA's
AudioAnalysisProvider lifecycle onto one RecognitionEngine per live analysis session and
uses public MA APIs: MediaType.AUDIO_SOURCE, _run_offloaded and report_stream_metadata.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

from .audd_client import AudDClient, redirects_to_placeholder
from .database import DB_FILENAME, SCHEMA_VERSION, FingerprintDatabase, open_database
from .debug_samples import write_debug_sample
from .engine import PLACEHOLDER, GapSettings, RecognitionEngine
from .fingerprints import DENSITY_DIVISOR, Fingerprints, PcmFormat, fingerprint_window, warm_up
from .loopwatch import LoopWatch
from .quiet_gc import SILENCE_SECONDS as QUIET_GC_SILENCE_SECONDS
from .quiet_gc import QuietCollector
from .recording import (
    FLUSH_SECONDS,
    MAX_RECORDING_SECONDS,
    RECORDING_KEEP_DAYS,
    RECORDING_SPLIT_SILENCE_SECONDS,
    InputRecorder,
    SessionLog,
    remove_expired_recordings,
    split_action,
)
from .snapshots import DatabaseSnapshots
from .sources import selected_match, source_key

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

VERSION = "0.9.0"
STORAGE_SUBDIR = "audd_local_recognizer"
DEBUG_SUBDIR = "debug"
RECORDINGS_SUBDIR = "recordings"
# Experiment switch, no setting on purpose: with this file in the provider's data folder, new
# sessions are only recorded and timed, never recognized. It shows whether event-loop stalls (and
# Sendspin's 100 ms silences) happen without our work at all (2026-11-24).
RECORD_ONLY_FLAG = "record_only"
# Leaner still: with this file, new sessions are not recognized and not recorded either; only the
# event-loop stalls are timed (every 100 ms silence lines up with a stall, so stalls suffice).
WATCH_ONLY_FLAG = "watch_only"
# With this file, the stall watcher also logs where MA's event loop thread was during each stall
# (a few stack samples), to name the code behind the stalls that are "nothing of ours".
STALL_STACKS_FLAG = "stall_stacks"

CONF_ALL_AUDIO_SOURCES = "all_audio_sources"
CONF_AUDIO_SOURCES = "audio_sources"
CONF_AUDD_API_TOKEN = "audd_api_token"  # noqa: S105 - the name of a setting, not a secret
CONF_SILENCE_DBFS = "silence_threshold_dbfs"
CONF_RESUME_DBFS = "resume_threshold_dbfs"
CONF_MIN_GAP_SECONDS = "minimum_gap_seconds"
CONF_SAVE_SAMPLES = "save_audd_samples"
CONF_AUDD_SPEED = "audd_speed_correction_percent"
CONF_FAST_START = "fast_start"
CONF_AUDD_THOROUGH = "audd_thorough"
FAST_START_SECONDS = 30.0
MAX_SPEED_CORRECTION = 10
CONF_RECORD_INPUT = "record_input_stream"
CONF_RECORD_MINUTES = "record_input_max_minutes"
MAX_RECORDING_MINUTES = 240  # 4 h: a few records (24 h until 2026-11-25; community-catalog#54)

DEFAULT_SILENCE_DBFS = -45
DEFAULT_RESUME_DBFS = -35
DEFAULT_MIN_GAP_SECONDS = 2
# after a session ends, wait this long before copying the database (late results land first)
SNAPSHOT_DELAY_SECONDS = 5.0


def _whole_seconds(value: Any) -> int | None:
    """Return a non-negative number of seconds as int (StreamMetadata wants int), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return round(value)


class AudDLocalRecognizerProvider(AudioAnalysisProvider):
    """Recognize live AudioSources locally first, with AudD as discovery fallback."""

    analysis_version = 2
    media_types = frozenset({MediaType.AUDIO_SOURCE})

    _db: FingerprintDatabase

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize provider state (the database opens in handle_async_init)."""
        super().__init__(mass, manifest, config, supported_features)
        self._engines: dict[str, RecognitionEngine] = {}
        # the source's own metadata at session start: shown again when our track has ended
        self._placeholders: dict[str, StreamMetadata | None] = {}
        self._recorders: dict[str, tuple[InputRecorder, asyncio.Lock]] = {}
        self._session_logs: dict[str, SessionLog] = {}
        # sessions whose recording was split at a long silence, and their PCM format
        self._split_waiting: set[str] = set()
        # sessions in record-only mode (an experiment: no recognition at all, see RECORD_ONLY_FLAG)
        self._record_only: set[str] = set()
        self._formats: dict[str, PcmFormat] = {}
        # with input recording on or debug logging (debug aids), stalls of MA's event loop and
        # the time our work takes are logged too
        self._loopwatch = LoopWatch(self.logger)
        # the database between sessions, so a recording's start state needs no copy (debug aid)
        self._snapshots: DatabaseSnapshots | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        # Python's full garbage collection, run in a silence between tracks (see quiet_gc.py)
        self._quiet_gc = QuietCollector()
        self._gc_done_in_gap: set[str] = set()  # sessions whose current silence had its turn

    async def handle_async_init(self) -> None:
        """Open the persistent fingerprint database off the event loop."""
        storage_dir = self._storage_dir()
        db_path = storage_dir / DB_FILENAME
        await asyncio.to_thread(storage_dir.mkdir, parents=True, exist_ok=True)
        self._db, moved_aside = await asyncio.to_thread(open_database, db_path)
        if moved_aside is not None:
            # updates reach users automatically: never stop on an old or damaged database
            self.logger.warning(
                "The fingerprint database could not be used (older format or damaged); "
                "started a new, empty one. The old file was kept as %s",
                moved_aside,
            )
        elif self._db.migrated_from is not None:
            self.logger.info(
                "Fingerprint database upgraded from schema version %d to %d; nothing was lost",
                self._db.migrated_from,
                SCHEMA_VERSION,
            )
        await self._remove_expired_recordings()
        self._snapshots = DatabaseSnapshots(self._db, storage_dir)
        if self.config.get_value(CONF_RECORD_INPUT):
            # MA just started (nothing plays yet): the first recording can then reuse this copy
            self._save_snapshot_soon()
        else:
            await asyncio.to_thread(self._snapshots.remove)
        stats = await asyncio.to_thread(self._db.stats)
        self.logger.info(
            "AudD + Local Fingerprints v%s: db=%s tracks=%d references=%d landmarks=%d "
            "density=%.2f%% audd_token=%s",
            VERSION,
            db_path,
            stats.tracks,
            stats.references,
            stats.landmarks,
            100 / DENSITY_DIVISOR,
            "configured" if self._token() else "not configured",
        )
        if not self.config.get_value(CONF_ALL_AUDIO_SOURCES) and not self._selected_sources():
            self.logger.warning(
                "No live audio source is selected, so nothing will be recognized. Open this "
                "provider's settings and choose your source (e.g. your turntable) under "
                "'Audio sources to recognize', or turn on 'Recognize all live audio sources'."
            )
        self.mass.create_task(self._clean_stored_artwork(), task_name=f"{self.domain}_artwork")
        # imports and first-call costs now, not during the first track (they stall MA's loop)
        self.mass.create_task(self._warm_up(), task_name=f"{self.domain}_warm_up")

    async def _clean_stored_artwork(self) -> None:
        """Drop stored Deezer covers that turn out to be Deezer's empty "no cover" image."""
        removed = 0
        try:
            for track_id, url in await asyncio.to_thread(self._db.artwork_urls, "dzcdn.net"):
                if await redirects_to_placeholder(self.mass.http_session, url) and (
                    await asyncio.to_thread(self._db.clear_artwork, track_id, url)
                ):
                    removed += 1
        except Exception:
            self.logger.debug("Artwork check failed", exc_info=True)
        if removed:
            self.logger.info("Removed %d empty Deezer covers from the stored metadata", removed)

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the provider settings."""
        return (
            ConfigEntry(
                key=CONF_AUDD_API_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                label="AudD API token",
                description=(
                    "Needed to identify music the provider has not learned yet. Without a "
                    "token only previously learned tracks are recognized (locally). Get a "
                    "token at https://dashboard.audd.io"
                ),
            ),
            ConfigEntry(
                key=CONF_AUDIO_SOURCES,
                type=ConfigEntryType.STRING,
                default_value=[],
                options=await self._audio_source_options(),
                multi_value=True,
                required=False,
                depends_on=CONF_ALL_AUDIO_SOURCES,
                depends_on_value=False,
                label="Audio sources to recognize",
                description=(
                    "Choose the live sources to recognize, e.g. your turntable. Nothing is "
                    "recognized until you choose one. The source must allow it too: 'Allow "
                    "analysis of the audio input' in the source's own settings (e.g. the Sendspin "
                    "device or VBAN Receiver; on by default)."
                ),
            ),
            ConfigEntry(
                key=CONF_ALL_AUDIO_SOURCES,
                type=ConfigEntryType.BOOLEAN,
                # Off by default and advanced (MA maintainers' advice): the user picks the
                # sources to analyse; recognizing everything is a deliberate expert choice.
                default_value=False,
                required=False,
                advanced=True,
                label="Recognize all live audio sources",
                description=(
                    "Advanced, off by default: normally you choose your sources (e.g. your "
                    "turntable) under 'Audio sources to recognize'. Turn on to recognize every "
                    "live audio source instead, including ones added later. Only sources that "
                    "allow it are recognized: 'Allow analysis of the audio input' in the source's "
                    "own settings (on by default; turn it off for an input that should not be "
                    "listened to, such as a microphone)."
                ),
            ),
            ConfigEntry(
                key=CONF_AUDD_THOROUGH,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                advanced=True,
                label="Ask AudD more often (live and gapless records)",
                description=(
                    "While music is still unidentified, ask AudD every 30 seconds instead of "
                    "slowing down to every 90 seconds after three tries. Helps on live and gapless "
                    "records, where tracks run into each other, but uses more AudD requests "
                    "(about 8 instead of 5 for a 4-minute track AudD does not know)."
                ),
            ),
            ConfigEntry(
                key=CONF_FAST_START,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
                label="Faster recognition at the start of known tracks",
                description=(
                    "Stores more fingerprints of the first 30 seconds of every track, so a record "
                    "you play again from the start is usually recognized within 12-18 seconds "
                    "without asking AudD. Uses about 25 % more disk space; turn off for a very "
                    "large collection. Applies to tracks learned (or played again) from now on."
                ),
            ),
            ConfigEntry(
                key=CONF_SILENCE_DBFS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_SILENCE_DBFS,
                required=False,
                advanced=True,
                label="Silence threshold (dBFS)",
                description="Audio at or below this level counts towards a track gap.",
            ),
            ConfigEntry(
                key=CONF_RESUME_DBFS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_RESUME_DBFS,
                required=False,
                advanced=True,
                label="Resume threshold (dBFS)",
                description="Audio at or above this level ends a track gap.",
            ),
            ConfigEntry(
                key=CONF_MIN_GAP_SECONDS,
                type=ConfigEntryType.INTEGER,
                default_value=DEFAULT_MIN_GAP_SECONDS,
                required=False,
                advanced=True,
                label="Minimum track gap (seconds)",
                description="How long the silence must last to count as a track change.",
            ),
            ConfigEntry(
                key=CONF_AUDD_SPEED,
                type=ConfigEntryType.FLOAT,
                default_value=0.0,
                range=(-MAX_SPEED_CORRECTION, MAX_SPEED_CORRECTION),
                required=False,
                advanced=True,
                label="Speed correction for AudD (%)",
                description=(
                    "If your turntable runs a little fast or slow, AudD may miss many tracks. "
                    "Samples sent to AudD are played this much faster (positive) or slower "
                    "(negative); e.g. -2 for a turntable that runs about 2 % fast. Local "
                    "recognition is not affected. Measure your turntable with an RPM app."
                ),
            ),
            ConfigEntry(
                key=CONF_SAVE_SAMPLES,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                advanced=True,
                label="Save AudD samples for debugging",
                description=(
                    "Keep the last 250 audio samples sent to AudD, with their outcome and "
                    "measurements, in the provider's data folder (debug/). Only for "
                    "troubleshooting; they stay on this server."
                ),
            ),
            ConfigEntry(
                key=CONF_RECORD_INPUT,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                required=False,
                advanced=True,
                label="Record the input stream for debugging",
                description=(
                    "Temporarily record the live input exactly as the recognizer receives it "
                    "(WAV, about 10 MB per minute), with the recognizer's log of each session, in "
                    "the provider's data folder (recordings/), up to the maximum below; then it "
                    "stops by itself. Only for troubleshooting recognition: every recording is "
                    f"deleted automatically {RECORDING_KEEP_DAYS} days after it started. The files "
                    "stay on this server (and are included in Home Assistant backups meanwhile)."
                ),
            ),
            ConfigEntry(
                key=CONF_RECORD_MINUTES,
                type=ConfigEntryType.INTEGER,
                default_value=MAX_RECORDING_SECONDS // 60,
                range=(10, MAX_RECORDING_MINUTES),
                required=False,
                advanced=True,
                label="Maximum recording (minutes)",
                description=(
                    "Total length of all input recordings kept on this server (about 10 MB per "
                    "minute). When it is reached, recording stops until recordings are removed."
                ),
            ),
        )

    async def _audio_source_options(self) -> list[ConfigValueOption]:
        options: list[ConfigValueOption] = []
        for provider in list(self.mass.providers):
            try:
                if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
                    continue
                get_sources = getattr(provider, "get_audio_sources", None)
                if get_sources is None:
                    continue
                options.extend(
                    ConfigValueOption(
                        source_key(provider.instance_id, source.item_id),
                        title=f"{provider.name}: {source.name}",
                    )
                    for source in await get_sources()
                )
            except Exception:
                self.logger.debug("Could not enumerate AudioSources", exc_info=True)
        options.sort(key=lambda option: str(option.title or option.value).casefold())
        return options

    def _setting_int(self, key: str, default: int) -> int:
        value = self.config.get_value(key)
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _speed_correction(self) -> float:
        value = self.config.get_value(CONF_AUDD_SPEED)
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return 0.0
        try:
            percent = float(value)
        except ValueError:
            return 0.0
        return max(-MAX_SPEED_CORRECTION, min(MAX_SPEED_CORRECTION, percent))

    def _recording_seconds(self) -> float:
        value = self.config.get_value(CONF_RECORD_MINUTES)
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return float(MAX_RECORDING_SECONDS)
        try:
            minutes = float(value)
        except ValueError:
            return float(MAX_RECORDING_SECONDS)
        return max(1.0, min(MAX_RECORDING_MINUTES, minutes)) * 60

    def _fast_start(self) -> bool:
        value = self.config.get_value(CONF_FAST_START)
        return True if value is None else bool(value)

    def _token(self) -> str:
        return str(self.config.get_value(CONF_AUDD_API_TOKEN) or "").strip()

    def _selected_sources(self) -> set[str]:
        value = self.config.get_value(CONF_AUDIO_SOURCES)
        if isinstance(value, str):
            return {value} if value else set()
        if isinstance(value, list):
            return {str(item) for item in value}
        return set()

    def _source_enabled(self, streamdetails: StreamDetails) -> bool:
        if bool(self.config.get_value(CONF_ALL_AUDIO_SOURCES)):
            return True
        loaded = {provider.instance_id for provider in list(self.mass.providers)}
        match = selected_match(
            self._selected_sources(), streamdetails.provider, streamdetails.item_id, loaded
        )
        if match is not None and match != source_key(streamdetails.provider, streamdetails.item_id):
            self.logger.info(
                "Recognizing live source %s|%s: selected earlier as %s (that provider is gone, "
                "e.g. Sendspin Source merged into Sendspin); re-select it in this provider's "
                "settings to make this permanent",
                streamdetails.provider,
                streamdetails.item_id,
                match,
            )
        return match is not None

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        """Accept enabled live sources in a supported PCM format."""
        if streamdetails.media_type != MediaType.AUDIO_SOURCE:
            return False
        if not self._source_enabled(streamdetails):
            self.logger.info(
                "Not recognizing live source %s|%s: it is not selected in this provider's "
                "settings (choose it under 'Audio sources to recognize', or turn on "
                "'Recognize all live audio sources')",
                streamdetails.provider,
                streamdetails.item_id,
            )
            return False
        fmt = PcmFormat(
            sample_rate=int(audio_format.sample_rate),
            channels=int(audio_format.channels or 0),
            sample_width=max(1, int(audio_format.bit_depth or 16) // 8),
            content_type=str(audio_format.content_type.value),
        )
        if fmt.channels not in (1, 2) or (
            fmt.sample_width,
            fmt.content_type.removeprefix("pcm_"),
        ) not in ((2, "s16le"), (4, "f32le"), (4, "s32le")):
            self.logger.warning(
                "Skipping %s: unsupported PCM %s/%dch/%d-bit",
                session_id,
                fmt.content_type,
                fmt.channels,
                fmt.sample_width * 8,
            )
            return False
        silence = self._setting_int(CONF_SILENCE_DBFS, DEFAULT_SILENCE_DBFS)
        resume = max(silence, self._setting_int(CONF_RESUME_DBFS, DEFAULT_RESUME_DBFS))
        min_gap = min(10, max(1, self._setting_int(CONF_MIN_GAP_SECONDS, DEFAULT_MIN_GAP_SECONDS)))
        token = self._token()
        audd = AudDClient(self.mass.http_session, token).recognize if token else None
        self.logger.info(
            "Analysis started: session=%s source=%s|%s format=%dHz/%dch/%s "
            "silence<=%ddBFS resume>=%ddBFS gap>=%ds AudD speed correction %+.1f%% "
            "fast start %s",
            session_id,
            streamdetails.provider,
            streamdetails.item_id,
            fmt.sample_rate,
            fmt.channels,
            fmt.content_type,
            silence,
            resume,
            min_gap,
            self._speed_correction(),
            "on" if self._fast_start() else "off",
        )
        self._placeholders[session_id] = streamdetails.stream_metadata
        self._engines[session_id] = RecognitionEngine(
            fmt=fmt,
            gap_settings=GapSettings(float(silence), float(resume), float(min_gap)),
            fingerprint=lambda pcm, divisor: self._fingerprint(pcm, fmt, divisor),
            db=self._db,
            audd=self._loopwatch.wrap("audd", audd) if audd else None,
            run_blocking=self._loopwatch.run_blocking,
            timed=self._loopwatch.timed,
            publish=lambda metadata: self._publish(session_id, metadata),
            spawn=lambda coro: self.mass.create_task(coro, task_name=f"{self.domain}_recognition"),
            logger=self.logger,
            save_sample=self._save_sample if self.config.get_value(CONF_SAVE_SAMPLES) else None,
            audd_speed_percent=self._speed_correction(),
            fast_start_seconds=FAST_START_SECONDS if self._fast_start() else 0.0,
            audd_thorough=bool(self.config.get_value(CONF_AUDD_THOROUGH)),
        )
        watch_only = await asyncio.to_thread((self._storage_dir() / WATCH_ONLY_FLAG).exists)
        if watch_only or await asyncio.to_thread((self._storage_dir() / RECORD_ONLY_FLAG).exists):
            self._record_only.add(session_id)
            self.logger.warning(
                "%s mode (%s exists): this session is %s, but not recognized (no fingerprints, "
                "no database, no AudD)",
                "Watch-only" if watch_only else "Record-only",
                WATCH_ONLY_FLAG if watch_only else RECORD_ONLY_FLAG,
                "only timed (event-loop stalls)" if watch_only else "recorded and timed",
            )
        stall_stacks = await asyncio.to_thread((self._storage_dir() / STALL_STACKS_FLAG).exists)
        if stall_stacks:
            self.logger.warning(
                "Stall stacks (%s exists): each event-loop stall also logs where the loop was",
                STALL_STACKS_FLAG,
            )
        if (
            watch_only
            or stall_stacks
            or self.config.get_value(CONF_RECORD_INPUT)
            or self.logger.isEnabledFor(logging.DEBUG)
        ):
            self._loopwatch.start(sample_stacks=stall_stacks)  # debug aid: stalls and timings
        if self.config.get_value(CONF_RECORD_INPUT) and not watch_only:
            await self._start_recording(session_id, fmt)
        return True

    async def _warm_up(self) -> None:
        started = time.monotonic()
        try:
            await asyncio.to_thread(warm_up)
        except Exception:
            self.logger.debug("Fingerprint warm-up failed", exc_info=True)
            return
        self.logger.debug("Fingerprinting warmed up in %.1fs", time.monotonic() - started)

    async def _remove_expired_recordings(self) -> None:
        """Delete recordings older than RECORDING_KEEP_DAYS: they are temporary."""
        folder = self._storage_dir() / RECORDINGS_SUBDIR
        if removed := await asyncio.to_thread(remove_expired_recordings, folder):
            self.logger.info(
                "Deleted %d input recording(s) older than %d days (recordings are temporary)",
                removed,
                RECORDING_KEEP_DAYS,
            )

    async def _start_recording(self, session_id: str, fmt: PcmFormat) -> None:
        self._formats[session_id] = fmt
        await self._remove_expired_recordings()
        folder = self._storage_dir() / RECORDINGS_SUBDIR
        max_seconds = self._recording_seconds()
        recorder = await asyncio.to_thread(
            lambda: InputRecorder(folder, fmt, max_seconds=max_seconds)
        )
        if recorder.budget <= 0:
            self.logger.warning(
                "Input recording is on, but %s already holds the maximum of %d minutes: "
                "remove the recordings (scripts/fetch-recordings.sh --delete) to record again",
                folder,
                max_seconds // 60,
            )
            return
        self._recorders[session_id] = (recorder, asyncio.Lock())
        log_path = folder / f"{recorder.started}.log"
        self._session_logs[session_id] = await asyncio.to_thread(SessionLog, self.logger, log_path)
        # the database as it is when the recording starts: replaying against a later copy would
        # include what was learned (and pruned) during this very recording
        if session_id not in self._record_only and self._snapshots is not None:
            if (pending := self._snapshot_task) is not None and not pending.done():
                await asyncio.shield(pending)  # a copy of the session before: let it finish
            started = time.monotonic()
            async with self._loopwatch.busy("database copy"):
                reused = await asyncio.to_thread(
                    self._snapshots.place, folder / f"{recorder.started}_db.sqlite3"
                )
            self.logger.info(
                "Database at the start of this recording: %s (%.1fs)",
                "the copy made after the previous session (unchanged since)"
                if reused
                else "copied now (changed since the last copy)",
                time.monotonic() - started,
            )
        self.logger.info(
            "Recording the input of session %s to %s (%.0f of %d minutes left); provider v%s, "
            "source %dHz/%dch/%s, speed correction %+.1f%%, fast start %s",
            session_id,
            folder,
            recorder.budget / 60,
            max_seconds // 60,
            VERSION,
            fmt.sample_rate,
            fmt.channels,
            fmt.content_type,
            self._speed_correction(),
            "on" if self._fast_start() else "off",
        )

    async def _maybe_split_recording(self, session_id: str, engine: RecognitionEngine) -> None:
        """End the recording at a long silence and start a new one when music returns."""
        action = split_action(
            recording=session_id in self._recorders,
            waiting=session_id in self._split_waiting,
            in_gap=engine.gap.in_gap,
            silence_seconds=engine.gap.silence_seconds,
        )
        if action == "split":
            await self._stop_recording(session_id)
            self._split_waiting.add(session_id)
            self._save_snapshot_soon()  # silence now: the next recording can reuse this copy
            self.logger.info(
                "Recording split after %ds of silence; the next recording starts with the music",
                int(RECORDING_SPLIT_SILENCE_SECONDS),
            )
        elif action == "resume" and (fmt := self._formats.get(session_id)) is not None:
            self._split_waiting.discard(session_id)
            await self._start_recording(session_id, fmt)

    async def _record(self, session_id: str, pcm_chunk: bytes) -> None:
        if (entry := self._recorders.get(session_id)) is None:
            return
        recorder, lock = entry
        with self._loopwatch.timed("recording chunk"):
            recorder.add(pcm_chunk)
        if recorder.buffered_seconds < FLUSH_SECONDS:
            return
        async with lock, self._loopwatch.busy("recording"):
            await asyncio.to_thread(recorder.flush)
        if recorder.full:
            self._recorders.pop(session_id, None)
            self.logger.info(
                "Input recording stopped: the maximum of %d minutes is reached",
                recorder.max_seconds // 60,
            )

    async def _stop_recording(self, session_id: str) -> None:
        if (entry := self._recorders.pop(session_id, None)) is not None:
            recorder, lock = entry
            async with lock:
                await asyncio.to_thread(recorder.flush)
                await asyncio.to_thread(recorder.close)
        if (session_log := self._session_logs.pop(session_id, None)) is not None:
            await asyncio.to_thread(session_log.close)

    def _collect_garbage_soon(self) -> None:
        """Run Python's full garbage collection now if none of our sources is in the music."""
        quiet = not self._record_only and all(
            engine.passage is None for engine in self._engines.values()
        )
        if self._quiet_gc.due(quiet=quiet):
            self._quiet_gc.running = True
            self.mass.create_task(self._collect_garbage(), task_name=f"{self.domain}_gc")

    async def _collect_garbage(self) -> None:
        started = time.monotonic()
        try:
            async with self._loopwatch.busy("gc in a silence"):
                found = await asyncio.to_thread(self._quiet_gc.collect)
        finally:
            self._quiet_gc.running = False
        self.logger.debug(
            "Collected garbage in a silence: %d unreachable objects, %.0f ms",
            found,
            (time.monotonic() - started) * 1000,
        )

    def _save_snapshot_soon(self) -> None:
        """Copy the database for the next recording while nothing plays (a debug aid)."""
        if self._snapshots is None or not self.config.get_value(CONF_RECORD_INPUT):
            return
        if self._recorders:  # another source is recording, so music plays: copy later
            return
        if self._snapshot_task is not None and not self._snapshot_task.done():
            return
        self._snapshot_task = asyncio.get_running_loop().create_task(self._save_snapshot())

    async def _save_snapshot(self) -> None:
        if self._snapshots is None:
            return
        await asyncio.sleep(SNAPSHOT_DELAY_SECONDS)  # late results of the session land first
        started = time.monotonic()
        try:
            async with self._loopwatch.busy("database copy (between sessions)"):
                copied = await asyncio.to_thread(self._snapshots.save)
        except Exception:
            self.logger.warning("Could not copy the database for the next recording", exc_info=True)
            return
        self.logger.debug(
            "Database copy for the next recording: %s (%.1fs)",
            "made" if copied else "not needed (unchanged, or changed while copying)",
            time.monotonic() - started,
        )

    async def _save_sample(self, wav: bytes, info: dict[str, Any]) -> None:
        async with self._loopwatch.busy("debug sample"):
            await asyncio.to_thread(
                write_debug_sample, self._storage_dir() / DEBUG_SUBDIR, wav, info
            )

    async def _fingerprint(self, pcm: bytes, fmt: PcmFormat, divisor: int) -> Fingerprints:
        # _run_offloaded keeps CPU work below playback (MA's analysis pool + solo lock).
        async with self._loopwatch.busy(f"fingerprint 1/{divisor}"):
            hashes: Fingerprints = await self._run_offloaded(fingerprint_window, pcm, fmt, divisor)
        return hashes

    def _publish(self, session_id: str, metadata: dict[str, Any]) -> bool:
        # Newer MA returns bool (False = stop recognizing); slightly older Nightlies
        # (e.g. the one this was first tested on) return None. Only False is a refusal.
        if metadata.get(PLACEHOLDER):
            # MA has no public way to withdraw reported metadata mid-session: show the
            # source's own placeholder again (what MA showed before anything was recognized).
            placeholder = self._placeholders.get(session_id) or StreamMetadata(
                title="Unknown track"
            )
            return self.report_stream_metadata(session_id, placeholder) is not False
        accepted = self.report_stream_metadata(
            session_id,
            StreamMetadata(
                title=metadata["title"],
                artist=metadata.get("artist"),
                album=metadata.get("album"),
                image_url=metadata.get("artwork"),
                uri=metadata.get("uri"),
                duration=_whole_seconds(metadata.get("playback_duration_seconds")),
                elapsed_time=_whole_seconds(metadata.get("elapsed_seconds")),
                elapsed_time_last_updated=(
                    time.time() if metadata.get("elapsed_seconds") is not None else None
                ),
            ),
        )
        return accepted is not False

    def _storage_dir(self) -> Path:
        return Path(self.mass.storage_path) / STORAGE_SUBDIR

    async def unload(self, is_removed: bool = False) -> None:  # noqa: FBT001, FBT002 - MA API
        """Unload; when the user removes the provider, also delete its fingerprint database."""
        for session_id in list(self._recorders) + list(self._session_logs):
            await self._stop_recording(session_id)
        if self._snapshot_task is not None:
            self._snapshot_task.cancel()
        await super().unload(is_removed)
        if is_removed:
            storage_dir = self._storage_dir()
            await asyncio.to_thread(shutil.rmtree, storage_dir, ignore_errors=True)
            self.logger.info("Provider removed: deleted its fingerprint database %s", storage_dir)

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        """Feed a chunk; returns immediately (heavy work runs in tasks)."""
        if (engine := self._engines.get(session_id)) is not None:
            if session_id not in self._record_only:
                with self._loopwatch.timed("chunk"):  # level, gap detection, buffering
                    engine.feed(pcm_chunk)
                if engine.passage is not None:
                    self._gc_done_in_gap.discard(session_id)
                elif (
                    engine.gap.silence_seconds >= QUIET_GC_SILENCE_SECONDS
                    and session_id not in self._gc_done_in_gap
                ):
                    self._gc_done_in_gap.add(session_id)  # once per silence, not every chunk
                    self._collect_garbage_soon()
            if self._recorders or self._split_waiting:
                await self._maybe_split_recording(session_id, engine)
        if self._recorders:
            try:
                await self._record(session_id, pcm_chunk)
            except Exception:
                # a debug recording must never affect recognition or the stream
                self._recorders.pop(session_id, None)
                self.logger.exception("Input recording failed and was stopped")

    async def cancel(self, session_id: str) -> None:
        """End a live session and drop its in-memory state (the DB is untouched)."""
        self._placeholders.pop(session_id, None)
        if (engine := self._engines.pop(session_id, None)) is not None:
            engine.close()
            self.logger.info("Analysis ended: session=%s", session_id)
        if not self._engines:
            self._loopwatch.stop()
        recorded = session_id in self._recorders and session_id not in self._record_only
        await self._stop_recording(session_id)
        self._split_waiting.discard(session_id)
        self._record_only.discard(session_id)
        self._formats.pop(session_id, None)
        self._gc_done_in_gap.discard(session_id)
        if recorded:
            self._save_snapshot_soon()  # the session ended: copy now, not when the next starts
        self._collect_garbage_soon()
        await super().cancel(session_id)

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Live sources are never finalized; nothing is stored in MA either way."""
        self._placeholders.pop(session_id, None)
        if (engine := self._engines.pop(session_id, None)) is not None:
            engine.close()
        if not self._engines:
            self._loopwatch.stop()
        await self._stop_recording(session_id)
        self._split_waiting.discard(session_id)
        self._record_only.discard(session_id)
        self._formats.pop(session_id, None)
        return None
