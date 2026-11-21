"""
Replay every real recording offline and compare the outcome with the previous run.

    uv run python scripts/regression.py                  # all folders, compare with the last run
    uv run python scripts/regression.py --only 20261119  # folders whose name contains this
    uv run python scripts/regression.py --jobs 1         # gentler on the laptop (default 6)
    uv run python scripts/regression.py --baseline debug-recordings/regression/<run>.json

Run it after a change to the recognition itself (engine.py, fingerprints.py, matching in
database.py), not after docs, settings or provider glue. Each recording folder under
debug-recordings/ is replayed with scripts/replay_recording.py (perfect AudD from each session's
own log when there is one, the live defaults: postpone AudD, fast start 30 s), starting from the
database as it was when the recording began, so the result only changes when the code does.

Per folder it prints passages, identified locally / via AudD / not at all, AudD calls, the median
local time, *wrong* identifications (the replay named another track than the live log at that
moment) and the highest accidental vote count, plus every passage whose outcome changed. A
difference is not automatically a regression: a deliberate change should change results. The run
is saved to debug-recordings/regression/<timestamp>-<commit>.json (git-ignored, like the
recordings).

Gentle on the laptop: replays run at low priority (nice 15), one thread each, at most --jobs at a
time, and macOS is kept awake (caffeinate) only while they run. All recordings take roughly
15-25 minutes; plug in the charger for a full run.
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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

from vote_report import parse  # noqa: E402

DEFAULT_RECORDINGS = ROOT / "debug-recordings"
REPLAY_OPTIONS = ["--oracle", "--postpone-audd"]
FAST_START_SECONDS = 30  # as shipped (setting on by default)
ONE_THREAD = dict.fromkeys(
    ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"), "1"
)
SEGMENT_SUFFIXES = (".wav", ".flac")
# Recordings from this moment on were made with the current way of learning (fast start,
# time-consistent evidence, stored song starts and lengths; before it no session database had a
# stored song start). Older recordings replay against databases learned under older rules, so
# their numbers are shown apart: decide on the recent group (user, 2026-11-20).
RECENT_SINCE = "20261119-090000"


def is_recent(folder_name: str, since: str = RECENT_SINCE) -> bool:
    """Return whether a recording folder (named by its start) belongs to the recent group."""
    return folder_name[:15] >= since


@dataclass
class PassageResult:
    """The outcome of one replayed passage."""

    start: str  # HH:MM:SS on the recording's clock
    seconds: int | None
    track: str | None
    via: str | None
    after: int | None
    audd_calls: int
    max_other_votes: int
    truth: str | None = None  # what the live log says played then (None: unknown)

    @property
    def wrong(self) -> bool:
        """Return whether the replay named another track than the live log."""
        return self.track is not None and self.truth is not None and self.track != self.truth


@dataclass
class FolderResult:
    """All passages of one recording folder."""

    passages: list[PassageResult] = field(default_factory=list)
    seconds_taken: float = 0.0
    error: str | None = None

    def totals(self) -> dict[str, Any]:
        """Return the summary numbers that are compared between runs."""
        local = [p for p in self.passages if p.via == "local"]
        times = [p.after for p in local if p.after is not None]
        return {
            "passages": len(self.passages),
            "local": len(local),
            "audd": sum(1 for p in self.passages if p.via == "AudD"),
            "unidentified": sum(1 for p in self.passages if p.track is None),
            "audd_calls": sum(p.audd_calls for p in self.passages),
            "median_local_s": statistics.median(times) if times else None,
            "wrong": sum(1 for p in self.passages if p.wrong),
            "max_other_votes": max((p.max_other_votes for p in self.passages), default=0),
        }


def recording_folders(root: Path, only: str | None) -> list[Path]:
    """Return the folders that hold recorded segments, oldest first."""
    folders = [
        folder
        for folder in sorted(root.iterdir())
        if folder.is_dir()
        and folder.name != "regression"
        and any(p.suffix in SEGMENT_SUFFIXES for p in folder.glob("*_[0-9][0-9].*"))
    ]
    return [f for f in folders if only is None or only in f.name]


def _truths(folder: Path) -> Any:
    """
    Return a function (moment -> live track) from the folder's logs, or None.

    A session log (<start>.log) only answers for its own session: the one that started last
    before that moment. An exported MA log (any other name) answers for any moment.
    """
    from replay_recording import Oracle  # noqa: PLC0415 - imports the engine

    sessions: list[tuple[datetime, Any]] = []
    general: list[Any] = []
    for log in sorted(folder.glob("*.log")):
        try:
            started = datetime.strptime(log.stem, "%Y%m%d-%H%M%S")  # noqa: DTZ007 - log clock
        except ValueError:
            general.append(Oracle(log))
        else:
            sessions.append((started, Oracle(log)))
    if not sessions and not general:
        return None

    def truth(when: datetime) -> str | None:
        own = [oracle for started, oracle in sessions if started <= when]
        for oracle in [*own[-1:], *general]:
            if (track := oracle.track_at(when)) is not None:
                return str(track)
        return None

    return truth


def _same_track(folder: Path) -> Any:
    """Return a function (name, name -> bool) that applies the DB's key rules and aliases."""
    from audd_local_recognizer.database import ALIASES, track_key  # noqa: PLC0415

    def key(label: str) -> str:
        artist, _, title = label.partition(" - ")
        return track_key({"artist": artist, "title": title or artist})

    canonical: dict[str, str] = {}
    snapshot = folder / "fingerprints.sqlite3"
    if snapshot.exists():
        with sqlite3.connect(f"file:{snapshot}?immutable=1", uri=True) as db:
            for (meta,) in db.execute("SELECT metadata_json FROM tracks"):
                metadata = json.loads(meta)
                own = track_key(metadata)
                for alias in metadata.get(ALIASES, []):
                    canonical[alias] = own

    def same(a: str, b: str) -> bool:
        ka, kb = key(a), key(b)
        return canonical.get(ka, ka) == canonical.get(kb, kb)

    return same


def summarize(lines: list[str], truth: Any, same: Any = None) -> list[PassageResult]:
    """Turn replay log lines into passage results (truth kept only when it names another track)."""
    passages, _versions = parse(lines)
    results = []
    for p in passages:
        others = [c.votes for c in p.candidates if p.track is None or c.track != p.track]
        known = None
        if truth is not None and p.track is not None and p.after is not None:
            known = truth(p.started + timedelta(seconds=p.after))
            if known is not None and (known == p.track or (same and same(known, p.track))):
                known = p.track
        results.append(
            PassageResult(
                start=f"{p.started:%H:%M:%S}",
                seconds=p.seconds,
                track=p.track,
                via=p.via,
                after=p.after,
                audd_calls=p.audd_calls,
                max_other_votes=max(others, default=0),
                truth=known,
            )
        )
    return results


def replay(folder: Path, fast_start: float = FAST_START_SECONDS) -> FolderResult:
    """Replay one folder at low priority and summarize it."""
    command = [
        sys.executable,
        str(SCRIPTS / "replay_recording.py"),
        str(folder),
        *REPLAY_OPTIONS,
        "--fast-start",
        f"{fast_start:g}",
    ]
    if shutil.which("caffeinate"):
        command = ["caffeinate", "-i", *command]  # keep macOS awake while this replay runs
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
        return result
    result.passages = summarize(
        done.stdout.splitlines(keepends=True), _truths(folder), _same_track(folder)
    )
    return result


def group_totals(
    results: dict[str, FolderResult], since: str = RECENT_SINCE
) -> list[tuple[str, dict[str, Any]]]:
    """Return the totals of the recent and the older folders, apart."""
    groups: list[tuple[str, dict[str, Any]]] = []
    for label, recent in ((f"recent (since {since})", True), ("older", False)):
        passages = [
            p
            for name, result in results.items()
            if not result.error and is_recent(name, since) == recent
            for p in result.passages
        ]
        if passages:
            groups.append((label, FolderResult(passages=passages).totals()))
    return groups


def compare(
    current: dict[str, FolderResult],
    baseline: dict[str, FolderResult] | None,
    since: str = RECENT_SINCE,
) -> list[str]:
    """Render totals per folder and the passages whose outcome changed."""
    out = [
        (
            f"{'folder':16} {'pass':>4} {'local':>5} {'AudD':>4} {'none':>4} {'calls':>5} "
            f"{'med':>4} {'wrong':>5} {'acc':>3}  vs baseline"
        )
    ]
    changes: list[str] = []
    for name, result in current.items():
        if result.error:
            out.append(f"{name:16} ERROR: {result.error}")
            continue
        now = result.totals()
        before = baseline.get(name) if baseline else None
        then = before.totals() if before and not before.error else None
        keys = ("local", "audd", "unidentified", "audd_calls", "wrong", "max_other_votes")
        diff = [f"{k} {then[k]}→{now[k]}" for k in keys if then and now[k] != then[k]]
        med = f"{now['median_local_s']:.0f}s" if now["median_local_s"] is not None else "-"
        note = ", ".join(diff) if diff else ("same" if then else "new")
        out.append(
            f"{name:16} {now['passages']:>4} {now['local']:>5} {now['audd']:>4} "
            f"{now['unidentified']:>4} {now['audd_calls']:>5} {med:>4} {now['wrong']:>5} "
            f"{now['max_other_votes']:>3}  {note}"
        )
        changes.extend(
            f"  {name} {p.start}: WRONG {p.track} (live: {p.truth})"
            for p in result.passages
            if p.wrong
        )
        if before and not before.error:
            changes.extend(_passage_changes(name, before.passages, result.passages))
    out.append("")
    for label, now in group_totals(current, since):
        then = dict(group_totals(baseline, since)).get(label) if baseline else None
        keys = ("local", "audd", "unidentified", "audd_calls", "wrong", "max_other_votes")
        diff = [f"{k} {then[k]}→{now[k]}" for k in keys if then and now[k] != then[k]]
        med = f"{now['median_local_s']:.0f}s" if now["median_local_s"] is not None else "-"
        out.append(
            f"{label:16} {now['passages']:>4} {now['local']:>5} {now['audd']:>4} "
            f"{now['unidentified']:>4} {now['audd_calls']:>5} {med:>4} {now['wrong']:>5} "
            f"{now['max_other_votes']:>3}  {', '.join(diff) if diff else ''}"
        )
    out.append("Decide on the recent group; older recordings replay against older learning.")
    if changes:
        out += ["", "Changed or suspicious passages:", *changes]
    return out


def _describe(p: PassageResult) -> str:
    if p.track is None:
        return f"not identified, {p.audd_calls} AudD"
    return f"{p.track[:40]} ({p.via} @{p.after}s, {p.audd_calls} AudD)"


def _passage_changes(
    name: str, before: list[PassageResult], after: list[PassageResult]
) -> list[str]:
    old = {p.start: p for p in before}
    new = {p.start: p for p in after}
    lines = []
    for start in sorted(old.keys() | new.keys()):
        a, b = old.get(start), new.get(start)
        if a is None and b is not None:
            lines.append(f"  {name} {start}: new passage: {_describe(b)}")
        elif b is None and a is not None:
            lines.append(f"  {name} {start}: passage gone (was {_describe(a)})")
        elif a is not None and b is not None and _describe(a) != _describe(b):
            lines.append(f"  {name} {start}: {_describe(a)} → {_describe(b)}")
    return lines


def save(path: Path, results: dict[str, FolderResult], commit: str) -> None:
    """Write the run as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "commit": commit,
        "options": REPLAY_OPTIONS,
        "folders": {name: asdict(result) for name, result in results.items()},
    }
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def load(path: Path) -> dict[str, FolderResult]:
    """Read a saved run."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        name: FolderResult(
            passages=[PassageResult(**p) for p in folder["passages"]],
            seconds_taken=folder["seconds_taken"],
            error=folder["error"],
        )
        for name, folder in data["folders"].items()
    }


def _commit() -> str:
    head = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return f"{head}{'-dirty' if dirty else ''}" or "unknown"


def main() -> None:
    """Replay all recordings and compare with the baseline."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--recordings", type=Path, default=DEFAULT_RECORDINGS)
    parser.add_argument("--only", help="only folders whose name contains this text")
    parser.add_argument("--jobs", type=int, default=6, help="replays at the same time (default 6)")
    parser.add_argument("--baseline", type=Path, help="run to compare with (default: the last)")
    parser.add_argument(
        "--fast-start",
        type=float,
        default=FAST_START_SECONDS,
        metavar="SECONDS",
        help=f"experiment: fast start window (default {FAST_START_SECONDS}, 0 = off); a run with "
        "another value is saved with a tag and never becomes the default baseline",
    )
    parser.add_argument(
        "--recent-since",
        default=RECENT_SINCE,
        metavar="YYYYmmdd-HHMMSS",
        help=f"recordings from then on form the recent group (default {RECENT_SINCE})",
    )
    args = parser.parse_args()
    tag = "" if args.fast_start == FAST_START_SECONDS else f"-fs{args.fast_start:g}"

    folders = recording_folders(args.recordings, args.only)
    if not folders:
        sys.exit(f"no recordings in {args.recordings}")
    runs = args.recordings / "regression"
    previous = (
        sorted(p for p in runs.glob("*.json") if "-fs" not in p.stem) if runs.exists() else []
    )
    baseline_path = args.baseline or (previous[-1] if previous else None)
    baseline = load(baseline_path) if baseline_path else None

    print(f"Replaying {len(folders)} folders, {args.jobs} at a time, at low priority…", flush=True)
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        jobs = pool.map(lambda folder: replay(folder, args.fast_start), folders)
        replayed = dict(zip(folders, jobs, strict=True))
    results = {folder.name: replayed[folder] for folder in folders}
    commit = _commit()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005 - local file name
    saved = runs / f"{stamp}-{commit}{tag}.json"
    save(saved, results, commit)

    print(f"Done in {(time.monotonic() - began) / 60:.1f} min (commit {commit}).")
    print(f"Baseline: {baseline_path.name if baseline_path else 'none (first run)'}")
    print()
    print("\n".join(compare(results, baseline, args.recent_since)))
    print()
    print("columns: pass(ages), local / AudD / none = how identified, calls = AudD calls,")
    print("med = median local time, wrong = named another track than live, acc = highest")
    print("accidental vote count (accepting needs >= 3 twice, or >= 6 at once)")
    print(f"Saved: {saved}")


if __name__ == "__main__":
    main()
