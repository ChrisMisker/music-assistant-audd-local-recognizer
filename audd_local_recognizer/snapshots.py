"""
The fingerprint database as it was when a debug recording started, at (almost) no cost.

A recording carries a copy of the database from its start (`<start>_db.sqlite3`), so a replay
starts from exactly what the provider knew then. Copying the whole database (~300 MB) at every
session start took ~6 s of disk work on the HA Blue and stalled MA's event loop while music
played (2026-11-24). The database only changes while music is recognized, so a copy made when a
session ends (or its recording splits at a long silence) is the start state of the next one:
`save` makes that copy with a content stamp, and `place` hard-links it into the next recording
when the stamp still matches; only otherwise (first session after an MA restart, another source
learned meanwhile) it copies at the start as before. Blocking: run in a worker thread.
MA-independent.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .database import FingerprintDatabase

SNAPSHOT_FILENAME = "between_sessions_db.sqlite3"
# per step: 1024 pages (4 MB), then a short rest, so the disk is never flooded
COPY_PAGES = 1024
COPY_PAUSE_SECONDS = 0.02


class DatabaseSnapshots:
    """Keep one copy of the database between sessions, stamped with its content."""

    def __init__(self, db: FingerprintDatabase, folder: Path) -> None:
        """Keep the copy in folder (outside the recordings, which are fetched and removed)."""
        self._db = db
        self.path = folder / SNAPSHOT_FILENAME
        self._stamp_path = self.path.with_suffix(".stamp")

    def _stored_stamp(self) -> str | None:
        try:
            stamp = self._stamp_path.read_text(encoding="ascii").strip()
        except OSError:
            return None
        return stamp if stamp and self.path.exists() else None

    def _copy(self, target: Path) -> None:
        self._db.backup_to(target, pages=COPY_PAGES, pause=COPY_PAUSE_SECONDS)

    def save(self) -> bool:
        """Copy the database unless the kept copy already holds it; True when copied."""
        stamp = self._db.content_stamp()
        if self._stored_stamp() == stamp:
            return False
        temporary = self.path.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        self._copy(temporary)
        if self._db.content_stamp() != stamp:  # written while copying: not a clean state
            temporary.unlink(missing_ok=True)
            return False
        # stamp first gone, then the new copy (a new file: links in recordings keep the old)
        self._stamp_path.unlink(missing_ok=True)
        temporary.replace(self.path)
        stamp_temporary = self._stamp_path.with_suffix(".stamp.tmp")
        stamp_temporary.write_text(stamp, encoding="ascii")
        stamp_temporary.replace(self._stamp_path)
        return True

    def place(self, target: Path) -> bool:
        """Put the database as it is now at target; True when the kept copy was reused."""
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._stored_stamp() == self._db.content_stamp():
            target.unlink(missing_ok=True)
            with contextlib.suppress(OSError):  # e.g. another file system: copy instead
                os.link(self.path, target)
                return True
        self._copy(target)
        return False

    def remove(self) -> None:
        """Delete the kept copy (input recording is off: nothing needs it)."""
        self._stamp_path.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)
