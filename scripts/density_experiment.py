"""
Compare fingerprint densities by replaying ALL recordings in time order from an EMPTY database.

    uv run python scripts/density_experiment.py                    # 1/32, 1/16 (shipped), 1/8
    uv run python scripts/density_experiment.py --divisors 16 8 4

The stored references only hold the shipped density, so another density cannot be tested on the
recordings' own database copies. Here every density starts empty and learns everything again:
the recording folders are replayed one after another into one database per density
(scripts/replay_recording.py --db), with a perfect AudD from each session's log (--oracle) and
the live defaults (postpone AudD, fast start 30 s). A track's first play is learned through the
oracle; every later play shows how well local recognition does at that density.

Per density it prints passages identified locally / via AudD / not at all, AudD calls, the median
local time, wrong identifications, the highest accidental vote count and the final database size.
The acceptance thresholds (votes) are tuned for 1/16: a denser fingerprint gives more votes for
both genuine and accidental matches, so watch "wrong" and "acc" next to "local".

Runs at low priority (nice 15, one thread per replay), one replay per density at a time; all
recordings take about as long as one regression run per density, in parallel. Results go to
debug-recordings/regression/density/<timestamp>.json (git-ignored), with the databases.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from regression import (  # noqa: E402
    DEFAULT_RECORDINGS,
    FAST_START_SECONDS,
    ONE_THREAD,
    REPLAY_OPTIONS,
    FolderResult,
    _same_track,
    _truths,
    recording_folders,
    summarize,
)


def run_density(
    folders: list[Path], divisor: int, db_path: Path, fast_start: float = FAST_START_SECONDS
) -> list[FolderResult]:
    """Replay every folder in order into one database at this density."""
    db_path.unlink(missing_ok=True)
    results: list[FolderResult] = []
    for folder in folders:
        command = [
            sys.executable,
            str(SCRIPTS / "replay_recording.py"),
            str(folder),
            *REPLAY_OPTIONS,
            "--fast-start",
            f"{fast_start:g}",
            "--density-divisor",
            str(divisor),
            "--db",
            str(db_path),
        ]
        if shutil.which("caffeinate"):
            command = ["caffeinate", "-i", *command]
        began = time.monotonic()
        done = subprocess.run(  # noqa: S603 - our own script, fixed arguments
            command,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **ONE_THREAD},
            preexec_fn=lambda: os.nice(15),
        )
        result = FolderResult(seconds_taken=time.monotonic() - began)
        if done.returncode != 0:
            result.error = (done.stderr.strip().splitlines() or ["failed"])[-1]
        else:
            result.passages = summarize(
                done.stdout.splitlines(keepends=True), _truths(folder), _same_track(folder)
            )
        results.append(result)
        print(f"  1/{divisor}: {folder.name} done", flush=True)
    return results


def _db_stats(path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{path}?immutable=1", uri=True) as db:
        landmarks = db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0]
        tracks = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    return {"bytes": path.stat().st_size, "landmarks": int(landmarks), "tracks": int(tracks)}


def _sum(results: list[FolderResult]) -> dict[str, Any]:
    passages = [p for r in results for p in r.passages]
    local = [p for p in passages if p.via == "local"]
    times = [p.after for p in local if p.after is not None]
    return {
        "passages": len(passages),
        "local": len(local),
        "audd": sum(1 for p in passages if p.via == "AudD"),
        "unidentified": sum(1 for p in passages if p.track is None),
        "audd_calls": sum(p.audd_calls for p in passages),
        "median_local_s": statistics.median(times) if times else None,
        "wrong": sum(1 for p in passages if p.wrong),
        "max_other_votes": max((p.max_other_votes for p in passages), default=0),
        "errors": [r.error for r in results if r.error],
    }


def main() -> None:
    """Run every density and print the comparison."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    parser.add_argument("--divisors", type=int, nargs="+", default=[32, 16, 8])
    parser.add_argument(
        "--fast-start",
        type=float,
        default=FAST_START_SECONDS,
        metavar="SECONDS",
        help="fast start window (0 = off: then only the tested density decides)",
    )
    args = parser.parse_args()
    folders = recording_folders(args.recordings, None)
    if not folders:
        sys.exit(f"no recordings in {args.recordings}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 - local file name
    out = args.recordings / "regression" / "density" / f"{stamp}-fs{args.fast_start:g}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Replaying {len(folders)} folders from an empty DB at 1/{args.divisors}…", flush=True)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(args.divisors)) as pool:
        futures = {
            d: pool.submit(run_density, folders, d, out / f"d{d}.sqlite3", args.fast_start)
            for d in args.divisors
        }
        runs = {d: future.result() for d, future in futures.items()}
    summary = {d: {**_sum(runs[d]), **_db_stats(out / f"d{d}.sqlite3")} for d in args.divisors}
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Done in {(time.monotonic() - began) / 60:.1f} min.\n")
    print("density   pass local AudD none calls  med wrong acc   landmarks     MB")
    for d, s in summary.items():
        med = f"{s['median_local_s']:.0f}s" if s["median_local_s"] is not None else "-"
        print(
            f"1/{d:<6} {s['passages']:5d} {s['local']:5d} {s['audd']:4d} {s['unidentified']:4d} "
            f"{s['audd_calls']:5d} {med:>4} {s['wrong']:5d} {s['max_other_votes']:3d} "
            f"{s['landmarks']:11d} {s['bytes'] / 1e6:6.1f}"
        )
        if s["errors"]:
            print(f"  errors: {s['errors']}")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
