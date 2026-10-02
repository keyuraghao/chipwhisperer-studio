"""The logic capture data model and the queries the viewer, the decoders and the API run on it.

A channel is stored as its value at sample 0 plus the sorted sample indices where its value changes (``edges[k]`` is the first sample with the new value). The value at sample ``i`` is ``init ^ (number of edges <= i) & 1``, so any value, edge or per-pixel summary is a binary search away and memory grows with the number of transitions, not the number of samples. Analog channels (the ADC trace an analog-to-logic capture was thresholded from, or a Husky trace captured together with the logic analyser) are kept as float32 samples with their own rate and start time on the same time base.

Times: sample ``i`` of a capture is at ``(i - trigger) / samplerate`` seconds, so t = 0 is the trigger.
"""
from __future__ import annotations

import itertools
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

PALETTE = ["#f2b134", "#5aa2ff", "#2fbf9b", "#f0645f", "#b98cff", "#ff9f43", "#4dd0e1", "#ff7ab6", "#9ccc65", "#a1887f", "#7986cb", "#e57373"]
_ids = itertools.count(1)


def edge_dtype(n: int):
    """int32 transition indices while they fit, else int64."""
    return np.int32 if n < 2 ** 31 - 1 else np.int64


def ss(a: np.ndarray, v, side: str = "left"):
    """``np.searchsorted`` on an integer sample array without converting the array: numpy would copy a whole int32 edge list to compare it with int64 or float keys, so the keys are cast to its dtype instead (floats rounded the way integer positions compare: down for side right, up for side left)."""
    v = np.asarray(v)
    if a.dtype.kind in "iu":
        if v.dtype.kind == "f":
            v = np.floor(v) if side == "right" else np.ceil(v)
        if v.dtype != a.dtype:
            info = np.iinfo(a.dtype)
            v = np.clip(v, info.min, info.max).astype(a.dtype)
    r = np.searchsorted(a, v, side=side)
    return int(r) if np.ndim(r) == 0 else r


def edges_from_bits(bits: np.ndarray, offset: int = 0) -> np.ndarray:
    """Sample indices where a 0/1 array changes value (``offset`` is added)."""
    b = np.asarray(bits).astype(np.int8, copy=False)
    if b.shape[0] < 2:
        return np.zeros(0, np.int64)
    return np.flatnonzero(b[1:] != b[:-1]).astype(np.int64) + 1 + offset


class Channel:
    """One digital channel: name, display settings and its transitions."""

    __slots__ = ("name", "init", "edges", "color", "hidden", "group")

    def __init__(self, name: str, init: int, edges: np.ndarray, color: Optional[str] = None, hidden: bool = False, group: Optional[str] = None):
        self.name = str(name)
        self.init = int(init) & 1
        self.edges = np.asarray(edges)
        self.color = color
        self.hidden = bool(hidden)
        self.group = group

    def value_at(self, idx):
        """Value(s) at sample index or index array."""
        k = ss(self.edges, idx, side="right")
        return (self.init ^ (k & 1)).astype(np.int8) if isinstance(k, np.ndarray) else int(self.init ^ (int(k) & 1))

    def dense(self, start: int, stop: int) -> np.ndarray:
        """Samples ``start..stop-1`` as a uint8 0/1 array."""
        n = max(0, stop - start)
        out = np.zeros(n + 1, np.int8)
        lo = ss(self.edges, start, side="right")
        hi = ss(self.edges, stop - 1, side="right")
        if hi > lo:
            out[self.edges[lo:hi] - start] = 1
        v0 = self.init ^ (int(lo) & 1)
        res = np.cumsum(out[:n], dtype=np.int64) & 1
        return (res ^ v0).astype(np.uint8)

    def info(self, index: int) -> Dict[str, Any]:
        return {"index": index, "name": self.name, "color": self.color, "hidden": self.hidden, "group": self.group, "edges": int(self.edges.shape[0]), "init": self.init}


class AnalogChannel:
    """An analog trace on the capture's time base: sample ``k`` is at ``t0 + k / samplerate`` seconds."""

    __slots__ = ("name", "data", "samplerate", "t0", "color", "hidden", "threshold")

    def __init__(self, name: str, data: np.ndarray, samplerate: float, t0: float = 0.0, color: Optional[str] = None, threshold: Optional[List[float]] = None):
        self.name = name
        self.data = np.asarray(data, dtype=np.float32)
        self.samplerate = float(samplerate)
        self.t0 = float(t0)
        self.color = color
        self.hidden = False
        self.threshold = threshold

    def info(self, index: int) -> Dict[str, Any]:
        d = self.data
        return {"index": index, "name": self.name, "color": self.color, "hidden": self.hidden, "samples": int(d.shape[0]), "samplerate": self.samplerate, "t0": self.t0,
                "min": float(d.min()) if d.shape[0] else 0.0, "max": float(d.max()) if d.shape[0] else 0.0, "threshold": self.threshold}


class LogicCapture:
    """A logic capture: digital channels as transition lists, optional analog channels, sample rate and trigger position."""

    def __init__(self, channels: Sequence[Channel], n: int, samplerate: float, trigger: int = 0, source: str = "", name: str = "", meta: Optional[Dict[str, Any]] = None, analog: Optional[List[AnalogChannel]] = None):
        if samplerate <= 0:
            raise ValueError("the sample rate must be positive")
        self.id = f"c{next(_ids)}"
        self.channels: List[Channel] = list(channels)
        self.n = int(n)
        self.samplerate = float(samplerate)
        self.trigger = int(trigger)
        self.source = source
        self.name = name or source or "capture"
        self.meta: Dict[str, Any] = dict(meta or {})
        self.analog: List[AnalogChannel] = list(analog or [])
        self.created = time.time()
        self.version = 0  # bumped when channel data changes (decoder results are cached per version)
        for i, ch in enumerate(self.channels):
            if not ch.color:
                ch.color = PALETTE[i % len(PALETTE)]
        for i, a in enumerate(self.analog):
            if not a.color:
                a.color = PALETTE[(len(self.channels) + i) % len(PALETTE)]

    # --- construction ----------------------------------------------------------------
    @classmethod
    def from_bits(cls, bits: Dict[str, np.ndarray], samplerate: float, trigger: int = 0, **kw) -> "LogicCapture":
        """From dense 0/1 arrays of equal length, one per channel name."""
        names = list(bits)
        n = int(np.asarray(bits[names[0]]).shape[0]) if names else 0
        chans = []
        for name in names:
            b = np.asarray(bits[name])
            if b.shape[0] != n:
                raise ValueError("all channels need the same number of samples")
            chans.append(Channel(name, int(b[0]) if n else 0, edges_from_bits(b).astype(edge_dtype(n))))
        return cls(chans, n, samplerate, trigger, **kw)

    @classmethod
    def from_edges(cls, edges: Dict[str, Any], n: int, samplerate: float, trigger: int = 0, **kw) -> "LogicCapture":
        """From ``{name: (init, edge_indices)}``."""
        chans = [Channel(name, init, np.asarray(e, dtype=edge_dtype(n))) for name, (init, e) in edges.items()]
        return cls(chans, n, samplerate, trigger, **kw)

    # --- basics --------------------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.n / self.samplerate

    def t(self, idx) -> Any:
        """Seconds relative to the trigger for a sample index (scalar or array)."""
        return (np.asarray(idx, dtype=np.float64) - self.trigger) / self.samplerate if isinstance(idx, np.ndarray) else (float(idx) - self.trigger) / self.samplerate

    def idx(self, t: float) -> float:
        return float(t) * self.samplerate + self.trigger

    def channel(self, ref) -> Channel:
        return self.channels[self.ch_index(ref)]

    def ch_index(self, ref) -> int:
        """A channel by index (int or digit string) or by name."""
        if isinstance(ref, (int, np.integer)):
            i = int(ref)
        elif isinstance(ref, str) and ref.strip().lstrip("-").isdigit() and not any(c.name == ref for c in self.channels):
            i = int(ref)
        else:
            for k, c in enumerate(self.channels):
                if c.name == ref:
                    return k
            low = str(ref).lower()
            for k, c in enumerate(self.channels):
                if c.name.lower() == low:
                    return k
            raise KeyError(f"no channel {ref!r}; channels are {', '.join(c.name for c in self.channels)}")
        if not 0 <= i < len(self.channels):
            raise KeyError(f"channel index {i} out of range (0..{len(self.channels) - 1})")
        return i

    def summary(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "source": self.source, "samples": self.n, "samplerate": self.samplerate, "trigger": self.trigger, "duration": self.duration,
                "t_start": self.t(0), "t_end": self.t(self.n), "created": self.created, "channels": [c.info(i) for i, c in enumerate(self.channels)],
                "analog": [a.info(i) for i, a in enumerate(self.analog)], "meta": self.meta, "edges": int(sum(c.edges.shape[0] for c in self.channels))}

    def packed(self, start: int, stop: int, channels: Optional[Sequence[int]] = None) -> np.ndarray:
        """Samples ``start..stop-1`` with channel k in bit k of a uint8/16/32/64 word (for .sr export)."""
        chans = list(range(len(self.channels))) if channels is None else list(channels)
        dt = np.uint8 if len(chans) <= 8 else np.uint16 if len(chans) <= 16 else np.uint32 if len(chans) <= 32 else np.uint64
        out = np.zeros(max(0, stop - start), dt)
        for bit, ci in enumerate(chans):
            out |= self.channels[ci].dense(start, stop).astype(dt) << dt(bit)
        return out

    # --- visible range queries ------------------------------------------------------------
    def view(self, a: float, b: float, px: int, channels: Optional[Iterable[int]] = None, buses: Optional[List[Dict[str, Any]]] = None, analog: bool = True) -> Dict[str, Any]:
        """What the viewer needs to draw samples ``a..b`` across ``px`` pixels, never more than about ``2 * px`` numbers per row.

        A channel comes back as its exact edges when there are at most ``px`` of them in range, else as one character per pixel bucket: '0' or '1' when the level is constant over the bucket and '2' when it changes inside it.
        """
        px = int(min(max(px, 8), 16384))
        a = max(0.0, float(a))
        b = min(float(self.n), float(b))
        if b <= a:
            b = min(float(self.n), a + 1)
        ia, ib = int(np.floor(a)), int(np.ceil(b))
        spp = (b - a) / px
        idx = list(range(len(self.channels))) if channels is None else [int(c) for c in channels if 0 <= int(c) < len(self.channels)]
        out_ch = []
        bounds = None
        for ci in idx:
            ch = self.channels[ci]
            e = ch.edges
            lo = int(ss(e, ia, side="right"))
            hi = int(ss(e, ib, side="right"))
            v0 = ch.init ^ (lo & 1)
            if hi - lo <= px:
                out_ch.append({"i": ci, "v0": v0, "edges": e[lo:hi].tolist()})
                continue
            if bounds is None:
                bounds = np.floor(a + spp * np.arange(px + 1)).astype(np.int64)
            k = ss(e, bounds, side="right")
            counts = np.diff(k)
            start_vals = ch.init ^ (k[:-1] & 1)
            codes = np.where(counts > 0, 2, start_vals).astype(np.uint8) + 48
            out_ch.append({"i": ci, "v0": v0, "b": codes.tobytes().decode("ascii")})
        res: Dict[str, Any] = {"a": a, "b": b, "px": px, "step": spp, "channels": out_ch}
        if buses:
            res["buses"] = [self._bus_view(bus, ia, ib, a, spp, px) for bus in buses]
        if analog and self.analog:
            res["analog"] = [self._analog_view(k, a, b, px) for k, an in enumerate(self.analog) if not an.hidden]
        return res

    def bus_value(self, members: Sequence[int], idx: int) -> int:
        v = 0
        for ci in members:
            v = (v << 1) | self.channels[ci].value_at(idx)
        return v

    def _bus_view(self, bus: Dict[str, Any], ia: int, ib: int, a: float, spp: float, px: int) -> Dict[str, Any]:
        """Run-length segments of a bus value over the range: [[start, end, value or None]]; None marks a stretch too dense to label."""
        members = [self.ch_index(c) for c in bus.get("channels", [])]
        if not members:
            return {"runs": []}
        ranges = []
        for ci in members:
            e = self.channels[ci].edges
            ranges.append((e, int(ss(e, ia, side="right")), int(ss(e, ib, side="right"))))
        total = sum(hi - lo for _, lo, hi in ranges)
        if total <= max(8, px // 6):
            ch_edges = np.unique(np.concatenate([e[lo:hi] for e, lo, hi in ranges])).astype(np.int64)
            starts = np.concatenate(([ia], ch_edges)).astype(np.int64)
            vals = np.zeros(starts.shape[0], np.int64)
            for ci in members:
                vals = (vals << 1) | self.channels[ci].value_at(starts).astype(np.int64)
            ends = np.concatenate((starts[1:], [ib]))
            return {"runs": [[int(s_), int(e_), int(v)] for s_, e_, v in zip(starts, ends, vals)]}
        # dense: one bucket per 4 pixels; a bucket where any member changes is busy, constant buckets keep their value
        nb = max(1, px // 4)
        bounds = np.floor(ia + (ib - ia) / nb * np.arange(nb + 1)).astype(np.int64)
        counts = np.zeros(nb, np.int64)
        vals = np.zeros(nb, np.int64)
        for (e, _lo, _hi), ci in zip(ranges, members):
            k = ss(e, bounds, side="right")
            counts += np.diff(k)
            vals = (vals << 1) | (self.channels[ci].init ^ (k[:-1] & 1)).astype(np.int64)
        runs: List[List[Any]] = []
        for i in range(nb):
            v = None if counts[i] else int(vals[i])
            if runs and runs[-1][2] == v:
                runs[-1][1] = int(bounds[i + 1])
            else:
                runs.append([int(bounds[i]), int(bounds[i + 1]), v])
        return {"runs": runs}

    def _analog_view(self, k: int, a: float, b: float, px: int) -> Dict[str, Any]:
        an = self.analog[k]
        # analog index for a logic sample index s: ((s - trigger) / sr - t0) * sr_a
        scale = an.samplerate / self.samplerate
        off = (-self.trigger / self.samplerate - an.t0) * an.samplerate
        n = an.data.shape[0]
        lo = max(0, int(np.floor(a * scale + off)) - 1)
        hi = min(n, int(np.ceil(b * scale + off)) + 2)
        base = {"i": k, "name": an.name, "color": an.color, "lo": float(an.data.min()) if n else 0.0, "hi": float(an.data.max()) if n else 1.0, "threshold": an.threshold,
                "span": [float((0 - off) / scale), float((n - off) / scale)]}
        if hi <= lo:
            return {**base, "x": [], "y": []}
        to_logic = lambda ai: (np.asarray(ai, dtype=np.float64) - off) / scale  # noqa: E731
        if hi - lo <= 2 * px:
            xs = np.arange(lo, hi)
            return {**base, "x": np.round(to_logic(xs), 3).tolist(), "y": np.round(an.data[lo:hi].astype(np.float64), 5).tolist()}
        edges = np.linspace(lo, hi, px + 1).astype(np.int64)
        edges = np.unique(edges)
        seg = an.data[lo:hi]
        rel = edges[:-1] - lo
        mins = np.minimum.reduceat(seg, rel)
        maxs = np.maximum.reduceat(seg, rel)
        return {**base, "x": np.round(to_logic(edges[:-1]), 3).tolist(), "w": float((hi - lo) / px / scale), "min": np.round(mins.astype(np.float64), 5).tolist(), "max": np.round(maxs.astype(np.float64), 5).tolist()}

    # --- measurements ----------------------------------------------------------------
    def measure(self, ref, a: Optional[float] = None, b: Optional[float] = None) -> Dict[str, Any]:
        """Edge counts, frequency, period, duty cycle and pulse widths of one channel between sample ``a`` and ``b`` (default: the whole capture)."""
        ci = self.ch_index(ref)
        ch = self.channels[ci]
        a = 0 if a is None else max(0, int(np.floor(a)))
        b = self.n if b is None else min(self.n, int(np.ceil(b)))
        if b < a:
            a, b = b, a
        e = ch.edges
        lo = int(ss(e, a, side="right"))
        hi = int(ss(e, b, side="right"))
        seg = e[lo:hi].astype(np.int64)
        k = np.arange(lo, hi)
        newval = ch.init ^ ((k + 1) & 1)
        rising = seg[newval == 1]
        falling = seg[newval == 0]
        sr = self.samplerate
        out: Dict[str, Any] = {"channel": ch.name, "index": ci, "from": a, "to": b, "t_from": self.t(a), "t_to": self.t(b), "duration": (b - a) / sr,
                               "edges": int(seg.shape[0]), "rising": int(rising.shape[0]), "falling": int(falling.shape[0]), "level_at_start": ch.value_at(a)}

        def stats(widths):
            if widths.shape[0] == 0:
                return None
            w = widths / sr
            return {"min": float(w.min()), "max": float(w.max()), "avg": float(w.mean()), "count": int(w.shape[0])}
        # complete pulses only: a high pulse is a rising edge followed by a falling edge inside the range
        high = low = np.zeros(0)
        if seg.shape[0] >= 2:
            d = np.diff(seg)
            first_new = newval[:-1]
            high = d[first_new == 1]
            low = d[first_new == 0]
        out["high"] = stats(high)
        out["low"] = stats(low)
        per = None
        for edges_ in (rising, falling):
            if edges_.shape[0] >= 2:
                p = np.diff(edges_)
                per = p if per is None else np.concatenate((per, p))
        if per is not None and per.shape[0]:
            out["period"] = {"min": float(per.min() / sr), "max": float(per.max() / sr), "avg": float(per.mean() / sr), "count": int(per.shape[0])}
            out["frequency"] = float(sr / per.mean())
        else:
            out["period"] = None
            out["frequency"] = None
        if high.shape[0] and low.shape[0]:
            out["duty"] = float(high.mean() / (high.mean() + low.mean()))
        elif seg.shape[0] == 0:
            out["duty"] = float(ch.value_at(a))
        else:
            out["duty"] = None
        return out

    # --- search ------------------------------------------------------------------------
    def next_edge(self, ref, frm: float, direction: int = 1, kind: str = "any") -> Optional[int]:
        """The next (direction 1) or previous (-1) edge of a channel strictly after/before sample ``frm``; kind any, rising or falling."""
        ch = self.channel(ref)
        e = ch.edges
        if direction >= 0:
            k = int(ss(e, np.floor(frm), side="right"))
            while k < e.shape[0]:
                if kind == "any" or (ch.init ^ ((k + 1) & 1)) == (1 if kind == "rising" else 0):
                    return int(e[k])
                k += 1
            return None
        k = int(ss(e, np.ceil(frm), side="left")) - 1
        while k >= 0:
            if kind == "any" or (ch.init ^ ((k + 1) & 1)) == (1 if kind == "rising" else 0):
                return int(e[k])
            k -= 1
        return None

    def find_pattern(self, pattern: Dict[int, int], frm: float, direction: int = 1, edge: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """First sample after (or before) ``frm`` where every channel in ``pattern`` ({index: 0/1}) has its level; with ``edge`` ({channel, kind}) only at that channel's rising/falling/any edges. Without an edge the match is where the pattern becomes true."""
        if edge is not None:
            ech = self.channels[self.ch_index(edge["channel"])]
            k = np.arange(ech.edges.shape[0])
            cand = ech.edges.astype(np.int64)
            kind = edge.get("kind", "any")
            if kind != "any":
                cand = cand[(ech.init ^ ((k + 1) & 1)) == (1 if kind == "rising" else 0)]
        else:
            parts = [self.channels[ci].edges.astype(np.int64) for ci in pattern]
            cand = np.unique(np.concatenate(parts + [np.array([0], np.int64)])) if parts else np.zeros(0, np.int64)
        if direction >= 0:
            cand = cand[cand > frm]
        else:
            cand = cand[cand < frm][::-1]
        chunk = 1 << 16
        for s in range(0, cand.shape[0], chunk):
            c = cand[s:s + chunk]
            ok = np.ones(c.shape[0], bool)
            for ci, v in pattern.items():
                ok &= self.channels[ci].value_at(c) == v
                if not ok.any():
                    break
            if edge is None and ok.any():
                # the pattern must become true here: it was false one sample before
                prev = np.ones(c.shape[0], bool)
                for ci, v in pattern.items():
                    prev &= self.channels[ci].value_at(np.maximum(c - 1, 0)) == v
                ok &= ~prev | (c == 0)
            hit = np.flatnonzero(ok)
            if hit.shape[0]:
                return int(c[hit[0]])
        return None


def parse_pattern(text: str, order: Sequence[int]) -> Dict[int, int]:
    """'1X0' over channels in ``order`` (first character = first channel) to {index: level}; X, x, - and . are don't care."""
    out = {}
    s = "".join(ch for ch in str(text) if not ch.isspace())
    if len(s) > len(order):
        raise ValueError(f"the pattern has {len(s)} positions but only {len(order)} channels are shown")
    for ch, ci in zip(s, order):
        if ch in "01":
            out[int(ci)] = int(ch)
        elif ch not in "Xx-.?":
            raise ValueError(f"pattern characters are 0, 1 and X, not {ch!r}")
    if not out:
        raise ValueError("the pattern needs at least one 0 or 1")
    return out


def schmitt(x: np.ndarray, level: float, hysteresis: float = 0.0) -> np.ndarray:
    """Threshold an analog trace with hysteresis: high above level + h/2, low below level - h/2, otherwise keep the previous state."""
    x = np.asarray(x, dtype=np.float64)
    n = x.shape[0]
    if n == 0:
        return np.zeros(0, np.uint8)
    h = abs(float(hysteresis)) / 2
    hi = x > level + h
    lo = x < level - h
    state = np.full(n, -1, np.int8)
    state[hi] = 1
    state[lo] = 0
    known = state >= 0
    if not known.any():
        return np.full(n, 1 if x[0] > level else 0, np.uint8)
    idx = np.where(known, np.arange(n), -1)
    np.maximum.accumulate(idx, out=idx)
    first = int(np.argmax(known))
    out = np.empty(n, np.uint8)
    out[first:] = state[idx[first:]]
    out[:first] = 1 if x[0] > level else 0
    return out


def auto_level(x: np.ndarray):
    """Midpoint of the 2nd and 98th percentiles and a hysteresis of 10 % of that swing."""
    if x.shape[0] == 0:
        return 0.0, 0.0
    lo, hi = np.percentile(x, [2, 98])
    return float((lo + hi) / 2), float((hi - lo) * 0.1)
