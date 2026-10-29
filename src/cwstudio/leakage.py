"""Leakage assessment and trace conditioning on the stored traces, numpy only: Welch's t-test (TVLA), signal-to-noise ratio by class, static alignment by cross-correlation, FIR and moving-average filters, and the spectrum.

Everything works on the (N, S) float32 matrix the store hands out and returns plain arrays; the session wraps the results for the API and writes conditioned traces back into the store.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from cwstudio.aes import HW, SBOX

TVLA_THRESHOLD = 4.5  # |t| above this at any sample is the usual "leaks" verdict


def welch_t(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Welch's t statistic per sample between two trace groups (N_a, S) and (N_b, S); 0 where either group has fewer than 2 traces."""
    na, nb = a.shape[0], b.shape[0]
    if na < 2 or nb < 2:
        return np.zeros(a.shape[1] if a.ndim == 2 else 0, np.float64)
    ma, mb = a.mean(axis=0, dtype=np.float64), b.mean(axis=0, dtype=np.float64)
    va, vb = a.var(axis=0, dtype=np.float64, ddof=1), b.var(axis=0, dtype=np.float64, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = (ma - mb) / np.sqrt(va / na + vb / nb)
    return np.nan_to_num(t, nan=0.0, posinf=0.0, neginf=0.0)


def ttest(waves: np.ndarray, textins: np.ndarray, fixed: bytes) -> Dict[str, Any]:
    """TVLA fixed-vs-random: traces whose plaintext equals ``fixed`` against the rest."""
    fixed_arr = np.frombuffer(bytes(fixed), np.uint8)
    n = min(textins.shape[1], fixed_arr.size)
    is_fixed = np.all(textins[:, :n] == fixed_arr[None, :n], axis=1) if n else np.zeros(waves.shape[0], bool)
    t = welch_t(waves[is_fixed], waves[~is_fixed])
    over = np.flatnonzero(np.abs(t) > TVLA_THRESHOLD)
    return {"t": t, "fixed": int(is_fixed.sum()), "random": int((~is_fixed).sum()), "max_abs_t": float(np.abs(t).max()) if t.size else 0.0, "argmax": int(np.abs(t).argmax()) if t.size else 0,
            "over_threshold": int(over.size), "threshold": TVLA_THRESHOLD, "leaks": bool(over.size), "first_over": int(over[0]) if over.size else None}


def snr(waves: np.ndarray, classes: np.ndarray) -> np.ndarray:
    """Signal-to-noise ratio per sample: variance of the class means over the mean within-class variance, classes an (N,) integer label per trace."""
    labels = np.unique(classes)
    if labels.size < 2:
        return np.zeros(waves.shape[1], np.float64)
    means, variances, counts = [], [], []
    for c in labels:
        sel = waves[classes == c]
        if sel.shape[0] < 2:
            continue
        means.append(sel.mean(axis=0, dtype=np.float64))
        variances.append(sel.var(axis=0, dtype=np.float64))
        counts.append(sel.shape[0])
    if len(means) < 2:
        return np.zeros(waves.shape[1], np.float64)
    means = np.asarray(means)
    w = np.asarray(counts, np.float64)[:, None]
    signal = (w * (means - (w * means).sum(axis=0) / w.sum()) ** 2).sum(axis=0) / w.sum()
    noise = (w * np.asarray(variances)).sum(axis=0) / w.sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = signal / noise
    return np.nan_to_num(out, nan=0.0, posinf=0.0)


def classes_for(kind: str, textins: np.ndarray, byte: int, key: Optional[bytes] = None) -> np.ndarray:
    """Class labels per trace: the plaintext byte itself, or the Hamming weight of the first-round S-box output under the known key (needs the key)."""
    pt = textins[:, byte].astype(np.int64)
    if kind == "byte":
        return pt
    if kind == "sbox_hw":
        if key is None or len(key) <= byte:
            raise ValueError("the S-box Hamming weight classes need the known key")
        return HW[SBOX[pt ^ int(key[byte])]].astype(np.int64)
    raise ValueError("classes must be byte or sbox_hw")


def align(waves: np.ndarray, ref: np.ndarray, start: int, end: int, max_shift: int) -> np.ndarray:
    """The shift (samples, positive = the trace is late) that best aligns each trace's window to the reference window, by maximum normalised cross-correlation within +-max_shift."""
    start, end = max(0, int(start)), min(waves.shape[1], int(end))
    ms = max(0, int(max_shift))
    seg = ref[start:end].astype(np.float64)
    seg = seg - seg.mean()
    norm = np.sqrt((seg * seg).sum()) or 1.0
    lo, hi = max(0, start - ms), min(waves.shape[1], end + ms)
    shifts = np.zeros(waves.shape[0], np.int64)
    n = end - start
    if n < 2 or hi - lo < n:
        return shifts
    win = waves[:, lo:hi].astype(np.float64)
    win = win - win.mean(axis=1, keepdims=True)
    sl = np.lib.stride_tricks.sliding_window_view(win, n, axis=1)  # (N, positions, n)
    corr = (sl * seg[None, None, :]).sum(axis=2)
    norms = np.sqrt((sl * sl).sum(axis=2))
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.nan_to_num(corr / (norms * norm))
    best = corr.argmax(axis=1)
    shifts[:] = (lo + best) - start
    return shifts


def shift_trace(w: np.ndarray, s: int) -> np.ndarray:
    """Move a trace earlier by ``s`` samples (its features were s samples late), padding the end with the edge value."""
    s = int(s)
    if s == 0:
        return w
    out = np.empty_like(w)
    if s > 0:
        out[:-s] = w[s:]
        out[-s:] = w[-1]
    else:
        out[-s:] = w[:s]
        out[:-s] = w[0]
    return out


def fir_kernel(kind: str, cutoff: float, taps: int = 63, cutoff2: Optional[float] = None) -> np.ndarray:
    """A windowed-sinc FIR kernel; cutoffs as fractions of the sample rate (0 to 0.5)."""
    taps = max(5, int(taps) | 1)
    n = np.arange(taps) - (taps - 1) / 2
    window = np.hamming(taps)

    def lowpass(fc):
        fc = min(max(float(fc), 1e-4), 0.5 - 1e-4)
        h = 2 * fc * np.sinc(2 * fc * n) * window
        return h / h.sum()
    if kind == "lowpass":
        return lowpass(cutoff)
    if kind == "highpass":
        h = -lowpass(cutoff)
        h[(taps - 1) // 2] += 1.0
        return h
    if kind == "bandpass":
        lo, hi = sorted((float(cutoff), float(cutoff2 if cutoff2 is not None else 0.5)))
        return lowpass(hi) - lowpass(lo)
    if kind == "moving_average":
        k = max(1, int(round(cutoff)))
        return np.ones(k) / k
    raise ValueError("kind must be lowpass, highpass, bandpass or moving_average")


def apply_filter(waves: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Convolve every trace with the kernel, keeping the length (edge samples use the edge value)."""
    k = kernel.size
    pad = k // 2
    out = np.empty_like(waves, dtype=np.float32)
    for i in range(waves.shape[0]):
        w = np.pad(waves[i].astype(np.float64), pad, mode="edge")
        out[i] = np.convolve(w, kernel, mode="valid")[: waves.shape[1]]
    return out


def spectrum(wave: np.ndarray, sample_rate: Optional[float] = None) -> Tuple[np.ndarray, np.ndarray]:
    """Magnitude spectrum (dB relative to the peak) of one trace and its frequency axis (Hz when the sample rate is known, else cycles per sample)."""
    w = np.asarray(wave, np.float64)
    w = w - w.mean()
    mag = np.abs(np.fft.rfft(w * np.hanning(w.size)))
    peak = mag.max() or 1.0
    db = 20 * np.log10(np.maximum(mag / peak, 1e-9))
    f = np.fft.rfftfreq(w.size, d=1.0 / sample_rate if sample_rate else 1.0)
    return f, db


def decimate_series(x: np.ndarray, max_points: int) -> Tuple[np.ndarray, int]:
    """Keep at most max_points points of a series for a plot: the absolute maximum of each bin, so peaks survive; returns (series, step)."""
    n = x.size
    if n <= max_points:
        return x, 1
    step = int(np.ceil(n / max_points))
    m = (n // step) * step
    blocks = x[:m].reshape(-1, step)
    idx = np.abs(blocks).argmax(axis=1)
    return blocks[np.arange(blocks.shape[0]), idx], step
