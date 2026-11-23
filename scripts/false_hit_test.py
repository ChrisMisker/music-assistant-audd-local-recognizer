"""
Measure false local hits: replay every recording against a database that lacks the real track.

    uv run python scripts/false_hit_test.py [--only <ts>]... [--jobs N] [--db PATH]

Every recorded passage is cut into the recognizer's own lookup windows (real engine, no AudD, fast
start on) and looked up in one database (default: the newest snapshot in debug-recordings), without
references learned in the same recording. With the real track removed, whatever a rule then accepts
is a false hit ("removed"); with it present the time to the real track shows what the rule costs
("present"). One trace per passage holds everything the acceptance rules look at, so many rules and
smaller databases are evaluated on the same trace. Nothing is stored, no AudD, no Home Assistant.
Low priority (nice 15). Windows and traces are cached in debug-recordings/false-hit-cache.
"""

# ruff: noqa: S608 - placeholders only, our own database
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pickle
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from itertools import groupby
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from replay_recording import (  # noqa: E402
    SEGMENT_SUFFIXES,
    Oracle,
    _segment_chunks,
    _segment_format,
)

from audd_local_recognizer import engine as eng  # noqa: E402
from audd_local_recognizer.fingerprints import (  # noqa: E402
    DENSITY_DIVISOR,
    PANAKO_FRAME_SECONDS,
    PcmFormat,
    density_subset,
    fingerprint_window,
    score_reference,
)

RECORDINGS = ROOT / "debug-recordings"
CACHE = RECORDINGS / "false-hit-cache"
RECENT_SINCE = "20261119-090000"
FAST_SECONDS = 30.0
MIN_CLUSTER = 4  # clusters below this can never be accepted by any rule tested
# live identifications known to be wrong: their audio is of another, unlearned track
WRONG_LIVE = {"9f5618bc", "54925ef2", "0c177af4", "5d0ecf22"}


class _Capture(eng.RecognitionEngine):
    """The real engine, but a lookup only records its window (nothing is identified)."""

    captured: list[tuple[str, float, float, dict[str, list[int]]]]
    fed = 0

    async def _recognize(self, passage: Any, pcm: bytes, window_end: float) -> None:
        query = await self._fingerprint(pcm, self._divisor(window_end))
        self.captured.append((passage.id, self.fed - passage.seconds, window_end, query))


def windows_of(session: str, files: list[Path]) -> list[dict[str, Any]]:
    """Return the passages of a recording: start (seconds) and lookup windows (cached)."""
    CACHE.mkdir(exist_ok=True)
    cache = CACHE / f"{files[0].parent.name}_{session}.win"
    if cache.exists():
        cached: list[dict[str, Any]] = pickle.loads(cache.read_bytes())  # noqa: S301 - our own
        return cached
    rate, channels = _segment_format(files[0])
    fmt = PcmFormat(rate, channels, 2, "s16le")

    async def run() -> _Capture:
        async def fp(pcm: bytes, divisor: int) -> dict[str, list[int]]:
            return fingerprint_window(pcm, fmt, divisor)

        cap = _Capture(
            fmt=fmt,
            gap_settings=eng.GapSettings(),
            fingerprint=fp,
            db=None,  # type: ignore[arg-type]
            audd=None,
            publish=lambda _m: True,
            spawn=asyncio.get_running_loop().create_task,
            logger=logging.getLogger("quiet"),
            fast_start_seconds=FAST_SECONDS,
        )
        cap.captured = []
        for path in files:
            for chunk in _segment_chunks(path, rate):
                cap.feed(chunk)
                cap.fed += 1
                if cap.task is not None and not cap.task.done():
                    await cap.task
        return cap

    logging.getLogger("quiet").setLevel(logging.CRITICAL)
    cap = asyncio.run(run())
    out: dict[str, dict[str, Any]] = {}
    for pid, start, end, query in cap.captured:
        out.setdefault(pid, {"start": start, "windows": []})["windows"].append((end, query))
    result = list(out.values())
    cache.write_bytes(pickle.dumps(result))
    return result


@dataclass
class Trace:
    """What the acceptance rules see, per lookup: per track best window, and all clusters."""

    ends: list[float] = field(default_factory=list)
    # per lookup: {track: (votes, ratio)} for tracks with >= 3 votes in this window
    singles: list[dict[int, tuple[int, float]]] = field(default_factory=list)
    # per lookup: {(track, ref passage): [(votes, sparse votes, lookups) of its best clusters]}
    clusters: list[dict[tuple[int, str], tuple[tuple[int, int, int], ...]]] = field(
        default_factory=list
    )


def _rows(db: sqlite3.Connection, hashes: list[str], skip: set[str]) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for i in range(0, len(hashes), 400):
        part = hashes[i : i + 400]
        rows += db.execute(
            "SELECT l.reference_id, r.track_id, r.passage_id, r.passage_offset_seconds, "
            "l.fingerprint_hash, l.position FROM landmarks l JOIN reference_windows r "
            f"ON r.id = l.reference_id WHERE l.fingerprint_hash IN ({','.join('?' * len(part))})",
            part,
        ).fetchall()
    return [r for r in rows if str(r[2])[:8] not in skip]


def trace_of(  # noqa: C901 - one pass over the lookups
    db: sqlite3.Connection,
    windows: list[tuple[float, Any]],
    skip: set[str],
    fast: bool,  # noqa: FBT001
) -> Trace:
    """Look every window up and keep, per lookup, what the rules need (same maths as the engine)."""
    trace = Trace()
    bins: dict[tuple[int, str], dict[int, dict[str, set[int]]]] = {}
    best: dict[tuple[int, str], dict[int, tuple[int, int, int]]] = {}
    for number, (end, dense_query) in enumerate(windows, 1):
        query = dense_query if fast else density_subset(dense_query, DENSITY_DIVISOR)
        sparse = set(density_subset(query, DENSITY_DIVISOR))
        rows = _rows(db, list(query), skip)
        per_ref: dict[int, tuple[int, list[tuple[str, int]]]] = {}
        touched: dict[tuple[int, str], set[int]] = defaultdict(set)
        for ref, track, pid, offset, fp, pos in rows:
            per_ref.setdefault(ref, (track, []))[1].append((fp, pos))
            for q in query.get(fp, ()):
                shift = offset + (pos - q) * PANAKO_FRAME_SECONDS - (end - eng.SAMPLE_SECONDS)
                b = round(shift / eng.COHERENCE_BIN_SECONDS)
                key = (track, pid)
                bins.setdefault(key, {}).setdefault(b, {}).setdefault(fp, set()).add(number)
                touched[key].add(b)
        singles: dict[int, tuple[int, float]] = {}
        for track, ref_rows in per_ref.values():
            _, _, votes, ratio = score_reference(query, ref_rows)
            if votes >= 3 and (votes, ratio) > singles.get(track, (0, 0.0)):
                singles[track] = (votes, ratio)
        for key, centers in touched.items():
            near = {
                c + d for c in centers for d in range(-eng.COHERENCE_BINS, eng.COHERENCE_BINS + 1)
            }
            for center in near:
                merged: dict[str, set[int]] = {}
                for index in range(center - eng.COHERENCE_BINS, center + eng.COHERENCE_BINS + 1):
                    for fp, seen in bins[key].get(index, {}).items():
                        merged.setdefault(fp, set()).update(seen)
                if len(merged) < MIN_CLUSTER:
                    continue
                best.setdefault(key, {})[center] = (
                    len(merged),
                    sum(1 for fp in merged if fp in sparse or _is_sparse(fp)),
                    len(set().union(*merged.values())),
                )
        trace.ends.append(end)
        trace.singles.append(singles)
        trace.clusters.append(
            {
                k: tuple({max(v.values()), max(v.values(), key=lambda c: (c[1], c[0]))})
                for k, v in best.items()
            }
        )
    return trace


def _is_sparse(fp: str) -> bool:
    return int(fp[:16], 16) % DENSITY_DIVISOR == 0


@dataclass(frozen=True)
class Rule:
    """One set of acceptance thresholds."""

    name: str
    strong: int = 6
    minimum: int = 6  # time-consistent votes
    lookups: int = 2
    sparse_min: int = 0  # ...of which this many must be ordinary (1/16) fingerprints
    early_minimum: int = 0  # other minimum while window_end <= early_until
    early_until: float = 30.0
    confirm_cluster: int = 5
    fast: bool = True
    large: int = 0  # a cluster this big needs no ordinary votes (0 = off)
    single: int = 0  # a cluster this big needs only 1 lookup (0 = off)
    single_sparse: int = 4  # ...if this many of its votes are ordinary


RULES = [
    Rule("shipped (6, >=4 sparse, large 10, single 20)", sparse_min=4, large=10, single=20),
    Rule("without single (until 2026-11-23)", sparse_min=4, large=10),
    Rule("single 15", sparse_min=4, large=10, single=15),
    Rule("single 30", sparse_min=4, large=10, single=30),
    Rule("6 (before 2026-11-22)"),
    Rule("time-consistent 7", minimum=7),
    Rule("time-consistent 8", minimum=8),
    Rule("8 in first 30 s", early_minimum=8),
    Rule("8 in first 60 s", early_minimum=8, early_until=60.0),
    Rule("6, 3 lookups", lookups=3),
    Rule("7, 3 lookups", minimum=7, lookups=3),
    Rule("6, >=4 sparse", sparse_min=4),
    Rule("6, >=5 sparse", sparse_min=5),
    Rule("strong 8", strong=8),
    Rule("strong 8 + 8 in first 60 s", strong=8, early_minimum=8, early_until=60.0),
    Rule("no fast start", fast=False),
]


def accept(trace: Trace, rule: Rule, keep: Any) -> tuple[int, str, float, int] | None:
    """Return (track, how, window end, votes) of the first track a rule accepts, or None."""
    pending, count = None, 0
    for i, end in enumerate(trace.ends):
        lookups = i + 1
        singles = {t: v for t, v in trace.singles[i].items() if keep(t)}
        clusters = [(t, c) for (t, _p), cs in trace.clusters[i].items() if keep(t) for c in cs]
        if singles:
            track, (votes, ratio) = max(singles.items(), key=lambda kv: kv[1])
            if votes >= rule.strong and ratio >= eng.STRONG_RATIO:
                return track, "strong", end, votes
            if votes >= eng.CONFIRM_VOTES and ratio >= eng.CONFIRM_RATIO:
                count = count + 1 if pending == track else 1
                pending = track
                if count >= eng.CONFIRMATIONS_REQUIRED:
                    top = max((c for t, c in clusters if t == track), default=(0, 0, 0))
                    if top[0] >= rule.confirm_cluster and top[2] >= eng.CONFIRMATIONS_REQUIRED:
                        return track, "confirmed", end, top[0]
                    count = eng.CONFIRMATIONS_REQUIRED - 1
            else:
                pending, count = None, 0
        else:
            pending, count = None, 0
        floor = max(
            rule.minimum, eng.COHERENT_VOTES_BASE + lookups / eng.COHERENT_LOOKUPS_PER_EXTRA_VOTE
        )
        if rule.early_minimum and end <= rule.early_until:
            floor = max(floor, rule.early_minimum)
        ok = [
            (c, t)
            for t, c in clusters
            if (
                c[0] >= floor
                and c[2] >= rule.lookups
                and (c[1] >= rule.sparse_min or (rule.large and c[0] >= rule.large))
            )
            or (rule.single and c[0] >= rule.single and c[1] >= rule.single_sparse)
        ]
        if ok:
            c, t = max(ok)
            return t, "time-consistent", end, c[0]
    return None


def _norm(text: str) -> str:
    text = re.sub(r"\(.*?\)|\[.*?\]| - live.*$| - .*remaster.*$", " ", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text.replace("in'", "ing")).strip()


def same_song(label: str, artist: str, title: str) -> bool:
    """Return whether a live label names (a version of) this track."""
    a, _, t = label.partition(" - ")
    x, y = _norm(t), _norm(title)
    near = (
        x == y or SequenceMatcher(None, x, y).ratio() >= 0.8 or x.startswith(y) or y.startswith(x)
    )
    words = {w for w in _norm(a).split() if len(w) > 2}
    return bool(x and y and near and (words & set(_norm(artist).split()) or x == y))


def run_session(job: tuple[str, str, list[str], str]) -> list[dict[str, Any]]:
    """Trace all passages of one recording session against the database."""
    folder, session, names, db_path = job
    os.nice(15)
    base = Path(folder)
    files = [base / n for n in names]
    passages = windows_of(session, files)
    log = base / f"{session}.log"
    spans: list[tuple[datetime, datetime, str, str]] = []
    skip: set[str] = set()
    if log.exists():
        oracle = Oracle(log)
        text = log.read_text(encoding="utf-8", errors="replace")
        skip = set(re.findall(r"[Pp]assage (\w{8})", text))
        wrong = {
            p: t
            for t, p in re.findall(r"Identified (.+?) via \w+ after \d+s \(passage (\w{8})", text)
            if p in WRONG_LIVE
        }
        spans = [(s, e, t, "wrong" if t in wrong.values() else "") for s, e, t in oracle.spans]
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tracks = {
        i: json.loads(m) for i, m in db.execute("SELECT id, metadata_json FROM tracks").fetchall()
    }
    began = datetime.strptime(session, "%Y%m%d-%H%M%S")  # noqa: DTZ007
    results = []
    for p in passages:
        a = began + timedelta(seconds=p["start"])
        b = a + timedelta(seconds=p["windows"][-1][0])
        live = [(t, w) for s, e, t, w in spans if min(b, e) - max(a, s) > timedelta(seconds=5)]
        labels = [t for t, w in live if not w]
        truth = {
            i
            for i, m in tracks.items()
            if any(
                same_song(
                    lab,
                    str(m.get("artist")),
                    str(m.get("title")),
                )
                for lab in labels
            )
        }
        others = [
            pid
            for (pid,) in db.execute(
                "SELECT DISTINCT passage_id FROM reference_windows WHERE track_id IN "
                f"({','.join('?' * len(truth))})",
                list(truth),
            )
            if pid[:8] not in skip
        ]
        traces = {f: trace_of(db, p["windows"], skip, f) for f in (True, False)}
        results.append(
            {
                "session": session,
                "folder": base.name,
                "start": a,
                "length": p["windows"][-1][0],
                "labels": labels,
                "wrong_live": any(w for _t, w in live),
                "truth": truth,
                "learned_elsewhere": bool(others),
                "traces": traces,
            }
        )
    return results


def report(results: list[dict[str, Any]], tracks: dict[int, str]) -> str:  # noqa: C901, PLR0912
    """Return the tables for recent and older recordings."""
    out: list[str] = []
    ids = sorted(tracks)
    for group, recent in (("recent", True), ("older", False)):
        rows = [
            r
            for r in results
            if (r["session"] >= RECENT_SINCE) is recent and r["traces"][True].ends
        ]
        known = [r for r in rows if r["labels"] or r["wrong_live"]]
        out.append(
            f"\n## {group}: {len(rows)} passages with a lookup, {len(known)} with a known track, "
            f"{sum(r['length'] for r in known) / 3600:.1f} h"
        )
        out.append("false hits with the real track removed (passages), per share of the database")
        out.append(
            f"{'rule':30} {'100%':>6} {'50%':>6} {'25%':>6} | present: ok  missed wrong median s"
        )
        for rule in RULES:
            cells = []
            for share in (1.0, 0.5, 0.25):
                seeds = 1 if share == 1.0 else 5
                total = 0
                for seed in range(seeds):
                    chosen = set(random.Random(seed).sample(ids, round(len(ids) * share)))  # noqa: S311
                    for r in known:
                        tr = r["traces"][rule.fast]
                        keep = lambda t, r=r, c=chosen: t in c and t not in r["truth"]  # noqa: E731
                        total += accept(tr, rule, keep) is not None
                cells.append(total / seeds)
            ok = missed = wrong = 0
            times = []
            for r in known:
                if not r["learned_elsewhere"]:
                    continue
                got = accept(r["traces"][rule.fast], rule, lambda _t: True)
                if got is None:
                    missed += 1
                elif got[0] in r["truth"]:
                    ok += 1
                    times.append(got[2])
                else:
                    wrong += 1
            times.sort()
            med = times[len(times) // 2] if times else 0
            out.append(
                f"{rule.name:30} {cells[0]:6.1f} {cells[1]:6.1f} {cells[2]:6.1f} | "
                f"{ok:11} {missed:6} {wrong:5} {med:6.0f}"
            )
        out.append("\nfalse hits of rule 'now' (real track removed, whole database):")
        magnets: Counter[str] = Counter()
        for r in known:
            got = accept(r["traces"][True], RULES[0], lambda t, r=r: t not in r["truth"])
            if got:
                magnets[tracks[got[0]]] += 1
                out.append(
                    f"  {r['session']} {r['start']:%H:%M:%S} real={'/'.join(r['labels']) or '?'} -> "
                    f"{tracks[got[0]]} ({got[1]}, {got[3]} votes at {got[2]:.0f}s)"
                )
        out.append(f"magnets: {magnets.most_common(8)}")
        hist: Counter[int] = Counter()
        for r in known:
            tops = [
                c[0]
                for (t, _p), cs in (
                    r["traces"][True].clusters[-1] if r["traces"][True].clusters else {}
                ).items()
                if t not in r["truth"]
                for c in cs
            ]
            hist[max(tops, default=0)] += 1
        out.append(
            f"highest accidental cluster per passage (<{MIN_CLUSTER} counts as 0): {dict(sorted(hist.items()))}"
        )
    return "\n".join(out)


def main() -> None:
    """Run the test over the recordings."""
    parser = argparse.ArgumentParser(description="measure false local hits")
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--out", type=Path, default=CACHE / "report.txt")
    args = parser.parse_args()
    CACHE.mkdir(exist_ok=True)
    folders = sorted(f for f in RECORDINGS.iterdir() if re.fullmatch(r"\d{8}-\d{6}", f.name))
    db = args.db or max(
        f / "fingerprints.sqlite3" for f in folders if (f / "fingerprints.sqlite3").exists()
    )
    jobs = []
    for folder in folders:
        if args.only and folder.name not in args.only:
            continue
        files = sorted(p for p in folder.glob("*_[0-9][0-9].*") if p.suffix in SEGMENT_SUFFIXES)
        for session, group in groupby(files, key=lambda p: p.name[:15]):
            jobs.append((str(folder), session, [p.name for p in group], str(db)))
    print(f"{len(jobs)} sessions, database {db}", flush=True)
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(args.jobs) as pool:
        for n, part in enumerate(pool.map(run_session, jobs), 1):
            results += part
            print(f"{n}/{len(jobs)} {len(results)} passages", flush=True)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tracks = {
        i: f"{json.loads(m).get('artist')} - {json.loads(m).get('title')}"
        for i, m in con.execute("SELECT id, metadata_json FROM tracks")
    }
    (CACHE / "results.pkl").write_bytes(pickle.dumps((results, tracks)))
    text = report(results, tracks)
    args.out.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
