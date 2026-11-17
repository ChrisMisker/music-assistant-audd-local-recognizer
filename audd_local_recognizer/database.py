"""Persistent Panako fingerprint store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .fingerprints import QueryMatch, score_reference


class FingerprintDatabase:
    """SQLite-backed track/reference/landmark store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY,
                    track_key TEXT NOT NULL UNIQUE,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS reference_windows (
                    id INTEGER PRIMARY KEY,
                    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    learned_at_seconds REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS landmarks (
                    reference_id INTEGER NOT NULL REFERENCES reference_windows(id) ON DELETE CASCADE,
                    fingerprint_hash TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY(reference_id, fingerprint_hash, position)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS idx_landmarks_hash
                    ON landmarks(fingerprint_hash);
                CREATE INDEX IF NOT EXISTS idx_reference_track
                    ON reference_windows(track_id);
                """
            )

    @staticmethod
    def track_key(metadata: dict[str, Any]) -> str:
        return f"{metadata['artist'].strip().casefold()}\x1f{metadata['title'].strip().casefold()}"

    def upsert_track(self, metadata: dict[str, Any]) -> int:
        key = self.track_key(metadata)
        payload = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as db:
            db.execute(
                """INSERT INTO tracks(track_key, metadata_json) VALUES(?, ?)
                   ON CONFLICT(track_key) DO UPDATE SET
                     metadata_json=excluded.metadata_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (key, payload),
            )
            row = db.execute("SELECT id FROM tracks WHERE track_key=?", (key,)).fetchone()
            assert row is not None
            return int(row[0])

    def add_reference(
        self,
        track_id: int,
        learned_at_seconds: float,
        hashes: dict[str, list[int]],
    ) -> tuple[int, int]:
        """Persist one 12-second reference window and return (reference_id, landmarks)."""
        rows = [
            (fingerprint_hash, int(position))
            for fingerprint_hash, positions in hashes.items()
            for position in positions
        ]
        if not rows:
            return 0, 0
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO reference_windows(track_id, learned_at_seconds) VALUES(?, ?)",
                (track_id, learned_at_seconds),
            )
            reference_id = int(cur.lastrowid)
            db.executemany(
                "INSERT OR IGNORE INTO landmarks(reference_id, fingerprint_hash, position) VALUES(?, ?, ?)",
                ((reference_id, fingerprint_hash, position) for fingerprint_hash, position in rows),
            )
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM landmarks WHERE reference_id=?", (reference_id,)
                ).fetchone()[0]
            )
            return reference_id, count

    def find_best_match(self, query: dict[str, list[int]]) -> tuple[QueryMatch, dict[str, Any]] | None:
        """Return the strongest reference candidate for a fingerprint query."""
        if not query:
            return None
        query_hashes = list(query)
        reference_ids: set[int] = set()
        with self._connect() as db:
            for start in range(0, len(query_hashes), 500):
                batch = query_hashes[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                for (reference_id,) in db.execute(
                    f"SELECT DISTINCT reference_id FROM landmarks WHERE fingerprint_hash IN ({placeholders})",
                    batch,
                ):
                    reference_ids.add(int(reference_id))

            best: QueryMatch | None = None
            for reference_id in reference_ids:
                info = db.execute(
                    "SELECT track_id FROM reference_windows WHERE id=?", (reference_id,)
                ).fetchone()
                if info is None:
                    continue
                track_id = int(info[0])
                rows = db.execute(
                    "SELECT fingerprint_hash, position FROM landmarks WHERE reference_id=?",
                    (reference_id,),
                ).fetchall()
                shared, offset, votes, ratio = score_reference(query, rows)
                candidate = QueryMatch(track_id, reference_id, shared, offset, votes, ratio)
                if best is None or (candidate.best_votes, candidate.ratio) > (
                    best.best_votes,
                    best.ratio,
                ):
                    best = candidate

            if best is None:
                return None
            row = db.execute("SELECT metadata_json FROM tracks WHERE id=?", (best.track_id,)).fetchone()
            if row is None:
                return None
            return best, json.loads(str(row[0]))
