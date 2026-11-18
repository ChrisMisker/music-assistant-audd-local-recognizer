"""
Music Assistant glue for Audio Recognizer: AudD + Local Fingerprints.

The recognition logic lives in engine.py (MA-independent). This module only maps MA's
AudioAnalysisProvider lifecycle onto one RecognitionEngine per live analysis session and
uses public MA APIs: MediaType.AUDIO_SOURCE, _run_offloaded and report_stream_metadata.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

from .audd_client import AudDClient, redirects_to_placeholder
from .database import DB_FILENAME, FingerprintDatabase, open_database
from .debug_samples import write_debug_sample
from .engine import GapSettings, RecognitionEngine
from .fingerprints import DENSITY_DIVISOR, Fingerprints, PcmFormat, fingerprint_window
from .recording import FLUSH_SECONDS, MAX_RECORDING_SECONDS, InputRecorder

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

VERSION = "0.8.0"
STORAGE_SUBDIR = "audd_local_recognizer"
DEBUG_SUBDIR = "debug"
RECORDINGS_SUBDIR = "recordings"

CONF_ALL_AUDIO_SOURCES = "all_audio_sources"
CONF_AUDIO_SOURCES = "audio_sources"
CONF_AUDD_API_TOKEN = "audd_api_token"  # noqa: S105 - the name of a setting, not a secret
CONF_SILENCE_DBFS = "silence_threshold_dbfs"
CONF_RESUME_DBFS = "resume_threshold_dbfs"
CONF_MIN_GAP_SECONDS = "minimum_gap_seconds"
CONF_SAVE_SAMPLES = "save_audd_samples"
CONF_AUDD_SPEED = "audd_speed_correction_percent"
CONF_FAST_START = "fast_start"
FAST_START_SECONDS = 30.0
MAX_SPEED_CORRECTION = 10
CONF_RECORD_INPUT = "record_input_stream"

DEFAULT_SILENCE_DBFS = -45
DEFAULT_RESUME_DBFS = -35
DEFAULT_MIN_GAP_SECONDS = 2


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
        self._recorders: dict[str, tuple[InputRecorder, asyncio.Lock]] = {}

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
                key=CONF_ALL_AUDIO_SOURCES,
                type=ConfigEntryType.BOOLEAN,
                # Off by default (MA maintainers' advice): the user picks the sources to analyse.
                default_value=False,
                required=False,
                label="Recognize all live audio sources",
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
                    "without asking AudD. Uses about 35 % more disk space; turn off for a very "
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
                    "Keep the last 40 audio samples sent to AudD, with their outcome and "
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
                    "Record the live input exactly as the recognizer receives it (WAV, about "
                    "10 MB per minute) in the provider's data folder (recordings/), up to 60 "
                    "minutes in total; then it stops by itself. Only for troubleshooting; the "
                    "files stay on this server and are included in Home Assistant backups, so "
                    "remove them afterwards."
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
                        f"{provider.instance_id}|{source.item_id}",
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
        return f"{streamdetails.provider}|{streamdetails.item_id}" in self._selected_sources()

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
        self._engines[session_id] = RecognitionEngine(
            fmt=fmt,
            gap_settings=GapSettings(float(silence), float(resume), float(min_gap)),
            fingerprint=lambda pcm, divisor: self._fingerprint(pcm, fmt, divisor),
            db=self._db,
            audd=audd,
            publish=lambda metadata: self._publish(session_id, metadata),
            spawn=lambda coro: self.mass.create_task(coro, task_name=f"{self.domain}_recognition"),
            logger=self.logger,
            save_sample=self._save_sample if self.config.get_value(CONF_SAVE_SAMPLES) else None,
            audd_speed_percent=self._speed_correction(),
            fast_start_seconds=FAST_START_SECONDS if self._fast_start() else 0.0,
        )
        if self.config.get_value(CONF_RECORD_INPUT):
            await self._start_recording(session_id, fmt)
        return True

    async def _start_recording(self, session_id: str, fmt: PcmFormat) -> None:
        folder = self._storage_dir() / RECORDINGS_SUBDIR
        recorder = await asyncio.to_thread(InputRecorder, folder, fmt)
        if recorder.budget <= 0:
            self.logger.warning(
                "Input recording is on, but %s already holds the maximum of %d minutes: "
                "remove the recordings (scripts/fetch-recordings.sh --delete) to record again",
                folder,
                MAX_RECORDING_SECONDS // 60,
            )
            return
        # the database as it is when the recording starts: replaying against a later copy would
        # include what was learned (and pruned) during this very recording
        await asyncio.to_thread(self._db.backup_to, folder / f"{recorder.started}_db.sqlite3")
        self._recorders[session_id] = (recorder, asyncio.Lock())
        self.logger.info(
            "Recording the input of session %s to %s (%.0f of %d minutes left)",
            session_id,
            folder,
            recorder.budget / 60,
            MAX_RECORDING_SECONDS // 60,
        )

    async def _record(self, session_id: str, pcm_chunk: bytes) -> None:
        if (entry := self._recorders.get(session_id)) is None:
            return
        recorder, lock = entry
        recorder.add(pcm_chunk)
        if recorder.buffered_seconds < FLUSH_SECONDS:
            return
        async with lock:
            await asyncio.to_thread(recorder.flush)
        if recorder.full:
            self._recorders.pop(session_id, None)
            self.logger.info(
                "Input recording stopped: the maximum of %d minutes is reached",
                MAX_RECORDING_SECONDS // 60,
            )

    async def _stop_recording(self, session_id: str) -> None:
        if (entry := self._recorders.pop(session_id, None)) is None:
            return
        recorder, lock = entry
        async with lock:
            await asyncio.to_thread(recorder.flush)
            await asyncio.to_thread(recorder.close)

    async def _save_sample(self, wav: bytes, info: dict[str, Any]) -> None:
        await asyncio.to_thread(write_debug_sample, self._storage_dir() / DEBUG_SUBDIR, wav, info)

    async def _fingerprint(self, pcm: bytes, fmt: PcmFormat, divisor: int) -> Fingerprints:
        # _run_offloaded keeps CPU work below playback (MA's analysis pool + solo lock).
        hashes: Fingerprints = await self._run_offloaded(fingerprint_window, pcm, fmt, divisor)
        return hashes

    def _publish(self, session_id: str, metadata: dict[str, Any]) -> bool:
        # Newer MA returns bool (False = stop recognizing); slightly older Nightlies
        # (e.g. the one this was first tested on) return None. Only False is a refusal.
        accepted = self.report_stream_metadata(
            session_id,
            StreamMetadata(
                title=metadata["title"],
                artist=metadata.get("artist"),
                album=metadata.get("album"),
                image_url=metadata.get("artwork"),
                uri=metadata.get("uri"),
            ),
        )
        return accepted is not False

    def _storage_dir(self) -> Path:
        return Path(self.mass.storage_path) / STORAGE_SUBDIR

    async def unload(self, is_removed: bool = False) -> None:  # noqa: FBT001, FBT002 - MA API
        """Unload; when the user removes the provider, also delete its fingerprint database."""
        await super().unload(is_removed)
        if is_removed:
            storage_dir = self._storage_dir()
            await asyncio.to_thread(shutil.rmtree, storage_dir, ignore_errors=True)
            self.logger.info("Provider removed: deleted its fingerprint database %s", storage_dir)

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        """Feed a chunk; returns immediately (heavy work runs in tasks)."""
        if (engine := self._engines.get(session_id)) is not None:
            engine.feed(pcm_chunk)
        if self._recorders:
            try:
                await self._record(session_id, pcm_chunk)
            except Exception:
                # a debug recording must never affect recognition or the stream
                self._recorders.pop(session_id, None)
                self.logger.exception("Input recording failed and was stopped")

    async def cancel(self, session_id: str) -> None:
        """End a live session and drop its in-memory state (the DB is untouched)."""
        if (engine := self._engines.pop(session_id, None)) is not None:
            engine.close()
            self.logger.info("Analysis ended: session=%s", session_id)
        await self._stop_recording(session_id)
        await super().cancel(session_id)

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Live sources are never finalized; nothing is stored in MA either way."""
        if (engine := self._engines.pop(session_id, None)) is not None:
            engine.close()
        await self._stop_recording(session_id)
        return None
