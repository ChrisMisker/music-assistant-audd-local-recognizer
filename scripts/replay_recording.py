"""
Replay recorded input offline through the recognizer, without AudD and without the turntable.

    uv run python scripts/replay_recording.py debug-recordings/<timestamp> [--report] [--oracle]

Uses the real Panako fingerprints and a *temporary copy* of the database snapshot in that
folder (the snapshot itself is never changed). AudD is never called. Without --oracle this
measures local recognition only: tracks that are not in the database stay unidentified.
With --oracle, a "perfect AudD" answers every AudD attempt with the track that really played at
that moment, taken from each session's own log next to its recording (`<start>.log`, written by
the provider since 0.9; or --oracle MA_LOG for an exported Music Assistant log). It also answers
with a timecode (the time since that passage started) and the track length when the log has one
(`Expected end of ... duration Ns`), so the gapless guard works offline too. The replay then learns a first play just like live, and a second play
shows how many AudD calls local recognition still leaves (ideally none). Segments may be WAV or FLAC
(`scripts/fetch-recordings.sh` stores FLAC). Every segment group (one recording session) is fed as one continuous stream, in 1-second chunks, waiting for each
lookup to finish (ideal timing). Log lines use the recording's own clock, in the same format as
Music Assistant, so `--report` (or piping into scripts/vote_report.py) gives the usual report.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import sys
import tempfile
import time
import wave
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audd_local_recognizer.database import FingerprintDatabase
from audd_local_recognizer.engine import GapSettings, RecognitionEngine
from audd_local_recognizer.fingerprints import PcmFormat, fingerprint_window

SEGMENT_SUFFIXES = (".wav", ".flac")


def _segment_format(path: Path) -> tuple[int, int]:
    """Return (sample rate, channels) of a WAV or FLAC segment."""
    if path.suffix == ".flac":
        import soundfile  # noqa: PLC0415 - dev dependency, only needed for FLAC

        info = soundfile.info(str(path))
        return int(info.samplerate), int(info.channels)
    with wave.open(str(path), "rb") as reader:
        return reader.getframerate(), reader.getnchannels()


def _segment_chunks(path: Path, frames: int) -> Iterator[bytes]:
    """Yield interleaved s16le chunks of `frames` frames from a WAV or FLAC segment."""
    if path.suffix == ".flac":
        import soundfile  # noqa: PLC0415

        with soundfile.SoundFile(str(path)) as reader:
            while len(block := reader.read(frames, dtype="int16", always_2d=True)):
                yield block.astype("<i2").tobytes()
        return
    with wave.open(str(path), "rb") as reader:
        while chunk := reader.readframes(frames):
            yield chunk


class _RecordingClock(logging.Formatter):
    """Format log times on the recording's clock (start of the file + audio seconds)."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s (MainThread) [%(name)s] %(message)s")
        self.now = datetime.now()  # noqa: DTZ005 - local time, like the MA log

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        """Return the recording time with milliseconds."""
        return self.now.strftime("%Y-%m-%d %H:%M:%S.") + f"{self.now.microsecond // 1000:03d}"


_LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


class Oracle:
    """What really played when, from the live MA log: "Identified A - T via ... (passage P"."""

    def __init__(self, log: Path) -> None:
        """Parse passage start/end times and the identified track of each passage."""
        starts: dict[str, datetime] = {}
        ends: dict[str, datetime] = {}
        titles: dict[str, str] = {}
        self.durations: dict[str, float] = {}
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not (stamp := _LOG_TIME.match(line)):
                continue
            when = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
            if started := re.search(r"Passage (\w{8}) started", line):
                starts[started.group(1)] = when
            elif closed := re.search(r"passage (\w{8}) closed after", line):
                ends[closed.group(1)] = when
            elif found := re.search(r"Identified (.+?) via \w+ after \d+s \(passage (\w{8})", line):
                titles[found.group(2)] = found.group(1)
            elif length := re.search(r"Expected end of (.+?) at .*, duration (\d+)s\)", line):
                self.durations[length.group(1)] = float(length.group(2))
        ordered = sorted(starts.items(), key=lambda item: item[1])
        next_start = {
            passage: ordered[i + 1][1] if i + 1 < len(ordered) else start + timedelta(hours=1)
            for i, (passage, start) in enumerate(ordered)
        }
        self.spans = sorted(
            (starts[p], min(ends.get(p, next_start[p]), next_start[p]), titles[p])
            for p in titles
            if p in starts
        )

    def track_at(self, when: datetime) -> str | None:
        """Return "Artist - Title" playing at that moment (a few seconds of slack)."""
        found = self.span_at(when)
        return found[1] if found else None

    def span_at(self, when: datetime) -> tuple[datetime, str] | None:
        """Return (passage start, "Artist - Title") playing at that moment, or None."""
        slack = timedelta(seconds=5)
        for start, end, title in reversed(self.spans):  # the most recent passage wins
            if start - slack <= when <= end + slack:
                return start, title
        return None

    def answer(self, when: datetime) -> dict[str, Any] | None:
        """Return AudD-like metadata for that moment: track, timecode and (if known) length."""
        if (found := self.span_at(when)) is None:
            return None
        start, label = found
        metadata = _metadata(label)
        position = max(0, int((when - start).total_seconds()))
        metadata["audd_timecode"] = f"{position // 60:02d}:{position % 60:02d}"
        metadata["duration_seconds"] = self.durations.get(label)
        return metadata


def _metadata(label: str) -> dict[str, Any]:
    artist, _, title = label.partition(" - ")
    return {"artist": artist, "title": title or artist, "album": None, "artwork": None, "uri": None}


async def _replay(
    files: list[Path],
    db: FingerprintDatabase,
    clock: _RecordingClock,
    oracle: Oracle | None,
    options: dict[str, Any],
) -> None:
    logger = logging.getLogger("music_assistant.Audio Recognizer: AudD + Local Fingerprints")
    rate, channels = _segment_format(files[0])
    fmt = PcmFormat(rate, channels, 2, "s16le")
    start = datetime.strptime(files[0].name[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007
    clock.now = start

    async def fingerprint(pcm: bytes, divisor: int) -> dict[str, list[int]]:
        return await asyncio.to_thread(fingerprint_window, pcm, fmt, divisor)

    async def perfect_audd(_wav: bytes) -> dict[str, Any] | None:
        assert oracle is not None
        return oracle.answer(clock.now)

    loop = asyncio.get_running_loop()
    engine = RecognitionEngine(
        fmt=fmt,
        gap_settings=GapSettings(),
        fingerprint=fingerprint,
        db=db,
        audd=perfect_audd if oracle else None,  # never the real AudD
        publish=lambda _metadata: True,
        spawn=loop.create_task,
        logger=logger,
        **options,
    )
    fed = 0
    for path in files:
        for chunk in _segment_chunks(path, fmt.sample_rate):
            engine.feed(chunk)
            fed += 1
            clock.now = start + timedelta(seconds=fed)
            if engine.task is not None and not engine.task.done():
                await engine.task
    engine.close()


def main() -> None:
    """Replay every recording session in the folder."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--report", action="store_true", help="print the vote report")
    parser.add_argument(
        "--oracle",
        nargs="?",
        const="session",
        metavar="MA_LOG",
        help="answer AudD attempts with the track that really played: from each session's own "
        "log (<start>.log next to the recording) when given without a value, else from MA_LOG",
    )
    parser.add_argument(
        "--postpone-audd", action="store_true", help="engine option: postpone AudD for candidates"
    )
    parser.add_argument(
        "--fast-start",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="engine option: denser fingerprints for windows ending within SECONDS (e.g. 30)",
    )
    parser.add_argument(
        "--lookup-phases",
        type=int,
        default=1,
        metavar="N",
        help="engine option: repeat each lookup at N sub-frame offsets (1 = off)",
    )
    parser.add_argument(
        "--keep-later-references",
        action="store_true",
        help="keep references learned at/after the recording started (by default they are "
        "removed from the temporary copy: they come from this very audio and would let the "
        "replay recognize itself)",
    )
    args = parser.parse_args()
    files = sorted(
        path for path in args.folder.glob("*_[0-9][0-9].*") if path.suffix in SEGMENT_SUFFIXES
    )
    snapshot = args.folder / "fingerprints.sqlite3"
    if not files:
        sys.exit(f"no recordings in {args.folder}")
    clock = _RecordingClock()
    handler = logging.StreamHandler(stream := _Capture())
    handler.setFormatter(clock)
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    began = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        for started, group in groupby(files, key=lambda p: p.name[:15]):
            group_files = list(group)
            db_path = Path(tmp) / f"{started}.sqlite3"
            at_start = args.folder / f"{started}_db.sqlite3"
            if at_start.exists():  # the database as it was when this recording started
                shutil.copyfile(at_start, db_path)
                print(
                    f"{started}: using the database copy from the recording's start",
                    file=sys.stderr,
                )
            elif snapshot.exists():
                shutil.copyfile(snapshot, db_path)
                if not args.keep_later_references:
                    removed = _drop_references_since(db_path, next(iter(group_files)))
                    print(
                        f"{started}: later snapshot; removed {removed} references learned during "
                        "the recording (older plays may already have been pruned)",
                        file=sys.stderr,
                    )
            oracle = _oracle(args.oracle, args.folder, started)
            options = {
                "postpone_audd": args.postpone_audd,
                "fast_start_seconds": args.fast_start,
                "lookup_phases": args.lookup_phases,
            }
            asyncio.run(_replay(group_files, FingerprintDatabase(db_path), clock, oracle, options))
    lines = stream.lines
    print(f"Replayed {len(files)} segments in {time.monotonic() - began:.0f}s", file=sys.stderr)
    if args.report:
        from vote_report import parse, report  # noqa: PLC0415

        print(report(*parse(lines)))
    else:
        print("".join(lines), end="")


def _oracle(option: str | None, folder: Path, started: str) -> Oracle | None:
    """Return the oracle for one session: none, its own log, or the given MA log."""
    if option is None:
        return None
    if option != "session":
        return Oracle(Path(option))
    session_log = folder / f"{started}.log"
    if not session_log.exists():
        print(f"{started}: no session log ({session_log.name}); no oracle", file=sys.stderr)
        return None
    return Oracle(session_log)


def _drop_references_since(db_path: Path, first_file: Path) -> int:
    """Delete references created at/after the recording's start (created_at is UTC)."""
    import sqlite3  # noqa: PLC0415

    local_start = datetime.strptime(first_file.name[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007
    utc_start = local_start.astimezone().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.execute("DELETE FROM reference_windows WHERE created_at >= ?", (utc_start,))
        return cur.rowcount


class _Capture:
    """Collect log output."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def flush(self) -> None:
        return


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
