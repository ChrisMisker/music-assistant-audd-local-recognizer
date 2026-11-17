"""PCM conversion, density subset, scoring and real Panako fingerprinting."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from audd_local_recognizer.fingerprints import (
    PcmFormat,
    density_subset,
    fingerprint_window,
    panako_fingerprint,
    pcm_dbfs,
    pcm_to_mono_s16,
    pcm_to_wav,
    score_reference,
)

SENDSPIN = PcmFormat(44100, 2, 4, "f32le")  # the real live format observed


def _tone(seconds: float, rate: int = 44100, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def test_f32le_stereo_12s_converts_to_expected_mono_s16() -> None:
    mono = _tone(12)
    pcm = np.repeat(mono, 2).astype("<f4").tobytes()
    assert len(pcm) == 4_233_600
    out = pcm_to_mono_s16(pcm, SENDSPIN)
    assert out.dtype == np.int16
    assert out.shape == (529_200,)
    assert abs(int(out.max()) - int(0.5 * 32767)) <= 1


def test_s16le_and_s32le_conversion() -> None:
    s16 = np.array([16384, -16384], dtype="<i2").tobytes()
    assert pcm_to_mono_s16(s16, PcmFormat(44100, 1, 2, "s16le")).tolist() == [16384, -16384]
    s32 = np.array([2**30, 2**30], dtype="<i4").tobytes()
    assert pcm_to_mono_s16(s32, PcmFormat(44100, 2, 4, "pcm_s32le")).tolist() == [16384]


def test_32bit_is_never_guessed() -> None:
    with pytest.raises(ValueError, match="Unsupported PCM format"):
        pcm_to_mono_s16(b"\0" * 8, PcmFormat(44100, 2, 4, "unknown"))
    with pytest.raises(ValueError, match="Unsupported channel count"):
        pcm_to_mono_s16(b"\0" * 12, PcmFormat(44100, 3, 2, "s16le"))


def test_f32le_nan_and_inf_are_zeroed() -> None:
    pcm = np.array([np.nan, np.inf, -np.inf, 0.5], dtype="<f4").tobytes()
    assert pcm_to_mono_s16(pcm, PcmFormat(44100, 1, 4, "f32le")).tolist() == [0, 0, 0, 16384]


def test_dbfs_levels() -> None:
    fmt = PcmFormat(44100, 1, 4, "f32le")
    assert pcm_dbfs(np.zeros(4410, dtype="<f4").tobytes(), fmt) == -120.0
    assert pcm_dbfs(b"", fmt) == -120.0
    full_sine = pcm_dbfs(_tone(0.1, amplitude=1.0).astype("<f4").tobytes(), fmt)
    assert full_sine == pytest.approx(-3.01, abs=0.05)
    quiet = pcm_dbfs(_tone(0.1, amplitude=0.001).astype("<f4").tobytes(), fmt)
    assert quiet == pytest.approx(-63.0, abs=0.5)


def test_wav_is_mono_16bit() -> None:
    pcm = np.repeat(_tone(1), 2).astype("<f4").tobytes()
    with wave.open(io.BytesIO(pcm_to_wav(pcm, SENDSPIN))) as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1,
            2,
            44100,
        )
        assert reader.getnframes() == 44100


def test_density_is_deterministic_and_about_one_sixteenth() -> None:
    hashes = {f"{i * 2654435761 % 2**64:016x}abcd": [i] for i in range(4096)}
    subset = density_subset(hashes)
    assert subset == density_subset(dict(reversed(list(hashes.items()))))
    assert 4096 / 16 * 0.75 <= len(subset) <= 4096 / 16 * 1.25
    # membership depends only on the hash itself
    for key in list(hashes)[:200]:
        assert (key in subset) == (key in density_subset({key: [0]}))


def test_density_handles_non_hex_hashes() -> None:
    hashes = {f"not-hex-{i}": [i] for i in range(1600)}
    assert density_subset(hashes) == density_subset(hashes)
    assert 0 < len(density_subset(hashes)) < 1600


def test_offset_votes() -> None:
    query = {"aa": [10], "bb": [20], "cc": [30]}
    stored = [("aa", 15), ("bb", 25), ("cc", 35)]
    assert score_reference(query, stored) == (3, 5, 3, 1.0)


def test_offset_votes_ignore_unshared_and_incoherent() -> None:
    query = {"aa": [10], "bb": [20], "cc": [30], "dd": [1]}
    stored = [("aa", 15), ("bb", 25), ("cc", 99), ("zz", 0)]
    shared, offset, votes, ratio = score_reference(query, stored)
    assert (shared, offset, votes) == (3, 5, 2)
    assert ratio == pytest.approx(2 / 3)
    assert score_reference({"aa": [1]}, [("bb", 1)]) == (0, 0, 0, 0.0)


def test_real_panako_on_synthetic_music() -> None:
    rng = np.random.default_rng(1)
    rate = 44100
    t = np.arange(rate * 12) / rate
    signal = sum(
        np.sin(2 * np.pi * f * t) * (0.5 + 0.5 * np.sin(2 * np.pi * m * t))
        for f, m in [(220, 0.3), (440, 0.7), (880, 1.1), (1320, 0.2), (523, 2.0)]
    )
    signal = 0.1 * signal + rng.normal(0, 0.02, t.size)
    pcm = np.repeat(signal.astype("<f4"), 2).tobytes()
    full = panako_fingerprint(pcm, SENDSPIN)
    assert full
    assert all(isinstance(k, str) and isinstance(v, list) for k, v in full.items())
    assert fingerprint_window(pcm, SENDSPIN) == density_subset(full)
    # same audio, same fingerprints
    assert panako_fingerprint(pcm, SENDSPIN) == full
