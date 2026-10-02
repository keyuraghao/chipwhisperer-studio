"""From cycles to ADC samples: the mapping, a power model of the emulated execution and the automatic alignment against captured traces.

Mapping (cycles relative to the trigger to sample indices of a captured trace)::

    sample = cycle * samples_per_cycle * scale - adc.offset / decimate + adc.presamples + shift

with ``samples_per_cycle = adc_freq / target_clock / decimate`` (4 for ChipWhisperer's default clkgen_x4 ADC clock). The ADC offset counts ADC clock cycles before recording starts, so it is divided by the decimation like the cycles are; pre-trigger samples are recorded samples. ``scale`` and ``shift`` start at 1 and 0; the alignment fits them, and the user can nudge them.

Power model: every executed instruction draws a base current for its class plus a data dependent part, the Hamming weight of the value it loaded, stored or computed and the Hamming distance to the previous such value (the usual CMOS leakage model), spread over the instruction's cycles. The same model makes the simulator's traces when it runs real firmware, so a CPA on simulated traces finds the key where the firmware handles the S-box outputs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np

from cwstudio.codemap import cycles as cy
from cwstudio.codemap.emu import Run

HW8 = np.array([bin(i).count("1") for i in range(256)], np.int64)
BASE = np.ones(len(cy.CLASS_NAMES))
BASE[[cy.LOAD, cy.STORE, cy.LDM, cy.STM, cy.LOADPC]] = 1.15
BASE[[cy.MUL, cy.MLA, cy.MULL, cy.DIV]] = 1.3
BASE[[cy.BRANCH, cy.CALL, cy.RET, cy.JUMPREG, cy.TBB]] = 0.9
HW_WEIGHT = 0.4
HD_WEIGHT = 0.2
IDLE = 1.0
EDGE_PHASE = 0.35  # phase of the model against the clock edges: lines up model_samples with synth_trace (current pulse right after the edge, the ADC sampling at instants), so traces of the simulator align at shift 0; on hardware the ADC clock phase is unknown and the alignment fits it


@dataclass
class Mapping:
    spc: float = 4.0  # samples per cycle (nominal)
    scale: float = 1.0
    shift: float = 0.0
    adc_offset: int = 0
    presamples: int = 0
    adc_freq: Optional[float] = None
    target_freq: Optional[float] = None
    decimate: int = 1

    @property
    def a(self) -> float:
        return self.spc * self.scale

    @property
    def b(self) -> float:
        return -self.adc_offset / max(1, self.decimate) + self.presamples + self.shift

    def sample(self, cycle):
        return np.asarray(cycle, np.float64) * self.a + self.b

    def cycle(self, sample):
        return (np.asarray(sample, np.float64) - self.b) / self.a

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["samples_per_cycle"] = self.a
        d["intercept"] = self.b  # sample index of cycle 0 (the trigger): sample = cycle * samples_per_cycle + intercept
        return d

    @classmethod
    def from_scope(cls, scope, **over) -> "Mapping":
        """Nominal mapping from a connected scope's clocks and ADC settings (defaults: 7.37 MHz target, x4 ADC)."""
        adc = tgt = None
        off = pre = 0
        dec = 1
        if scope is not None:
            try:
                adc = float(scope.clock.adc_freq)
            except Exception:  # noqa: BLE001
                adc = None
            for path in ("clock.clkgen_freq", "clock.freq_ctr"):
                try:
                    obj = scope
                    for p in path.split("."):
                        obj = getattr(obj, p)
                    tgt = float(obj)
                    if tgt > 0:
                        break
                except Exception:  # noqa: BLE001
                    tgt = None
            for name, default in (("offset", 0), ("presamples", 0), ("decimate", 1)):
                try:
                    v = int(getattr(scope.adc, name))
                except Exception:  # noqa: BLE001
                    v = default
                if name == "offset":
                    off = v
                elif name == "presamples":
                    pre = v
                else:
                    dec = max(1, v)
        if not adc or not tgt:
            adc, tgt = adc or 4 * 7.37e6, tgt or 7.37e6
        m = cls(spc=adc / tgt / dec, adc_offset=off, presamples=pre, adc_freq=adc, target_freq=tgt, decimate=dec)
        for k, v in over.items():
            if v is not None and hasattr(m, k):
                setattr(m, k, type(getattr(m, k))(v) if getattr(m, k) is not None else v)
        return m


def popcount(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, np.int64) & 0xFFFFFFFF
    return HW8[v & 0xFF] + HW8[(v >> 8) & 0xFF] + HW8[(v >> 16) & 0xFF] + HW8[(v >> 24) & 0xFF]


def instruction_power(run: Run) -> np.ndarray:
    """Model power of each executed instruction (per cycle of that instruction)."""
    n = len(run.pcs)
    if not n:
        return np.zeros(0)
    base = BASE[np.clip(run.cls.astype(np.int64), 0, len(BASE) - 1)]
    leak = run.leak
    valid = leak >= 0
    norm = 8.0 / max(8, run.leak_bits)  # a 32-bit word counts like 4 bytes, scaled to one byte's range
    hw = np.where(valid, popcount(np.where(valid, leak, 0)), 0) * norm
    # Hamming distance to the previous leaking value (bus transitions)
    pos = np.where(valid, np.arange(n), -1)
    last = np.maximum.accumulate(pos)
    prev = np.empty(n, np.int64)
    prev[0] = -1
    prev[1:] = last[:-1]
    pv = np.where(prev >= 0, leak[np.maximum(prev, 0)], 0)
    hd = np.where(valid & (prev >= 0), popcount((np.where(valid, leak, 0) ^ pv)), 0) * norm
    return base + HW_WEIGHT * hw + HD_WEIGHT * hd


def cycle_power(run: Run) -> np.ndarray:
    """Model power per clock cycle from the start of the run."""
    p = instruction_power(run)
    dur = np.maximum(run.dur, 0)
    return np.repeat(p, dur)


def model_samples(P: np.ndarray, t0: int, mapping: Mapping, n: int, first: int = 0) -> np.ndarray:
    """Average model power in each of ``n`` samples starting at sample index ``first``. ``P`` is per cycle from the run start, ``t0`` the trigger cycle in the same counting."""
    k = np.arange(first, first + n + 1, dtype=np.float64) - 0.5  # sample boundaries
    c = mapping.cycle(k) + t0 + EDGE_PHASE  # absolute cycles at the boundaries; a cycle's current flows mostly just after its clock edge
    cum = np.concatenate([[0.0], np.cumsum(P, dtype=np.float64)])
    L = len(P)
    inside = np.interp(np.clip(c, 0, L), np.arange(L + 1, dtype=np.float64), cum)
    val = inside + np.where(c < 0, c * IDLE, 0) + np.where(c > L, (c - L) * IDLE, 0)
    width = np.diff(c)
    return np.diff(val) / np.where(width > 0, width, 1)


def synth_trace(P: np.ndarray, t0: int, mapping: Mapping, n: int, rng: np.random.Generator, noise: float = 0.012, amp: float = 0.03, gain: float = 1.0) -> np.ndarray:
    """A simulated ADC trace of ``n`` samples for per-cycle power ``P``: each clock cycle draws its power as a sharp pulse after the edge (so the clock shows as ripple), the measured voltage drops with current (as on ChipWhisperer's shunt), plus noise."""
    k = np.arange(n, dtype=np.float64)
    c = mapping.cycle(k) + t0
    ci = np.floor(c).astype(np.int64)
    ph = c - ci
    L = len(P)
    pw = np.where((ci >= 0) & (ci < L), P[np.clip(ci, 0, max(L - 1, 0))] if L else IDLE, IDLE)
    shape = 0.35 + 2.6 * np.exp(-ph * 6.0)  # averages to about 0.78 per cycle
    wave = -amp * gain * pw * shape + 0.06 * gain + rng.normal(0.0, noise, n)
    return np.clip(wave, -0.5, 0.5).astype(np.float32)


# --- alignment ---------------------------------------------------------------------------------------
def _box(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return x
    k = np.ones(w) / w
    return np.convolve(x, k, mode="same")


def _prep(x: np.ndarray, spc: float) -> np.ndarray:
    """Remove the clock ripple (average over a cycle) and the slow baseline (subtract a 48-cycle moving average)."""
    x = np.asarray(x, np.float64)
    y = _box(x, max(1, int(round(spc))))
    y = y - _box(y, max(3, int(round(48 * spc))))
    return y


def _ncc(x: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of ``x`` (length L) against every window of ``m`` (length M >= L): r[j] = corr(x, m[j:j+L])."""
    L, M = len(x), len(m)
    xz = x - x.mean()
    xn = np.sqrt((xz * xz).sum()) or 1.0
    nfft = 1 << int(np.ceil(np.log2(L + M)))
    num = np.fft.irfft(np.fft.rfft(m, nfft) * np.conj(np.fft.rfft(xz, nfft)), nfft)[: M - L + 1]
    c1 = np.concatenate([[0.0], np.cumsum(m)])
    c2 = np.concatenate([[0.0], np.cumsum(m * m)])
    s1 = c1[L:] - c1[:-L]
    s2 = c2[L:] - c2[:-L]
    var = np.maximum(s2 - s1 * s1 / L, 1e-12)
    return num / (xn * np.sqrt(var))


def align(trace: np.ndarray, P: np.ndarray, t0: int, mapping: Mapping, max_shift: Optional[int] = None, scale_range: Tuple[float, float] = (0.9, 1.1), coarse: int = 41, fine: int = 21) -> Dict[str, Any]:
    """Fit ``shift`` (samples) and ``scale`` of ``mapping`` so the power model best matches ``trace`` (the mean trace, or one trace). Returns the fit with its correlation and a confidence in 0..1.

    The shift is searched within ``max_shift`` samples of the nominal mapping (default 200 target cycles, at least 256 samples): the trigger pins cycle 0 to within a few cycles on real hardware, and a wider search lets repetitive code (AES rounds, loops) match a whole round early or late. Confidence combines the correlation with how far the best peak stands above the best other local peak in the window (outside the main lobe of a couple of cycles), so a periodic match that could be off by a loop iteration reads as low.
    """
    x = np.asarray(trace, np.float64)
    L = len(x)
    if L < 16 or not len(P):
        raise ValueError("need a trace and an emulated run to align")
    spc = mapping.spc
    if max_shift is None:
        max_shift = int(min(max(256, round(200 * spc)), 20000))
    xp = _prep(x, spc)
    base = Mapping(**{**mapping.__dict__, "shift": 0.0})

    def scan(scales):
        out = []
        for s in scales:
            base.scale = float(s)
            m = model_samples(P, t0, base, L + 2 * max_shift, first=-max_shift)
            mp = _prep(m, spc * s)
            r = _ncc(xp, mp)
            j = int(np.argmax(np.abs(r)))
            out.append((abs(r[j]), float(s), j - max_shift, float(r[j]), r))
        return out
    lo, hi = scale_range
    res = scan(np.linspace(lo, hi, coarse))
    res.sort(key=lambda t: -t[0])
    step = (hi - lo) / max(1, coarse - 1)
    s0 = res[0][1]
    res2 = scan(np.linspace(max(lo, s0 - step), min(hi, s0 + step), fine))
    res2.sort(key=lambda t: -t[0])
    a, s, lag, r, curve = res2[0]
    # sub-sample peak position from a parabola through the peak and its neighbours
    j0 = lag + max_shift
    frac = 0.0
    if 0 < j0 < len(curve) - 1:
        y0, y1, y2 = abs(curve[j0 - 1]), abs(curve[j0]), abs(curve[j0 + 1])
        den = y0 - 2 * y1 + y2
        if den < 0:
            frac = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    # shift: model sample (k + lag) matches trace sample k, so the trace's sample k sits at model index k + lag
    shift = -(lag + frac)
    # confidence: correlation strength and how much the best peak stands out from the best one elsewhere
    guard = max(4, int(round(spc * s * 2)))
    ac = np.abs(curve)
    peak = np.zeros(len(ac), bool)  # local maxima: the slopes of the main peak are not a rival match
    if len(ac) > 2:
        peak[1:-1] = (ac[1:-1] >= ac[:-2]) & (ac[1:-1] >= ac[2:])
    peak[max(0, j0 - guard):j0 + guard + 1] = False
    second = float(ac[peak].max()) if peak.any() else 0.0
    distinct = max(0.0, 1.0 - second / a) if a > 0 else 0.0
    conf = float(np.clip(a * 1.25, 0, 1) * np.clip(distinct * 3, 0, 1))
    return {"shift": float(shift), "scale": float(s), "r": float(r), "abs_r": float(a), "second": second, "distinct": round(distinct, 4), "confidence": round(conf, 4),
            "inverted": bool(r < 0), "label": "high" if conf > 0.6 else "medium" if conf > 0.3 else "low", "max_shift": max_shift}
