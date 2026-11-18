"""
Persistent SQLite store for tracks, passage-tagged reference windows and landmarks.

All methods are blocking; call them through a thread offload from async code.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .audd_client import is_placeholder_artwork
from .fingerprints import Fingerprints, QueryMatch, score_reference

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SCHEMA_VERSION = 1
DB_FILENAME = "fingerprints.sqlite3"
_QUERY_BATCH = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    track_key TEXT NOT NULL UNIQUE,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- One learned 12-second window. passage_id identifies the continuous piece of music
-- (between confirmed gaps) it was learned from: a uuid4 hex, unique across restarts.
CREATE TABLE IF NOT EXISTS reference_windows (
    id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    passage_id TEXT NOT NULL,
    passage_offset_seconds REAL NOT NULL,
    landmark_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS landmarks (
    reference_id INTEGER NOT NULL REFERENCES reference_windows(id) ON DELETE CASCADE,
    fingerprint_hash TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY(reference_id, fingerprint_hash, position)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_landmarks_hash ON landmarks(fingerprint_hash);
CREATE INDEX IF NOT EXISTS idx_reference_track ON reference_windows(track_id);
CREATE INDEX IF NOT EXISTS idx_reference_passage ON reference_windows(passage_id);
"""


class SchemaVersionError(RuntimeError):
    """The database file was written by an incompatible schema version."""


@dataclass(frozen=True)
class DatabaseStats:
    """Row counts for logging and diagnostics."""

    tracks: int
    references: int
    landmarks: int


_QUOTES = re.compile(r"[\"'`\u00b4\u2018\u2019\u201a\u201c\u201d\u201e]")
_SPACES = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """
    Casefold, drop quote characters and collapse whitespace.

    AudD spells the same artist differently between results, e.g. 'Nat "King" Cole'
    and 'Nat King Cole'; both must map to one track.
    """
    text = _QUOTES.sub("", unicodedata.normalize("NFKC", text))
    return _SPACES.sub(" ", text).strip().casefold()


def track_key(metadata: dict[str, Any]) -> str:
    """Return the normalized identity key of a track (artist + title)."""
    return f"{_normalize(metadata['artist'])}\x1f{_normalize(metadata['title'])}"


def _is_empty(field_name: str, value: Any) -> bool:
    """Return True for a missing value (artwork: also a "no cover" placeholder)."""
    if field_name == "artwork":
        return is_placeholder_artwork(value)
    return value in (None, "")


def _dump(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


class FingerprintDatabase:
    """SQLite-backed track/reference/landmark store."""

    def __init__(self, path: Path) -> None:
        """Open (and create if needed) the database at path."""
        self.path = path
        self._init_schema()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a transaction and always close it."""
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    def _init_schema(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            has_tables = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            if version not in (0, SCHEMA_VERSION) or (version == 0 and has_tables):
                msg = (
                    f"{self.path} has schema version {version}, expected {SCHEMA_VERSION}; "
                    "reset the fingerprint database"
                )
                raise SchemaVersionError(msg)
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.executescript(_SCHEMA)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._rekey_tracks()

    def _rekey_tracks(self) -> None:
        """
        Re-apply track_key to stored tracks, merging tracks that now coincide.

        Keeps the database consistent when the key normalization improves. References of
        a merged duplicate move to the surviving (oldest) track.
        """
        with self._transaction() as db:
            rows = db.execute("SELECT id, track_key, metadata_json FROM tracks ORDER BY id")
            for track_id, old_key, metadata_json in rows.fetchall():
                metadata = json.loads(metadata_json)
                if metadata.get("artwork") and is_placeholder_artwork(metadata["artwork"]):
                    # an older version stored Deezer's empty "no cover" image as artwork
                    metadata["artwork"] = None
                    db.execute(
                        "UPDATE tracks SET metadata_json=? WHERE id=?", (_dump(metadata), track_id)
                    )
                new_key = track_key(metadata)
                if new_key == old_key:
                    continue
                existing = db.execute(
                    "SELECT id FROM tracks WHERE track_key=?", (new_key,)
                ).fetchone()
                if existing is None:
                    db.execute("UPDATE tracks SET track_key=? WHERE id=?", (new_key, track_id))
                    continue
                db.execute(
                    "UPDATE reference_windows SET track_id=? WHERE track_id=?",
                    (existing[0], track_id),
                )
                db.execute("DELETE FROM tracks WHERE id=?", (track_id,))

    def upsert_track(self, metadata: dict[str, Any]) -> int:
        """
        Insert a track, or complete an existing one, and return its id.

        Metadata of a known track stays stable: AudD often names a different (compilation)
        album on every hit, so the values stored first are kept and a new result only fills
        fields that are still empty (e.g. artwork or ISRC that an earlier result lacked).
        """
        key = track_key(metadata)
        with self._transaction() as db:
            row = db.execute(
                "SELECT id, metadata_json FROM tracks WHERE track_key=?", (key,)
            ).fetchone()
            if row is None:
                cur = db.execute(
                    "INSERT INTO tracks(track_key, metadata_json) VALUES(?, ?)",
                    (key, _dump(metadata)),
                )
                return int(cur.lastrowid or 0)
            stored: dict[str, Any] = json.loads(str(row[1]))
            merged = {**stored}
            for field_name, value in metadata.items():
                if value not in (None, "") and _is_empty(field_name, merged.get(field_name)):
                    merged[field_name] = value
            if merged != stored:
                db.execute(
                    "UPDATE tracks SET metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (_dump(merged), row[0]),
                )
            return int(row[0])

    def artwork_urls(self, host: str) -> list[tuple[int, str]]:
        """Return (track_id, artwork URL) of tracks whose artwork is on host."""
        with self._transaction() as db:
            rows = db.execute("SELECT id, metadata_json FROM tracks").fetchall()
        result = []
        for track_id, metadata_json in rows:
            url = json.loads(str(metadata_json)).get("artwork")
            if isinstance(url, str) and host in url:
                result.append((int(track_id), url))
        return result

    def clear_artwork(self, track_id: int, url: str) -> bool:
        """Remove a track's artwork if it is still url; return whether it was removed."""
        with self._transaction() as db:
            row = db.execute("SELECT metadata_json FROM tracks WHERE id=?", (track_id,)).fetchone()
            if row is None:
                return False
            metadata = json.loads(str(row[0]))
            if metadata.get("artwork") != url:
                return False
            metadata["artwork"] = None
            db.execute("UPDATE tracks SET metadata_json=? WHERE id=?", (_dump(metadata), track_id))
        return True

    def track_metadata(self, track_id: int) -> dict[str, Any] | None:
        """Return the stored metadata of a track."""
        with self._transaction() as db:
            row = db.execute("SELECT metadata_json FROM tracks WHERE id=?", (track_id,)).fetchone()
        return None if row is None else dict(json.loads(str(row[0])))

    def add_reference(
        self,
        track_id: int,
        passage_id: str,
        passage_offset_seconds: float,
        hashes: Fingerprints,
    ) -> tuple[int, int]:
        """
        Persist one reference window; return (reference_id, landmark_count).

        Returns (0, 0) and stores nothing when the window has no landmarks.
        """
        return self.add_references(track_id, passage_id, [(passage_offset_seconds, hashes)])[0]

    def add_references(
        self,
        track_id: int,
        passage_id: str,
        windows: list[tuple[float, Fingerprints]],
    ) -> list[tuple[int, int]]:
        """
        Persist several reference windows in one transaction.

        Returns (reference_id, landmark_count) per window, (0, 0) for empty windows.
        """
        results: list[tuple[int, int]] = []
        with self._transaction() as db:
            for passage_offset_seconds, hashes in windows:
                rows = {(h, int(p)) for h, positions in hashes.items() for p in positions}
                if not rows:
                    results.append((0, 0))
                    continue
                cur = db.execute(
                    """INSERT INTO reference_windows
                       (track_id, passage_id, passage_offset_seconds, landmark_count)
                       VALUES(?, ?, ?, ?)""",
                    (track_id, passage_id, passage_offset_seconds, len(rows)),
                )
                reference_id = int(cur.lastrowid or 0)
                db.executemany(
                    "INSERT INTO landmarks(reference_id, fingerprint_hash, position) "
                    "VALUES(?, ?, ?)",
                    ((reference_id, h, p) for h, p in rows),
                )
                results.append((reference_id, len(rows)))
        return results

    def find_best_match(
        self,
        query: Fingerprints,
        exclude_passage_id: str | None = None,
    ) -> tuple[QueryMatch, dict[str, Any]] | None:
        """
        Return the strongest reference candidate for a query, or None.

        References learned in exclude_passage_id are ignored, so a passage can never
        recognize itself from windows it learned seconds earlier.
        """
        if not query:
            return None
        hashes = list(query)
        per_reference: dict[int, tuple[int, str, list[tuple[str, int]]]] = {}
        with self._transaction() as db:
            for start in range(0, len(hashes), _QUERY_BATCH):
                batch = hashes[start : start + _QUERY_BATCH]
                placeholders = ",".join("?" * len(batch))
                for reference_id, track_id, passage_id, fp_hash, position in db.execute(
                    f"""SELECT l.reference_id, r.track_id, r.passage_id,
                               l.fingerprint_hash, l.position
                        FROM landmarks l JOIN reference_windows r ON r.id = l.reference_id
                        WHERE l.fingerprint_hash IN ({placeholders})
                          AND r.passage_id IS NOT ?""",  # noqa: S608 - placeholders only
                    (*batch, exclude_passage_id),
                ):
                    entry = per_reference.setdefault(reference_id, (track_id, passage_id, []))
                    entry[2].append((fp_hash, position))

            best: QueryMatch | None = None
            for reference_id in sorted(per_reference):
                track_id, passage_id, rows = per_reference[reference_id]
                shared, offset, votes, ratio = score_reference(query, rows)
                candidate = QueryMatch(
                    track_id, reference_id, passage_id, shared, offset, votes, ratio
                )
                if best is None or (votes, ratio) > (best.best_votes, best.ratio):
                    best = candidate
            if best is None:
                return None
            row = db.execute(
                "SELECT metadata_json FROM tracks WHERE id=?", (best.track_id,)
            ).fetchone()
        if row is None:
            return None
        return best, json.loads(str(row[0]))

    def prune_track(self, track_id: int, keep_passage_id: str, max_plays: int) -> int:
        """
        Keep the references of at most max_plays plays (passages) of a track.

        The play being learned (keep_passage_id) always stays; of the others only the most
        recent ones are kept, so long tracks keep as many plays as short ones. Returns the
        number of references removed.
        """
        with self._transaction() as db:
            older = [
                passage_id
                for (passage_id,) in db.execute(
                    """SELECT passage_id FROM reference_windows
                       WHERE track_id=? AND passage_id != ?
                       GROUP BY passage_id ORDER BY MAX(id) DESC""",
                    (track_id, keep_passage_id),
                )
            ]
            removed = 0
            for passage_id in older[max(0, max_plays - 1) :]:
                # landmarks go with their reference (ON DELETE CASCADE)
                cur = db.execute(
                    "DELETE FROM reference_windows WHERE track_id=? AND passage_id=?",
                    (track_id, passage_id),
                )
                removed += cur.rowcount
        return removed

    def backup_to(self, target: Path) -> None:
        """Write a consistent copy of the database to target (safe while it is in use)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(sqlite3.connect(self.path, timeout=30)) as src,
            closing(sqlite3.connect(target)) as dst,
        ):
            src.backup(dst)

    def stats(self) -> DatabaseStats:
        """Return row counts."""
        with self._transaction() as db:
            tracks = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            refs = db.execute("SELECT COUNT(*) FROM reference_windows").fetchone()[0]
            landmarks = db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0]
        return DatabaseStats(int(tracks), int(refs), int(landmarks))


def open_database(path: Path) -> tuple[FingerprintDatabase, Path | None]:
    """
    Open the database, recreating it when it cannot be used as is.

    Provider updates reach users automatically, so a database written by an incompatible
    schema version (or a damaged file) must never stop the provider. The old file is moved
    aside to ``<name>.incompatible`` (replacing an earlier one) and a new, empty database is
    created. Returns the database and the path of the moved-aside file, if any.

    :param path: The database file.
    """
    try:
        return FingerprintDatabase(path), None
    except SchemaVersionError:
        pass
    except sqlite3.DatabaseError as err:
        # only an unreadable file; never a transient error such as "database is locked"
        if "not a database" not in str(err) and "malformed" not in str(err):
            raise
    backup = path.with_name(f"{path.name}.incompatible")
    for suffix in ("", "-wal", "-shm"):
        source = path.with_name(path.name + suffix)
        if source.exists():
            source.replace(backup.with_name(backup.name + suffix))
    return FingerprintDatabase(path), backup
