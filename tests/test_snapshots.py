"""Tests for the database copy kept between sessions (MA-independent)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from audd_local_recognizer.database import FingerprintDatabase
from audd_local_recognizer.snapshots import DatabaseSnapshots

if TYPE_CHECKING:
    from pathlib import Path

META = {"artist": "Artist", "title": "Song"}
OTHER = {"artist": "Other", "title": "Tune"}


def _landmarks(count: int, prefix: str = "h") -> dict[str, list[int]]:
    return {f"{prefix}{i:04d}": [i * 3] for i in range(count)}


def _counts(path: Path) -> tuple[int, int, int]:
    stats = FingerprintDatabase(path).stats()
    return stats.tracks, stats.references, stats.landmarks


def test_a_copy_made_between_sessions_is_reused_while_nothing_changed(tmp_path: Path) -> None:
    db = FingerprintDatabase(tmp_path / "fingerprints.sqlite3")
    db.add_references(db.upsert_track(META), "p", [(0.0, _landmarks(7))])
    snapshots = DatabaseSnapshots(db, tmp_path)
    assert snapshots.save()
    assert not snapshots.save()  # unchanged: no second copy
    target = tmp_path / "recordings" / "x_db.sqlite3"
    assert snapshots.place(target)
    assert target.stat().st_ino == snapshots.path.stat().st_ino  # linked, not copied
    assert _counts(target) == (1, 1, 7)


def test_a_changed_database_is_copied_at_the_start_and_old_links_keep_their_state(
    tmp_path: Path,
) -> None:
    db = FingerprintDatabase(tmp_path / "fingerprints.sqlite3")
    db.add_references(db.upsert_track(META), "p", [(0.0, _landmarks(7))])
    snapshots = DatabaseSnapshots(db, tmp_path)
    snapshots.save()
    first = tmp_path / "recordings" / "1_db.sqlite3"
    assert snapshots.place(first)
    db.add_references(db.upsert_track(OTHER), "q", [(0.0, _landmarks(3, prefix="z"))])
    second = tmp_path / "recordings" / "2_db.sqlite3"
    assert not snapshots.place(second)  # learned since the copy: copied now
    assert _counts(second) == (2, 2, 10)
    assert snapshots.save()  # a new copy replaces the kept one...
    assert _counts(first) == (1, 1, 7)  # ...but never the one a recording links to
    third = tmp_path / "recordings" / "3_db.sqlite3"
    assert snapshots.place(third)
    assert _counts(third) == (2, 2, 10)


def test_a_metadata_change_alone_changes_the_stamp(tmp_path: Path) -> None:
    db = FingerprintDatabase(tmp_path / "fingerprints.sqlite3")
    track = db.upsert_track(META)
    stamp = db.content_stamp()
    db.upsert_track({**META, "album": "Greatest Hits"})
    assert db.track_metadata(track) is not None
    assert db.content_stamp() != stamp


def test_without_a_kept_copy_place_copies_and_remove_deletes_it(tmp_path: Path) -> None:
    db = FingerprintDatabase(tmp_path / "fingerprints.sqlite3")
    db.add_references(db.upsert_track(META), "p", [(0.0, _landmarks(2))])
    snapshots = DatabaseSnapshots(db, tmp_path)
    target = tmp_path / "recordings" / "x_db.sqlite3"
    assert not snapshots.place(target)
    assert _counts(target) == (1, 1, 2)
    snapshots.save()
    snapshots.remove()
    assert not snapshots.path.exists()
    assert not snapshots.place(tmp_path / "recordings" / "y_db.sqlite3")


def test_a_stepwise_backup_is_complete(tmp_path: Path) -> None:
    db = FingerprintDatabase(tmp_path / "fingerprints.sqlite3")
    db.add_references(db.upsert_track(META), "p", [(float(i), _landmarks(50)) for i in range(20)])
    copy = tmp_path / "copy.sqlite3"
    db.backup_to(copy, pages=2, pause=0.001)
    assert _counts(copy) == (1, 20, 1000)
