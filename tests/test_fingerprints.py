"""PCM conversion, density subset, scoring and real Panako fingerprinting."""

from __future__ import annotations

import io
import wave
from typing import TYPE_CHECKING

import numpy as np
import pytest

from audd_local_recognizer import fingerprints
from audd_local_recognizer.fingerprints import (
    FINGERPRINT_SAMPLE_RATE,
    PcmFormat,
    change_speed,
    density_subset,
    fingerprint_window,
    panako_fingerprint,
    pcm_dbfs,
    pcm_to_mono_s16,
    pcm_to_wav,
    resample_s16,
    sample_stats,
    score_reference,
    stream_holes,
)

if TYPE_CHECKING:
    from pathlib import Path

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


def _music(rate: int, seconds: float = 12) -> np.ndarray:
    rng = np.random.default_rng(7)
    t = np.arange(int(rate * seconds)) / rate
    signal = sum(
        np.sin(2 * np.pi * f * t) * (0.5 + 0.5 * np.sin(2 * np.pi * m * t))
        for f, m in [(196, 0.4), (392, 0.9), (784, 1.3), (1175, 0.25), (587, 1.7)]
    )
    # deterministic "noise" defined in time, so both rates sample the same waveform
    noise = sum(
        0.01 * np.sin(2 * np.pi * f * t + p)
        for f, p in zip(rng.uniform(2000, 6000, 12), rng.uniform(0, 6.28, 12), strict=True)
    )
    music: np.ndarray = (0.1 * signal + noise).astype("<f4")
    return music


def test_resample_is_noop_at_target_rate() -> None:
    samples = np.arange(-5, 5, dtype=np.int16)
    assert resample_s16(samples, 44100, 44100) is samples


def test_resample_length() -> None:
    samples = np.zeros(48000, dtype=np.int16)
    assert resample_s16(samples, 48000, 44100).shape == (44100,)


def test_48k_and_44k_give_matching_fingerprints() -> None:
    """The same music played to a 48 kHz and a 44.1 kHz device must match locally."""
    at_44k = panako_fingerprint(
        np.repeat(_music(44100), 2).tobytes(), PcmFormat(44100, 2, 4, "f32le")
    )
    at_48k = panako_fingerprint(
        np.repeat(_music(48000), 2).tobytes(), PcmFormat(48000, 2, 4, "f32le")
    )
    assert FINGERPRINT_SAMPLE_RATE == 44100
    # Production density, as matched in the database. Without resampling this gave 0 votes.
    query, stored = density_subset(at_44k), density_subset(at_48k)
    rows = [(h, p) for h, positions in stored.items() for p in positions]
    _shared, offset, votes, ratio = score_reference(query, rows)
    assert votes >= 3 * 5  # well above STRONG_VOTES
    assert offset == 0
    assert ratio >= 0.8


def test_a_repeated_hash_counts_as_one_vote() -> None:
    """Repetitive audio: one hash at several positions must not look like several matches."""
    query = {"loop": [10, 20, 30, 40, 50]}
    stored = [("loop", 15), ("loop", 25), ("loop", 35), ("loop", 45), ("loop", 55)]
    shared, _offset, votes, ratio = score_reference(query, stored)
    # before the fix: 5 votes at offset +5, ratio 5.0
    assert (shared, votes, ratio) == (1, 1, 1.0)


def test_distinct_hashes_still_vote_together() -> None:
    query = {"a": [10, 40], "b": [20], "c": [30]}
    stored = [("a", 15), ("a", 45), ("b", 25), ("c", 35), ("c", 99)]
    shared, offset, votes, ratio = score_reference(query, stored)
    assert (shared, offset, votes) == (3, 5, 3)
    assert ratio == 1.0


def _stereo(left: np.ndarray, right: np.ndarray) -> bytes:
    out = np.empty(2 * left.size, dtype="<f4")
    out[0::2], out[1::2] = left, right
    return out.tobytes()


def test_sample_stats_detect_phase_problems_and_clipping() -> None:
    tone = _tone(1)
    same = sample_stats(_stereo(tone, tone), SENDSPIN)
    assert same["lr_correlation"] == 1.0
    assert same["clipped_ratio"] == 0.0
    inverted = sample_stats(_stereo(tone, -tone), SENDSPIN)
    assert inverted["lr_correlation"] == -1.0
    assert inverted["rms_dbfs"] == -120.0  # the mono sum AudD gets is silent
    loud = np.clip(tone * 4, -1, 1).astype(np.float32)
    clipped = sample_stats(_stereo(loud, loud), SENDSPIN)
    assert clipped["clipped_ratio"] is not None
    assert clipped["clipped_ratio"] > 0.3
    mono = sample_stats(tone.astype("<f4").tobytes(), PcmFormat(44100, 1, 4, "f32le"))
    assert mono["lr_correlation"] is None


def _dominant_hz(samples: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples.astype(np.float64)))
    return float(np.argmax(spectrum) * rate / samples.size)


def test_change_speed_changes_tempo_and_pitch_together() -> None:
    rate = 44100
    tone = (0.5 * 32767 * np.sin(2 * np.pi * 440 * np.arange(rate * 4) / rate)).astype(np.int16)
    slower = change_speed(tone, -2.0)
    assert slower.size == pytest.approx(tone.size / 0.98, rel=0.002)  # longer
    assert _dominant_hz(slower, rate) == pytest.approx(440 * 0.98, abs=1.0)  # lower: ~431 Hz
    faster = change_speed(tone, 2.0)
    assert faster.size == pytest.approx(tone.size / 1.02, rel=0.002)
    assert _dominant_hz(faster, rate) == pytest.approx(440 * 1.02, abs=1.0)
    assert change_speed(tone, 0.0) is tone


def test_wav_with_speed_correction() -> None:
    pcm = np.repeat(_tone(1), 2).astype("<f4").tobytes()
    with wave.open(io.BytesIO(pcm_to_wav(pcm, SENDSPIN, -2.0))) as reader:
        assert reader.getframerate() == 44100  # the rate stays; the audio is longer
        assert reader.getnframes() == pytest.approx(44100 / 0.98, rel=0.002)


def test_import_quietly_hides_dependency_syntax_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pydub's invalid escape sequences must not reach the MA log on a first import."""
    import warnings  # noqa: PLC0415

    from audd_local_recognizer.fingerprints import import_quietly  # noqa: PLC0415

    (tmp_path / "noisy_dependency.py").write_text('PATTERN = "\\("\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr("sys.dont_write_bytecode", True)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = import_quietly("noisy_dependency")
    assert module.PATTERN == "\\("
    assert not [w for w in caught if issubclass(w.category, SyntaxWarning)]


def test_stream_holes_counts_runs_of_digital_zeros_in_all_channels() -> None:
    fmt = PcmFormat(1000, 2, 4, "f32le")  # 1 kHz: 1 frame = 1 ms
    audio = np.full((100, 2), 0.01, dtype="<f4")
    audio[10:15] = 0.0  # 5 ms hole
    audio[40:41] = 0.0  # 1 ms: shorter than the minimum
    audio[60:90, 0] = 0.0  # one channel only: still audio, not a hole
    audio[95:100] = 0.0  # 5 ms hole at the end
    assert stream_holes(audio.tobytes(), fmt, 0.002) == (2, 0.005)


def test_stream_holes_of_clean_or_empty_audio() -> None:
    fmt = PcmFormat(1000, 1, 2, "s16le")
    assert stream_holes(np.full(50, 7, dtype="<i2").tobytes(), fmt) == (0, 0.0)
    assert stream_holes(b"", fmt) == (0, 0.0)


def test_the_fast_peak_search_gives_exactly_the_same_peaks() -> None:
    """The diamond filter as repeated cross filters (audalign's peak search, 14x faster)."""
    from scipy import ndimage  # noqa: PLC0415 - audalign dependency, heavy

    _config, fingerprinter = fingerprints._audalign()
    rng = np.random.default_rng(7)
    spectrogram = rng.normal(40, 15, size=(300, 120))
    spectrogram[rng.random(spectrogram.shape) < 0.05] = 0  # some empty bins, as real ones have
    diamond = ndimage.iterate_structure(ndimage.generate_binary_structure(2, 1), 20)
    assert np.array_equal(
        fingerprinter.maximum_filter(spectrogram, footprint=diamond),
        ndimage.maximum_filter(spectrogram, footprint=diamond),
    )
    background = spectrogram == 0
    assert np.array_equal(
        fingerprinter.binary_erosion(background, structure=diamond, border_value=1),
        ndimage.binary_erosion(background, structure=diamond, border_value=1),
    )
    square = np.ones((5, 5), dtype=bool)  # any other footprint goes to scipy unchanged
    assert np.array_equal(
        fingerprinter.maximum_filter(spectrogram, footprint=square),
        ndimage.maximum_filter(spectrogram, footprint=square),
    )
