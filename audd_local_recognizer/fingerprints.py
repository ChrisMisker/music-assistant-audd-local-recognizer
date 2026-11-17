"""PCM helpers, Panako fingerprinting, density reduction and offset scoring.

Nothing in this module depends on Music Assistant, so it can be unit-tested on its own.
"""

from __future__ import annotations

import hashlib
import io
import math
import wave
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable

# Keep 1/16 (6.25%) of Panako hashes. Chosen after real vinyl replay tests: still dozens of
# coherent votes on a replay, ~0-1 accidental votes on unrelated audio. Do not retune without
# evidence from real recordings.
DENSITY_DIVISOR = 16

# Panako works in samples (FFT window, hop), so hashes and positions depend on the sample
# rate. Live sources change rate with the output device (48 kHz for one player, 44.1 kHz for
# AirPlay), so every window is resampled to this fixed rate before fingerprinting. 44.1 kHz
# is the rate the density and thresholds were validated at.
FINGERPRINT_SAMPLE_RATE = 44100

SILENCE_FLOOR_DBFS = -120.0

Fingerprints = dict[str, list[int]]


@dataclass(frozen=True)
class PcmFormat:
    """Explicit little-endian PCM format of a live stream."""

    sample_rate: int
    channels: int
    sample_width: int  # bytes per sample
    content_type: str  # e.g. "f32le", "s16le", "s32le"

    @property
    def bytes_per_second(self) -> int:
        """Return the byte rate of this format."""
        return self.sample_rate * self.channels * self.sample_width


@dataclass(frozen=True)
class QueryMatch:
    """Best local fingerprint candidate."""

    track_id: int
    reference_id: int
    reference_passage_id: str
    shared_hashes: int
    best_offset: int
    best_votes: int
    ratio: float


def pcm_to_mono_s16(pcm: bytes, fmt: PcmFormat) -> np.ndarray:
    """Convert explicit little-endian PCM to mono int16 samples.

    32-bit audio is never guessed: the content type decides float vs integer.
    """
    content_type = fmt.content_type.removeprefix("pcm_")
    if fmt.sample_width == 2 and content_type == "s16le":
        data = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    elif fmt.sample_width == 4 and content_type == "f32le":
        data = np.nan_to_num(
            np.frombuffer(pcm, dtype="<f4"), nan=0.0, posinf=0.0, neginf=0.0
        ).astype(np.float32)
    elif fmt.sample_width == 4 and content_type == "s32le":
        data = np.frombuffer(pcm, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        msg = f"Unsupported PCM format: {fmt.content_type} / {fmt.sample_width * 8}-bit"
        raise ValueError(msg)

    if fmt.channels == 2:
        data = data[: data.size - data.size % 2].reshape(-1, 2).mean(axis=1)
    elif fmt.channels != 1:
        msg = f"Unsupported channel count: {fmt.channels}"
        raise ValueError(msg)

    return np.clip(np.rint(data * 32767.0), -32768, 32767).astype(np.int16)


def pcm_dbfs(pcm: bytes, fmt: PcmFormat) -> float:
    """Return the RMS level of a PCM chunk in dBFS."""
    if not pcm:
        return SILENCE_FLOOR_DBFS
    samples = pcm_to_mono_s16(pcm, fmt).astype(np.float32)
    if not samples.size:
        return SILENCE_FLOOR_DBFS
    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 0:
        return SILENCE_FLOOR_DBFS
    return max(SILENCE_FLOOR_DBFS, 20.0 * math.log10(rms / 32767.0))


def pcm_to_wav(pcm: bytes, fmt: PcmFormat) -> bytes:
    """Encode PCM as a mono 16-bit WAV file (the payload sent to AudD)."""
    mono = pcm_to_mono_s16(pcm, fmt)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(fmt.sample_rate)
        wav.writeframes(mono.tobytes())
    return out.getvalue()


def resample_s16(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample mono int16 audio with a polyphase filter (no-op when rates match)."""
    if source_rate == target_rate or not samples.size:
        return samples
    from scipy.signal import resample_poly  # noqa: PLC0415 - audalign dependency, heavy

    divisor = math.gcd(source_rate, target_rate)
    resampled = resample_poly(
        samples.astype(np.float32), target_rate // divisor, source_rate // divisor
    )
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def panako_fingerprint(pcm: bytes, fmt: PcmFormat) -> Fingerprints:
    """Return the full Audalign Panako hashes for one PCM window (CPU heavy: offload it)."""
    # Imported lazily: audalign pulls in a large dependency tree.
    from audalign.config.fingerprint import FingerprintConfig  # noqa: PLC0415
    from audalign.recognizers.fingerprint import fingerprinter  # noqa: PLC0415

    samples = resample_s16(pcm_to_mono_s16(pcm, fmt), fmt.sample_rate, FINGERPRINT_SAMPLE_RATE)
    config = FingerprintConfig()
    config.sample_rate = FINGERPRINT_SAMPLE_RATE
    if hasattr(config, "set_hash_style"):
        config.set_hash_style("panako")
    else:
        config.hash_style = "panako"
    hashes = fingerprinter.fingerprint(samples, config)
    if not isinstance(hashes, dict):
        msg = "Audalign returned an unexpected fingerprint result"
        raise TypeError(msg)
    return hashes


def fingerprint_window(pcm: bytes, fmt: PcmFormat) -> Fingerprints:
    """Fingerprint one window and keep the deterministic production density subset."""
    return density_subset(panako_fingerprint(pcm, fmt))


def _stable_bucket(value: str) -> int:
    """Return a stable integer bucket for a Panako hash string (hex for audalign)."""
    try:
        return int(value[:16], 16)
    except ValueError:
        return int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big")


def density_subset(hashes: Fingerprints, divisor: int = DENSITY_DIVISOR) -> Fingerprints:
    """Select a deterministic 1/divisor subset of fingerprint hashes (never random)."""
    if divisor <= 1:
        return hashes
    return {key: pos for key, pos in hashes.items() if _stable_bucket(key) % divisor == 0}


def score_reference(
    query: Fingerprints,
    stored_rows: Iterable[tuple[str, int]],
) -> tuple[int, int, int, float]:
    """Score one stored reference against a query by coherent time offset.

    Returns (shared_hashes, best_offset, best_votes, ratio) where ratio is
    best_votes / shared_hashes. Only rows whose hash occurs in the query matter.
    """
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
    # Deterministic tie-break: most votes, then smallest offset.
    best_offset, best_votes = min(offsets.items(), key=lambda item: (-item[1], item[0]))
    return len(shared), best_offset, best_votes, best_votes / len(shared)
