"""
Replay recorded input offline through the recognizer, without AudD and without the turntable.

    uv run python scripts/replay_recording.py debug-recordings/<timestamp> [--report]

Uses the real Panako fingerprints and a *temporary copy* of the database snapshot in that
folder (the snapshot itself is never changed). AudD is never called, so this measures local
recognition only: tracks that are not in the database stay unidentified. Every segment group
(one recording session) is fed as one continuous stream, in 1-second chunks, waiting for each
lookup to finish (ideal timing). Log lines use the recording's own clock, in the same format as
Music Assistant, so `--report` (or piping into scripts/vote_report.py) gives the usual report.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import tempfile
import time
import wave
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audd_local_recognizer.database import FingerprintDatabase
from audd_local_recognizer.engine import GapSettings, RecognitionEngine
from audd_local_recognizer.fingerprints import PcmFormat, fingerprint_window


class _RecordingClock(logging.Formatter):
    """Format log times on the recording's clock (start of the file + audio seconds)."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s (MainThread) [%(name)s] %(message)s")
        self.now = datetime.now()  # noqa: DTZ005 - local time, like the MA log

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        """Return the recording time with milliseconds."""
        return self.now.strftime("%Y-%m-%d %H:%M:%S.") + f"{self.now.microsecond // 1000:03d}"


async def _replay(files: list[Path], db: FingerprintDatabase, clock: _RecordingClock) -> None:
    logger = logging.getLogger("music_assistant.Audio Recognizer: AudD + Local Fingerprints")
    with wave.open(str(files[0]), "rb") as first:
        fmt = PcmFormat(first.getframerate(), first.getnchannels(), 2, "s16le")
    start = datetime.strptime(files[0].name[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007
    clock.now = start

    async def fingerprint(pcm: bytes) -> dict[str, list[int]]:
        return await asyncio.to_thread(fingerprint_window, pcm, fmt)

    loop = asyncio.get_running_loop()
    engine = RecognitionEngine(
        fmt=fmt,
        gap_settings=GapSettings(),
        fingerprint=fingerprint,
        db=db,
        audd=None,  # offline: never call AudD
        publish=lambda _metadata: True,
        spawn=loop.create_task,
        logger=logger,
    )
    fed = 0
    for path in files:
        with wave.open(str(path), "rb") as reader:
            while chunk := reader.readframes(fmt.sample_rate):
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
    args = parser.parse_args()
    files = sorted(args.folder.glob("*.wav"))
    snapshot = args.folder / "fingerprints.sqlite3"
    if not files:
        sys.exit(f"no recordings in {args.folder}")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fingerprints.sqlite3"
        if snapshot.exists():
            shutil.copyfile(snapshot, db_path)
        db = FingerprintDatabase(db_path)
        clock = _RecordingClock()
        handler = logging.StreamHandler(stream := _Capture())
        handler.setFormatter(clock)
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        began = time.monotonic()
        for _, group in groupby(files, key=lambda p: p.name[:15]):
            asyncio.run(_replay(list(group), db, clock))
        lines = stream.lines
        print(f"Replayed {len(files)} segments in {time.monotonic() - began:.0f}s", file=sys.stderr)
    if args.report:
        from vote_report import parse, report  # noqa: PLC0415

        print(report(*parse(lines)))
    else:
        print("".join(lines), end="")


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
