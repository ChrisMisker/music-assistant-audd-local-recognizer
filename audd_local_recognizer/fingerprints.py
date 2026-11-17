"""Panako fingerprint helpers for AudD + Local Fingerprints."""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from audalign.config.fingerprint import FingerprintConfig
from audalign.recognizers.fingerprint import fingerprinter

DENSITY_DIVISOR = 16  # 6.25%, validated against two vinyl replay tests.


@dataclass(frozen=True)
class QueryMatch:
    """Best local fingerprint candidate."""

    track_id: int
    reference_id: int
    shared_hashes: int
    best_offset: int
    best_votes: int
    ratio: float


def pcm_to_mono_s16(
    pcm: bytes,
    sample_width: int,
    channels: int,
    content_type: str,
) -> np.ndarray:
    """Convert explicit little-endian MA PCM format to mono int16 samples."""
    if sample_width == 2 and content_type in ("s16le", "pcm_s16le"):
        data = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        data /= 32768.0
    elif sample_width == 4 and content_type in ("f32le", "pcm_f32le"):
        data = np.frombuffer(pcm, dtype="<f4")
        data = np.nan_to_num(
            data,
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        ).astype(np.float32)
    elif sample_width == 4 and content_type in ("s32le", "pcm_s32le"):
        data = np.frombuffer(pcm, dtype="<i4").astype(np.float32)
        data /= 2147483648.0
    else:
        raise ValueError(
            f"Unsupported PCM format: {content_type} / {sample_width * 8}-bit"
        )

    if channels == 2:
        data = data.reshape(-1, 2).mean(axis=1)
    elif channels != 1:
        raise ValueError(f"Unsupported channel count: {channels}")

    return np.clip(data * 32767.0, -32768, 32767).astype(np.int16)


def panako_fingerprint(
    pcm: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int,
    content_type: str,
) -> dict[str, list[int]]:
    """Return Audalign Panako hashes for one PCM window."""
    samples = pcm_to_mono_s16(
        pcm,
        sample_width,
        channels,
        content_type,
    )
    config = FingerprintConfig()
    config.sample_rate = sample_rate
    if hasattr(config, "set_hash_style"):
        config.set_hash_style("panako")
    else:
        config.hash_style = "panako"
    hashes = fingerprinter.fingerprint(samples, config)
    if not isinstance(hashes, dict):
        raise TypeError("Audalign returned an unexpected fingerprint result")
    return hashes


def _stable_bucket(value: str) -> int:
    """Return a stable integer bucket for a Panako hash string."""
    try:
        return int(value[:16], 16)
    except ValueError:
        return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big")


def density_subset(
    hashes: dict[str, list[int]], divisor: int = DENSITY_DIVISOR
) -> dict[str, list[int]]:
    """Select a deterministic 1/divisor subset of fingerprint hashes."""
    if divisor <= 1:
        return hashes
    return {key: positions for key, positions in hashes.items() if _stable_bucket(key) % divisor == 0}


def score_reference(
    query: dict[str, list[int]],
    stored_rows: Iterable[tuple[str, int]],
) -> tuple[int, int, int, float]:
    """Score one stored reference against a query by coherent time offset."""
    stored: dict[str, list[int]] = {}
    for fingerprint_hash, position in stored_rows:
        stored.setdefault(fingerprint_hash, []).append(position)
    shared = query.keys() & stored.keys()
    offsets: Counter[int] = Counter()
    for fingerprint_hash in shared:
        for query_position in query[fingerprint_hash]:
            for stored_position in stored[fingerprint_hash]:
                offsets[stored_position - query_position] += 1
    if not offsets:
        return len(shared), 0, 0, 0.0
    best_offset, best_votes = offsets.most_common(1)[0]
    ratio = best_votes / len(shared) if shared else 0.0
    return len(shared), best_offset, best_votes, ratio
