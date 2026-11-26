"""Tests of the provider loaded by a real Music Assistant server (CI only, like the template)."""

from pathlib import Path

import pytest

# The server is only installed in CI (`uv sync --all-groups`); locally these tests skip.
pytest.importorskip("music_assistant.testing")

from music_assistant_models.enums import MediaType, ProviderTier

from music_assistant.mass import MusicAssistant
from music_assistant.models.audio_analysis_provider import AudioAnalysisProvider
from tests import DOMAIN


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_provider_sets_up_without_token(mass: MusicAssistant) -> None:
    """The provider can be added with default settings (no AudD token) and opens its database."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    provider = mass.get_provider(step.result["instance_id"])
    assert isinstance(provider, AudioAnalysisProvider)
    assert MediaType.AUDIO_SOURCE in provider.media_types
    entries = await provider.get_config_entries()
    assert {entry.key for entry in entries} >= {"audd_api_token", "all_audio_sources"}


async def test_removing_the_provider_deletes_its_database(mass: MusicAssistant) -> None:
    """Removing the provider (not a restart) also removes its fingerprint database."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    storage = Path(mass.storage_path) / DOMAIN
    assert (storage / "fingerprints.sqlite3").exists()
    await mass.config.remove_provider_config(step.result["instance_id"])
    assert not storage.exists(), sorted(str(p.relative_to(storage)) for p in storage.rglob("*"))
