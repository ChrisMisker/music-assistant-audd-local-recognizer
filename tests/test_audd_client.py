"""AudD response parsing, artwork fallbacks and token safety."""

from __future__ import annotations

from typing import Any, Self

import pytest

from audd_local_recognizer.audd_client import (
    AudDClient,
    AudDError,
    AudDNoMatch,
    parse_response,
    parse_response_detail,
    pick_artwork,
    pick_isrc,
)

TOKEN = "secret-token-123"  # noqa: S105 - a fake token for the tests

APPLE = {"artwork": {"url": "https://apple/{w}x{h}bb.jpg"}}
SPOTIFY = {
    "album": {
        "images": [
            {"url": "https://spotify/64.jpg", "width": 64},
            {"url": "https://spotify/640.jpg", "width": 640},
        ]
    }
}
DEEZER = {
    "album": {
        "cover": "https://deezer/cover",
        "cover_medium": "https://deezer/250",
        "cover_big": "https://deezer/500",
        "cover_xl": "https://deezer/1000",
    }
}


def _success(**extra: Any) -> dict[str, Any]:
    return {
        "status": "success",
        "result": {
            "artist": "Nat King Cole",
            "title": "Unforgettable",
            "album": "20 Greatest Hits",
            "song_link": "https://lis.tn/x",
            "timecode": "00:12",
            **extra,
        },
    }


def test_success_is_parsed() -> None:
    meta = parse_response(_success(deezer=DEEZER))
    assert meta == {
        "artist": "Nat King Cole",
        "title": "Unforgettable",
        "album": "20 Greatest Hits",
        "artwork": "https://deezer/500",
        "uri": "https://lis.tn/x",
        "isrc": None,
        "audd_timecode": "00:12",
    }


def test_artwork_priority() -> None:
    both = {"apple_music": APPLE, "spotify": SPOTIFY, "deezer": DEEZER}
    assert pick_artwork(both) == "https://apple/600x600bb.jpg"
    assert pick_artwork({"spotify": SPOTIFY, "deezer": DEEZER}) == "https://spotify/640.jpg"
    assert pick_artwork({"deezer": DEEZER}) == "https://deezer/500"
    assert pick_artwork({"deezer": {"album": {"cover_medium": "m", "cover": "c"}}}) == "m"
    assert pick_artwork({"deezer": {"album": {"cover": "c"}}}) == "c"
    assert pick_artwork({}) is None
    # malformed blocks fall through instead of crashing
    assert pick_artwork({"apple_music": [], "spotify": {"album": {}}, "deezer": "x"}) is None


def test_no_match_and_error_300_are_normal() -> None:
    assert parse_response({"status": "success", "result": None}) is None
    assert parse_response(_success(artist="")) is None
    error_300 = {"status": "error", "error": {"error_code": 300, "error_message": "no fp"}}
    assert parse_response(error_300) is None


def test_real_errors_raise_without_echoing_request_params() -> None:
    payload = {
        "status": "error",
        "error": {"error_code": 901, "error_message": "Recognition failed"},
        "request_params": {"api_token": TOKEN},
    }
    with pytest.raises(AudDError) as err:
        parse_response(payload)
    assert "901" in str(err.value)
    assert TOKEN not in str(err.value)
    with pytest.raises(AudDError) as err:
        parse_response({"status": "error", "request_params": {"api_token": TOKEN}})
    assert TOKEN not in str(err.value)
    with pytest.raises(AudDError):
        parse_response(["not", "a", "dict"])


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: Any = None) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.posts = 0

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts += 1
        return self.response


async def test_client_http_error_never_contains_token() -> None:
    session = _FakeSession(_FakeResponse(500, {"request_params": {"api_token": TOKEN}}))
    client = AudDClient(session, TOKEN)  # type: ignore[arg-type]
    with pytest.raises(AudDError) as err:
        await client.recognize(b"RIFF")
    assert TOKEN not in str(err.value)
    assert TOKEN not in repr(client)


async def test_client_invalid_json_is_an_error() -> None:
    client = AudDClient(_FakeSession(_FakeResponse(200, ValueError("html"))), TOKEN)  # type: ignore[arg-type]
    with pytest.raises(AudDError, match="invalid JSON"):
        await client.recognize(b"RIFF")


async def test_client_success() -> None:
    client = AudDClient(_FakeSession(_FakeResponse(200, _success())), TOKEN)  # type: ignore[arg-type]
    meta = await client.recognize(b"RIFF")
    assert isinstance(meta, dict)
    assert meta["title"] == "Unforgettable"


def test_isrc_from_any_enrichment_block() -> None:
    assert pick_isrc({"apple_music": {"isrc": "usab12345678"}}) == "USAB12345678"
    assert pick_isrc({"spotify": {"external_ids": {"isrc": "GBXYZ7600001"}}}) == "GBXYZ7600001"
    assert pick_isrc({"deezer": {"isrc": "NLA123400001"}}) == "NLA123400001"
    assert pick_isrc({"apple_music": {}, "spotify": {"external_ids": None}, "deezer": []}) is None
    meta = parse_response(_success(deezer={"isrc": "NLA123400001", **DEEZER}))
    assert meta is not None
    assert meta["isrc"] == "NLA123400001"


def test_no_match_says_why() -> None:
    reasons = [
        parse_response_detail({"status": "success", "result": None}),
        parse_response_detail(_success(artist="")),
        parse_response_detail({"status": "error", "error": {"error_code": 300}}),
    ]
    assert all(isinstance(r, AudDNoMatch) for r in reasons)
    assert [r.reason for r in reasons if isinstance(r, AudDNoMatch)] == [
        "no result",
        "result without artist or title",
        "error 300 (no fingerprint from the sample)",
    ]


async def test_client_returns_no_match_with_reason() -> None:
    session = _FakeSession(_FakeResponse(200, {"status": "success", "result": None}))
    result = await AudDClient(session, TOKEN).recognize(b"RIFF")  # type: ignore[arg-type]
    assert isinstance(result, AudDNoMatch)
    assert result.reason == "no result"


def test_deezer_no_cover_placeholder_is_not_artwork() -> None:
    empty = "https://cdn-images.dzcdn.net/images/cover/d41d8cd98f00b204e9800998ecf8427e/500x500-000000-80-0-0.jpg"
    placeholder = {"deezer": {"album": {"cover_big": empty, "cover_medium": empty}}}
    assert pick_artwork(placeholder) is None
    assert pick_artwork({**placeholder, "spotify": SPOTIFY}) == "https://spotify/640.jpg"
