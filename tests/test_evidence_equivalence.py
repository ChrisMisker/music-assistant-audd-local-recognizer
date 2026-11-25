"""The incremental evidence count gives exactly what a full recount gives (MA-independent)."""

from __future__ import annotations

import random

import pytest

from audd_local_recognizer import engine as eng
from audd_local_recognizer.database import MatchRow
from audd_local_recognizer.engine import TimelineEvidence
from audd_local_recognizer.fingerprints import DENSITY_DIVISOR, density_subset


def _reference_clusters(
    evidence: TimelineEvidence, adds: list[tuple[dict[str, list[int]], list[MatchRow], float, bool]]
) -> dict[tuple[int, str], dict[int, tuple[int, int, int, int]]]:
    """Recount everything from scratch, with sets, as the first version of best() did."""
    bins: dict[tuple[int, str], dict[int, dict[str, set[int]]]] = {}
    dense_only: set[str] = set()
    lookups = 0
    for query, rows, start, dense in adds:
        lookups += 1
        if dense:
            dense_only |= query.keys() - density_subset(query, DENSITY_DIVISOR).keys()
        for row in rows:
            for query_position in query.get(row.fingerprint_hash, ()):
                shift = (
                    row.passage_offset_seconds
                    + (row.position - query_position) * evidence.frame_seconds
                    - start
                )
                shift_bin = round(shift / eng.COHERENCE_BIN_SECONDS)
                play = bins.setdefault((row.track_id, row.passage_id), {})
                play.setdefault(shift_bin, {}).setdefault(row.fingerprint_hash, set()).add(lookups)
    result: dict[tuple[int, str], dict[int, tuple[int, int, int, int]]] = {}
    for key, play in bins.items():
        for center in play:
            hashes: dict[str, set[int]] = {}
            for index in range(center - eng.COHERENCE_BINS, center + eng.COHERENCE_BINS + 1):
                for fp_hash, seen in play.get(index, {}).items():
                    hashes.setdefault(fp_hash, set()).update(seen)
            result.setdefault(key, {})[center] = (
                len(hashes),
                len(set().union(*hashes.values())),
                sum(1 for seen in hashes.values() if lookups in seen),
                sum(1 for fp_hash in hashes if fp_hash not in dense_only),
            )
    return result


def _reference_best(
    clusters: dict[tuple[int, str], dict[int, tuple[int, int, int, int]]],
    track_id: int | None,
    min_ordinary: int,
    large: int | None,
) -> tuple[int, str, int, int, int, int] | None:
    best = None
    for key in sorted(clusters):
        if track_id is not None and key[0] != track_id:
            continue
        for center in sorted(clusters[key]):
            votes, lookups, _latest, ordinary = clusters[key][center]
            if ordinary < min_ordinary and (large is None or votes < large):
                continue
            if best is None or (votes, lookups) > best[2:4]:
                best = (key[0], key[1], votes, lookups, ordinary, center)
    return best


@pytest.mark.parametrize("seed", range(12))
def test_incremental_best_equals_a_full_recount(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - test data, not cryptography
    pool = [f"{rng.getrandbits(64):016x}0000" for _ in range(300)]
    evidence = TimelineEvidence()
    adds: list[tuple[dict[str, list[int]], list[MatchRow], float, bool]] = []
    for lookup in range(40):
        dense = lookup < 8 and rng.random() < 0.8  # fast start first
        query = {h: [rng.randrange(260)] for h in rng.sample(pool, 60)}
        if not dense:
            query = density_subset(query, DENSITY_DIVISOR) or query
        rows = []
        for fp_hash in rng.sample(sorted(query), min(len(query), 25)):
            track = rng.randrange(1, 4)
            passage = rng.choice("abc")
            genuine = track == 1 and passage == "a" and rng.random() < 0.6
            position = query[fp_hash][0] + (
                round(lookup * 6 / 0.0464) if genuine else rng.randrange(-4000, 4000)
            )
            rows.append(MatchRow(fp_hash, position, track, passage, rng.choice([0.0, 6.0, 12.0])))
        start = lookup * 6.0
        evidence.add(query, rows, start, dense=dense)
        adds.append((query, rows, start, dense))
        clusters = _reference_clusters(evidence, adds)
        for track_id, min_ordinary, large in [
            (None, 0, None),
            (1, 0, None),
            (2, 0, None),
            (None, 4, 10),
        ]:
            got = evidence.best(track_id, min_ordinary, large)
            want = _reference_best(clusters, track_id, min_ordinary, large)
            if want is None:
                assert got is None
                continue
            assert got is not None
            track, passage, votes, lookups, ordinary, center = want
            assert (
                got.track_id,
                got.reference_passage_id,
                got.votes,
                got.lookups,
                got.ordinary_votes,
            ) == (
                track,
                passage,
                votes,
                lookups,
                ordinary,
            )
            assert got.shift_seconds == center * eng.COHERENCE_BIN_SECONDS
            assert got.latest_votes == clusters[track, passage][center][2]
