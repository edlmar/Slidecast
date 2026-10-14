"""Audio utilities: I/O, silence trimming, de-click fades, concatenation, loudness normalisation,
and reference-clip validation.  Pure numpy/scipy; no torch here so tests stay GPU-free."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt
import soundfile as sf

from slidecast.core.config import AudioPostSettings

FloatArray = npt.NDArray[np.float32]


def load_wav(path: Path) -> tuple[FloatArray, int]:
    """Load any soundfile-supported file as mono float32 (channels are averaged)."""
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1).astype(np.float32)
    return mono, int(sr)


def save_wav(path: Path, wav: FloatArray, sr: int, subtype: str = "PCM_16") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(wav, -1.0, 1.0), sr, subtype=subtype)


def iter_wav_blocks(path: Path, block: int = 1 << 20) -> Iterator[FloatArray]:
    """Yield a mono WAV file as successive float32 blocks."""
    with sf.SoundFile(str(path)) as f:
        while True:
            data = f.read(block, dtype="float32")
            if len(data) == 0:
                return
            # Stereo is downmixed on load everywhere else; keep the convention here.
            yield data.mean(axis=1).astype(np.float32) if data.ndim > 1 else data


def write_wav_blocks(
    blocks: Iterable[FloatArray], path: Path, sr: int, subtype: str, clip: bool = True
) -> None:
    """Write float32 blocks to a mono WAV file incrementally.

    ``clip`` mirrors ``save_wav``'s convention of never emitting samples beyond +-1. Pass
    ``clip=False`` for float intermediates inside processing chains, where clipping would
    hide the true peaks the limiter is meant to see.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sf.SoundFile(str(path), "w", samplerate=sr, channels=1, subtype=subtype) as f:
        for b in blocks:
            f.write(np.clip(b, -1.0, 1.0) if clip else b)


def _blocks_with_context(
    blocks: Iterable[FloatArray], left: int, right: int
) -> Iterator[tuple[FloatArray, int, int]]:
    """Yield ``(segment, core_offset, core_len)`` per block with up to ``left``/``right``
    samples of neighbouring context - the same window the array versions slice."""
    prev_tail = np.zeros(0, dtype=np.float32)
    it = iter(blocks)
    cur = next(it, None)
    while cur is not None:
        nxt = next(it, None)
        head = nxt[:right] if (nxt is not None and right) else np.zeros(0, dtype=np.float32)
        seg = np.concatenate([prev_tail, cur, head])
        yield seg, len(prev_tail), len(cur)
        prev_tail = cur[-left:] if left else np.zeros(0, dtype=np.float32)
        cur = nxt


def resample(wav: FloatArray, sr_in: int, sr_out: int) -> FloatArray:
    if sr_in == sr_out:
        return wav
    from scipy.signal import resample_poly

    g = math.gcd(sr_in, sr_out)
    out = resample_poly(wav, sr_out // g, sr_in // g)
    return np.asarray(out, dtype=np.float32)


def silence(seconds: float, sr: int) -> FloatArray:
    return np.zeros(max(round(seconds * sr), 0), dtype=np.float32)


def duration_seconds(wav: FloatArray, sr: int) -> float:
    return len(wav) / sr if sr > 0 else 0.0


def trim_silence(
    wav: FloatArray, sr: int, threshold_db: float = -40.0, pad_ms: int = 60
) -> FloatArray:
    """Trim leading/trailing regions below ``threshold_db`` (RMS over 10 ms windows)."""
    if len(wav) == 0:
        return wav
    win = max(int(sr * 0.010), 1)
    n_win = math.ceil(len(wav) / win)
    padded = np.zeros(n_win * win, dtype=np.float32)
    padded[: len(wav)] = wav
    frames = padded.reshape(n_win, win)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    loud = np.flatnonzero(db > threshold_db)
    if loud.size == 0:
        return wav[:0]
    pad = int(sr * pad_ms / 1000)
    start = max(int(loud[0]) * win - pad, 0)
    end = min((int(loud[-1]) + 1) * win + pad, len(wav))
    return wav[start:end]


def apply_fades(wav: FloatArray, sr: int, fade_ms: int = 5) -> FloatArray:
    """Gentle de-click: short linear fade in/out so chunk joins never step."""
    n = min(int(sr * fade_ms / 1000), len(wav) // 2)
    if n <= 0:
        return wav
    out = wav.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out


def postprocess_chunk(wav: FloatArray, sr: int, settings: AudioPostSettings) -> FloatArray:
    out = wav.astype(np.float32, copy=False)
    if settings.trim_silence:
        out = trim_silence(out, sr, settings.trim_threshold_db, settings.trim_pad_ms)
    if settings.declick:
        out = apply_fades(out, sr, settings.declick_fade_ms)
    return out


@dataclass
class ChunkTiming:
    text: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class ConcatResult:
    wav: FloatArray
    timings: list[ChunkTiming] = field(default_factory=list)


def concat_with_gaps(
    chunks: list[tuple[str, FloatArray]], sr: int, gap_seconds: float
) -> ConcatResult:
    """Join chunk audio with silence between chunks; returns timings for each chunk."""
    parts: list[FloatArray] = []
    timings: list[ChunkTiming] = []
    gap = silence(gap_seconds, sr)
    cursor = 0
    for i, (text, wav) in enumerate(chunks):
        if i > 0 and len(gap):
            parts.append(gap)
            cursor += len(gap)
        parts.append(wav)
        timings.append(ChunkTiming(text=text, start=cursor / sr, end=(cursor + len(wav)) / sr))
        cursor += len(wav)
    joined = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return ConcatResult(wav=joined.astype(np.float32, copy=False), timings=timings)


def measure_loudness_blocks(blocks: Iterable[FloatArray], sr: int) -> float:
    """Integrated loudness (ITU-R BS.1770) of a block stream, in LUFS.

    Same computation as pyloudnorm's ``integrated_loudness`` - the K-weighting filter
    coefficients come from its Meter and are applied with carried filter state, and the
    400 ms gating energies are accumulated from 100 ms hop sums - but O(hop) memory, so a
    60-minute track never becomes a second copy of the signal in float64.
    """
    import pyloudnorm as pyln
    from scipy.signal import lfilter

    meter = pyln.Meter(sr)
    coeffs = [(f.b, f.a) for f in meter._filters.values()]  # high shelf, then high pass
    zis = [np.zeros(max(len(a), len(b)) - 1) for b, a in coeffs]
    block_n = int(sr * meter.block_size)  # 400 ms gating blocks
    hop_n = int(sr * meter.block_size * (1.0 - meter.overlap))  # 75% overlap -> 100 ms
    hop_sums: list[float] = []  # sum of x^2 per hop; ~10 entries per second of audio
    total = 0
    buf = np.zeros(0, dtype=np.float64)
    for chunk in blocks:
        x = np.asarray(chunk, dtype=np.float64)
        for i, (b, a) in enumerate(coeffs):
            x, zis[i] = lfilter(b, a, x, zi=zis[i])
        buf = np.concatenate([buf, x])
        n_hops = len(buf) // hop_n
        for h in range(n_hops):
            seg = buf[h * hop_n : (h + 1) * hop_n]
            hop_sums.append(float(np.sum(seg * seg)))
        buf = buf[n_hops * hop_n :]
        total += len(x)
    if len(buf):  # tail partial hop still contributes to the final partial window
        hop_sums.append(float(np.sum(buf * buf)))
    # pyloudnorm rounds the block count and lets the last window run short (dividing by the
    # full 400 ms anyway); replicate that exactly.
    t_sec = total / sr
    num_blocks = round((t_sec - 0.4) / 0.1) + 1
    if num_blocks <= 0:
        return float("-inf")
    z = np.zeros(num_blocks)
    for j in range(num_blocks):
        lo = j * hop_n
        hi = min(lo + block_n, total)
        z[j] = sum(hop_sums[lo // hop_n : hi // hop_n]) / block_n
        # lo/hi are hop-aligned only when sr*0.1 is integral; that is true for the sample
        # rates this app uses (24/44.1/48 kHz).
    with np.errstate(divide="ignore"):
        block_lufs = -0.691 + 10.0 * np.log10(z)
    gated = z[block_lufs >= -70.0]
    if gated.size == 0:
        return float("-inf")
    relative_gate = -0.691 + 10.0 * math.log10(float(gated.mean())) - 10.0
    gated = z[block_lufs > relative_gate]
    if gated.size == 0:
        return float("-inf")
    return float(-0.691 + 10.0 * math.log10(float(gated.mean())))


def measure_loudness(wav: FloatArray, sr: int) -> float:
    """Integrated loudness in LUFS (BS.1770). Returns -inf for silence or < 400 ms of audio."""
    if len(wav) < sr * 0.4:  # BS.1770 needs at least one 400 ms block
        return float("-inf")
    return measure_loudness_blocks([wav], sr)


_TP_BLOCK = 1 << 20  # samples per block for chunked true-peak measurement
_TP_OVERLAP = 256


def true_peak_db_blocks(blocks: Iterable[FloatArray], sr: int, oversample: int = 4) -> float:
    """True peak (4x oversampled) in dBTP of a block stream."""
    from scipy.signal import resample_poly

    peak = 0.0
    for seg, off, core_len in _blocks_with_context(blocks, _TP_OVERLAP, _TP_OVERLAP):
        up = resample_poly(seg.astype(np.float32), oversample, 1)
        # Discard the overlap so filter transients at block edges are not counted.
        core = up[off * oversample : (off + core_len) * oversample]
        if len(core):
            peak = max(peak, float(np.max(np.abs(core))))
    return 20.0 * math.log10(peak) if peak > 0 else float("-inf")


def true_peak_db(wav: FloatArray, sr: int, oversample: int = 4) -> float:
    """True peak (4x oversampled) in dBTP, measured block-wise so 90-minute tracks stay cheap."""
    if len(wav) == 0:
        return float("-inf")
    return true_peak_db_blocks(
        (wav[i : i + _TP_BLOCK] for i in range(0, len(wav), _TP_BLOCK)), sr, oversample
    )


def peak_limit_blocks(
    blocks: Iterable[FloatArray],
    sr: int,
    ceiling_db: float = -1.0,
    lookahead_ms: float = 5.0,
    hold_ms: float = 50.0,
) -> Iterator[FloatArray]:
    """The peak_limit gain envelope applied block-by-block, yielding processed cores."""
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    ceiling = 10.0 ** ((ceiling_db - 0.5) / 20.0)
    attack = max(int(sr * lookahead_ms / 1000), 1)
    hold = max(int(sr * hold_ms / 1000), 2 * attack + 1)
    for seg, off, core_len in _blocks_with_context(blocks, hold, hold):
        need = np.minimum(1.0, ceiling / np.maximum(np.abs(seg), 1e-9)).astype(np.float32)
        gain = minimum_filter1d(need, size=hold, mode="nearest")
        gain = uniform_filter1d(gain, size=attack, mode="nearest")
        yield (seg * gain)[off : off + core_len].astype(np.float32)


def peak_limit(
    wav: FloatArray,
    sr: int,
    ceiling_db: float = -1.0,
    lookahead_ms: float = 5.0,
    hold_ms: float = 50.0,
) -> FloatArray:
    """Transparent look-ahead brick-wall limiter (min-filter + moving-average gain envelope).

    The gain envelope is the running minimum of ``ceiling / |x|`` over a ``hold`` window, then
    smoothed with a ``lookahead``-length moving average. Because the hold window is at least twice
    the smoothing window, the smoothed gain never exceeds what any sample needs, so sample peaks
    are guaranteed below the ceiling; a 0.5 dB margin covers inter-sample (true) peaks.
    """
    if len(wav) == 0:
        return wav
    block = 1 << 20
    return np.concatenate(
        list(
            peak_limit_blocks(
                (wav[i : i + block] for i in range(0, len(wav), block)),
                sr,
                ceiling_db,
                lookahead_ms,
                hold_ms,
            )
        )
    )


def _gain_blocks(blocks: Iterable[FloatArray], gain_db: float) -> Iterator[FloatArray]:
    g = np.float32(10.0 ** (gain_db / 20.0))
    for b in blocks:
        yield (b * g).astype(np.float32)


def loudness_normalize_file(
    src: Path,
    dst: Path,
    sr: int = 24000,
    target_lufs: float = -16.0,
    true_peak_limit_db: float = -1.0,
) -> dict[str, float]:
    """``loudness_normalize`` file -> file: identical iteration, O(block) memory.

    Writes the normalized track to ``dst`` (PCM_24). Intermediate passes stay in a temp
    directory next to ``dst`` and are cleaned up on success or failure.
    """
    src, dst = Path(src), Path(dst)

    def measure(p: Path) -> float:
        return measure_loudness_blocks(iter_wav_blocks(p), sr)

    def tpeak(p: Path) -> float:
        return true_peak_db_blocks(iter_wav_blocks(p), sr)

    def gain_to(a: Path, b: Path, gain_db: float, subtype: str = "FLOAT") -> None:
        clip = subtype != "FLOAT"
        write_wav_blocks(_gain_blocks(iter_wav_blocks(a), gain_db), b, sr, subtype, clip=clip)

    lufs = measure(src)
    if not math.isfinite(lufs):
        write_wav_blocks(iter_wav_blocks(src), dst, sr, "PCM_24")
        return {
            "input_lufs": lufs,
            "gain_db": 0.0,
            "output_lufs": lufs,
            "true_peak_db": -99.0,
            "limited": 0.0,
        }
    gain_db = target_lufs - lufs
    tp = tpeak(src)
    if math.isfinite(tp) and tp + gain_db > true_peak_limit_db:
        limited = 1.0
        with tempfile.TemporaryDirectory(dir=dst.parent) as td:
            paths = [Path(td) / "a.wav", Path(td) / "b.wav"]
            gain_to(src, paths[0], gain_db)
            cur = 0
            ceiling = true_peak_limit_db
            for _ in range(8):
                nxt = cur ^ 1
                write_wav_blocks(
                    peak_limit_blocks(iter_wav_blocks(paths[cur]), sr, ceiling),
                    paths[nxt],
                    sr,
                    "FLOAT",
                    clip=False,
                )
                after = measure(paths[nxt])
                tp_out = tpeak(paths[nxt])
                loud_err = (target_lufs - after) if math.isfinite(after) else 0.0
                tp_err = (tp_out - true_peak_limit_db) if math.isfinite(tp_out) else 0.0
                if abs(loud_err) <= 0.1 and tp_err <= 0.05:
                    cur = nxt
                    break
                if tp_err > 0.0:
                    ceiling -= tp_err
                if abs(loud_err) > 0.1:
                    # paths[cur] is fully consumed; overwrite it with the re-gained file.
                    gain_to(paths[nxt], paths[cur], loud_err)
                else:
                    cur = nxt
            write_wav_blocks(iter_wav_blocks(paths[cur]), dst, sr, "PCM_24")
    else:
        limited = 0.0
        gain_to(src, dst, gain_db, "PCM_24")
    return {
        "input_lufs": lufs,
        "gain_db": gain_db,
        "output_lufs": measure(dst),
        "true_peak_db": tpeak(dst),
        "limited": limited,
    }


def loudness_normalize(
    wav: FloatArray,
    sr: int,
    target_lufs: float = -16.0,
    true_peak_limit_db: float = -1.0,
) -> tuple[FloatArray, dict[str, float]]:
    """Bring the track to ``target_lufs`` with true peaks at or below ``true_peak_limit_db``.

    Gain first; if that would push true peaks over the ceiling, iterate limit -> re-measure ->
    re-gain until loudness converges (each pass removes less energy than the last, so this is a
    contraction). The limiter ceiling is tightened by any residual true-peak overshoot, which
    covers inter-sample peaks that the sample-domain margin underestimates on clicky audio.
    A single pass was measured to leave tracks as much as ~3 LU under target on dense,
    transient-heavy narration.
    """
    lufs = measure_loudness(wav, sr)
    if not math.isfinite(lufs):
        return wav, {
            "input_lufs": lufs,
            "gain_db": 0.0,
            "output_lufs": lufs,
            "true_peak_db": -99.0,
            "limited": 0.0,
        }
    gain_db = target_lufs - lufs
    tp = true_peak_db(wav, sr)
    out = (wav * (10.0 ** (gain_db / 20.0))).astype(np.float32)
    limited = 0.0
    if math.isfinite(tp) and tp + gain_db > true_peak_limit_db:
        limited = 1.0
        ceiling = true_peak_limit_db
        for _ in range(8):
            out = peak_limit(out, sr, ceiling)
            after = measure_loudness(out, sr)
            tp_out = true_peak_db(out, sr)
            loud_err = (target_lufs - after) if math.isfinite(after) else 0.0
            tp_err = (tp_out - true_peak_limit_db) if math.isfinite(tp_out) else 0.0
            if abs(loud_err) <= 0.1 and tp_err <= 0.05:
                break
            if tp_err > 0.0:
                ceiling -= tp_err
            if abs(loud_err) > 0.1:
                out = (out * (10.0 ** (loud_err / 20.0))).astype(np.float32)
    out_lufs = measure_loudness(out, sr)
    return out, {
        "input_lufs": lufs,
        "gain_db": gain_db,
        "output_lufs": out_lufs,
        "true_peak_db": true_peak_db(out, sr),
        "limited": limited,
    }


@dataclass
class ReferenceReport:
    path: Path
    ok: bool
    duration: float
    sample_rate: int
    channels: int
    peak_db: float
    loudness_lufs: float
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.duration:.1f}s", f"{self.sample_rate} Hz", f"{self.channels} ch"]
        return ", ".join(bits)


def validate_reference_clip(path: Path) -> ReferenceReport:
    """Check that a clip is suitable for Chatterbox-Turbo voice cloning.

    Chatterbox asserts > 5 s and uses only the first 10 s (loaded at 24 kHz, mono).
    """
    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return ReferenceReport(path, False, 0.0, 0, 0, -99.0, -99.0, [f"File not found: {path}"])
    try:
        info = sf.info(str(path))
    except Exception as exc:  # soundfile raises RuntimeError on unreadable files
        return ReferenceReport(path, False, 0.0, 0, 0, -99.0, -99.0, [f"Unreadable audio: {exc}"])

    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:  # header parsed but the frames did not
        return ReferenceReport(path, False, 0.0, 0, 0, -99.0, -99.0, [f"Unreadable audio: {exc}"])
    channels = int(data.shape[1])
    mono = data.mean(axis=1).astype(np.float32)
    dur = len(mono) / sr
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
    peak_db = 20 * math.log10(peak) if peak > 0 else -99.0
    lufs = measure_loudness(mono, sr)

    if len(mono) and not np.isfinite(mono).all():
        errors.append("Clip contains corrupt (non-finite) samples.")
    elif peak <= 0.0:
        errors.append("Clip is silent; there is no voice to clone.")
    if dur <= 5.0:
        errors.append(f"Clip is {dur:.1f}s; Chatterbox requires more than 5 seconds.")
    elif dur < 8.0:
        warnings.append(f"Clip is {dur:.1f}s; 10-20 s of clean speech clones better.")
    if dur > 20.0:
        warnings.append(f"Clip is {dur:.1f}s; only the first 10 s are used by the model.")
    if channels > 1:
        warnings.append("Clip is stereo; it will be down-mixed to mono.")
    if sr < 16000:
        warnings.append(f"Sample rate {sr} Hz is low; 24 kHz or higher is recommended.")
    if peak_db > -0.1:
        warnings.append("Clip peaks at 0 dBFS; it may be clipped. Use an unclipped recording.")
    if math.isfinite(lufs) and lufs < -35:
        warnings.append(f"Clip is very quiet ({lufs:.0f} LUFS).")
    if info.subtype and "PCM" not in info.subtype and "FLOAT" not in info.subtype:
        warnings.append(f"Encoded as {info.subtype}; lossless PCM WAV is preferred.")
    return ReferenceReport(
        path=path,
        ok=not errors,
        duration=dur,
        sample_rate=int(sr),
        channels=channels,
        peak_db=peak_db,
        loudness_lufs=lufs,
        errors=errors,
        warnings=warnings,
    )
