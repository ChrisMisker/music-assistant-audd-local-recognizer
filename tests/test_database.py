"""SQLite persistence, offset matching and passage exclusion."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from audd_local_recognizer.database import (
    SCHEMA_VERSION,
    FingerprintDatabase,
    SchemaVersionError,
    track_key,
)

if TYPE_CHECKING:
    from pathlib import Path

META = {"artist": "Nat King Cole", "title": "Unforgettable", "album": "20 Greatest Hits"}
OTHER = {"artist": "Phil Collins", "title": "In the Air Tonight", "album": None}


def _landmarks(n: int, shift: int = 0, prefix: str = "h") -> dict[str, list[int]]:
    return {f"{prefix}{i:04d}": [i * 3 + shift] for i in range(n)}


def test_offset_match_100_landmarks_shifted_21(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    ref, count = db.add_reference(track, "a" * 32, 0.0, _landmarks(100))
    assert count == 100
    match = db.find_best_match(_landmarks(100, shift=21))
    assert match is not None
    candidate, metadata = match
    assert (candidate.shared_hashes, candidate.best_votes) == (100, 100)
    assert candidate.best_offset == -21
    assert candidate.ratio == 1.0
    assert candidate.reference_id == ref
    assert candidate.reference_passage_id == "a" * 32
    assert metadata == META


def test_current_passage_is_excluded_but_earlier_passages_match(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    db.add_reference(track, "passage-a", 0.0, _landmarks(50))
    db.add_reference(track, "passage-c", 0.0, _landmarks(50))
    assert db.find_best_match(_landmarks(50), exclude_passage_id="passage-x") is not None
    match = db.find_best_match(_landmarks(50), exclude_passage_id="passage-c")
    assert match is not None
    assert match[0].reference_passage_id == "passage-a"
    only_c = FingerprintDatabase(db_path.with_name("c.sqlite3"))
    track_c = only_c.upsert_track(META)
    only_c.add_reference(track_c, "passage-c", 0.0, _landmarks(50))
    assert only_c.find_best_match(_landmarks(50), exclude_passage_id="passage-c") is None


def test_best_reference_wins_across_tracks(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    a = db.upsert_track(META)
    b = db.upsert_track(OTHER)
    db.add_reference(a, "p1", 0.0, _landmarks(10))
    db.add_reference(b, "p2", 0.0, _landmarks(40, prefix="x"))
    match = db.find_best_match({**_landmarks(3), **_landmarks(30, prefix="x")})
    assert match is not None
    assert match[1]["title"] == OTHER["title"]


def test_large_query_is_batched(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    db.add_reference(track, "p", 0.0, _landmarks(1200))
    match = db.find_best_match(_landmarks(1200, shift=5))
    assert match is not None
    assert match[0].best_votes == 1200


def test_no_match_and_empty_inputs(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    assert db.find_best_match({}) is None
    assert db.find_best_match(_landmarks(10)) is None
    track = db.upsert_track(META)
    assert db.add_reference(track, "p", 0.0, {}) == (0, 0)
    assert db.stats().references == 0


def test_upsert_is_case_insensitive_and_updates_metadata(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    first = db.upsert_track(META)
    second = db.upsert_track({**META, "artist": " nat king cole ", "album": "Other"})
    assert first == second
    assert track_key(META) == track_key({"artist": "NAT KING COLE", "title": "unforgettable"})
    assert db.stats().tracks == 1


def test_persists_across_reopen(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    db.add_reference(db.upsert_track(META), "p", 0.0, _landmarks(20))
    reopened = FingerprintDatabase(db_path)
    assert reopened.stats().references == 1
    assert reopened.find_best_match(_landmarks(20)) is not None


def test_schema_version_is_set(db_path: Path) -> None:
    FingerprintDatabase(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_old_v05_schema_is_refused(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE reference_windows (id INTEGER PRIMARY KEY)")
    with pytest.raises(SchemaVersionError):
        FingerprintDatabase(db_path)


def test_track_key_ignores_quote_variants() -> None:
    plain = {"artist": "Nat King Cole", "title": "Unforgettable"}
    quoted = {"artist": 'Nat "King" Cole', "title": "Unforgettable"}
    curly = {"artist": "Nat \u201cKing\u201d  Cole", "title": " unforgettable "}
    assert track_key(plain) == track_key(quoted) == track_key(curly)
    assert track_key({"artist": "A", "title": "Don\u2019t"}) == track_key(
        {"artist": "a", "title": "Don't"}
    )


def test_reopen_merges_tracks_that_now_share_a_key(db_path: Path) -> None:
    import json  # noqa: PLC0415

    db = FingerprintDatabase(db_path)
    plain = db.upsert_track({"artist": "Nat King Cole", "title": "Unforgettable"})
    db.add_reference(plain, "p1", 0.0, _landmarks(5))
    # simulate a row written by the old, quote-sensitive key
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tracks(track_key, metadata_json) VALUES(?, ?)",
            (
                'nat "king" cole\x1funforgettable',
                json.dumps({"artist": 'Nat "King" Cole', "title": "Unforgettable"}),
            ),
        )
        old = int(cur.lastrowid)
    db.add_reference(old, "p2", 0.0, _landmarks(5, prefix="x"))
    reopened = FingerprintDatabase(db_path)
    stats = reopened.stats()
    assert (stats.tracks, stats.references) == (1, 2)
    match = reopened.find_best_match(_landmarks(5, prefix="x"))
    assert match is not None
    assert match[0].track_id == plain


def test_add_references_batch(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    results = db.add_references(
        track, "p", [(0.0, _landmarks(3)), (6.0, {}), (12.0, _landmarks(4, prefix="y"))]
    )
    assert [count for _, count in results] == [3, 0, 4]
    assert results[1] == (0, 0)
    assert db.stats().references == 2
