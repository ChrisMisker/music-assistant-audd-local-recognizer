"""SQLite persistence, offset matching and passage exclusion."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from audd_local_recognizer.database import (
    SCHEMA_VERSION,
    FingerprintDatabase,
    SchemaVersionError,
    open_database,
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
        old = int(cur.lastrowid or 0)
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


def test_prune_keeps_the_most_recent_plays_whole(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    for play in ("old", "middle", "current"):
        db.add_references(
            track, play, [(i * 6.0, _landmarks(3, prefix=f"{play}{i}")) for i in range(5)]
        )
    assert db.prune_track(track, "current", max_plays=2) == 5  # "old" goes
    with sqlite3.connect(db_path) as conn:
        plays = [r[0] for r in conn.execute("SELECT DISTINCT passage_id FROM reference_windows")]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM landmarks WHERE reference_id NOT IN "
            "(SELECT id FROM reference_windows)"
        ).fetchone()[0]
    assert sorted(plays) == ["current", "middle"]
    assert orphans == 0  # landmarks went with their references
    # the current play is never removed
    assert db.prune_track(track, "current", max_plays=1) == 5
    assert db.stats().references == 5


def test_long_tracks_keep_as_many_plays_as_short_ones(db_path: Path) -> None:
    """Regression: a reference cap left a 5-minute track with only its latest play."""
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    for play in ("previous", "current"):  # 50 windows each, like a 5-minute track
        db.add_references(
            track, play, [(i * 6.0, _landmarks(2, prefix=f"{play}{i}x")) for i in range(50)]
        )
    assert db.prune_track(track, "current", max_plays=2) == 0
    assert db.stats().references == 100


def test_prune_is_per_track(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    a = db.upsert_track(META)
    b = db.upsert_track(OTHER)
    db.add_references(a, "pa", [(0.0, _landmarks(3))])
    db.add_references(b, "pb", [(0.0, _landmarks(3, prefix="b")), (6.0, _landmarks(3, prefix="c"))])
    db.add_references(
        b, "pb2", [(0.0, _landmarks(3, prefix="d")), (6.0, _landmarks(3, prefix="e"))]
    )
    db.add_references(
        b, "now", [(0.0, _landmarks(3, prefix="f")), (6.0, _landmarks(3, prefix="g"))]
    )
    assert db.prune_track(a, "now", max_plays=2) == 0
    assert db.prune_track(b, "now", max_plays=2) == 2  # keeps the newest other play (pb2)
    assert db.stats().references == 5


def _play(
    db: FingerprintDatabase, track: int, play: str, song_seconds: range, start: float
) -> None:
    """Learn a play covering song_seconds (every 6 s) of a song that started at start."""
    db.add_references(
        track,
        play,
        [(second + start, _landmarks(2, prefix=f"{play}{second}x")) for second in song_seconds],
    )
    db.set_song_start(track, play, start)


def _plays(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return sorted(
            r[0] for r in conn.execute("SELECT DISTINCT passage_id FROM reference_windows")
        )


def test_a_needle_drop_never_evicts_the_play_that_holds_the_start(db_path: Path) -> None:
    """Regression (Foreigner Suite, 2026-11-19): two drops mid-song removed both full plays."""
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    _play(db, track, "full1", range(0, 600, 6), 0.0)
    _play(db, track, "full2", range(0, 600, 6), 3.0)
    _play(db, track, "drop1", range(588, 690, 6), -588.0)
    assert db.prune_track(track, "drop1", 2, 6.0, 30) == 0
    _play(db, track, "drop2", range(844, 1050, 6), -844.0)
    assert db.prune_track(track, "drop2", 2, 6.0, 30) == 0
    assert _plays(db_path) == ["drop1", "drop2", "full1", "full2"]
    # a third full play of the same part makes the oldest full play redundant
    _play(db, track, "full3", range(0, 600, 6), 1.0)
    assert db.prune_track(track, "full3", 2, 6.0, 30) == 100
    assert _plays(db_path) == ["drop1", "drop2", "full2", "full3"]


def test_an_older_play_with_little_unique_coverage_goes(db_path: Path) -> None:
    """A few seconds of edge difference do not keep an old play forever."""
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    _play(db, track, "old", range(0, 180, 6), 0.0)
    _play(db, track, "middle", range(12, 180, 6), 0.0)
    _play(db, track, "current", range(6, 186, 6), 0.0)
    assert db.prune_track(track, "current", 2, 6.0, 30) == 30  # only 0-6 s were its own
    assert _plays(db_path) == ["current", "middle"]


def test_a_play_without_a_stored_start_counts_from_its_passage_start(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    for play in ("legacy1", "legacy2", "now"):
        db.add_references(
            track, play, [(i * 6.0, _landmarks(2, prefix=f"{play}{i}x")) for i in range(20)]
        )
    assert db.prune_track(track, "now", 2, 6.0, 30) == 20
    assert _plays(db_path) == ["legacy2", "now"]


def test_open_database_recreates_an_incompatible_file(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE reference_windows (id INTEGER PRIMARY KEY)")
    db, moved = open_database(db_path)
    assert moved is not None
    assert moved.name == "fingerprints.sqlite3.incompatible"
    assert moved.exists()
    assert db.stats().tracks == 0


def test_open_database_recreates_a_damaged_file(db_path: Path) -> None:
    db_path.write_bytes(b"this is not sqlite" * 100)
    db, moved = open_database(db_path)
    assert moved is not None
    assert db.stats().references == 0


def test_open_database_keeps_a_good_file(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    db.upsert_track(META)
    reopened, moved = open_database(db_path)
    assert moved is None
    assert reopened.stats().tracks == 1


def test_metadata_of_a_known_track_stays_stable(db_path: Path) -> None:
    """AudD names another compilation on every hit; the first album stays, gaps are filled."""
    db = FingerprintDatabase(db_path)
    first = {**META, "album": "20 Greatest Hits", "artwork": None, "isrc": None}
    track = db.upsert_track(first)
    later = {**META, "album": "Route 66", "artwork": "https://art/1.jpg", "isrc": "USCA29900001"}
    assert db.upsert_track(later) == track
    stored = db.track_metadata(track)
    assert stored is not None
    assert stored["album"] == "20 Greatest Hits"  # kept
    assert stored["artwork"] == "https://art/1.jpg"  # filled
    assert stored["isrc"] == "USCA29900001"  # filled
    # an emptier result never removes what is stored
    db.upsert_track({**META, "album": None, "artwork": None})
    assert db.track_metadata(track) == stored
    assert db.track_metadata(9999) is None


def test_backup_is_a_consistent_copy(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    db.add_references(db.upsert_track(META), "p", [(0.0, _landmarks(7))])
    copy = db_path.with_name("copy.sqlite3")
    db.backup_to(copy)
    db.add_references(db.upsert_track(OTHER), "q", [(0.0, _landmarks(3, prefix="z"))])
    copied = FingerprintDatabase(copy).stats()
    assert (copied.tracks, copied.references, copied.landmarks) == (1, 1, 7)


EMPTY_DEEZER = (
    "https://cdn-images.dzcdn.net/images/cover/d41d8cd98f00b204e9800998ecf8427e/500x500.jpg"
)


def test_placeholder_artwork_is_replaced_by_real_artwork(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track({**META, "artwork": EMPTY_DEEZER})
    db.upsert_track({**META, "artwork": "https://art/real.jpg"})
    stored = db.track_metadata(track)
    assert stored is not None
    assert stored["artwork"] == "https://art/real.jpg"


def test_reopen_clears_stored_placeholder_artwork(db_path: Path) -> None:
    import json  # noqa: PLC0415

    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    with sqlite3.connect(db_path) as conn:  # as written by an older version
        conn.execute(
            "UPDATE tracks SET metadata_json=? WHERE id=?",
            (json.dumps({**META, "artwork": EMPTY_DEEZER}), track),
        )
    stored = FingerprintDatabase(db_path).track_metadata(track)
    assert stored is not None
    assert stored["artwork"] is None


def test_artwork_listing_and_conditional_clear(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    deezer = "https://cdn-images.dzcdn.net/images/cover/x/500.jpg"
    a = db.upsert_track({**META, "artwork": deezer})
    db.upsert_track({**OTHER, "artwork": "https://apple/600.jpg"})
    assert db.artwork_urls("dzcdn.net") == [(a, deezer)]
    assert not db.clear_artwork(a, "https://some/other.jpg")  # changed meanwhile: keep
    assert db.clear_artwork(a, deezer)
    stored = db.track_metadata(a)
    assert stored is not None
    assert stored["artwork"] is None


def test_merge_tracks_moves_references_and_keeps_an_alias(db_path: Path) -> None:
    """One recording named in two ways becomes one track; the other name keeps landing on it."""
    db = FingerprintDatabase(db_path)
    orchestra = {"artist": "The Love Unlimited Orchestra", "title": "Satin Soul", "album": "Gold"}
    barry = {
        "artist": "Barry White",
        "title": "Satin Soul",
        "album": "White Gold",
        "duration_seconds": 255.0,
    }
    keep = db.upsert_track(orchestra)
    drop = db.upsert_track(barry)
    db.add_reference(keep, "p1", 0.0, _landmarks(30))
    db.add_reference(drop, "p2", 0.0, _landmarks(30, shift=5))
    assert db.merge_tracks(keep, drop) == 1
    merged = db.track_metadata(keep)
    assert merged is not None
    assert (merged["artist"], merged["album"]) == ("The Love Unlimited Orchestra", "Gold")
    assert merged["duration_seconds"] == 255.0  # filled from the dropped track
    assert merged["aliases"] == [track_key(barry)]
    assert db.track_metadata(drop) is None
    assert db.stats().references == 2
    # AudD names it "Barry White" again: no duplicate, the kept track answers
    assert db.upsert_track(barry) == keep
    assert db.stats().tracks == 1
    match = db.find_best_match(_landmarks(30, shift=5))
    assert match is not None
    assert match[1]["artist"] == "The Love Unlimited Orchestra"


def test_merge_tracks_rejects_unknown_or_identical_ids(db_path: Path) -> None:
    db = FingerprintDatabase(db_path)
    track = db.upsert_track(META)
    with pytest.raises(ValueError, match="itself"):
        db.merge_tracks(track, track)
    with pytest.raises(KeyError, match="unknown"):
        db.merge_tracks(track, 999)


@pytest.mark.parametrize(
    ("title", "plain"),
    [
        ("You See The Trouble With Me (Album Version)", "You See The Trouble With Me"),
        (
            "It's Ecstasy When You Lay Down Next To Me (Single Version)",
            "It's Ecstasy When You Lay Down Next To Me",
        ),
        ("Sussudio (2016 Remaster)", "Sussudio"),
        ("Impossible (Digitally Remastered)", "Impossible"),
        ("Two Hearts - 2016 Remaster", "Two Hearts"),
        ("Easy Lover [Remastered 2009]", "Easy Lover"),
    ],
)
def test_track_key_ignores_edition_suffixes(title: str, plain: str) -> None:
    assert track_key({"artist": "A", "title": title}) == track_key({"artist": "A", "title": plain})


@pytest.mark.parametrize(
    "title",
    [
        "Separate Lives (Love Theme From White Nights)",
        "Against All Odds (Take A Look At Me Now)",
        "Easy Lover (Live)",
        "Easy Lover (Radio Edit)",
        "(I Love You) For Sentimental Reasons",
        "Remastered",
        "(Album Version)",
    ],
)
def test_track_key_keeps_other_parts_of_the_title(title: str) -> None:
    key = track_key({"artist": "A", "title": title})
    assert key == f"a\x1f{title.casefold()}"


def test_reopen_merges_an_edition_into_the_oldest_track(db_path: Path) -> None:
    """Old keys with '(Album Version)' merge with the plain title; the older track survives."""
    import json  # noqa: PLC0415

    db = FingerprintDatabase(db_path)
    old_meta = {"artist": "Barry White", "title": "You See The Trouble With Me (Album Version)"}
    with sqlite3.connect(db_path) as conn:  # written with the old key, before this change
        cur = conn.execute(
            "INSERT INTO tracks(track_key, metadata_json) VALUES(?, ?)",
            ("barry white\x1fyou see the trouble with me (album version)", json.dumps(old_meta)),
        )
        older = int(cur.lastrowid or 0)
    newer = db.upsert_track(
        {"artist": "Barry White", "title": "You See The Trouble With Me", "duration_seconds": 200}
    )
    assert newer != older
    db.add_reference(older, "p1", 0.0, _landmarks(5))
    db.add_reference(newer, "p2", 0.0, _landmarks(5, prefix="x"))
    reopened = FingerprintDatabase(db_path)
    assert reopened.stats().tracks == 1
    kept = reopened.track_metadata(older)
    assert kept is not None
    assert kept["title"] == old_meta["title"]  # the older track keeps its name
    assert kept["duration_seconds"] == 200  # and gets the newer one's length
    assert "aliases" not in kept  # the old key is no alias: the new key covers it
    assert (
        reopened.upsert_track({"artist": "Barry White", "title": "You See The Trouble With Me"})
        == older
    )
