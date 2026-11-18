"""
Summarize recognition behaviour from the provider's log lines (read from stdin).

Per passage: how and when it was identified, AudD calls, and the votes of every local
candidate. Then totals and the vote distribution of the identified track versus other tracks,
which shows the margin between real matches and accidental collisions.

Usage: scripts/vote-report.sh [--since 3h]   (or: <log> | uv run python scripts/vote_report.py)
"""

from __future__ import annotations

import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \w+ \(.*?\) \[.*?\] (.*)$")
STARTED = re.compile(r"Passage (\w{8}) started")
CLOSED = re.compile(r"Track boundary: .*?passage (\w{8}) closed after (\d+)s")
LOCAL = re.compile(
    r"LOCAL (HIT|CANDIDATE): (.*?) votes=(\d+)(?: acc=\d+)? ratio=[\d.]+ .*?"
    r"(?: at=(\d+)s)? -> (.*?) \(passage (\w{8})\)$"
)
AUDD_ATTEMPT = re.compile(r"AudD attempt \d+/\d+ .*\(passage (\w{8})\)")
AUDD_HIT = re.compile(r"AudD HIT: (.*) \(passage (\w{8}), attempt \d+\)")
IDENTIFIED = re.compile(r"Identified (.*) via (\w+) after (\d+)s \(passage (\w{8})")
LOADED = re.compile(r"AudD \+ Local Fingerprints v([\d.]+):")
MIN_PASSAGE_SECONDS = 12


@dataclass
class Candidate:
    """One local candidate of a lookup."""

    track: str
    votes: int
    decision: str
    after: int  # seconds into the passage


@dataclass
class Passage:
    """What happened in one passage."""

    passage_id: str
    started: datetime
    seconds: int | None = None
    track: str | None = None
    via: str | None = None
    after: int | None = None
    audd_calls: int = 0
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class Log:
    """Parsed state while reading lines."""

    passages: dict[str, Passage] = field(default_factory=dict)
    versions: list[str] = field(default_factory=list)

    def line(self, when: datetime, text: str) -> None:  # noqa: C901 - flat dispatch per line type
        """Apply one log line."""
        if loaded := LOADED.search(text):
            self.versions.append(f"{when:%m-%d %H:%M} v{loaded.group(1)}")
        elif started := STARTED.search(text):
            self.passages[started.group(1)] = Passage(started.group(1), when)
        elif closed := CLOSED.search(text):
            if passage := self.passages.get(closed.group(1)):
                passage.seconds = int(closed.group(2))
        elif local := LOCAL.search(text):
            self._local(when, local)
        elif attempt := AUDD_ATTEMPT.search(text):
            if passage := self.passages.get(attempt.group(1)):
                passage.audd_calls += 1
        elif identified := IDENTIFIED.search(text):
            if passage := self.passages.get(identified.group(4)):
                passage.track, passage.via = identified.group(1), identified.group(2)
                passage.after = int(identified.group(3))
        elif hit := AUDD_HIT.search(text):
            # logs from before v0.7 have no "Identified" line
            passage = self.passages.get(hit.group(2))
            if passage and passage.track is None:
                passage.track, passage.via = hit.group(1), "AudD"
                passage.after = _seconds(passage, when)

    def _local(self, when: datetime, local: re.Match[str]) -> None:
        passage = self.passages.get(local.group(6))
        if passage is None:
            return
        after = int(local.group(4)) if local.group(4) else _seconds(passage, when)
        passage.candidates.append(
            Candidate(local.group(2), int(local.group(3)), local.group(5), after)
        )
        if local.group(1) == "HIT" and passage.track is None:  # also covers pre-v0.7 logs
            passage.track, passage.via, passage.after = local.group(2), "local", after


def _seconds(passage: Passage, when: datetime) -> int:
    return int((when - passage.started).total_seconds())


def parse(lines: list[str]) -> tuple[list[Passage], list[str]]:
    """Parse log lines into passages, plus the provider versions seen."""
    log = Log()
    for raw in lines:
        match = LINE.match(ANSI.sub("", raw.rstrip("\n")))
        if match:
            when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 - log time
            log.line(when, match.group(2))
    relevant = [
        p
        for p in log.passages.values()
        if p.seconds is None or p.seconds >= MIN_PASSAGE_SECONDS or p.candidates or p.audd_calls
    ]
    return sorted(relevant, key=lambda p: p.started), log.versions


def report(passages: list[Passage], versions: list[str]) -> str:
    """Render the report."""
    out: list[str] = []
    if versions:
        out.append("Provider loaded: " + ", ".join(versions))
    out.append("")
    out.append(f"{'start':8} {'passage':8} {'len':>5}  {'result':44} {'AudD':>4}  local votes")
    for p in passages:
        length = f"{p.seconds}s" if p.seconds is not None else "…"
        result = f"{p.track[:30]} ({p.via} @{p.after}s)" if p.track else "- not identified"
        votes = " ".join(
            f"{c.votes}{'*' if c.decision.startswith('acc') else ''}"
            + ("" if p.track is None or c.track == p.track else "x")
            for c in p.candidates
        )
        out.append(
            f"{p.started:%H:%M:%S} {p.passage_id} {length:>5}  {result:44} {p.audd_calls:>4}  {votes}"
        )
    out.append("")
    out.append("votes: * = accepted, x = candidate of another track than the identified one")
    out.extend(summary(passages))
    return "\n".join(out)


def summary(passages: list[Passage]) -> list[str]:
    """Totals and the vote distribution."""
    local = [p for p in passages if p.via == "local"]
    audd = [p for p in passages if p.via == "AudD"]
    unknown = [p for p in passages if not p.track]
    lines = ["", "Summary"]
    lines.append(
        f"  passages: {len(passages)}  identified locally: {len(local)}  "
        f"via AudD: {len(audd)}  not identified: {len(unknown)}"
    )
    for name, group in (("local", local), ("AudD", audd)):
        times = [p.after for p in group if p.after is not None]
        if times:
            lines.append(
                f"  time to identify ({name}): median {statistics.median(times):.0f}s, "
                f"min {min(times)}s, max {max(times)}s"
            )
    lines.append(
        f"  AudD calls: {sum(p.audd_calls for p in passages)} "
        f"(of which in passages identified locally: {sum(p.audd_calls for p in local)})"
    )
    same: Counter[int] = Counter()
    other: Counter[int] = Counter()
    for p in passages:
        for c in p.candidates:
            if p.track is not None and c.track == p.track:
                same[c.votes] += 1
            else:
                other[c.votes] += 1
    lines.append(
        "  votes per lookup window   " + "  ".join(f"{v:>3}" for v in range(1, 11)) + "  >10"
    )
    for label, counts in (("identified track", same), ("other / unknown  ", other)):
        cells = [f"{counts.get(v, 0):>3}" for v in range(1, 11)]
        over = sum(n for v, n in counts.items() if v > 10)
        lines.append(f"    {label}        " + "  ".join(cells) + f"  {over:>3}")
    lines.append("  (lookups without any candidate are not logged at INFO and are not counted)")
    return lines


def main() -> None:
    """Read logs from stdin and print the report."""
    passages, versions = parse(sys.stdin.readlines())
    if not passages:
        print("No passages found in the log lines.")
        return
    print(report(passages, versions))


if __name__ == "__main__":
    main()
