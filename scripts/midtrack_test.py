"""
Measure how fast a known track is recognized when the needle drops in the middle of it.

    uv run python scripts/midtrack_test.py debug-recordings/<ts> [more folders]
    uv run python scripts/midtrack_test.py debug-recordings/<ts> --drops 5 --density-divisor 32

For every track a recording session played (from its session log) that the database already
knew when the session started, the test cuts the recording at --drops random moments in the
track (sample-exact, a fixed --seed: random so the cut never lines up with the learned windows by
luck, repeatable so runs can be compared),
puts a few seconds of turntable hiss in front (the needle coming down: a real gap, so the replay
opens a passage "after a gap"), and replays LISTEN seconds of it through the recognizer, without
AudD, against the database copy from the session's start. It reports when the track was
recognized locally (or not) and how far the song position the recognizer worked out is from the
real one (the live log's song start).

--density-divisor 32 also thins the database copy to 1/32, to see what a sparser fingerprint
does mid-track (where fast start cannot help: the dense part of the references is only the start
of each earlier play). Low priority (nice 15), one thread per replay.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import wave
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent))

from regression import _same_track  # noqa: E402
from replay_recording import (  # noqa: E402
    SEGMENT_SUFFIXES,
    Oracle,
    _RecordingClock,
    _replay,
    _segment_chunks,
    _segment_format,
    override_engine_constant,
)

from audd_local_recognizer.database import FingerprintDatabase  # noqa: E402
from audd_local_recognizer.fingerprints import _stable_bucket  # noqa: E402

HISS_SECONDS = 4
LOGGER = "music_assistant.Audio Recognizer: AudD + Local Fingerprints"


@dataclass
class Case:
    """One needle drop."""

    folder: Path
    session: str
    title: str
    offset: float  # seconds into the song
    drop_at: float  # session seconds where the cut starts
    true_position: float  # song position at the cut


def _cases(folder: Path, drops: int, listen: int, rng: np.random.Generator) -> list[Case]:
    cases: list[Case] = []
    files = sorted(p for p in folder.glob("*_[0-9][0-9].*") if p.suffix in SEGMENT_SUFFIXES)
    for session, _group in groupby(files, key=lambda p: p.name[:15]):
        log = folder / f"{session}.log"
        db = folder / f"{session}_db.sqlite3"
        if not log.exists() or not db.exists():
            continue
        oracle = Oracle(log)
        session_start = datetime.strptime(session, "%Y%m%d-%H%M%S")  # noqa: DTZ007
        known = _known_tracks(db)
        same = _same_track(folder)
        for start, end, title in oracle.spans:
            if not any(same(title, name) for name in known):
                continue  # its first play: nothing to recognize locally
            song_start = oracle.song_starts.get(start, start)
            earliest = max(20.0, (start - song_start).total_seconds() + 1)
            latest = (end - song_start).total_seconds() - listen - 5
            if latest <= earliest:
                continue
            for offset in sorted(rng.uniform(earliest, latest, size=drops)):
                cut = song_start + timedelta(seconds=float(offset))
                cases.append(
                    Case(
                        folder,
                        session,
                        title,
                        float(offset),
                        (cut - session_start).total_seconds(),
                        (cut - song_start).total_seconds() / oracle.speed_factor,
                    )
                )
    return cases


def _known_tracks(db: Path) -> list[str]:
    with sqlite3.connect(f"file:{db}?immutable=1", uri=True) as conn:
        rows = conn.execute("SELECT metadata_json FROM tracks").fetchall()
    import json  # noqa: PLC0415

    names = []
    for (meta,) in rows:
        metadata = json.loads(meta)
        names.append(f"{metadata.get('artist')} - {metadata.get('title')}")
    return names


def _cut(case: Case, listen: int, tmp: Path) -> Path:
    """Write hiss + LISTEN seconds of the session from drop_at as one WAV."""
    files = sorted(
        p for p in case.folder.glob(f"{case.session}_[0-9][0-9].*") if p.suffix in SEGMENT_SUFFIXES
    )
    rate, channels = _segment_format(files[0])
    pcm = bytearray()
    first = int(case.drop_at)
    for second, chunk in enumerate(c for f in files for c in _segment_chunks(f, rate)):
        if second > first + listen:
            break
        if second >= first:
            pcm.extend(chunk)
    frame = 2 * channels
    skip = round((case.drop_at - first) * rate) * frame  # cut inside the second, sample-exact
    pcm = pcm[skip : skip + listen * rate * frame]
    rng = np.random.default_rng(0)
    hiss = rng.integers(-3, 4, size=rate * channels * HISS_SECONDS, dtype=np.int16)
    out = tmp / f"{case.session}_01.wav"
    with wave.open(str(out), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(hiss.astype("<i2").tobytes() + bytes(pcm))
    return out


def _thin(db_path: Path, divisor: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.create_function("bucket", 1, _stable_bucket, deterministic=True)
        conn.execute("DELETE FROM landmarks WHERE bucket(fingerprint_hash) % ? != 0", (divisor,))


def run_case(
    case: Case, listen: int, divisor: int | None, overrides: tuple[str, ...] = ()
) -> dict[str, object]:
    """Replay one needle drop and return what happened."""
    os.nice(15)
    for assignment in overrides:
        override_engine_constant(assignment)
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        wav = _cut(case, listen, tmp)
        db_path = tmp / "db.sqlite3"
        shutil.copyfile(case.folder / f"{case.session}_db.sqlite3", db_path)
        if divisor:
            from audd_local_recognizer import engine  # noqa: PLC0415

            vars(engine)["DENSITY_DIVISOR"] = divisor
            _thin(db_path, divisor)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        clock = _RecordingClock()
        handler.setFormatter(clock)
        logger = logging.getLogger(LOGGER)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        options = {"postpone_audd": True, "fast_start_seconds": 30.0}
        asyncio.run(_replay([wav], FingerprintDatabase(db_path), clock, None, options))
    text = stream.getvalue()
    found = re.search(r"Identified (.+?) via local after (\d+)s", text)
    starts = re.findall(r"song started at (-?\d+)s", text)
    start = starts[-1] if starts else None  # the last one: a position decided later wins
    uncertain = "uncertain" in text and "decided later" not in text
    result: dict[str, object] = {
        "title": case.title,
        "offset": case.offset,
        "found": found.group(1) if found else None,
        "after": int(found.group(2)) if found else None,
        "uncertain": uncertain,
        "position_error": None,
    }
    if found and start and not uncertain:
        # the passage opens when the music resumes after the hiss; song start = -position
        result["position_error"] = -int(start) - case.true_position
    return result


def main() -> None:
    """Run every needle drop of the given folders."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("folders", type=Path, nargs="+")
    parser.add_argument("--drops", type=int, default=3, help="random needle drops per track")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--listen", type=int, default=60, help="seconds after the drop")
    parser.add_argument("--density-divisor", type=int, help="thin the DB and lookups to 1/N")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument(
        "--set", action="append", default=[], metavar="NAME=VALUE", help="engine constant"
    )
    args = parser.parse_args()
    rng = np.random.default_rng(args.seed)
    cases = [c for folder in args.folders for c in _cases(folder, args.drops, args.listen, rng)]
    print(f"{len(cases)} needle drops…", flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        results = list(
            pool.map(
                run_case,
                cases,
                [args.listen] * len(cases),
                [args.density_divisor] * len(cases),
                [tuple(args.set)] * len(cases),
            )
        )
    hits = wrong = uncertain = close = far = 0
    for case, r in zip(cases, results, strict=True):
        same = _same_track(case.folder)
        ok = r["found"] is not None and same(str(r["found"]), case.title)
        hits += ok
        if r["found"] is None:
            outcome = "not recognized"
        elif not ok:
            wrong += 1
            outcome = f"WRONG: {r['found']}"
        else:
            err = r["position_error"]
            if r["uncertain"] or err is None:
                uncertain += 1
            elif abs(float(str(err))) <= 5:
                close += 1
            else:
                far += 1
            if r["uncertain"]:
                where = "position uncertain → start 0"
            elif err is None:
                where = "position not worked out"
            else:
                where = f"position off {float(str(err)):+.0f}s"
            outcome = f"local @{r['after']}s, {where}"
        print(f"{case.title[:45]:45} drop @{case.offset:5.1f}s: {outcome}")
    times = sorted(int(str(r["after"])) for r in results if r["after"] is not None)
    median = times[len(times) // 2] if times else None
    print(f"\nrecognized {hits}/{len(cases)}, median {median}s after the drop")
    print(
        f"summary: drops={len(cases)} recognized={hits} wrong={wrong} median={median} "
        f"position_close={close} position_far={far} position_uncertain={uncertain}"
    )


if __name__ == "__main__":
    main()
