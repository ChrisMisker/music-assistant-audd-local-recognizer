"""Minimal AudD standard-recognition client."""
from __future__ import annotations

from typing import Any
import logging

import aiohttp

AUDD_URL = "https://api.audd.io/"
LOGGER = logging.getLogger(__name__)


class AudDClient:
    """Call AudD without an extra SDK dependency."""

    def __init__(self, session: aiohttp.ClientSession, api_token: str) -> None:
        self.session = session
        self.api_token = api_token

    async def recognize(self, wav_bytes: bytes) -> dict[str, Any] | None:
        form = aiohttp.FormData()
        form.add_field("api_token", self.api_token)
        form.add_field("return", "apple_music,spotify,deezer")
        form.add_field("file", wav_bytes, filename="sample.wav", content_type="audio/wav")
        async with self.session.post(AUDD_URL, data=form, timeout=aiohttp.ClientTimeout(total=20)) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"AudD HTTP {response.status}: {payload}")
        if payload.get("status") != "success":
            error = payload.get("error") or payload

            # AudD error 300 means the supplied audio could not produce
            # a usable fingerprint. This is expected for silence, noise,
            # or otherwise unrecognizable audio and is therefore a normal
            # no-match rather than an API/network failure.
            if isinstance(error, dict) and error.get("error_code") == 300:
                return None

            raise RuntimeError(f"AudD error: {error}")
        result = payload.get("result")
        if not isinstance(result, dict):
            return None

        # Temporary v0.5 diagnostic: inspect AudD metadata returned for a
        # successful recognition. The API token/request is never logged.
        LOGGER.info("AUDD RAW RESULT: %s", result)
        artist = result.get("artist")
        title = result.get("title")
        if not artist or not title:
            return None
        artwork = None
        apple = result.get("apple_music")
        if isinstance(apple, dict):
            artwork_data = apple.get("artwork")
            if isinstance(artwork_data, dict):
                artwork = artwork_data.get("url")
                if isinstance(artwork, str):
                    artwork = artwork.replace("{w}", "600").replace("{h}", "600")
        return {
            "artist": str(artist),
            "title": str(title),
            "album": result.get("album"),
            "artwork": artwork,
            "uri": result.get("song_link"),
            "audd_timecode": result.get("timecode"),
        }
