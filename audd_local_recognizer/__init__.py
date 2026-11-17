"""Audio Recognizer: AudD + Local Fingerprints provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

    from .provider import AudDLocalRecognizerProvider


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> AudDLocalRecognizerProvider:
    """Initialize the provider instance."""
    # Imported here so the MA-independent modules (engine, database, fingerprints,
    # audd_client) can be imported and unit-tested without Music Assistant installed.
    from .provider import AudDLocalRecognizerProvider  # noqa: PLC0415

    return AudDLocalRecognizerProvider(mass, manifest, config)
