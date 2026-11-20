"""
Switch the engine's features off one at a time (and in combinations) and compare: an overnight run.

    uv run python scripts/ablation.py                    # all variants, 5 at a time
    uv run python scripts/ablation.py --only baseline no-gap-prior --jobs 2
    uv run python scripts/ablation.py --list

Each variant replays ALL recordings in time order from an EMPTY database (like
scripts/density_experiment.py), with a perfect AudD from each session's log (--oracle), so it
also shows effects that build up across sessions (learning, pruning, observed lengths). A variant
is the shipped engine with some features switched off through engine constants (--set NAME=VALUE
of scripts/replay_recording.py) or replay options. Then the needle-drop test
(scripts/midtrack_test.py, against each session's own database copy) runs for every variant that
touches local acceptance or position.

Per variant: passages identified locally / via AudD / not at all, AudD calls, median local time,
wrong identifications, the highest accidental vote count, soft boundaries, uncertain positions,
tails moved, lengths adopted, the final database size, and the needle-drop results. Compare each
row with "baseline": a feature earns its place when switching it off makes something worse.

Runs at low priority (nice 15, one thread per replay). Results: debug-recordings/regression/
ablation/<timestamp>/ (summary.json, report.txt, per-variant logs and databases; git-ignored).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from regression import (  # noqa: E402
    DEFAULT_RECORDINGS,
    ONE_THREAD,
    _same_track,
    _truths,
    recording_folders,
    summarize,
)

BIG = "1000000"


@dataclass
class Variant:
    """The shipped engine with some features switched off."""

    name: str
    about: str
    sets: list[str] = field(default_factory=list)
    fast_start: float = 30.0
    postpone: bool = True
    needle_drops: bool = False  # also run the needle-drop test


VARIANTS = [
    Variant("baseline", "everything as shipped", needle_drops=True),
    Variant("no-fast-start", "no denser fingerprints in the first 30 s", fast_start=0.0),
    Variant("no-postpone", "AudD is not postponed for a local candidate", postpone=False),
    Variant(
        "no-time-consistent",
        "no acceptance from evidence that lines up in time over the passage",
        [f"COHERENT_VOTES={BIG}"],
        needle_drops=True,
    ),
    Variant(
        "no-confirm-coherent",
        "two weak checks need no time-consistent cluster (before 2026-11-19)",
        ["CONFIRM_COHERENT_VOTES=0"],
        needle_drops=True,
    ),
    Variant(
        "no-gap-prior",
        "a weak far local position after a gap is trusted (before 2026-11-19)",
        [f"LOCAL_START_PRIOR_SECONDS={BIG}"],
        needle_drops=True,
    ),
    Variant(
        "no-uncertain-end-guard",
        "after a soft boundary learning stops 5 s (not 20 s) before an unmeasured end",
        ["UNCERTAIN_END_GUARD_SECONDS=5"],
    ),
    Variant(
        "no-known-end-learning",
        "learning stops after 600 s even when the end is known (before 2026-11-19)",
        ["MAX_LEARN_SECONDS_KNOWN_END=600"],
    ),
    Variant(
        "acceptance-0.8",
        "local acceptance as in 0.8: no confirm-coherent, no gap prior",
        ["CONFIRM_COHERENT_VOTES=0", f"LOCAL_START_PRIOR_SECONDS={BIG}"],
        needle_drops=True,
    ),
    Variant(
        "weak-checks-only",
        "neither time-consistent evidence nor its use in confirmation",
        [f"COHERENT_VOTES={BIG}", "CONFIRM_COHERENT_VOTES=0"],
        needle_drops=True,
    ),
    Variant(
        "no-speedups",
        "neither fast start nor postponing AudD",
        fast_start=0.0,
        postpone=False,
    ),
    Variant(
        "all-0.9-off",
        "every 0.9 safeguard off (confirm-coherent, gap prior, end guard, long learning)",
        [
            "CONFIRM_COHERENT_VOTES=0",
            f"LOCAL_START_PRIOR_SECONDS={BIG}",
            "UNCERTAIN_END_GUARD_SECONDS=5",
            "MAX_LEARN_SECONDS_KNOWN_END=600",
        ],
        needle_drops=True,
    ),
    Variant(
        "stricter-strong",
        "a single window needs 6 votes (not 5) to be accepted at once",
        ["STRONG_VOTES=6"],
        needle_drops=True,
    ),
]

MARKERS = {
    "soft_boundaries": "Track boundary (no gap)",
    "uncertain_positions": "uncertain (song start",
    "tails_moved": "of the previous track's windows past its real end",
    "lengths_adopted": "observed length",  # counted as "adopted" below
    "learning_capped": "stopped learning after",
}


def _options(variant: Variant) -> list[str]:
    options = ["--oracle", "--fast-start", f"{variant.fast_start:g}"]
    if variant.postpone:
        options.append("--postpone-audd")
    for assignment in variant.sets:
        options += ["--set", assignment]
    return options


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    if shutil.which("caffeinate"):
        command = ["caffeinate", "-i", *command]
    return subprocess.run(  # noqa: S603 - our own scripts, fixed arguments
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **ONE_THREAD},
        preexec_fn=lambda: os.nice(15),
    )


def run_chain(variant: Variant, folders: list[Path], out: Path) -> dict[str, Any]:
    """Replay every folder in order into one empty database; return the totals."""
    db_path = out / f"{variant.name}.sqlite3"
    db_path.unlink(missing_ok=True)
    passages = []
    counts = dict.fromkeys(MARKERS, 0)
    errors = []
    with (out / f"{variant.name}.log").open("w", encoding="utf-8") as log:
        for folder in folders:
            command = [sys.executable, str(SCRIPTS / "replay_recording.py"), str(folder)]
            done = _run([*command, *_options(variant), "--db", str(db_path)])
            log.write(f"===== {folder.name}\n{done.stdout}")
            if done.returncode != 0:
                errors.append(f"{folder.name}: {(done.stderr.strip().splitlines() or ['?'])[-1]}")
                continue
            lines = done.stdout.splitlines(keepends=True)
            passages += summarize(lines, _truths(folder), _same_track(folder))
            for key, marker in MARKERS.items():
                hits = [line for line in lines if marker in line]
                if key == "lengths_adopted":
                    hits = [line for line in hits if "adopted" in line]
                counts[key] += len(hits)
            print(f"  {variant.name}: {folder.name} done", flush=True)
    local = [p for p in passages if p.via == "local"]
    times = [p.after for p in local if p.after is not None]
    with sqlite3.connect(f"file:{db_path}?immutable=1", uri=True) as db:
        landmarks = db.execute("SELECT COUNT(*) FROM landmarks").fetchone()[0]
    return {
        "passages": len(passages),
        "local": len(local),
        "audd": sum(1 for p in passages if p.via == "AudD"),
        "unidentified": sum(1 for p in passages if p.track is None),
        "audd_calls": sum(p.audd_calls for p in passages),
        "median_local_s": statistics.median(times) if times else None,
        "wrong": sum(1 for p in passages if p.wrong),
        "wrong_detail": [f"{p.start} {p.track} (live: {p.truth})" for p in passages if p.wrong],
        "max_other_votes": max((p.max_other_votes for p in passages), default=0),
        **counts,
        "landmarks": int(landmarks),
        "mb": round(db_path.stat().st_size / 1e6, 1),
        "errors": errors,
    }


def run_needle_drops(variant: Variant, folders: list[Path], out: Path, jobs: int) -> dict[str, Any]:
    """Run the needle-drop test with this variant's settings; return its summary line."""
    command = [sys.executable, str(SCRIPTS / "midtrack_test.py"), *map(str, folders)]
    command += ["--jobs", str(jobs)]
    for assignment in variant.sets:
        command += ["--set", assignment]
    done = _run(command)
    (out / f"{variant.name}.needle.txt").write_text(done.stdout + done.stderr, encoding="utf-8")
    found = re.search(r"^summary: (.+)$", done.stdout, re.MULTILINE)
    if not found:
        return {"error": (done.stderr.strip().splitlines() or ["no summary"])[-1]}
    return {k: v for k, _, v in (item.partition("=") for item in found.group(1).split())}


def _report(summary: dict[str, dict[str, Any]], variants: list[Variant]) -> str:
    rows = [
        (
            "variant                  pass local AudD none calls  med wrong acc soft unc moved"
            " adopt cap     MB | drops: ok wrong close far unc"
        ),
    ]
    for v in variants:
        s = summary[v.name]
        med = f"{s['median_local_s']:.0f}s" if s.get("median_local_s") is not None else "-"
        n = s.get("needle", {})
        drops = (
            f"{n.get('recognized', '-'):>3}/{n.get('drops', '-'):<3} {n.get('wrong', '-'):>4} "
            f"{n.get('position_close', '-'):>5} {n.get('position_far', '-'):>3} "
            f"{n.get('position_uncertain', '-'):>3}"
            if n
            else ""
        )
        rows.append(
            f"{v.name:24} {s['passages']:4d} {s['local']:5d} {s['audd']:4d} "
            f"{s['unidentified']:4d} {s['audd_calls']:5d} {med:>4} {s['wrong']:5d} "
            f"{s['max_other_votes']:3d} {s['soft_boundaries']:4d} "
            f"{s['uncertain_positions']:3d} {s['tails_moved']:5d} {s['lengths_adopted']:5d} "
            f"{s['learning_capped']:3d} {s['mb']:6.1f} | {drops}"
        )
        rows.extend(f"    WRONG {detail}" for detail in s["wrong_detail"])
        rows.extend(f"    ERROR {error}" for error in s["errors"])
    rows.append("")
    rows += [f"{v.name}: {v.about}" for v in variants]
    return "\n".join(rows) + "\n"


def main() -> None:
    """Run the variants and write the comparison."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    parser.add_argument("--only", nargs="+", metavar="VARIANT")
    parser.add_argument("--jobs", type=int, default=5, help="variants replayed at the same time")
    parser.add_argument("--no-needle-drops", action="store_true")
    parser.add_argument("--list", action="store_true", help="list the variants and exit")
    args = parser.parse_args()
    if args.list:
        for v in VARIANTS:
            print(f"{v.name:24} {v.about}  {' '.join(_options(v))}")
        return
    variants = [v for v in VARIANTS if not args.only or v.name in args.only]
    if not variants:
        sys.exit("no such variant (see --list)")
    folders = recording_folders(args.recordings, None)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 - local file name
    out = args.recordings / "regression" / "ablation" / stamp
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(variants)} variants x {len(folders)} folders, {args.jobs} at a time…", flush=True)
    began = time.monotonic()
    summary: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {v.name: pool.submit(run_chain, v, folders, out) for v in variants}
        for name, future in futures.items():
            summary[name] = future.result()
            (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Replays done in {(time.monotonic() - began) / 60:.0f} min.", flush=True)
    if not args.no_needle_drops:
        for v in variants:
            if v.needle_drops:
                summary[v.name]["needle"] = run_needle_drops(v, folders, out, args.jobs + 1)
                (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
                print(f"  {v.name}: needle drops done", flush=True)
    report = _report(summary, variants)
    (out / "report.txt").write_text(report, encoding="utf-8")
    print(f"Done in {(time.monotonic() - began) / 60:.0f} min.\n\n{report}\nSaved: {out}")


if __name__ == "__main__":
    main()
