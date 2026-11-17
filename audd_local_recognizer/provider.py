"""
Music Assistant glue for Audio Recognizer: AudD + Local Fingerprints.

The recognition logic lives in engine.py (MA-independent). This module only maps MA's
AudioAnalysisProvider lifecycle onto one RecognitionEngine per live analysis session and
uses public MA APIs: MediaType.AUDIO_SOURCE, _run_offloaded and report_stream_metadata.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider

from .audd_client import AudDClient
from .database import DB_FILENAME, FingerprintDatabase
from .engine import GapSettings, RecognitionEngine
from .fingerprints import DENSITY_DIVISOR, Fingerprints, PcmFormat, fingerprint_window

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.provider import ProviderManifest
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.audio_analysis import AudioAnalysisData

VERSION = "0.5.3"
STORAGE_SUBDIR = "audd_local_recognizer"

CONF_ALL_AUDIO_SOURCES = "all_audio_sources"
CONF_AUDIO_SOURCES = "audio_sources"
CONF_AUDD_API_TOKEN = "audd_api_token"  # noqa: S105 - the name of a setting, not a secret
CONF_SILENCE_DBFS = "silence_threshold_dbfs"
CONF_RESUME_DBFS = "resume_threshold_dbfs"
CONF_MIN_GAP_SECONDS = "minimum_gap_seconds"

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

    async def handle_async_init(self) -> None:
        """Open the persistent fingerprint database off the event loop."""
        storage_dir = Path(self.mass.storage_path) / STORAGE_SUBDIR
        db_path = storage_dir / DB_FILENAME
        await asyncio.to_thread(storage_dir.mkdir, parents=True, exist_ok=True)
        self._db = await asyncio.to_thread(FingerprintDatabase, db_path)
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
                default_value=True,
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

    def _token(self) -> str:
        return str(self.config.get_value(CONF_AUDD_API_TOKEN) or "").strip()

    def _source_enabled(self, streamdetails: StreamDetails) -> bool:
        if bool(self.config.get_value(CONF_ALL_AUDIO_SOURCES)):
            return True
        value = self.config.get_value(CONF_AUDIO_SOURCES)
        if isinstance(value, str):
            selected = {value}
        elif isinstance(value, list):
            selected = {str(item) for item in value}
        else:
            selected = set()
        return f"{streamdetails.provider}|{streamdetails.item_id}" in selected

    async def _start_analysis(
        self, session_id: str, streamdetails: StreamDetails, audio_format: AudioFormat
    ) -> bool:
        """Accept enabled live sources in a supported PCM format."""
        if streamdetails.media_type != MediaType.AUDIO_SOURCE:
            return False
        if not self._source_enabled(streamdetails):
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
            "silence<=%ddBFS resume>=%ddBFS gap>=%ds",
            session_id,
            streamdetails.provider,
            streamdetails.item_id,
            fmt.sample_rate,
            fmt.channels,
            fmt.content_type,
            silence,
            resume,
            min_gap,
        )
        self._engines[session_id] = RecognitionEngine(
            fmt=fmt,
            gap_settings=GapSettings(float(silence), float(resume), float(min_gap)),
            fingerprint=lambda pcm: self._fingerprint(pcm, fmt),
            db=self._db,
            audd=audd,
            publish=lambda metadata: self._publish(session_id, metadata),
            spawn=lambda coro: self.mass.create_task(coro, task_name=f"{self.domain}_recognition"),
            logger=self.logger,
        )
        return True

    async def _fingerprint(self, pcm: bytes, fmt: PcmFormat) -> Fingerprints:
        # _run_offloaded keeps CPU work below playback (MA's analysis pool + solo lock).
        hashes: Fingerprints = await self._run_offloaded(fingerprint_window, pcm, fmt)
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

    async def process_pcm_chunk(self, session_id: str, pcm_chunk: bytes) -> None:
        """Feed a chunk; returns immediately (heavy work runs in tasks)."""
        if (engine := self._engines.get(session_id)) is not None:
            engine.feed(pcm_chunk)

    async def cancel(self, session_id: str) -> None:
        """End a live session and drop its in-memory state (the DB is untouched)."""
        if (engine := self._engines.pop(session_id, None)) is not None:
            engine.close()
            self.logger.info("Analysis ended: session=%s", session_id)
        await super().cancel(session_id)

    async def _finalize(self, session_id: str) -> AudioAnalysisData | None:
        """Live sources are never finalized; nothing is stored in MA either way."""
        if (engine := self._engines.pop(session_id, None)) is not None:
            engine.close()
        return None
