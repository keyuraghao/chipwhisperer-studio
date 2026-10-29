"""From cycles to ADC samples: the mapping, a power model of the emulated execution and the automatic alignment against captured traces.

Mapping (cycles relative to the trigger to sample indices of a captured trace)::

    sample = cycle * samples_per_cycle * scale - adc.offset / decimate + adc.presamples + shift

with ``samples_per_cycle = adc_freq / target_clock / decimate`` (4 for ChipWhisperer's default clkgen_x4 ADC clock). The ADC offset counts ADC clock cycles before recording starts, so it is divided by the decimation like the cycles are; pre-trigger samples are recorded samples. ``scale`` and ``shift`` start at 1 and 0; the alignment fits them, and the user can nudge them.

Power model: every executed instruction draws a base current for its class plus a data dependent part, the Hamming weight of the value it loaded, stored or computed and the Hamming distance to the previous such value (the usual CMOS leakage model), spread over the instruction's cycles. The same model makes the simulator's traces when it runs real firmware, so a CPA on simulated traces finds the key where the firmware handles the S-box outputs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from cwstudio.codemap import cycles as cy
from cwstudio.codemap.emu import Run

HW8 = np.array([bin(i).count("1") for i in range(256)], np.int64)
_BITCOUNT = getattr(np, "bitwise_count", None)  # numpy 2.0+: one ufunc instead of four byte table lookups
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
    # local refinement: a piecewise linear correction in samples on top of the linear mapping, (cycle, delta) knots from the alignment's window fits; the emulator's cycle counts drift from the real core's (wait states, bus stalls, branch timing) and one scale cannot follow that
    knots: Optional[List[List[float]]] = None

    @property
    def a(self) -> float:
        return self.spc * self.scale

    @property
    def b(self) -> float:
        return -self.adc_offset / max(1, self.decimate) + self.presamples + self.shift

    def _warp(self, cycle):
        if not self.knots:
            return 0.0
        kc = np.fromiter((k[0] for k in self.knots), np.float64, len(self.knots))
        kd = np.fromiter((k[1] for k in self.knots), np.float64, len(self.knots))
        return np.interp(np.asarray(cycle, np.float64), kc, kd)  # flat beyond the first and last knot

    def sample(self, cycle):
        c = np.asarray(cycle, np.float64)
        return c * self.a + self.b + self._warp(c)

    def cycle(self, sample):
        s = np.asarray(sample, np.float64)
        if not self.knots:
            return (s - self.b) / self.a
        kc = np.fromiter((k[0] for k in self.knots), np.float64, len(self.knots))
        ks = kc * self.a + self.b + np.fromiter((k[1] for k in self.knots), np.float64, len(self.knots))  # the knots' sample positions (monotonic by construction)
        inside = np.interp(s, ks, kc)
        lo = (s - self.b - self.knots[0][1]) / self.a
        hi = (s - self.b - self.knots[-1][1]) / self.a
        return np.where(s < ks[0], lo, np.where(s > ks[-1], hi, inside))

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["samples_per_cycle"] = self.a
        d["intercept"] = self.b  # sample index of cycle 0 (the trigger): sample = cycle * samples_per_cycle + intercept
        if self.knots:
            deltas = [k[1] for k in self.knots]
            d["warp"] = {"knots": len(self.knots), "max_abs": round(float(max(abs(x) for x in deltas)), 2), "span": [round(float(min(deltas)), 2), round(float(max(deltas)), 2)]}
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
                setattr(m, k, type(getattr(m, k))(v) if getattr(m, k) is not None and k != "knots" else v)
        return m


def popcount(v: np.ndarray) -> np.ndarray:
    """Hamming weight of the low 32 bits of each value."""
    v = np.asarray(v, np.int64) & 0xFFFFFFFF
    if _BITCOUNT is not None:
        return _BITCOUNT(v.astype(np.uint32)).astype(np.int64)
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


def cumulative_power(P: np.ndarray) -> np.ndarray:
    """Running sum of the per-cycle power with a leading 0 (``cum[i]`` is the energy of cycles 0 to i-1), computed once per run for :func:`model_samples`."""
    return np.concatenate([[0.0], np.cumsum(P, dtype=np.float64)])


def model_samples(P: np.ndarray, t0: int, mapping: Mapping, n: int, first: int = 0, cum: Optional[np.ndarray] = None) -> np.ndarray:
    """Average model power in each of ``n`` samples starting at sample index ``first``. ``P`` is per cycle from the run start, ``t0`` the trigger cycle in the same counting; ``cum`` is :func:`cumulative_power` of ``P`` when the caller has it (the alignment resamples the same run many times)."""
    k = np.arange(first, first + n + 1, dtype=np.float64) - 0.5  # sample boundaries
    c = mapping.cycle(k) + t0 + EDGE_PHASE  # absolute cycles at the boundaries; a cycle's current flows mostly just after its clock edge
    if cum is None:
        cum = cumulative_power(P)
    L = len(P)
    cc = np.clip(c, 0, L)
    i = np.minimum(cc.astype(np.int64), max(L - 1, 0))  # the cycle each boundary falls in: linear interpolation of cum by hand (np.interp would search the whole run per boundary)
    inside = cum[i] + (cc - i) * P[i] if L else np.zeros_like(cc)
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


def _decimate(x: np.ndarray, d: int) -> np.ndarray:
    """Average over blocks of ``d`` samples (one point per clock cycle for the coarse search)."""
    if d <= 1:
        return x
    n = len(x) // d
    return x[: n * d].reshape(n, d).mean(axis=1)


def align(trace: np.ndarray, P: np.ndarray, t0: int, mapping: Mapping, max_shift: Optional[int] = None, scale_range: Tuple[float, float] = (0.9, 1.1), coarse: int = 41, fine: int = 7, cum: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """Fit ``shift`` (samples) and ``scale`` of ``mapping`` so the power model best matches ``trace`` (the mean trace, or one trace). Returns the fit with its correlation and a confidence in 0..1. ``cum`` is :func:`cumulative_power` of ``P`` if already known.

    The shift is searched within ``max_shift`` samples of the nominal mapping (default 200 target cycles, at least 256 samples): the trigger pins cycle 0 to within a few cycles on real hardware, and a wider search lets repetitive code (AES rounds, loops) match a whole round early or late. Confidence combines the correlation with how far the best peak stands above the best other local peak in the window (outside the main lobe of a couple of cycles), so a periodic match that could be off by a loop iteration reads as low.

    Two resolutions keep it fast on long traces: the ``coarse`` scales are scanned on the trace and model averaged to one point per clock cycle (the ripple is gone there anyway), then ``fine`` scales around the winner at the full sample rate, and a parabola through the three best gives the scale between grid points.
    """
    x = np.asarray(trace, np.float64)
    L = len(x)
    if L < 16 or not len(P):
        raise ValueError("need a trace and an emulated run to align")
    spc = mapping.spc
    if max_shift is None:
        max_shift = int(min(max(256, round(200 * spc)), 20000))
    xp = _prep(x, spc)
    base = Mapping(**{**mapping.__dict__, "shift": 0.0, "knots": None})
    if cum is None:
        cum = cumulative_power(P)
    dec = max(1, int(round(spc)))
    xd = _decimate(xp, dec)
    ms_d = max_shift // dec

    def scan(scales, full):
        """(abs correlation, scale, lag, correlation, curve) per scale; the curve is kept for the winner only (one per scale would be a gigabyte on a long trace)."""
        out = []
        for sc in scales:
            base.scale = float(sc)
            if full:
                m = model_samples(P, t0, base, L + 2 * max_shift, first=-max_shift, cum=cum)
                r = _ncc(xp, _prep(m, spc * sc))
                j = int(np.argmax(np.abs(r)))
                out.append((abs(r[j]), float(sc), j - max_shift, float(r[j]), r))
            else:
                m = model_samples(P, t0, base, (len(xd) + 2 * ms_d) * dec, first=-ms_d * dec, cum=cum)
                r = _ncc(xd, _decimate(_prep(m, spc * sc), dec))
                j = int(np.argmax(np.abs(r)))
                out.append((abs(r[j]), float(sc), (j - ms_d) * dec, float(r[j]), None))
        return out
    lo, hi = scale_range
    step = (hi - lo) / max(1, coarse - 1)
    s0 = max(scan(np.linspace(lo, hi, coarse), full=False))[1]
    grid = np.linspace(max(lo, s0 - step), min(hi, s0 + step), fine)
    res = scan(grid, full=True)
    k = int(np.argmax([r[0] for r in res]))
    a, s, lag, r, curve = res[k]
    if 0 < k < len(res) - 1:  # the scale between grid points from a parabola through the three best correlations, then one pass at that scale for the exact lag
        y0, y1, y2 = res[k - 1][0], a, res[k + 1][0]
        den = y0 - 2 * y1 + y2
        if den < 0:
            sx = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5)) * (grid[1] - grid[0]) + s
            cand = scan([sx], full=True)[0]
            if cand[0] >= a:
                a, s, lag, r, curve = cand
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


def refine(trace: np.ndarray, P: np.ndarray, t0: int, mapping: Mapping, windows: int = 24, min_cycles: int = 48, max_local: Optional[float] = None, min_r: float = 0.3, cum: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """Local refinement after :func:`align`: the trace is cut into ``windows`` pieces over the part the model covers and each piece is correlated with the model within ``max_local`` samples (default 6 cycles) of where the previous piece matched, so a drift that accumulates along the run (the emulator's cycle counts against the real core's wait states, bus stalls and branch timing) is followed window by window. The lags of the pieces that match (correlation at least ``min_r``) become the mapping's ``knots``, a piecewise linear correction on top of the linear mapping. Returns the knots, every window's correlation and lag, and whether the correction was applied (not when fewer than three windows match, or the correction would run backwards)."""
    x = np.asarray(trace, np.float64)
    L = len(x)
    if L < 16 or not len(P):
        raise ValueError("need a trace and an emulated run to refine")
    spc = mapping.a
    lin = Mapping(**{**mapping.__dict__, "knots": None})
    if cum is None:
        cum = cumulative_power(P)
    xp = _prep(x, mapping.spc)
    # the samples the run covers (the trigger window when there is one, else the whole run)
    s_lo = float(np.clip(lin.sample(-t0), 0, L))
    s_hi = float(np.clip(lin.sample(len(P) - t0), 0, L))
    if s_hi - s_lo < 2 * min_cycles * spc:
        s_lo, s_hi = 0.0, float(L)
    w = max(int(min_cycles * spc), int((s_hi - s_lo) / max(1, windows)))
    if max_local is None:
        max_local = max(4.0, 6.0 * spc, 0.08 * w)  # a drift of up to 8 percent of a window between neighbours stays in reach
    ml = int(round(max_local))
    starts = np.arange(int(s_lo), int(s_hi) - w + 1, w)
    pad = int(round(mapping.spc)) * 24  # the baseline filter of _prep needs context on both sides of the piece
    rows = []
    track = 0.0  # where the previous piece matched: the search for the next one is centred there
    reach = ml  # widened after a miss (up to four times) so the track is found again further on
    for st in starts.tolist():
        seg = xp[st:st + w]
        if seg.std() < 1e-12:
            rows.append({"start": st, "end": st + w, "r": 0.0, "lag": round(track, 2), "used": False})
            continue
        centre = int(round(track))
        first = st + centre - reach - pad
        m = model_samples(P, t0, lin, w + 2 * reach + 2 * pad, first=first, cum=cum)
        win = _prep(m, mapping.spc)[pad:pad + w + 2 * reach]  # model indices st + centre - reach .. st + centre + w + reach
        r = _ncc(seg, win)
        j = int(np.argmax(np.abs(r)))
        frac = 0.0
        if 0 < j < len(r) - 1:
            y0, y1, y2 = abs(r[j - 1]), abs(r[j]), abs(r[j + 1])
            den = y0 - 2 * y1 + y2
            if den < 0:
                frac = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
        lag = centre + j + frac - reach  # the trace piece sits at model index st + lag: the model must move by -lag to meet it
        good = bool(abs(r[j]) >= min_r)
        rows.append({"start": st, "end": st + w, "r": round(float(abs(r[j])), 4), "lag": round(float(lag), 2), "used": good})
        if good:
            track = float(lag)
            reach = ml
        else:
            reach = min(4 * ml, 2 * reach)
    used = [row for row in rows if row["used"]]
    knots: List[List[float]] = []
    applied = False
    reason = None
    if len(used) >= 3:
        cs = np.array([lin.cycle((row["start"] + row["end"]) / 2.0) for row in used])
        ds = np.array([-row["lag"] for row in used])
        # the corrected mapping must keep running forward: successive knots may not move closer than half a window
        if np.all(np.diff(cs * lin.a + ds) > w * 0.5):
            knots = [[round(float(c), 2), round(float(d), 3)] for c, d in zip(cs, ds)]
            applied = True
        else:
            reason = "the window lags do not form a forward mapping (repetitive code matched different iterations); correction not applied"
    else:
        reason = f"only {len(used)} of {len(rows)} windows match the model (correlation {min_r} or better): correction needs at least three"
    rs = [row["r"] for row in rows]
    return {"knots": knots, "windows": rows, "applied": applied, "reason": reason, "window_samples": int(w), "max_local": float(max_local),
            "mean_r": round(float(np.mean(rs)), 4) if rs else 0.0, "matched": len(used), "max_abs": round(float(max(abs(k[1]) for k in knots)), 2) if knots else 0.0}
