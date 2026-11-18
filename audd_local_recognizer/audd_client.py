"""
Minimal AudD standard-recognition client (no SDK dependency).

Product boundary: AudD only identifies unknown audio. We never store, imitate or
reverse-engineer AudD fingerprints; local fingerprints are our own Panako hashes.
"""

from __future__ import annotations

from typing import Any

import aiohttp

AUDD_URL = "https://api.audd.io/"
AUDD_RETURN = "apple_music,spotify,deezer"
AUDD_TIMEOUT_SECONDS = 20
ARTWORK_SIZE = 600

# AudD error 300: no usable fingerprint could be made from the audio (silence, noise).
# That is a normal no-match, not an API failure.
AUDD_NO_FINGERPRINT_ERROR = 300


class AudDError(Exception):
    """A real AudD/HTTP/network failure. Messages never contain the API token."""


class AudDNoMatch:
    """A normal no-match, with the reason (for logs and debugging)."""

    def __init__(self, reason: str) -> None:
        """Keep the reason, e.g. "no result" or "error 300 (no fingerprint)"."""
        self.reason = reason

    def __repr__(self) -> str:
        """Return a readable representation."""
        return f"AudDNoMatch({self.reason!r})"


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _apple_artwork(result: dict[str, Any]) -> str | None:
    apple = result.get("apple_music")
    if not isinstance(apple, dict) or not isinstance(apple.get("artwork"), dict):
        return None
    url = _str(apple["artwork"].get("url"))
    if url is None:
        return None
    return url.replace("{w}", str(ARTWORK_SIZE)).replace("{h}", str(ARTWORK_SIZE))


def _spotify_artwork(result: dict[str, Any]) -> str | None:
    spotify = result.get("spotify")
    album = spotify.get("album") if isinstance(spotify, dict) else None
    images = album.get("images") if isinstance(album, dict) else None
    if not isinstance(images, list):
        return None
    candidates = [img for img in images if isinstance(img, dict) and _str(img.get("url"))]
    if not candidates:
        return None
    # Spotify lists largest first; prefer the largest explicitly anyway.
    best = max(candidates, key=lambda img: int(img.get("width") or 0))
    return str(best["url"])


# Deezer answers "no cover" with a placeholder image whose path is the MD5 of an empty string.
_PLACEHOLDER_ARTWORK = ("d41d8cd98f00b204e9800998ecf8427e",)


def is_placeholder_artwork(url: Any) -> bool:
    """Return True for a missing artwork URL or a known "no cover" placeholder."""
    return (
        not isinstance(url, str) or not url.strip() or any(p in url for p in _PLACEHOLDER_ARTWORK)
    )


def _deezer_artwork(result: dict[str, Any]) -> str | None:
    deezer = result.get("deezer")
    album = deezer.get("album") if isinstance(deezer, dict) else None
    if not isinstance(album, dict):
        return None
    for key in ("cover_big", "cover_medium", "cover"):  # cover_big is ~500x500
        url = _str(album.get(key))
        if url and not is_placeholder_artwork(url):
            return url
    return None


def pick_artwork(result: dict[str, Any]) -> str | None:
    """Best-effort artwork: Apple Music, then Spotify, then Deezer, else None."""
    return _apple_artwork(result) or _spotify_artwork(result) or _deezer_artwork(result)


def pick_isrc(result: dict[str, Any]) -> str | None:
    """
    Return the recording's ISRC from AudD's enrichment blocks, if any.

    Stored as an identity hint only; recognition never depends on it.
    """
    apple = result.get("apple_music")
    spotify = result.get("spotify")
    deezer = result.get("deezer")
    external = spotify.get("external_ids") if isinstance(spotify, dict) else None
    for candidate in (
        apple.get("isrc") if isinstance(apple, dict) else None,
        external.get("isrc") if isinstance(external, dict) else None,
        deezer.get("isrc") if isinstance(deezer, dict) else None,
    ):
        if value := _str(candidate):
            return value.strip().upper()
    return None


def parse_result(result: Any) -> dict[str, Any] | None:
    """Turn an AudD `result` object into our metadata dict, or None if unusable."""
    if not isinstance(result, dict):
        return None
    artist = _str(result.get("artist"))
    title = _str(result.get("title"))
    if artist is None or title is None:
        return None
    return {
        "artist": artist,
        "title": title,
        "album": _str(result.get("album")),
        "artwork": pick_artwork(result),
        "uri": _str(result.get("song_link")),
        "isrc": pick_isrc(result),
        "audd_timecode": _str(result.get("timecode")),
    }


def parse_response(payload: Any) -> dict[str, Any] | None:
    """
    Interpret a full AudD JSON response.

    Returns metadata on a match, None on a normal no-match (including error 300),
    and raises AudDError on any real API error.
    """
    result = parse_response_detail(payload)
    return None if isinstance(result, AudDNoMatch) else result


def parse_response_detail(payload: Any) -> dict[str, Any] | AudDNoMatch:
    """Like parse_response, but a no-match says why."""
    if not isinstance(payload, dict):
        msg = "AudD returned a non-object response"
        raise AudDError(msg)
    if payload.get("status") == "success":
        result = payload.get("result")
        if not isinstance(result, dict):
            return AudDNoMatch("no result")
        return parse_result(result) or AudDNoMatch("result without artist or title")
    error = payload.get("error")
    code = error.get("error_code") if isinstance(error, dict) else None
    if code == AUDD_NO_FINGERPRINT_ERROR:
        return AudDNoMatch("error 300 (no fingerprint from the sample)")
    message = error.get("error_message") if isinstance(error, dict) else None
    # Only the code and AudD's message: the payload may echo request params (the token).
    msg = f"AudD error {code}: {message}"
    raise AudDError(msg)


class AudDClient:
    """Call AudD's standard recognition endpoint."""

    def __init__(self, session: aiohttp.ClientSession, api_token: str) -> None:
        """Initialize with a shared aiohttp session and the (secret) API token."""
        self._session = session
        self._api_token = api_token

    def __repr__(self) -> str:
        """Never expose the token, even in debug output."""
        return "AudDClient(api_token=***)"

    async def recognize(self, wav_bytes: bytes) -> dict[str, Any] | AudDNoMatch:
        """Recognize a WAV sample. AudDNoMatch means no match; AudDError means failure."""
        form = aiohttp.FormData()
        form.add_field("api_token", self._api_token)
        form.add_field("return", AUDD_RETURN)
        form.add_field("file", wav_bytes, filename="sample.wav", content_type="audio/wav")
        try:
            async with self._session.post(
                AUDD_URL,
                data=form,
                timeout=aiohttp.ClientTimeout(total=AUDD_TIMEOUT_SECONDS),
            ) as response:
                if response.status >= 400:
                    msg = f"AudD HTTP {response.status}"
                    raise AudDError(msg)
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    msg = f"AudD returned invalid JSON (HTTP {response.status})"
                    raise AudDError(msg) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            msg = f"AudD request failed: {type(err).__name__}"
            raise AudDError(msg) from err
        result = parse_response_detail(payload)
        if isinstance(result, dict) and result.get("artwork"):
            result["artwork"] = await self._real_artwork(result["artwork"])
        return result

    async def _real_artwork(self, url: str) -> str | None:
        """Return url, or None when it turns out to be Deezer's empty "no cover" image."""
        return None if await redirects_to_placeholder(self._session, url) else url


async def redirects_to_placeholder(session: aiohttp.ClientSession, url: str) -> bool:
    """
    Return True when a Deezer cover URL redirects to Deezer's empty "no cover" image.

    Deezer answers some ordinary-looking cover URLs with a redirect to the placeholder, which
    only a request reveals. One HEAD request without following redirects; on any problem the
    answer is False (keep the URL: artwork is best effort). Other hosts are never contacted.
    """
    if "dzcdn.net" not in url:
        return False
    try:
        async with session.head(
            url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)
        ) as response:
            location = response.headers.get("Location", "")
    except aiohttp.ClientError, TimeoutError:
        return False
    return bool(location) and is_placeholder_artwork(location)
