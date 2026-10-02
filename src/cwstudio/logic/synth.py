"""Protocol waveform generators in continuous time, and rendering them to sampled logic channels.

The simulator uses them for its demo traffic (UART SimpleSerial exchange, SPI flash JEDEC ID, I2C EEPROM, 1-Wire ROM read, CAN, JTAG, SWD, trigger and clock) and the tests use them to build known waveforms for the decoder round trips. They are written independently of :mod:`cwstudio.logic.decoders`: each one follows the protocol's timing rules, not the decoder's sampling points.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np


class Line:
    """A digital line built from level changes at times in seconds."""

    def __init__(self, init: int = 1):
        self.init = int(init) & 1
        self.times: List[float] = []
        self.vals: List[int] = []

    @property
    def level(self) -> int:
        return self.vals[-1] if self.vals else self.init

    def set(self, t: float, v: int) -> "Line":
        v = int(v) & 1
        if self.vals and t < self.times[-1]:
            self.times.append(float(t))
            self.vals.append(v)
            order = np.argsort(np.asarray(self.times), kind="stable")
            self.times = [self.times[i] for i in order]
            self.vals = [self.vals[i] for i in order]
            return self
        if v != self.level:
            self.times.append(float(t))
            self.vals.append(v)
        return self

    def events(self) -> Tuple[np.ndarray, np.ndarray]:
        return np.asarray(self.times, np.float64), np.asarray(self.vals, np.int8)


class Clock:
    """A free-running clock: rising edges at ``phase + k / freq``, high for ``duty`` of the period, optionally only between t_from and t_to."""

    def __init__(self, freq: float, duty: float = 0.5, phase: float = 0.0, t_from: float = -math.inf, t_to: float = math.inf, init: int = 0):
        self.freq, self.duty, self.phase, self.t_from, self.t_to, self.init = float(freq), float(duty), float(phase), t_from, t_to, int(init)


Signal = Union[Line, Clock]


# ----- rendering -------------------------------------------------------------------------------------
def _render_line(times: np.ndarray, vals: np.ndarray, init: int, sr: float, t_start: float, n: int) -> Tuple[int, np.ndarray]:
    if times.shape[0] == 0:
        return init, np.zeros(0, np.int64)
    order = np.argsort(times, kind="stable")
    times, vals = times[order], vals[order]
    # sample i sees every change at or before t_start + i / sr
    idx = np.ceil((times - t_start) * sr - 1e-6).astype(np.int64)
    idx = np.maximum(idx, 0)
    last = np.ones(idx.shape[0], bool)
    last[:-1] = idx[1:] != idx[:-1]
    idx, vals = idx[last], vals[last]
    prev = np.concatenate(([init], vals[:-1]))
    keep = vals != prev
    idx, vals = idx[keep], vals[keep]
    v0 = init
    if idx.shape[0] and idx[0] == 0:
        v0 = int(vals[0])
        idx, vals = idx[1:], vals[1:]
    idx = idx[idx < n]
    return v0, idx


def _render_clock(c: Clock, sr: float, t_start: float, n: int) -> Tuple[int, np.ndarray]:
    t_end = t_start + n / sr
    lo, hi = max(t_start, c.t_from), min(t_end, c.t_to)
    if hi <= lo:
        return c.init, np.zeros(0, np.int64)
    if 2 * c.freq * (hi - lo) > n / 2:
        # more edges than half the samples: sample the clock directly (it aliases like on real hardware)
        i0 = max(0, int(math.ceil((lo - t_start) * sr)))
        i1 = min(n, int(math.ceil((hi - t_start) * sr)))
        t = t_start + np.arange(i0, i1) / sr
        bits = (np.mod((t - c.phase) * c.freq, 1.0) < c.duty).astype(np.int8)
        full = np.full(n, c.init, np.int8)
        full[i0:i1] = bits
        from cwstudio.logic.model import edges_from_bits
        return int(full[0]), edges_from_bits(full)
    k0 = int(math.floor((lo - c.phase) * c.freq)) - 1
    k1 = int(math.ceil((hi - c.phase) * c.freq)) + 1
    k = np.arange(k0, k1)
    rise = c.phase + k / c.freq
    fall = rise + c.duty / c.freq
    times = np.stack((rise, fall), axis=1).ravel()
    vals = np.tile(np.array([1, 0], np.int8), k.shape[0])
    m = (times >= lo) & (times < hi)
    times, vals = times[m], vals[m]
    # level at lo
    lvl = int(np.mod((lo - c.phase) * c.freq, 1.0) < c.duty)
    if c.t_from > t_start:
        times = np.concatenate(([c.t_from], times))
        vals = np.concatenate(([lvl], vals)).astype(np.int8)
        init = c.init
    else:
        init = lvl
    if c.t_to < t_end:
        times = np.concatenate((times, [c.t_to]))
        vals = np.concatenate((vals, [c.init])).astype(np.int8)
    return _render_line(times, vals, init, sr, t_start, n)


def render(signals: Dict[str, Signal], samplerate: float, t_start: float, n: int, jitter: float = 0.0, glitch_rate: float = 0.0, seed: Optional[int] = None) -> Dict[str, Tuple[int, np.ndarray]]:
    """Sample each signal at ``t_start + i / samplerate`` for i in 0..n-1 and return ``{name: (init, edges)}``.

    ``jitter`` is the standard deviation (seconds) added to every edge time; ``glitch_rate`` adds that many one-sample spikes per second per line, both for realistic noise.
    """
    rng = np.random.default_rng(seed)
    out = {}
    t_end = t_start + n / samplerate
    for name, s in signals.items():
        if isinstance(s, Clock):  # clocks stay exact; jitter and glitches apply to the protocol lines
            out[name] = _render_clock(s, samplerate, t_start, n)
            continue
        ln_t, ln_v = s.events()
        init = s.init
        if jitter and ln_t.shape[0]:
            ln_t = ln_t + rng.normal(0, jitter, ln_t.shape[0])
        if glitch_rate:
            count = rng.poisson(glitch_rate * (t_end - t_start))
            if count:
                gt = rng.uniform(t_start, t_end, count)
                # a spike to the opposite level for 1.5 samples at each time
                lv_t = np.concatenate((ln_t, gt, gt + 1.5 / samplerate))
                base_order = np.argsort(ln_t, kind="stable")
                st, sv = ln_t[base_order], ln_v[base_order]
                def level(tt):
                    k = np.searchsorted(st, tt, side="right")
                    return np.where(k > 0, sv[np.maximum(k - 1, 0)], init)
                lv = level(gt)
                lv_v = np.concatenate((ln_v, 1 - lv, lv)).astype(np.int8)
                ln_t, ln_v = lv_t, lv_v
        out[name] = _render_line(ln_t, ln_v, init, samplerate, t_start, n)
    return out


def render_bits(signals: Dict[str, Signal], samplerate: float, t_start: float, n: int, **kw) -> Dict[str, np.ndarray]:
    """Like :func:`render` but as dense 0/1 arrays."""
    res = {}
    for name, (v0, e) in render(signals, samplerate, t_start, n, **kw).items():
        arr = np.zeros(n + 1, np.int8)
        arr[e] = 1
        res[name] = ((np.cumsum(arr[:n]) & 1) ^ v0).astype(np.uint8)
    return res


def to_analog(bits: np.ndarray, rng: Optional[np.random.Generator] = None, low: float = -0.3, high: float = 0.3, noise: float = 0.02, rise_samples: float = 2.0, ringing: float = 0.06) -> np.ndarray:
    """A digital line as the ChipWhisperer ADC would see it: RC edges, a little ringing, noise, clipped to the +-0.5 range."""
    rng = rng or np.random.default_rng()
    b = np.asarray(bits, dtype=np.float64)
    n = b.shape[0]
    if n == 0:
        return np.zeros(0, np.float32)
    k = np.arange(int(max(4, rise_samples * 8)))
    rc = np.exp(-k / max(rise_samples, 0.1))
    rc /= rc.sum()
    pad = np.concatenate((np.full(rc.shape[0], b[0]), b))
    smooth = np.convolve(pad, rc, mode="full")[rc.shape[0]:rc.shape[0] + n]
    ring_k = np.arange(24)
    ring = ringing * np.exp(-ring_k / 5.0) * np.sin(2 * np.pi * ring_k / 6.0)
    step = np.diff(np.concatenate(([b[0]], b)))
    wave = low + (high - low) * smooth + np.convolve(step, ring, mode="full")[:n]
    wave += rng.normal(0, noise, n)
    return np.clip(wave, -0.5, 0.5).astype(np.float32)


# ----- UART -------------------------------------------------------------------------------------------
def uart(line: Line, t: float, data: Iterable[int], baud: float, data_bits: int = 8, parity: str = "none", stop_bits: float = 1, inverted: bool = False, gap: float = 0.0, bad_parity: Sequence[int] = (), msb_first: bool = False) -> float:
    """Send bytes as UART frames starting at ``t``; returns the end time. ``bad_parity`` lists frame numbers sent with a wrong parity bit; ``gap`` adds idle bit times between frames."""
    bt = 1.0 / baud
    inv = 1 if inverted else 0
    for i, byte in enumerate(data):
        bits = [0]
        word = [(byte >> k) & 1 for k in range(data_bits)]
        if msb_first:
            word.reverse()
        bits += word
        ones = sum(word)
        if parity != "none":
            p = {"odd": (ones + 1) & 1, "even": ones & 1, "mark": 1, "space": 0}[parity]
            if i in bad_parity:
                p ^= 1
            bits.append(p)
        for k, b in enumerate(bits):
            line.set(t + k * bt, b ^ inv)
        line.set(t + len(bits) * bt, 1 ^ inv)
        t += (len(bits) + stop_bits + gap) * bt
    return t


# ----- SPI --------------------------------------------------------------------------------------------
def spi(cs: Optional[Line], sck: Line, mosi: Optional[Line], miso: Optional[Line], t: float, tx: Sequence[int], rx: Sequence[int], freq: float, cpol: int = 0, cpha: int = 0, msb_first: bool = True, word_size: int = 8, cs_active: int = 0) -> float:
    """One chip-select framed SPI transfer of ``tx`` words on MOSI while the device answers ``rx`` on MISO; returns the end time."""
    half = 0.5 / freq
    dly = half * 0.1
    if cs is not None:
        cs.set(t, cs_active)
    sck.set(t, cpol)
    nbits = len(tx) * word_size
    bits_tx, bits_rx = [], []
    for w_tx, w_rx in zip(tx, list(rx) + [0] * (len(tx) - len(rx))):
        order = range(word_size - 1, -1, -1) if msb_first else range(word_size)
        bits_tx += [(w_tx >> k) & 1 for k in order]
        bits_rx += [(w_rx >> k) & 1 for k in order]
    start = t + half
    for b in range(nbits):
        lead = start + 2 * b * half
        trail = lead + half
        if cpha == 0:
            setup = (t + dly) if b == 0 else (lead - half + dly)
        else:
            setup = lead + dly
        if mosi is not None:
            mosi.set(setup, bits_tx[b])
        if miso is not None:
            miso.set(setup, bits_rx[b])
        sck.set(lead, 1 - cpol)
        sck.set(trail, cpol)
    end = start + 2 * nbits * half + half
    if cs is not None:
        cs.set(end, 1 - cs_active)
    return end + half


# ----- I2C --------------------------------------------------------------------------------------------
def i2c(scl: Line, sda: Line, t: float, ops: Sequence[Tuple], freq: float = 100e3, stretch: float = 0.0) -> float:
    """I2C bus activity from ops: ("start",), ("stop",), ("byte", value, ack) where ack True means the receiver pulls SDA low in the 9th clock; ``stretch`` seconds of clock stretching (SCL held low by the device) before every byte but the first of a transfer; returns the end time."""
    q = 0.25 / freq
    tl = t  # time SCL went (or is) low
    after_byte = False
    for op in ops:
        if op[0] == "start":
            if scl.level == 1 and sda.level == 1:
                sda.set(tl, 0)
                scl.set(tl + 2 * q, 0)
                tl = tl + 2 * q
            else:  # repeated start from SCL low
                sda.set(tl + q, 1)
                scl.set(tl + 2 * q, 1)
                sda.set(tl + 3 * q, 0)
                scl.set(tl + 4 * q, 0)
                tl = tl + 4 * q
        elif op[0] == "stop":
            sda.set(tl + q, 0)
            scl.set(tl + 2 * q, 1)
            sda.set(tl + 3 * q, 1)
            tl = tl + 4 * q
        elif op[0] == "byte":
            value, ack = op[1], op[2]
            if stretch and after_byte:
                tl += stretch
            bits = [(value >> k) & 1 for k in range(7, -1, -1)] + [0 if ack else 1]
            for b in bits:
                sda.set(tl + q, b)
                scl.set(tl + 2 * q, 1)
                scl.set(tl + 4 * q, 0)
                tl = tl + 4 * q
        after_byte = op[0] == "byte"
    return tl + q


def i2c_address(addr: int, read: bool, ten_bit: bool = False) -> List[int]:
    """The address byte(s) for a transfer: 7-bit (addr << 1 | R/W) or the 10-bit 11110xx form plus the low byte."""
    if not ten_bit:
        return [((addr & 0x7F) << 1) | int(read)]
    return [0xF0 | ((addr >> 7) & 0x06) | int(read), addr & 0xFF]


# ----- 1-Wire ------------------------------------------------------------------------------------------
def onewire(line: Line, t: float, ops: Sequence[Tuple]) -> float:
    """1-Wire from ops: ("reset", presence), ("write", bytes), ("read", bytes the device sends), ("overdrive", on) to switch to overdrive timing (Maxim AN126 recommended values); returns the end time."""
    us = 1e-6
    od = False
    for op in ops:
        if op[0] == "overdrive":
            od = bool(op[1])
        elif op[0] == "reset":
            low, wait, pres, total = (70, 8.5, 10, 150) if od else (480, 35, 120, 960)
            line.set(t, 0)
            line.set(t + low * us, 1)
            if op[1]:
                line.set(t + (low + wait) * us, 0)
                line.set(t + (low + wait + pres) * us, 1)
            t += total * us
        else:
            w1, w0, r1, r0, slot = (1, 7.5, 1, 6, 10) if od else (6, 60, 2, 30, 70)
            for byte in op[1]:
                for k in range(8):
                    b = (byte >> k) & 1
                    line.set(t, 0)
                    if op[0] == "write":
                        line.set(t + (w1 if b else w0) * us, 1)
                    else:  # read slot: the master pulls low briefly, a 0 is held low by the device
                        line.set(t + (r1 if b else r0) * us, 1)
                    t += slot * us
    return t


# ----- CAN --------------------------------------------------------------------------------------------
def crc15(bits: Sequence[int]) -> int:
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def can_bits(ident: int, data: bytes = b"", extended: bool = False, rtr: bool = False, ack: bool = True, bad_crc: bool = False, dlc: Optional[int] = None) -> List[int]:
    """The bits of one classic CAN frame on the bus (with stuff bits), from SOF to the end of the intermission."""
    f = [0]
    w = lambda v, n: [(v >> k) & 1 for k in range(n - 1, -1, -1)]  # noqa: E731
    if extended:
        f += w(ident >> 18, 11) + [1, 1] + w(ident & 0x3FFFF, 18) + [int(rtr), 0, 0]
    else:
        f += w(ident, 11) + [int(rtr), 0, 0]
    f += w(len(data) if dlc is None else dlc, 4)
    if not rtr:
        for b in data:
            f += w(b, 8)
    crc = crc15(f) ^ (1 if bad_crc else 0)
    f += w(crc, 15)
    stuffed, run, last = [], 0, None
    for b in f:
        stuffed.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            stuffed.append(1 - b)
            last, run = 1 - b, 1
    return stuffed + [1, 0 if ack else 1, 1] + [1] * 7 + [1] * 3


def can(line: Line, t: float, bitrate: float, ident: int, data: bytes = b"", **kw) -> float:
    bt = 1.0 / bitrate
    bits = can_bits(ident, data, **kw)
    for k, b in enumerate(bits):
        line.set(t + k * bt, b)
    return t + len(bits) * bt


# ----- JTAG -------------------------------------------------------------------------------------------
def jtag(tck: Line, tms: Line, tdi: Line, tdo: Optional[Line], t: float, ops: Sequence[Tuple], freq: float = 100e3) -> float:
    """JTAG from ops starting in Run-Test/Idle (or after ("reset",)): ("reset",) five TMS=1 clocks then Run-Test/Idle, ("idle", n), ("ir", tdi_value, bits, tdo_value), ("dr", tdi_value, bits, tdo_value). Shift values go LSB first; returns the end time."""
    half = 0.5 / freq
    cycles: List[Tuple[int, int, Optional[int]]] = []  # (tms, tdi, tdo for this rising edge)
    for op in ops:
        if op[0] == "reset":
            cycles += [(1, 0, None)] * 5 + [(0, 0, None)]
        elif op[0] == "idle":
            cycles += [(0, 0, None)] * int(op[1])
        elif op[0] in ("ir", "dr"):
            _, val, nbits, out = op
            cycles += [(1, 0, None)] + ([(1, 0, None)] if op[0] == "ir" else []) + [(0, 0, None), (0, 0, None)]  # select(-IR), capture, enter shift
            for k in range(nbits):
                cycles.append((1 if k == nbits - 1 else 0, (val >> k) & 1, (out >> k) & 1))
            cycles += [(1, 0, None), (0, 0, None)]  # update, idle
    for tms_v, tdi_v, tdo_v in cycles:
        tms.set(t + 0.1 * half, tms_v)
        tdi.set(t + 0.1 * half, tdi_v)
        if tdo is not None and tdo_v is not None:
            tdo.set(t + 0.15 * half, tdo_v)
        tck.set(t + half, 1)
        tck.set(t + 2 * half, 0)
        t += 2 * half
    return t


# ----- SWD --------------------------------------------------------------------------------------------
SWD_ACK = {"ok": 0b001, "wait": 0b010, "fault": 0b100}
JTAG_TO_SWD = 0xE79E


def swd(swclk: Line, swdio: Line, t: float, ops: Sequence[Tuple], freq: float = 100e3) -> float:
    """SWD from ops: ("line_reset",) 52 clocks high, ("jtag_to_swd",), ("idle", n), ("read"|"write", ap, addr, ack, data). The host changes SWDIO before the rising edge, the target right after the rising edge (it is read at the falling edge); returns the end time."""
    half = 0.5 / freq
    cyc: List[Tuple[str, Optional[int]]] = []  # ("h", bit) host drives, ("t", bit) target drives, ("z", None) turnaround (pulled up)
    for op in ops:
        if op[0] == "line_reset":
            cyc += [("h", 1)] * 52
        elif op[0] == "jtag_to_swd":
            cyc += [("h", (JTAG_TO_SWD >> k) & 1) for k in range(16)]
        elif op[0] == "idle":
            cyc += [("h", 0)] * int(op[1])
        else:
            kind, ap, addr, ack, data = op
            rnw = 1 if kind == "read" else 0
            a2, a3 = (addr >> 2) & 1, (addr >> 3) & 1
            par = (ap + rnw + a2 + a3) & 1
            cyc += [("h", b) for b in (1, ap, rnw, a2, a3, par, 0, 1)]
            cyc += [("z", None)]
            code = SWD_ACK[ack]
            cyc += [("t", (code >> k) & 1) for k in range(3)]
            if ack == "ok" and rnw:
                cyc += [("t", (data >> k) & 1) for k in range(32)] + [("t", bin(data & 0xFFFFFFFF).count("1") & 1), ("z", None)]
            elif ack == "ok":
                cyc += [("z", None)] + [("h", (data >> k) & 1) for k in range(32)] + [("h", bin(data & 0xFFFFFFFF).count("1") & 1)]
            else:
                cyc += [("z", None)]
    for who, bit in cyc:
        if who == "h":
            swdio.set(t + 0.1 * half, bit)
        elif who == "z":
            swdio.set(t + 0.1 * half, 1)
        else:
            swdio.set(t + half + 0.2 * half, bit)
        swclk.set(t + half, 1)
        swclk.set(t + 2 * half, 0)
        t += 2 * half
    return t


# ----- SimpleSerial ------------------------------------------------------------------------------------
def ss1_line(cmd: str, payload: bytes) -> bytes:
    return (cmd + payload.hex()).encode() + b"\n"


def crc8_ss2(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x4D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    out, block = bytearray(), bytearray()
    for b in data:
        if b == 0:
            out.append(len(block) + 1)
            out += block
            block = bytearray()
        else:
            block.append(b)
            if len(block) == 254:
                out.append(255)
                out += block
                block = bytearray()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def ss2_frame(cmd: str, scmd: int, payload: bytes) -> bytes:
    body = bytes([ord(cmd), scmd, len(payload)]) + payload
    return cobs_encode(body + bytes([crc8_ss2(body)])) + b"\x00"


# ----- the simulator's demo traffic ----------------------------------------------------------------------
DEMO_CHANNELS = ["UART TX", "UART RX", "TRIG", "CLK", "SPI CS", "SPI SCK", "SPI MOSI", "SPI MISO", "I2C SCL", "I2C SDA", "1-Wire", "CAN", "JTAG TCK", "JTAG TMS", "JTAG TDI", "JTAG TDO", "SWCLK", "SWDIO"]
DEMO_JEDEC = bytes([0xEF, 0x40, 0x18])
DEMO_ROM = bytes([0x28, 0xFF, 0x4C, 0x1A, 0x62, 0x16, 0x03])
DEMO_IDCODE = 0x4BA00477
DEMO_DPIDR = 0x2BA01477


def demo_signals(baud: float = 38400, pt: Optional[bytes] = None, ct: Optional[bytes] = None, clock_hz: float = 1e6, target_clock_hz: float = 7.37e6, adc_clock_hz: float = 29.538e6, ss_version: str = "1.1") -> Tuple[Dict[str, Signal], Dict[str, Any]]:
    """The simulator's traffic on every demo channel, with t = 0 at the trigger. The host's SimpleSerial command is before the trigger, everything else after it.

    Returns the signals and a description of what is on them (for the help text and the tests).
    """
    from cwstudio.aes import encrypt_block
    from cwstudio.sim_interfaces import crc8_maxim
    pt = pt or bytes.fromhex("00112233445566778899aabbccddeeff")
    if ct is None:
        ct = encrypt_block(bytes(range(16)), pt)
    s: Dict[str, Signal] = {name: Line(1) for name in DEMO_CHANNELS}
    for name in ("TRIG", "SPI SCK", "SPI MOSI", "JTAG TCK", "JTAG TMS", "JTAG TDI", "JTAG TDO", "SWCLK"):
        s[name] = Line(0)
    us, ms = 1e-6, 1e-3
    # SimpleSerial: host command on the target's RX (UART RX), trigger high while encrypting, response on TX
    if ss_version.startswith("2"):
        cmd = ss2_frame("p", 0, pt)
        resp = ss2_frame("r", 0, ct) + ss2_frame("e", 0, b"\x00")
    else:
        cmd = ss1_line("p", pt)
        resp = ss1_line("r", ct) + b"z00\n"
    frame_t = 10.0 / baud
    uart(s["UART RX"], -0.1 * ms - len(cmd) * frame_t, cmd, baud)
    s["TRIG"].set(0.0, 1).set(0.25 * ms, 0)
    uart(s["UART TX"], 0.4 * ms, resp, baud)
    s["CLK"] = Clock(clock_hz)
    s["HS1"] = Clock(target_clock_hz)
    s["ADC"] = Clock(adc_clock_hz)
    # SPI flash: JEDEC ID read then a 4 byte read of the greeting
    t = spi(s["SPI CS"], s["SPI SCK"], s["SPI MOSI"], s["SPI MISO"], 0.05 * ms, [0x9F, 0, 0, 0], [0xFF] + list(DEMO_JEDEC), 125e3)
    spi(s["SPI CS"], s["SPI SCK"], s["SPI MOSI"], s["SPI MISO"], t + 20 * us, [0x03, 0, 0, 0, 0, 0, 0, 0], [0xFF] * 4 + list(b"Chip"), 125e3)
    # I2C: EEPROM at 0x50 random read with a repeated start, then a NACKed probe of 0x23
    ops = [("start",), ("byte", 0xA0, True), ("byte", 0x00, True), ("byte", 0x10, True), ("start",), ("byte", 0xA1, True), ("byte", 0xCA, True), ("byte", 0xFE, False), ("stop",)]
    t = i2c(s["I2C SCL"], s["I2C SDA"], 0.6 * ms, ops, 100e3)
    i2c(s["I2C SCL"], s["I2C SDA"], t + 50 * us, [("start",), ("byte", 0x46, False), ("stop",)], 100e3)
    # 1-Wire: reset, presence, READ ROM, the 8 ROM bytes
    rom = DEMO_ROM + bytes([crc8_maxim(DEMO_ROM)])
    onewire(s["1-Wire"], 0.6 * ms, [("reset", True), ("write", b"\x33"), ("read", rom)])
    # CAN 125 kbit/s: a standard and an extended frame
    t = can(s["CAN"], 1.5 * ms, 125e3, 0x123, b"\xde\xad\xbe\xef")
    can(s["CAN"], t + 50 * us, 125e3, 0x18DAF110, b"\x02\x10\x03", extended=True)
    # JTAG: reset, IR = IDCODE (4 bits), DR read of the IDCODE
    jtag(s["JTAG TCK"], s["JTAG TMS"], s["JTAG TDI"], s["JTAG TDO"], 1.5 * ms, [("reset",), ("ir", 0xE, 4, 0x1), ("dr", 0, 32, DEMO_IDCODE), ("idle", 2)], 100e3)
    # SWD: line reset, JTAG-to-SWD, line reset, DPIDR read, a WAIT and a FAULT
    swd(s["SWCLK"], s["SWDIO"], 2.4 * ms, [("line_reset",), ("jtag_to_swd",), ("line_reset",), ("idle", 2), ("read", 0, 0x0, "ok", DEMO_DPIDR), ("idle", 2), ("write", 0, 0x4, "ok", 0x50000000), ("idle", 2), ("read", 1, 0xC, "wait", 0), ("idle", 2), ("read", 1, 0xC, "fault", 0), ("idle", 4)], 100e3)
    s["SWDIO"].init = 1
    desc = {"pt": pt.hex(), "ct": ct.hex(), "baud": baud, "jedec": DEMO_JEDEC.hex(), "rom": rom.hex(), "idcode": DEMO_IDCODE, "dpidr": DEMO_DPIDR, "uart_cmd": cmd, "uart_resp": resp, "t_first": -0.1 * ms - len(cmd) * frame_t, "t_last": 0.4 * ms + len(resp) * frame_t}
    return s, desc
