"""The simulator's advanced triggers: the Husky UART pattern, the Pro UART decoder, the edge counter, the ADC level, SAD and the trigger sequencer are evaluated on the signals the simulated target really produces.

The simulated target records its serial traffic since the scope was armed (the bytes it receives on its RX line, TIO2 by default, and sends on its TX line, TIO1 by default) and the commands that raise its trigger pin (TIO4). Those are laid out in time like on the wire (8 data bits, the target's parity and stop bits, at the target's baud rate, in ADC samples) with the rising edge of the trigger pin of the captured command at time 0. Each trigger then gives the time it fires relative to that edge (None: it never fires and the capture times out, as on the hardware), and the scope records its trace from there.
"""
from __future__ import annotations

import bisect
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("cwstudio.sim")

TRAFFIC_MAX = 256  # traffic entries kept between arms (serial console use without captures must not grow without bound)

# trigger.module each configured kind sets; a module changed afterwards (Scope tab) means the stored parameters no longer apply
MODULE_OF = {"uart_pattern": "UART", "uart_decode": "DECODEIO", "edge_counter": "edge_counter", "adc_level": "ADC", "sad": "SAD"}
SAD_LENGTH = {"pro": 128, "husky": 32, "huskyplus": 32}  # reference samples the SAD module compares
SAD_BITS = {"pro": 10, "husky": 8, "huskyplus": 8}  # resolution of the comparison (the threshold is in these units)


class Line:
    """A logic line as its level transitions: times (ADC samples, ascending) and the level after each; ``idle`` before the first."""

    def __init__(self, idle: int = 1):
        self.idle = idle
        self.t: List[float] = []
        self.v: List[int] = []

    def set(self, t: float, v: int) -> None:
        if self.level(t) == v:
            return
        if self.t and t <= self.t[-1]:
            return  # layout is in time order; anything else is ignored
        self.t.append(float(t))
        self.v.append(int(v))

    def level(self, t: float) -> int:
        i = bisect.bisect_right(self.t, t) - 1
        return self.idle if i < 0 else self.v[i]

    def edges(self, kind: str = "both") -> List[float]:
        """Times of rising, falling or both kinds of edges."""
        out = []
        for t, v in zip(self.t, self.v):
            if kind == "both" or (kind == "rising") == (v == 1):
                out.append(t)
        return out


def uart_bits(byte: int, data_bits: int = 8, parity: str = "none", stop_bits: float = 1) -> List[Tuple[int, float]]:
    """(level, length in bits) of one UART frame, LSB first."""
    bits = [(0, 1.0)] + [((byte >> i) & 1, 1.0) for i in range(data_bits)]
    if parity in ("odd", "even"):
        ones = bin(byte & ((1 << data_bits) - 1)).count("1")
        bits.append((ones % 2 if parity == "even" else 1 - ones % 2, 1.0))
    bits.append((1, float(stop_bits)))
    return bits


def send(line: Line, t: float, data: bytes, spb: float, parity: str = "none", stop_bits: float = 1) -> float:
    """Drive ``data`` on ``line`` from time ``t`` back to back (8 data bits); returns the end of the last stop bit."""
    for b in data:
        for lvl, n in uart_bits(b, 8, parity, stop_bits):
            line.set(t, lvl)
            t += n * spb
    return t


def uart_decode(line: Line, spb: float, data_bits: int = 8, parity: str = "none", stop_bits: float = 1, start: float = float("-inf")) -> List[Tuple[Optional[int], float]]:
    """What a UART receiver with these settings reads on ``line``: (byte, time the frame ends), byte None for a frame with a parity or framing error."""
    out: List[Tuple[Optional[int], float]] = []
    falls = line.edges("falling")
    t = start
    nbits = 1 + data_bits + (1 if parity in ("odd", "even") else 0)
    while True:
        i = bisect.bisect_left(falls, t)
        if i >= len(falls):
            return out
        f = falls[i]
        if line.level(f + 0.5 * spb) != 0:  # a glitch, not a start bit
            t = f + 1e-6
            continue
        val = 0
        for k in range(data_bits):
            val |= line.level(f + (1.5 + k) * spb) << k
        ok = True
        if parity in ("odd", "even"):
            p = line.level(f + (1.5 + data_bits) * spb)
            ones = bin(val).count("1") + p
            ok = (ones % 2 == 0) if parity == "even" else (ones % 2 == 1)
        if line.level(f + (nbits + 0.5) * spb) != 1:
            ok = False
        out.append((val if ok else None, f + (nbits + stop_bits) * spb))
        t = f + (nbits + 0.5) * spb  # the next start bit can come right after the first stop bit
        # a framing error leaves the receiver hunting for the next falling edge from here


def match(stream: List[Tuple[Optional[int], float]], pattern: List[Any]) -> Optional[float]:
    """End time of the first occurrence of ``pattern`` (bytes, 'XX' matches any byte) in a decoded stream."""
    n = len(pattern)
    for i in range(len(stream) - n + 1):
        if all(stream[i + k][0] is not None and (pattern[k] == "XX" or stream[i + k][0] == pattern[k]) for k in range(n)):
            return stream[i + n - 1][1]
    return None


class Timeline:
    """The simulated target's lines since the scope was armed, in ADC samples relative to the rising edge of the trigger pin of the captured command."""

    def __init__(self, scope, target):
        self.scope = scope
        self.target = target
        self.spc = float(scope._spc())
        try:
            adc = float(scope.clock.adc_freq)
        except Exception:  # noqa: BLE001
            adc = 4 * 7.37e6
        self.baud = float(getattr(target, "baud", 38400) or 38400)
        self.spb = adc / self.baud  # samples per bit on the target's serial lines
        self.parity = str(getattr(target, "parity", "none") or "none")
        self.stop_bits = float(getattr(target, "stop_bits", 1) or 1)
        tx_pin, rx_pin = "tio1", "tio2"  # target TX goes to the scope's serial_rx pin, target RX comes from its serial_tx pin
        try:
            f1, f2 = str(scope.io.tio1), str(scope.io.tio2)
            if f1 == "serial_tx" and f2 == "serial_rx":
                tx_pin, rx_pin = "tio2", "tio1"
        except Exception:  # noqa: BLE001
            pass
        self.lines: Dict[str, Line] = {tx_pin: Line(1), rx_pin: Line(1), "tio4": Line(0)}
        self.tx_pin, self.rx_pin = tx_pin, rx_pin
        self.start = 0.0
        self.end = 0.0
        self.high = 0.0  # how long the trigger pin of the captured command stays high
        self._layout(list(getattr(target, "_traffic", [])))

    def _op_times(self, op) -> Tuple[float, float, float]:
        """(trigger rise after the command was received, trigger high time, command duration) in samples."""
        if op is None:  # the built-in AES model: the trigger rises when the command is in and stays high for the encryption
            leak_start, leak_spacing = self.scope._leak_pos()
            high = float(leak_start + 16 * leak_spacing)
            return 0.0, high, high
        hi, lo, total = op
        spc = self.spc
        hi = hi or 0
        lo = total if lo is None else lo
        return hi * spc, max(0, lo - hi) * spc, max(total, lo) * spc

    def _layout(self, traffic: List[tuple]) -> None:
        cur = 0.0
        rise_last = 0.0
        first = None
        for entry in traffic:
            kind = entry[0]
            if kind == "rx":
                if entry[1]:
                    first = cur if first is None else first
                    cur = send(self.lines[self.rx_pin], cur, entry[1], self.spb, self.parity, self.stop_bits) + self.spb
            elif kind == "tx":
                if entry[1]:
                    first = cur if first is None else first
                    cur = send(self.lines[self.tx_pin], cur, entry[1], self.spb, self.parity, self.stop_bits) + self.spb
            elif kind == "op":
                pre, high, dur = self._op_times(entry[1])
                first = cur if first is None else first
                rise = cur + pre
                self.lines["tio4"].set(rise, 1)
                self.lines["tio4"].set(rise + max(high, 1.0), 0)
                rise_last, self.high = rise, high
                cur += max(dur, high + 1.0)
        for ln in self.lines.values():  # time 0 is the rise of the captured command's trigger
            ln.t = [t - rise_last for t in ln.t]
        self.start = (first if first is not None else 0.0) - rise_last
        self.end = cur - rise_last

    # --- the triggers --------------------------------------------------------------------
    def edge_counter(self, pin: str, edges: int) -> Optional[float]:
        """Fires on the nth edge (rising and falling both count, as on the Husky) on ``pin`` since arming."""
        ln = self.lines.get(pin)
        e = ln.edges() if ln is not None else []
        return e[edges - 1] if 0 < edges <= len(e) else None

    def uart(self, pin: str, baud: float, pattern: List[Any], data_bits: int = 8, parity: str = "none", stop_bits: float = 1) -> Optional[float]:
        """Fires at the end of the first frame sequence on ``pin`` that a UART receiver with these settings decodes as ``pattern``."""
        ln = self.lines.get(pin)
        if ln is None or not pattern:
            return None
        adc = self.spb * self.baud
        return match(uart_decode(ln, adc / float(baud), data_bits, parity, stop_bits), pattern)

    def step_events(self, pin: str, mode: str) -> List[float]:
        ln = self.lines.get(pin)
        if ln is None:
            return []
        if mode in ("rising_edge", "falling_edge"):
            return ln.edges("rising" if mode == "rising_edge" else "falling")
        want = 1 if mode == "high" else 0
        ev = [t for t, v in zip(ln.t, ln.v) if v == want]
        if ln.level(self.start) == want:
            ev.insert(0, self.start)
        return ev

    def sequencer(self, pins: List[str], window_start: int, window_end: int, mode: str = "rising_edge") -> Optional[float]:
        """Fires when the second step's pin triggers window_start..window_end ADC cycles after the first's (0 = no limit), as the Husky sequencer does."""
        if len(pins) != 2:
            return None
        first, second = self.step_events(pins[0], mode), self.step_events(pins[1], mode)
        for t1 in first:
            lo = t1 + max(0, int(window_start))
            for t2 in second:
                if t2 <= t1 or t2 < lo:
                    continue
                if window_end and t2 > t1 + int(window_end):
                    break
                return t2
        return None


def configured(scope) -> Optional[Dict[str, Any]]:
    """The advanced trigger the simulated scope uses, or None for the basic trigger on pins (the existing behaviour, unchanged). A module the simulator does not model, or one set without its parameters, gives {'kind': 'fallback', 'why': ...}."""
    cfg = getattr(scope, "_sim_trigger", None) or {}
    kind = cfg.get("kind")
    module = str(getattr(scope.trigger, "module", "basic") or "basic")
    if kind == "sequencer" and cfg.get("enabled"):
        return cfg
    if kind in MODULE_OF and MODULE_OF[kind] == module:
        return cfg
    if module == "basic":
        return None
    for k, m in MODULE_OF.items():
        if m == module:
            return {"kind": "fallback", "why": f"trigger.module {module} was set without its parameters (configure the {k.replace('_', ' ')} trigger in the Interfaces tab)"}
    return {"kind": "fallback", "why": f"the simulator does not model the {module} trigger module"}


def fire_time(scope, cfg: Dict[str, Any]) -> Tuple[Optional[float], Optional["Window"]]:
    """When ``cfg`` fires for the captured command (ADC samples from its trigger rise; None: never) and, for the triggers that look at the trace itself, the rendered trace they looked at."""
    tl = Timeline(scope, scope.target)
    kind = cfg["kind"]
    if kind == "edge_counter":
        return tl.edge_counter(cfg.get("pin", "tio4"), int(cfg.get("edges", 1))), None
    if kind == "uart_pattern":
        return tl.uart(cfg.get("pin", "tio1"), cfg.get("baud", 38400), list(cfg.get("pattern") or []), int(cfg.get("data_bits", 8)), cfg.get("parity", "none"), float(cfg.get("stop_bits", 1))), None
    if kind == "uart_decode":
        return tl.uart(cfg.get("pin", "tio1"), cfg.get("baud", 38400), list(cfg.get("pattern") or [])), None
    if kind == "sequencer":
        return tl.sequencer(list(cfg.get("pins") or []), int(cfg.get("window_start", 0)), int(cfg.get("window_end", 0)), str(scope.adc.basic_mode)), None
    if kind in ("adc_level", "sad"):
        w = Window(scope, tl.start, tl.end + 2 * tl.spb)
        if kind == "adc_level":
            return w.level_crossing(float(cfg.get("level", 0.1))), w
        return w.sad(np.asarray(getattr(scope, "_sim_sad_ref", None), np.float64), int(cfg.get("threshold", 10)), SAD_BITS.get(scope.sim_model, 8)), w
    return 0.0, None


class Window:
    """The analog signal the simulated scope sees from ``start`` to ``end`` (ADC samples from the trigger rise), rendered once so that the trigger and the recorded trace see the same noise."""

    def __init__(self, scope, start: float, end: float):
        self.scope = scope
        self.start = int(np.floor(start))
        self.end = int(np.ceil(end))
        self.extra = int(scope.adc.offset) + int(scope.adc.samples) + int(getattr(scope.adc, "presamples", 0) or 0) + 1
        self.base = self.start - (int(getattr(scope.adc, "presamples", 0) or 0) + 1)  # room for pre-trigger samples
        self.wave = render(scope, self.base, self.end + self.extra - self.base)

    def level_crossing(self, level: float) -> Optional[float]:
        """The first sample above ``level`` (positive) or below it (negative), as the Husky ADC trigger."""
        seg = self.wave[self.start - self.base:self.end - self.base]
        hit = np.flatnonzero(seg > level) if level >= 0 else np.flatnonzero(seg < level)
        return float(self.start + hit[0]) if hit.size else None

    def sad(self, ref: np.ndarray, threshold: int, bits: int) -> Optional[float]:
        """Where the reference matches: the best position (lowest sum of absolute differences, in ``bits``-bit ADC units) of the first run of positions under the threshold; the trigger comes when the last reference sample is in, as on the hardware."""
        n = int(ref.size)
        seg = self.wave[self.start - self.base:self.end - self.base].astype(np.float64)
        if n == 0 or seg.size < n:
            return None
        q = lambda x: np.floor((np.clip(x, -0.5, 0.5 - 1e-9) + 0.5) * (1 << bits))  # noqa: E731
        win = np.lib.stride_tricks.sliding_window_view(q(seg), n)
        score = np.abs(win - q(ref)).sum(axis=1)
        under = np.flatnonzero(score <= threshold)
        if not under.size:
            return None
        i0 = under[0]
        i1 = i0
        while i1 + 1 < score.size and score[i1 + 1] <= threshold:
            i1 += 1
        best = i0 + int(np.argmin(score[i0:i1 + 1]))
        return float(self.start + best + n)

    def trace(self, t0: int, n: int) -> Optional[np.ndarray]:
        a = t0 - self.base
        if a < 0 or a + n > self.wave.size:
            return None
        return self.wave[a:a + n].copy()


def render(scope, t0: int, n: int) -> np.ndarray:
    """``n`` samples of what the measure input sees from ``t0`` ADC samples after the captured command's trigger rise (the built-in AES model or the emulated firmware)."""
    run = getattr(scope.target, "_last_run", None)
    if run is None:
        return scope._synth(scope.target._last_pt, scope.target._key, n=n, offset=t0)
    from cwstudio.codemap import power
    r, P = run
    hi, _lo = r.trigger_window()
    try:
        adc_freq, tgt = float(scope.clock.adc_freq), float(scope.clock.clkgen_freq)
    except Exception:  # noqa: BLE001
        adc_freq, tgt = 4 * 7.37e6, 7.37e6
    dec = max(1, int(getattr(scope.adc, "decimate", 1) or 1))
    m = power.Mapping(spc=adc_freq / tgt / dec, shift=-float(t0), decimate=dec)
    gain = (0.6 + scope.gain.gain / 78.0) * (1.4 if scope.gain.mode == "high" else 0.7)
    return power.synth_trace(P, int(hi or 0), m, int(n), scope._rng, noise=scope.noise * 0.6, amp=0.03, gain=gain)


def trace_start(scope, fire: float) -> int:
    """The first recorded sample (from the trigger rise) for a trigger at ``fire``: offset samples later, minus the pre-trigger samples (which the built-in AES model, like before, does not use)."""
    t = int(round(fire)) + int(scope.adc.offset)
    if getattr(scope.target, "_last_run", None) is not None:
        t -= int(getattr(scope.adc, "presamples", 0) or 0)
    return t


def describe(cfg: Dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in cfg.items() if k != "kind")
