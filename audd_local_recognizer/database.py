"""Persistent SQLite store for tracks, passage-tagged reference windows and landmarks.

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
    """Casefold, drop quote characters and collapse whitespace.

    AudD spells the same artist differently between results, e.g. 'Nat "King" Cole'
    and 'Nat King Cole'; both must map to one track.
    """
    text = _QUOTES.sub("", unicodedata.normalize("NFKC", text))
    return _SPACES.sub(" ", text).strip().casefold()


def track_key(metadata: dict[str, Any]) -> str:
    """Return the normalized identity key of a track (artist + title)."""
    return f"{_normalize(metadata['artist'])}\x1f{_normalize(metadata['title'])}"


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
        """Re-apply track_key to stored tracks, merging tracks that now coincide.

        Keeps the database consistent when the key normalization improves. References of
        a merged duplicate move to the surviving (oldest) track.
        """
        with self._transaction() as db:
            rows = db.execute("SELECT id, track_key, metadata_json FROM tracks ORDER BY id")
            for track_id, old_key, metadata_json in rows.fetchall():
                new_key = track_key(json.loads(metadata_json))
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
        """Insert or refresh a track by artist/title and return its id."""
        key = track_key(metadata)
        payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        with self._transaction() as db:
            db.execute(
                """INSERT INTO tracks(track_key, metadata_json) VALUES(?, ?)
                   ON CONFLICT(track_key) DO UPDATE SET
                     metadata_json=excluded.metadata_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (key, payload),
            )
            row = db.execute("SELECT id FROM tracks WHERE track_key=?", (key,)).fetchone()
        return int(row[0])

    def add_reference(
        self,
        track_id: int,
        passage_id: str,
        passage_offset_seconds: float,
        hashes: Fingerprints,
    ) -> tuple[int, int]:
        """Persist one reference window; return (reference_id, landmark_count).

        Returns (0, 0) and stores nothing when the window has no landmarks.
        """
        return self.add_references(track_id, passage_id, [(passage_offset_seconds, hashes)])[0]

    def add_references(
        self,
        track_id: int,
        passage_id: str,
        windows: list[tuple[float, Fingerprints]],
    ) -> list[tuple[int, int]]:
        """Persist several reference windows in one transaction.

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
        """Return the strongest reference candidate for a query, or None.

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

    def stats(self) -> DatabaseStats:
        """Return row counts."""
        with self._transaction() as db:
            tracks = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            refs = db.execute("SELECT COUNT(*) FROM reference_windows").fetchone()[0]
            landmarks = db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0]
        return DatabaseStats(int(tracks), int(refs), int(landmarks))
