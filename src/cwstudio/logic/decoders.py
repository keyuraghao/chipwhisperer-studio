"""Protocol decoders that run in software on a :class:`~cwstudio.logic.model.LogicCapture` from any source.

Each decoder takes a channel mapping (``{"rx": 0, ...}`` by index or name) and options, and returns a :class:`Result`: rows of annotations (start and end sample, text, kind, optional value) that the viewer draws under the decoder's channels and the results table lists. Kinds pick the colour: data, addr, cmd, ack, nack, error, warn, start, stop, state, info.

Decoders: UART (baud auto-detect, 5-9 data bits, parity, stop bits, inverted, bit order), SPI (CPOL/CPHA, bit order, word size, CS level), I2C (7 and 10-bit addresses, read/write, ACK/NACK, repeated start), 1-Wire (reset/presence, ROM commands, bytes, standard and overdrive speed), JTAG (TAP state tracking, IR/DR shifts), SWD (requests, ACK, data, parity), CAN (classic, standard and extended IDs, DLC, data, CRC, ACK, stuffing; CAN FD frames are recognised and skipped) and SimpleSerial v1/v2 on top of UART. Every decoder has a glitch filter that drops pulses shorter than a set time on its channels.
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from cwstudio.logic.model import Channel, LogicCapture, ss

STANDARD_BAUDS = [300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 31250, 38400, 57600, 76800, 115200, 128000, 153600, 230400, 250000, 256000, 460800, 500000, 576000, 921600, 1000000, 1500000, 2000000, 3000000]
STANDARD_CAN = [10e3, 20e3, 33.333e3, 50e3, 83.333e3, 100e3, 125e3, 250e3, 500e3, 800e3, 1e6]


class DecodeError(ValueError):
    pass


class Result:
    """Annotations by row. ``rows[id] = {"label", "channel", "s", "e", "text", "kind", "value"}``; after :meth:`finalize` s/e are sorted int64 arrays."""

    def __init__(self, decoder: str = ""):
        self.decoder = decoder
        self.rows: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.meta: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self._final = False

    def row(self, rid: str, label: str, channel: Optional[int]) -> str:
        if rid not in self.rows:
            self.rows[rid] = {"label": label, "channel": channel, "s": [], "e": [], "text": [], "kind": [], "value": []}
        return rid

    def add(self, rid: str, s, e, text: str, kind: str = "data", value: Any = None) -> None:
        r = self.rows[rid]
        s, e = int(round(float(s))), int(round(float(e)))
        r["s"].append(s)
        r["e"].append(max(e, s + 1))
        r["text"].append(text)
        r["kind"].append(kind)
        r["value"].append(value)

    def finalize(self) -> "Result":
        if self._final:
            return self
        for r in self.rows.values():
            order = np.argsort(np.asarray(r["s"], np.int64), kind="stable")
            r["s"] = np.asarray(r["s"], np.int64)[order]
            r["e"] = np.asarray(r["e"], np.int64)[order]
            r["text"] = [r["text"][i] for i in order]
            r["kind"] = [r["kind"][i] for i in order]
            r["value"] = [r["value"][i] for i in order]
            # end times sorted for range queries (rows are mostly non overlapping)
            r["emax"] = np.maximum.accumulate(r["e"]) if r["e"].shape[0] else r["e"]
        self._final = True
        return self

    @property
    def count(self) -> int:
        return sum(len(r["s"]) for r in self.rows.values())

    def items(self) -> List[Dict[str, Any]]:
        """Every annotation, sorted by start."""
        self.finalize()
        out = []
        for rid, r in self.rows.items():
            for k in range(len(r["s"])):
                out.append({"row": rid, "label": r["label"], "channel": r["channel"], "s": int(r["s"][k]), "e": int(r["e"][k]), "text": r["text"][k], "kind": r["kind"][k], "value": r["value"][k]})
        out.sort(key=lambda x: (x["s"], x["e"]))
        return out

    def texts(self, rid: str, kinds: Optional[Sequence[str]] = None) -> List[str]:
        self.finalize()
        r = self.rows.get(rid)
        if not r:
            return []
        return [t for t, k in zip(r["text"], r["kind"]) if kinds is None or k in kinds]

    def values(self, rid: str, kinds: Optional[Sequence[str]] = ("data",)) -> List[Any]:
        self.finalize()
        r = self.rows.get(rid)
        if not r:
            return []
        return [v for v, k in zip(r["value"], r["kind"]) if kinds is None or k in kinds]

    def view(self, a: float, b: float, px: int) -> List[Dict[str, Any]]:
        """Annotations of each row in samples ``a..b`` for ``px`` pixels. When a row has more than px/6 of them, neighbours closer than 4 pixels merge into one block that shows how many it holds."""
        self.finalize()
        spp = max((b - a) / max(px, 1), 1e-9)
        out = []
        for rid, r in self.rows.items():
            s, e = r["s"], r["e"]
            lo = int(ss(r["emax"], a, side="right"))
            hi = int(ss(s, b, side="left"))
            items: List[List[Any]] = []
            if hi - lo <= max(8, px // 6):
                for k in range(lo, hi):
                    if e[k] > a:
                        items.append([int(s[k]), int(e[k]), r["text"][k], r["kind"][k]])
            else:
                key = np.floor((s[lo:hi] - a) / (4 * spp)).astype(np.int64)
                cut = np.flatnonzero(np.diff(key)) + 1
                starts = np.concatenate(([0], cut))
                ends = np.concatenate((cut, [hi - lo]))
                for g0, g1 in zip(starts, ends):
                    k0, k1 = lo + int(g0), lo + int(g1)
                    if k1 - k0 == 1:
                        items.append([int(s[k0]), int(e[k0]), r["text"][k0], r["kind"][k0]])
                    else:
                        kinds = set(r["kind"][k0:k1])
                        items.append([int(s[k0]), int(e[k0:k1].max()), f"{k1 - k0}", "error" if "error" in kinds else "agg", k1 - k0])
            out.append({"row": rid, "label": r["label"], "channel": r["channel"], "items": items})
        return out


# ----- helpers --------------------------------------------------------------------------------------------
def _ch(cap: LogicCapture, chans: Dict[str, Any], key: str, required: bool = True) -> Optional[int]:
    v = chans.get(key)
    if v is None or v == "" or v == -1:
        if required:
            raise DecodeError(f"choose the {key.upper()} channel")
        return None
    return cap.ch_index(v)


def _newvals(c: Channel) -> np.ndarray:
    k = np.arange(c.edges.shape[0])
    return (c.init ^ ((k + 1) & 1)).astype(np.int8)


def _snap(rate: float, table: Sequence[float], tol: float = 0.03) -> float:
    best = min(table, key=lambda b: abs(b - rate) / b)
    return float(best) if abs(best - rate) / best <= tol else float(rate)


def detect_bit_rate(c: Channel, samplerate: float, max_edges: int = 20000) -> Optional[float]:
    """Bit rate from the shortest pulses: the smallest cluster of widths, refined by fitting every width as a whole number of bits."""
    e = c.edges[:max_edges + 1].astype(np.int64)
    if e.shape[0] < 3:
        return None
    w = np.diff(e).astype(np.float64)
    m = np.percentile(w, 3)
    if m <= 0:
        return None
    cl = w[w < 1.5 * m]
    bitlen = cl.mean() if cl.shape[0] else m
    for _ in range(2):
        ratio = w / bitlen
        k = np.round(ratio)
        sel = (k >= 1) & (k <= 12) & (np.abs(ratio - k) < 0.3)
        if sel.sum() < 2:
            break
        bitlen = w[sel].sum() / k[sel].sum()
    return samplerate / bitlen


def _fmt(v: int, bits: int, fmt: str) -> str:
    if fmt == "dec":
        return str(v)
    if fmt == "bin":
        return format(v, f"0{bits}b")
    if fmt == "ascii":
        if 32 <= v < 127:
            return chr(v)
        return {10: "\\n", 13: "\\r", 9: "\\t", 0: "\\0"}.get(v, f"\\x{v:02x}")
    return f"{v:0{max(1, (bits + 3) // 4)}X}"


def _opt(o: Dict[str, Any], key: str, default):
    v = o.get(key, default)
    return default if v is None or v == "" else v


# ----- UART -------------------------------------------------------------------------------------------------
def uart_frames(cap: LogicCapture, ci: int, o: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
    """Frames on one UART line: [{s, e, value, parity_ok, frame_ok}] and the baud used."""
    c = cap.channels[ci]
    sr = cap.samplerate
    inv = 1 if str(_opt(o, "inverted", False)).lower() in ("1", "true", "yes") else 0
    baud = _opt(o, "baud", "auto")
    if str(baud).lower() == "auto":
        est = detect_bit_rate(c, sr)
        if not est:
            return [], 0.0
        baud = _snap(est, STANDARD_BAUDS)
    baud = float(baud)
    if baud <= 0 or sr / baud < 2:
        raise DecodeError(f"{baud:g} baud needs a sample rate of at least {2 * baud:g} Hz (the capture has {sr:g})")
    db = int(_opt(o, "data_bits", 8))
    if not 5 <= db <= 9:
        raise DecodeError("data bits must be 5 to 9")
    parity = str(_opt(o, "parity", "none")).lower()
    stop = float(_opt(o, "stop_bits", 1))
    msb = str(_opt(o, "bit_order", "lsb")).lower() == "msb"
    bl = sr / baud
    npar = 0 if parity == "none" else 1
    nv = _newvals(c)
    starts = c.edges[nv == (1 if inv else 0)].astype(np.int64)
    nb = 1 + db + npar
    nstop = max(1, int(np.ceil(stop)))
    # a frame needs its stop bits inside the capture (allowing one bit of slack at the end)
    starts = starts[starts + (nb + stop) * bl <= cap.n + bl]
    if starts.shape[0] == 0:
        return [], baud
    # the start bit must still be low half a bit in, or the edge was a glitch
    valid = (c.value_at(np.minimum((starts + 0.5 * bl).astype(np.int64), cap.n - 1)) ^ inv) == 0
    # after a frame the next one starts at the first start edge after the middle of its last data/parity bit plus half a bit
    nxt = ss(starts, starts + (nb + 0.5) * bl, side="right")
    chosen = []
    i, total = 0, starts.shape[0]
    valid_l, nxt_l = valid.tolist(), np.asarray(nxt).tolist()
    while i < total:
        if not valid_l[i]:
            i += 1
            continue
        chosen.append(i)
        i = nxt_l[i]
    st = starts[np.asarray(chosen, np.int64)]
    # every bit's middle sample at once: start bit, data bits, parity, stop bits
    offs = (np.arange(nb + nstop) + 0.5) * bl
    pts = np.minimum((st[:, None] + offs[None, :]).astype(np.int64), cap.n - 1)
    v = (c.value_at(pts.ravel()).reshape(pts.shape).astype(np.int64)) ^ inv
    bits = v[:, 1:1 + db]
    if msb:
        bits = bits[:, ::-1]
    vals = bits @ (1 << np.arange(db, dtype=np.int64))
    if npar:
        ones = bits.sum(1)
        exp = {"odd": (ones + 1) & 1, "even": ones & 1, "mark": np.ones_like(ones), "space": np.zeros_like(ones)}.get(parity, np.zeros_like(ones))
        par_ok = v[:, 1 + db] == exp
    else:
        par_ok = np.ones(st.shape[0], bool)
    frame_ok = np.all(v[:, nb:nb + nstop] == 1, axis=1)
    stf = st.astype(np.float64)
    frames = [{"s": int(s0), "e": float(s0 + (nb + stop) * bl), "value": int(val), "parity_ok": bool(p), "frame_ok": bool(f), "data_s": float(s0 + bl), "data_e": float(s0 + (1 + db) * bl)}
              for s0, val, p, f in zip(stf.tolist(), vals.tolist(), par_ok.tolist(), frame_ok.tolist())]
    return frames, baud


def decode_uart(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    res = Result("uart")
    fmt = str(_opt(o, "format", "hex"))
    db = int(_opt(o, "data_bits", 8))
    any_ch = False
    for key, label in (("rx", "RX"), ("tx", "TX")):
        ci = _ch(cap, chans, key, required=False)
        if ci is None:
            continue
        any_ch = True
        frames, baud = uart_frames(cap, ci, o)
        rid = res.row(key, f"UART {label}", ci)
        res.meta[f"{key}_baud"] = baud
        res.meta[f"{key}_frames"] = len(frames)
        errors = 0
        for f in frames:
            v = f["value"]
            text = _fmt(v, db, fmt)
            kind = "data"
            if not f["parity_ok"]:
                text += " parity error"
                kind = "error"
            if not f["frame_ok"]:
                text += " framing error" if v else " break"
                kind = "error"
            errors += kind == "error"
            res.add(rid, f["s"], f["e"], text, kind, v)
        res.meta[f"{key}_errors"] = errors
    if not any_ch:
        raise DecodeError("choose an RX or TX channel")
    bauds = [v for k, v in res.meta.items() if k.endswith("_baud") and v]
    res.meta["baud"] = bauds[0] if bauds else None
    return res.finalize()


# ----- SPI --------------------------------------------------------------------------------------------------
def decode_spi(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    res = Result("spi")
    sck = _ch(cap, chans, "sck")
    cs = _ch(cap, chans, "cs", required=False)
    mosi = _ch(cap, chans, "mosi", required=False)
    miso = _ch(cap, chans, "miso", required=False)
    if mosi is None and miso is None:
        raise DecodeError("choose a MOSI or MISO channel")
    cpol, cpha = int(_opt(o, "cpol", 0)), int(_opt(o, "cpha", 0))
    if "mode" in o and o["mode"] not in (None, ""):
        m = int(o["mode"])
        cpol, cpha = m >> 1, m & 1
    ws = int(_opt(o, "word_size", 8))
    msb = str(_opt(o, "bit_order", "msb")).lower() != "lsb"
    cs_level = 1 if str(_opt(o, "cs_active", "low")).lower() in ("high", "1") else 0
    fmt = str(_opt(o, "format", "hex"))
    c_sck = cap.channels[sck]
    nv = _newvals(c_sck)
    sample_rising = cpol == cpha
    se_all = c_sck.edges[nv == (1 if sample_rising else 0)].astype(np.int64)
    all_e = c_sck.edges.astype(np.int64)
    if cs is not None:
        c_cs = cap.channels[cs]
        cnv = _newvals(c_cs)
        assert_e = c_cs.edges[cnv == cs_level].astype(np.int64)
        release_e = c_cs.edges[cnv != cs_level].astype(np.int64)
        windows = []
        if c_cs.init == cs_level:
            first_rel = release_e[0] if release_e.shape[0] else cap.n
            windows.append((0, int(first_rel)))
        for a in assert_e:
            k = ss(release_e, a, side="right")
            windows.append((int(a), int(release_e[k]) if k < release_e.shape[0] else cap.n))
    else:
        windows = [(0, cap.n)]
    rows = {}
    for key, ci in (("mosi", mosi), ("miso", miso)):
        if ci is not None:
            rows[key] = res.row(key, key.upper(), ci)
    trow = res.row("transfer", "SPI", cs if cs is not None else sck)
    words = {"mosi": [], "miso": []}
    mode_warn = False
    for (w0, w1) in windows:
        lo, hi = ss(se_all, w0, side="right"), ss(se_all, w1, side="left")
        se = se_all[lo:hi]
        if cs is not None and not mode_warn and c_sck.value_at(w0) != cpol and se.shape[0]:
            res.warnings.append(f"SCK idles {'high' if c_sck.value_at(w0) else 'low'} at chip select but CPOL is {cpol}; check the SPI mode")
            mode_warn = True
        nwords = se.shape[0] // ws
        tw = {"mosi": [], "miso": []}
        for wi in range(nwords):
            idx = se[wi * ws:(wi + 1) * ws]
            k0 = int(ss(all_e, idx[0], side="left")) - 1
            start = max(w0, int(all_e[k0])) if k0 >= 0 else w0
            k1 = int(ss(all_e, idx[-1], side="right"))
            end = min(w1, int(all_e[k1])) if k1 < all_e.shape[0] else w1
            for key, ci in (("mosi", mosi), ("miso", miso)):
                if ci is None:
                    continue
                bits = cap.channels[ci].value_at(idx)
                order = bits if msb else bits[::-1]
                v = 0
                for b in order:
                    v = (v << 1) | int(b)
                res.add(rows[key], start, end, _fmt(v, ws, fmt), "data", v)
                tw[key].append(v)
                words[key].append(v)
        if se.shape[0] % ws and cs is not None:
            res.add(trow, se[nwords * ws], w1, f"incomplete word ({se.shape[0] % ws} bits)", "warn")
        if nwords and cs is not None:
            parts = []
            for key in ("mosi", "miso"):
                if tw[key]:
                    parts.append(f"{key.upper()} " + " ".join(_fmt(x, ws, "hex") for x in tw[key]))
            res.add(trow, w0, w1, " / ".join(parts), "cmd", {"mosi": tw["mosi"], "miso": tw["miso"]})
    res.meta.update({"mode": cpol * 2 + cpha, "words": max(len(words["mosi"]), len(words["miso"])), "transfers": len(windows) if cs is not None else None})
    return res.finalize()


# ----- I2C --------------------------------------------------------------------------------------------------
def decode_i2c(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    res = Result("i2c")
    scl, sda = _ch(cap, chans, "scl"), _ch(cap, chans, "sda")
    c_scl, c_sda = cap.channels[scl], cap.channels[sda]
    addr8 = str(_opt(o, "address_format", "7-bit")).startswith("8")
    rid = res.row("i2c", "I2C", sda)
    snv = _newvals(c_scl)
    scl_rise = c_scl.edges[snv == 1].astype(np.int64)
    scl_all = c_scl.edges.astype(np.int64)
    sda_e = c_sda.edges.astype(np.int64)
    dnv = _newvals(c_sda)
    # events: (index, order, kind) with SDA changes before SCL rises at the same sample (data setup)
    ev = np.concatenate((np.stack((sda_e, np.zeros_like(sda_e), dnv.astype(np.int64)), 1), np.stack((scl_rise, np.ones_like(scl_rise), np.full_like(scl_rise, 2)), 1))) if sda_e.shape[0] + scl_rise.shape[0] else np.zeros((0, 3), np.int64)
    ev = ev[np.lexsort((ev[:, 1], ev[:, 0]))] if ev.shape[0] else ev

    def next_fall(i):
        k = int(ss(scl_all, i, side="right"))
        while k < scl_all.shape[0]:
            if c_scl.value_at(int(scl_all[k])) == 0:
                return int(scl_all[k])
            k += 1
        return i + 1

    def prev_fall(i):
        k = int(ss(scl_all, i, side="left")) - 1
        while k >= 0:
            if c_scl.value_at(int(scl_all[k])) == 0:
                return int(scl_all[k])
            k -= 1
        return max(0, i - 1)

    state = "idle"
    bits: List[Tuple[int, int]] = []
    byte_no = 0
    rw = 0
    addr10_hi = None
    last_addr = None
    stats = {"transactions": 0, "nacks": 0, "bytes": 0}
    for idx, order, kind in ev:
        idx = int(idx)
        if order == 0:
            if idx > 0 and c_scl.value_at(idx - 1) == 1 and c_scl.value_at(idx) == 1:
                if kind == 0:  # SDA falls while SCL high: (repeated) start
                    rep = state != "idle"
                    res.add(rid, idx, next_fall(idx), "Sr" if rep else "S", "start", "repeated start" if rep else "start")
                    state, bits, byte_no = "data", [], 0
                    stats["transactions"] += 0 if rep else 1
                else:  # SDA rises while SCL high: stop
                    res.add(rid, prev_fall(idx), idx + 1, "P", "stop", "stop")
                    state, bits = "idle", []
            continue
        if state == "idle":
            continue
        bits.append((idx, c_sda.value_at(idx)))
        if len(bits) < 9:
            continue
        val = 0
        for _, b in bits[:8]:
            val = (val << 1) | b
        ack = bits[8][1] == 0
        s = prev_fall(bits[0][0])
        e_data = next_fall(bits[7][0])
        e_ack = next_fall(bits[8][0])
        if byte_no == 0:
            rw = val & 1
            if (val & 0xF8) == 0xF0 and not rw:
                addr10_hi = (val >> 1) & 0x3
                res.add(rid, s, e_data, f"10-bit address, high bits {addr10_hi}", "addr", val)
            elif (val & 0xF8) == 0xF0 and rw and last_addr is not None and last_addr > 0x7F:
                res.add(rid, s, e_data, f"Read 0x{last_addr:03X}", "addr", last_addr)
            else:
                a = val >> 1
                last_addr = a
                shown = (a << 1) | rw if addr8 else a
                res.add(rid, s, e_data, f"{'Read' if rw else 'Write'} 0x{shown:02X}", "addr", a)
        elif byte_no == 1 and addr10_hi is not None and not rw:
            a = (addr10_hi << 8) | val
            last_addr = a
            addr10_hi = None
            res.add(rid, s, e_data, f"Write 0x{a:03X}", "addr", a)
        else:
            res.add(rid, s, e_data, f"{val:02X}", "data", val)
            stats["bytes"] += 1
        res.add(rid, e_data, e_ack, "ACK" if ack else "NACK", "ack" if ack else "nack", ack)
        stats["nacks"] += 0 if ack else 1
        bits = []
        byte_no += 1
    res.meta.update(stats)
    return res.finalize()


# ----- 1-Wire -----------------------------------------------------------------------------------------------
ONEWIRE_ROM_CMDS = {0x33: "Read ROM", 0x0F: "Read ROM", 0x55: "Match ROM", 0xF0: "Search ROM", 0xEC: "Alarm search", 0xCC: "Skip ROM", 0x3C: "Overdrive skip ROM", 0x69: "Overdrive match ROM", 0xA5: "Resume"}
ONEWIRE_FN_CMDS = {0x44: "Convert T", 0xBE: "Read scratchpad", 0x4E: "Write scratchpad", 0x48: "Copy scratchpad", 0xB8: "Recall E2", 0xB4: "Read power supply", 0xF0: "Read memory", 0x0F: "Write scratchpad", 0xAA: "Read scratchpad", 0x55: "Copy scratchpad"}


def _crc8_maxim(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def decode_onewire(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    """1-Wire at standard speed, switching to overdrive timing after an Overdrive Skip ROM (0x3C) or Overdrive Match ROM (0x69) until the next standard-length reset (480 us)."""
    res = Result("onewire")
    ci = _ch(cap, chans, "owr")
    c = cap.channels[ci]
    sr = cap.samplerate
    us = sr * 1e-6
    rid = res.row("onewire", "1-Wire", ci)
    rom_row = res.row("rom", "ROM", ci)
    nv = _newvals(c)
    e = c.edges.astype(np.int64)
    pulses = []  # (fall, rise)
    for j in range(e.shape[0]):
        if nv[j] == 0:
            r = int(e[j + 1]) if j + 1 < e.shape[0] else cap.n
            pulses.append((int(e[j]), r))
    if us < 1:
        res.warnings.append(f"the sample rate {sr:g} Hz is too low for 1-Wire bit slots (at least 1 MHz is needed)")
    od = False  # overdrive speed
    od_seen = False
    bits: List[Tuple[int, int]] = []
    byte_no = 0
    rom_cmd = None
    rom_bytes: List[Tuple[int, int, int]] = []
    search_bits: List[int] = []
    stats = {"resets": 0, "presence": 0, "bytes": 0}
    skip = -1
    for j, (f, r) in enumerate(pulses):
        if j == skip:
            continue
        w = (r - f) / us
        if w >= 380 or (od and w >= 40):
            if w >= 380:
                od = False
            res.add(rid, f, r, "Reset" + (" (overdrive)" if od else ""), "start", "reset")
            stats["resets"] += 1
            bits, byte_no, rom_cmd, rom_bytes, search_bits = [], 0, None, [], []
            nxt = pulses[j + 1] if j + 1 < len(pulses) else None
            gap_max, pw = (15, (5, 40)) if od else (80, (40, 300))
            if nxt is not None and (nxt[0] - r) / us <= gap_max and pw[0] <= (nxt[1] - nxt[0]) / us <= pw[1]:
                res.add(rid, nxt[0], nxt[1], "Presence", "ack", True)
                stats["presence"] += 1
                skip = j + 1
            else:
                res.add(rid, r, min(cap.n, r + (24 if od else 240) * us), "No presence", "nack", False)
            continue
        slot = (10 if od else 60) * us
        bit = 1 if w < (2.5 if od else 15) else 0
        bits.append((f, bit))
        if rom_cmd in (0xF0, 0xEC) and byte_no == 1:
            search_bits.append(bit)
            if len(search_bits) == 192:
                rom = sum((search_bits[3 * i + 2] << i) for i in range(64))
                rb = rom.to_bytes(8, "little")
                res.add(rom_row, bits[0][0] if bits else f, f + slot, f"ROM {rb.hex(' ')} {'CRC ok' if _crc8_maxim(rb[:7]) == rb[7] else 'CRC error'}", "addr", rb.hex())
                byte_no, bits = 2, []
            continue
        if len(bits) < 8:
            continue
        val = sum(b << k for k, (_, b) in enumerate(bits))
        s, e_ = bits[0][0], bits[-1][0] + slot
        bits = []
        stats["bytes"] += 1
        if byte_no == 0:
            rom_cmd = val
            res.add(rid, s, e_, f"{ONEWIRE_ROM_CMDS.get(val, 'Command')} (0x{val:02X})", "cmd", val)
            if val in (0x3C, 0x69):
                od = od_seen = True
                if us < 2:
                    res.warnings.append(f"overdrive needs at least 2 MHz sampling; the capture has {sr:g} Hz")
        elif rom_cmd in (0x33, 0x0F, 0x55, 0x69) and 1 <= byte_no <= 8:
            rom_bytes.append((s, e_, val))
            res.add(rid, s, e_, f"{val:02X}", "data", val)
            if len(rom_bytes) == 8:
                rb = bytes(x[2] for x in rom_bytes)
                ok = _crc8_maxim(rb[:7]) == rb[7]
                res.add(rom_row, rom_bytes[0][0], rom_bytes[-1][1], f"family 0x{rb[0]:02X} serial {rb[1:7][::-1].hex().upper()} CRC {'ok' if ok else 'error'}", "addr" if ok else "error", rb.hex())
        elif (rom_cmd in (0xCC, 0x3C) and byte_no == 1) or (rom_cmd in (0x33, 0x0F, 0x55, 0x69) and byte_no == 9) or (rom_cmd in (0xF0, 0xEC) and byte_no == 2):
            res.add(rid, s, e_, f"{ONEWIRE_FN_CMDS.get(val, 'Function')} (0x{val:02X})", "cmd", val)
        else:
            res.add(rid, s, e_, f"{val:02X}", "data", val)
        byte_no += 1
    stats["overdrive"] = od_seen
    res.meta.update(stats)
    return res.finalize()


# ----- JTAG -------------------------------------------------------------------------------------------------
TAP = {
    "Test-Logic-Reset": ("Run-Test/Idle", "Test-Logic-Reset"), "Run-Test/Idle": ("Run-Test/Idle", "Select-DR-Scan"),
    "Select-DR-Scan": ("Capture-DR", "Select-IR-Scan"), "Capture-DR": ("Shift-DR", "Exit1-DR"), "Shift-DR": ("Shift-DR", "Exit1-DR"), "Exit1-DR": ("Pause-DR", "Update-DR"),
    "Pause-DR": ("Pause-DR", "Exit2-DR"), "Exit2-DR": ("Shift-DR", "Update-DR"), "Update-DR": ("Run-Test/Idle", "Select-DR-Scan"),
    "Select-IR-Scan": ("Capture-IR", "Test-Logic-Reset"), "Capture-IR": ("Shift-IR", "Exit1-IR"), "Shift-IR": ("Shift-IR", "Exit1-IR"), "Exit1-IR": ("Pause-IR", "Update-IR"),
    "Pause-IR": ("Pause-IR", "Exit2-IR"), "Exit2-IR": ("Shift-IR", "Update-IR"), "Update-IR": ("Run-Test/Idle", "Select-DR-Scan"),
}
ARM_IR4 = {0x8: "ABORT", 0xA: "DPACC", 0xB: "APACC", 0xE: "IDCODE", 0xF: "BYPASS"}


def decode_jtag(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    res = Result("jtag")
    tck, tms, tdi = _ch(cap, chans, "tck"), _ch(cap, chans, "tms"), _ch(cap, chans, "tdi")
    tdo = _ch(cap, chans, "tdo", required=False)
    state = str(_opt(o, "initial_state", "Test-Logic-Reset"))
    if state not in TAP:
        raise DecodeError(f"unknown TAP state {state!r}")
    c = cap.channels[tck]
    nv = _newvals(c)
    rise = c.edges[nv == 1].astype(np.int64)
    if rise.shape[0] == 0:
        return res.finalize()
    vt = cap.channels[tms].value_at(rise)
    vi = cap.channels[tdi].value_at(rise)
    vo = cap.channels[tdo].value_at(rise) if tdo is not None else None
    period = float(np.median(np.diff(rise))) if rise.shape[0] > 1 else 1.0
    srow = res.row("state", "TAP state", tms)
    irow = res.row("ir", "IR", tdi)
    drow = res.row("dr", "DR", tdo if tdo is not None else tdi)
    seg_start, seg_state = int(rise[0]), state
    shift: Optional[Dict[str, Any]] = None
    last_ir: Optional[int] = None
    last_ir_len = 0
    ones = 0
    stats = {"ir": 0, "dr": 0}
    for k in range(rise.shape[0]):
        r = int(rise[k])
        ones = ones + 1 if vt[k] else 0
        if state in ("Shift-IR", "Shift-DR"):
            if shift is None or shift["reg"] != state[-2:]:
                shift = {"reg": state[-2:], "s": r, "tdi": [], "tdo": []}
            shift["tdi"].append(int(vi[k]))
            if vo is not None:
                shift["tdo"].append(int(vo[k]))
            shift["e"] = r + period
        nxt = TAP[state][int(vt[k])]
        if ones >= 5:
            nxt = "Test-Logic-Reset"
        if nxt != state:
            res.add(srow, seg_start, r, seg_state, "state", seg_state)
            seg_start, seg_state = r, nxt
        if nxt in ("Update-IR", "Update-DR", "Test-Logic-Reset") and shift is not None:
            n = len(shift["tdi"])
            ti = sum(b << i for i, b in enumerate(shift["tdi"]))
            to = sum(b << i for i, b in enumerate(shift["tdo"])) if shift["tdo"] else None
            w = max(1, (n + 3) // 4)
            if shift["reg"] == "IR":
                last_ir, last_ir_len = ti, n
                name = ARM_IR4.get(ti) if n == 4 else None
                text = f"IR {ti:0{w}X}" + (f" {name}" if name else "") + (f" (TDO {to:0{w}X})" if to is not None else "") + f" {n} bits"
                res.add(irow, shift["s"], shift["e"], text, "cmd", {"tdi": ti, "tdo": to, "bits": n})
                stats["ir"] += 1
            else:
                text = f"DR TDI {ti:0{w}X}" + (f" TDO {to:0{w}X}" if to is not None else "") + f" {n} bits"
                if last_ir_len == 4 and ARM_IR4.get(last_ir or -1) == "IDCODE" and n == 32 and to is not None:
                    text += f" (IDCODE: part 0x{(to >> 12) & 0xFFFF:04X}, manufacturer 0x{(to >> 1) & 0x7FF:03X}, version {to >> 28})"
                res.add(drow, shift["s"], shift["e"], text, "data", {"tdi": ti, "tdo": to, "bits": n})
                stats["dr"] += 1
            shift = None
        state = nxt
    res.add(srow, seg_start, int(rise[-1]) + period, seg_state, "state", seg_state)
    res.meta.update(stats, final_state=state)
    return res.finalize()


# ----- SWD --------------------------------------------------------------------------------------------------
SWD_DP_R = {0x0: "DPIDR", 0x4: "CTRL/STAT", 0x8: "RESEND", 0xC: "RDBUFF"}
SWD_DP_W = {0x0: "ABORT", 0x4: "CTRL/STAT", 0x8: "SELECT", 0xC: "TARGETSEL"}


def decode_swd(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    res = Result("swd")
    clk, dio = _ch(cap, chans, "swclk"), _ch(cap, chans, "swdio")
    c = cap.channels[clk]
    nv = _newvals(c)
    rise = c.edges[nv == 1].astype(np.int64)
    fall = c.edges[nv == 0].astype(np.int64)
    rid = res.row("swd", "SWD", dio)
    if rise.shape[0] < 8:
        return res.finalize()
    k_f = ss(fall, rise, side="right")
    f_after = np.where(k_f < fall.shape[0], fall[np.minimum(k_f, fall.shape[0] - 1)], np.minimum(rise + 1, cap.n - 1))
    d = cap.channels[dio]
    hb = d.value_at(rise).astype(np.int64)
    tb = d.value_at(f_after).astype(np.int64)
    period = float(np.median(np.diff(rise)))
    N = rise.shape[0]
    stats = {"ok": 0, "wait": 0, "fault": 0, "errors": 0, "resets": 0}

    def word(bits):
        return int(sum(int(b) << i for i, b in enumerate(bits)))
    k = 0
    while k < N:
        if hb[k] != 1:
            k += 1
            continue
        run = 0
        while k + run < N and hb[k + run] == 1:
            run += 1
        if run >= 50:
            res.add(rid, rise[k], rise[k + run - 1] + period, "Line reset", "start", run)
            stats["resets"] += 1
            k += run
            if k + 16 <= N and word(hb[k:k + 16]) == 0xE79E:
                res.add(rid, rise[k], rise[k + 15] + period, "JTAG-to-SWD", "info", 0xE79E)
                k += 16
            continue
        if k + 12 > N:
            break
        h = hb[k:k + 8]
        if not (h[6] == 0 and h[7] == 1):
            k += 1
            continue
        ap, rnw, addr = int(h[1]), int(h[2]), int(h[3]) * 4 + int(h[4]) * 8
        par_ok = (int(h[1:5].sum()) & 1) == int(h[5])
        reg = (f"AP 0x{addr:X}" if ap else (SWD_DP_R if rnw else SWD_DP_W).get(addr, f"0x{addr:X}"))
        text = f"{'AP' if ap else 'DP'} {'read' if rnw else 'write'} {reg if not ap else f'0x{addr:X}'}"
        res.add(rid, rise[k], rise[k + 7] + period, text + ("" if par_ok else " parity error"), "addr" if par_ok else "error", {"ap": ap, "rnw": rnw, "addr": addr})
        ack = word(tb[k + 9:k + 12])
        name = {1: "OK", 2: "WAIT", 4: "FAULT"}.get(ack, "no response" if ack == 7 else f"bad ACK {ack:03b}")
        kind = {1: "ack", 2: "warn", 4: "nack"}.get(ack, "error")
        res.add(rid, rise[k + 9], rise[k + 11] + period, name, kind, ack)
        stats[{1: "ok", 2: "wait", 4: "fault"}.get(ack, "errors")] += 1
        if ack == 1:
            if rnw:
                if k + 46 > N:
                    break
                bits, p = tb[k + 12:k + 44], int(tb[k + 44])
                s0, s1 = rise[k + 12], rise[k + 44] + period
                k_next = k + 46
            else:
                if k + 46 > N:
                    break
                bits, p = hb[k + 13:k + 45], int(hb[k + 45])
                s0, s1 = rise[k + 13], rise[k + 45] + period
                k_next = k + 46
            v = word(bits)
            pok = (int(bits.sum()) & 1) == p
            res.add(rid, s0, s1, f"0x{v:08X}" + ("" if pok else " parity error"), "data" if pok else "error", v)
            stats["errors"] += 0 if pok else 1
            k = k_next
        else:
            k += 13
    res.meta.update(stats)
    return res.finalize()


# ----- CAN --------------------------------------------------------------------------------------------------
def _crc15(bits):
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def to_int_(bb) -> int:
    return int("".join(map(str, bb)), 2) if len(bb) else 0


def decode_can(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    """Classic CAN (2.0A/B). A CAN FD frame (FDF bit recessive) is marked and skipped, not decoded; set the nominal bit rate for buses carrying CAN FD, since auto detection would find the faster data phase."""
    res = Result("can")
    ci = _ch(cap, chans, "can")
    c = cap.channels[ci]
    sr = cap.samplerate
    br = _opt(o, "bitrate", "auto")
    if str(br).lower() == "auto":
        est = detect_bit_rate(c, sr)
        if not est:
            return res.finalize()
        br = _snap(est, STANDARD_CAN, 0.05)
    br = float(br)
    sp = float(_opt(o, "sample_point", 70)) / 100.0
    bl = sr / br
    if bl < 3:
        raise DecodeError(f"{br:g} bit/s needs a sample rate of at least {3 * br:g} Hz")
    res.meta["bitrate"] = br
    frow = res.row("fields", "CAN", ci)
    mrow = res.row("frames", "CAN frames", ci)
    e = c.edges.astype(np.int64)
    nv = _newvals(c)
    falls = e[nv == 0]
    stats = {"frames": 0, "errors": 0}
    pos = 0
    while True:
        k = int(ss(falls, pos, side="left"))
        sof = None
        while k < falls.shape[0]:
            f = int(falls[k])
            j = int(ss(e, f, side="left"))
            prev = int(e[j - 1]) if j > 0 else 0
            if (f - prev) >= 6 * bl or j == 0:
                sof = f
                break
            k += 1
        if sof is None:
            break
        # read bits with resynchronisation on recessive to dominant edges
        bit_start = float(sof)
        raw: List[Tuple[int, float]] = []

        def read_bit():
            nonlocal bit_start
            v = c.value_at(min(int(bit_start + sp * bl), cap.n - 1))
            s = bit_start
            exp = bit_start + bl
            j = int(ss(e, exp - 0.5 * bl, side="left"))
            if j < e.shape[0] and e[j] <= exp + 0.5 * bl and nv[j] == 0:
                bit_start = float(e[j])
            else:
                bit_start = exp
            return v, s
        bits: List[int] = []
        pos_of: List[float] = []
        run, last, err = 0, None, None

        def destuffed():
            nonlocal run, last, err
            v, s = read_bit()
            if run == 5:
                if v == last:
                    err = ("stuff error", s)
                    return None
                run, last = 1, v
                v, s = read_bit()
            run = run + 1 if v == last else 1
            last = v
            bits.append(v)
            pos_of.append(s)
            return v
        need = 1 + 11 + 2
        ok = True
        while len(bits) < need:
            if destuffed() is None or bit_start >= cap.n:
                ok = False
                break
        ext = False
        if ok:
            ext = bits[13] == 1
            need = 39 if ext else 19
            while len(bits) < need and ok:
                ok = destuffed() is not None and bit_start < cap.n
        if ok and bits[33 if ext else 14] == 1:
            # FDF (r0 in a standard frame, r1 in an extended one) recessive: a CAN FD frame, whose data phase this classic decoder does not read
            ident = ((to_int_(bits[1:12]) << 18) | to_int_(bits[14:32])) if ext else to_int_(bits[1:12])
            res.add(frow, sof, pos_of[-1] + bl, f"CAN FD frame, ID 0x{ident:0{8 if ext else 3}X}: not decoded (classic CAN only)", "warn", ident)
            res.add(mrow, sof, pos_of[-1] + bl, f"CAN FD {'ext ' if ext else ''}0x{ident:0{8 if ext else 3}X} (not decoded)", "warn", {"id": ident, "extended": ext, "fd": True})
            stats["fd_frames"] = stats.get("fd_frames", 0) + 1
            pos = int(pos_of[-1] + bl)
            continue
        dlc = nbytes = 0
        if ok:
            dlc_bits = bits[need - 4:need]
            dlc = int("".join(map(str, dlc_bits)), 2)
            rtr = bits[32] if ext else bits[12]
            nbytes = 0 if rtr else min(dlc, 8)
            need = need + 8 * nbytes + 15
            while len(bits) < need and ok:
                ok = destuffed() is not None and bit_start < cap.n
        if not ok:
            s_err = err[1] if err else sof
            res.add(frow, s_err, s_err + bl, err[0] if err else "truncated frame", "error")
            stats["errors"] += 1
            pos = int(s_err + bl)
            continue
        def span(i0, i1):
            return pos_of[i0], pos_of[i1 - 1] + bl
        to_int = lambda bb: int("".join(map(str, bb)), 2) if bb else 0  # noqa: E731
        res.add(frow, *span(0, 1), "SOF", "start")
        if ext:
            ident = (to_int(bits[1:12]) << 18) | to_int(bits[14:32])
            res.add(frow, *span(1, 32), f"ID 0x{ident:08X} (ext)", "addr", ident)
            res.add(frow, *span(32, 35), "RTR" if bits[32] else "data frame", "info", bits[32])
            dstart = 39
            rtr = bits[32]
        else:
            ident = to_int(bits[1:12])
            res.add(frow, *span(1, 12), f"ID 0x{ident:03X}", "addr", ident)
            res.add(frow, *span(12, 15), "RTR" if bits[12] else "data frame", "info", bits[12])
            dstart = 19
            rtr = bits[12]
        res.add(frow, *span(dstart - 4, dstart), f"DLC {dlc}", "info", dlc)
        data = []
        for b in range(nbytes):
            v = to_int(bits[dstart + 8 * b:dstart + 8 * b + 8])
            data.append(v)
            res.add(frow, *span(dstart + 8 * b, dstart + 8 * b + 8), f"{v:02X}", "data", v)
        cpos = dstart + 8 * nbytes
        crc_rx = to_int(bits[cpos:cpos + 15])
        crc_ok = _crc15(bits[:cpos]) == crc_rx
        res.add(frow, *span(cpos, cpos + 15), f"CRC {crc_rx:04X} {'ok' if crc_ok else 'error'}", "ack" if crc_ok else "error", crc_rx)
        # fixed form: CRC delimiter, ACK slot, ACK delimiter, EOF (no stuffing)
        tail = []
        for _ in range(10):
            v, s = read_bit()
            tail.append((v, s))
        delim_ok = tail[0][0] == 1
        acked = tail[1][0] == 0
        res.add(frow, tail[1][1], tail[1][1] + bl, "ACK" if acked else "NACK", "ack" if acked else "nack", acked)
        eof_ok = all(v == 1 for v, _ in tail[3:10])
        res.add(frow, tail[3][1], tail[9][1] + bl, "EOF" if eof_ok and delim_ok else "form error", "stop" if eof_ok and delim_ok else "error")
        fr_ok = crc_ok and acked and eof_ok and delim_ok
        stats["frames"] += 1
        stats["errors"] += 0 if fr_ok else 1
        txt = f"{'ext ' if ext else ''}0x{ident:0{8 if ext else 3}X}" + (" RTR" if rtr else f" [{dlc}] " + " ".join(f"{x:02X}" for x in data)) + ("" if crc_ok else " CRC error") + ("" if acked else " no ACK")
        res.add(mrow, sof, tail[9][1] + bl, txt, "cmd" if fr_ok else "error", {"id": ident, "extended": ext, "rtr": bool(rtr), "dlc": dlc, "data": data, "crc_ok": crc_ok, "ack": acked})
        pos = int(tail[9][1] + bl)
    res.meta.update(stats)
    return res.finalize()


# ----- SimpleSerial -----------------------------------------------------------------------------------------
def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0:
            raise ValueError("zero byte inside a COBS frame")
        out += data[i + 1:i + code]
        i += code
        if code < 255 and i < len(data):
            out.append(0)
    return bytes(out)


def _crc8_ss2(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x4D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


SS_CMDS = {"p": "plaintext", "k": "key", "r": "response", "z": "ack", "e": "error/ack", "v": "version", "w": "ver", "x": "reset", "g": "glitch loop", "y": "number of commands"}


def decode_simpleserial(cap: LogicCapture, chans: Dict[str, Any], o: Dict[str, Any]) -> Result:
    res = Result("simpleserial")
    version = str(_opt(o, "version", "auto"))
    any_ch = False
    for key, label in (("rx", "host to target"), ("tx", "target to host")):
        ci = _ch(cap, chans, key, required=False)
        if ci is None:
            continue
        any_ch = True
        frames, baud = uart_frames(cap, ci, o)
        res.meta[f"{key}_baud"] = baud
        rid = res.row(key, f"SimpleSerial {label}", ci)
        good = [f for f in frames if f["frame_ok"]]
        data = bytes(f["value"] & 0xFF for f in good)
        v = version
        if v == "auto":
            v = "2" if b"\x00" in data else "1"
        msgs = []
        if v.startswith("1"):
            cur: List[Dict[str, Any]] = []
            for f in good:
                b = f["value"] & 0xFF
                if b in (0x0A, 0x0D):
                    if cur:
                        msgs.append((cur[0]["s"], f["e"], bytes(x["value"] & 0xFF for x in cur)))
                    cur = []
                else:
                    cur.append(f)
            for s, e, raw in msgs:
                txt = raw.decode("latin-1")
                cmd, hexpart = txt[:1], txt[1:]
                try:
                    payload = bytes.fromhex(hexpart)
                    ok = True
                except ValueError:
                    payload, ok = hexpart.encode(), False
                if cmd == "z":
                    status = payload[0] if payload else 0
                    res.add(rid, s, e, f"ack {status:02X}" + ("" if status == 0 else " (error)"), "ack" if status == 0 else "nack", {"cmd": cmd, "status": status})
                else:
                    kind = "cmd" if key == "rx" else "data"
                    text = f"{cmd} {payload.hex(' ').upper()}" if ok else f"{cmd} {hexpart!r} (not hex)"
                    res.add(rid, s, e, text + f"  [{SS_CMDS.get(cmd, 'command')}, {len(payload)} bytes]", kind if ok else "warn", {"cmd": cmd, "payload": payload.hex(), "version": 1})
        else:
            cur = []
            for f in good:
                if (f["value"] & 0xFF) == 0:
                    if cur:
                        msgs.append((cur[0]["s"], f["e"], bytes(x["value"] & 0xFF for x in cur)))
                    cur = []
                else:
                    cur.append(f)
            for s, e, raw in msgs:
                try:
                    body = cobs_decode(raw)
                except ValueError as ex:
                    res.add(rid, s, e, f"bad frame: {ex}", "error")
                    continue
                if len(body) < 4:
                    res.add(rid, s, e, "short frame", "error")
                    continue
                cmd, scmd, dlen = chr(body[0]), body[1], body[2]
                payload, crc = body[3:-1], body[-1]
                crc_ok = _crc8_ss2(body[:-1]) == crc and dlen == len(payload)
                if cmd == "e":
                    status = payload[0] if payload else 0
                    res.add(rid, s, e, f"ack {status:02X}" + ("" if status == 0 else " (error)") + ("" if crc_ok else " CRC error"), "ack" if status == 0 and crc_ok else "nack", {"cmd": cmd, "status": status, "crc_ok": crc_ok})
                else:
                    kind = ("cmd" if key == "rx" else "data") if crc_ok else "error"
                    res.add(rid, s, e, f"{cmd} 0x{scmd:02X} {payload.hex(' ').upper()}  [{SS_CMDS.get(cmd, 'command')}, {len(payload)} bytes{'' if crc_ok else ', CRC error'}]", kind, {"cmd": cmd, "scmd": scmd, "payload": payload.hex(), "crc_ok": crc_ok, "version": 2})
        res.meta[f"{key}_messages"] = len(msgs)
        res.meta[f"{key}_version"] = v
    if not any_ch:
        raise DecodeError("choose the target RX or TX channel")
    return res.finalize()


GLITCH_OPT = {"id": "glitch_ns", "label": "Glitch filter (ns)", "type": "number", "default": 0, "min": 0, "help": "ignore pulses shorter than this on the decoder's channels (0: off)"}


# ----- registry ---------------------------------------------------------------------------------------------
_UART_OPTS = [
    {"id": "baud", "label": "Baud", "type": "text", "default": "auto", "help": "a number or auto (detected from the shortest pulses)"},
    {"id": "data_bits", "label": "Data bits", "type": "select", "values": [5, 6, 7, 8, 9], "default": 8},
    {"id": "parity", "label": "Parity", "type": "select", "values": ["none", "odd", "even", "mark", "space"], "default": "none"},
    {"id": "stop_bits", "label": "Stop bits", "type": "select", "values": [1, 1.5, 2], "default": 1},
    {"id": "bit_order", "label": "Bit order", "type": "select", "values": ["lsb", "msb"], "default": "lsb"},
    {"id": "inverted", "label": "Inverted", "type": "bool", "default": False},
]
DECODERS: Dict[str, Dict[str, Any]] = {
    "uart": {"label": "UART", "fn": decode_uart, "channels": [{"id": "rx", "label": "RX", "required": False, "hint": ["uart rx", "rx", "io2"]}, {"id": "tx", "label": "TX", "required": False, "hint": ["uart tx", "tx", "io1"]}],
             "options": _UART_OPTS + [{"id": "format", "label": "Show as", "type": "select", "values": ["hex", "ascii", "dec", "bin"], "default": "hex"}]},
    "spi": {"label": "SPI", "fn": decode_spi, "channels": [{"id": "cs", "label": "CS", "required": False, "hint": ["spi cs", "cs", "d0"]}, {"id": "sck", "label": "SCK", "required": True, "hint": ["spi sck", "sck", "d1"]}, {"id": "mosi", "label": "MOSI", "required": False, "hint": ["spi mosi", "mosi", "d2"]}, {"id": "miso", "label": "MISO", "required": False, "hint": ["spi miso", "miso", "d3"]}],
            "options": [{"id": "mode", "label": "Mode (CPOL/CPHA)", "type": "select", "values": [0, 1, 2, 3], "default": 0}, {"id": "bit_order", "label": "Bit order", "type": "select", "values": ["msb", "lsb"], "default": "msb"},
                        {"id": "word_size", "label": "Word size", "type": "number", "default": 8, "min": 1, "max": 32}, {"id": "cs_active", "label": "CS active", "type": "select", "values": ["low", "high"], "default": "low"},
                        {"id": "format", "label": "Show as", "type": "select", "values": ["hex", "dec", "bin", "ascii"], "default": "hex"}]},
    "i2c": {"label": "I2C", "fn": decode_i2c, "channels": [{"id": "scl", "label": "SCL", "required": True, "hint": ["i2c scl", "scl", "d4"]}, {"id": "sda", "label": "SDA", "required": True, "hint": ["i2c sda", "sda", "d5"]}],
            "options": [{"id": "address_format", "label": "Address", "type": "select", "values": ["7-bit", "8-bit"], "default": "7-bit", "help": "10-bit addresses are recognised from their 11110 prefix"}]},
    "onewire": {"label": "1-Wire", "fn": decode_onewire, "channels": [{"id": "owr", "label": "Data", "required": True, "hint": ["1-wire", "onewire", "owr", "io3", "d6"]}], "options": []},
    "jtag": {"label": "JTAG", "fn": decode_jtag, "channels": [{"id": "tck", "label": "TCK", "required": True, "hint": ["jtag tck", "tck"]}, {"id": "tms", "label": "TMS", "required": True, "hint": ["jtag tms", "tms"]}, {"id": "tdi", "label": "TDI", "required": True, "hint": ["jtag tdi", "tdi"]}, {"id": "tdo", "label": "TDO", "required": False, "hint": ["jtag tdo", "tdo"]}],
             "options": [{"id": "initial_state", "label": "Start state", "type": "select", "values": list(TAP), "default": "Test-Logic-Reset", "help": "five clocks with TMS high always reach Test-Logic-Reset"}]},
    "swd": {"label": "SWD", "fn": decode_swd, "channels": [{"id": "swclk", "label": "SWCLK", "required": True, "hint": ["swclk", "swd clk", "ck"]}, {"id": "swdio", "label": "SWDIO", "required": True, "hint": ["swdio", "swd io", "d7"]}], "options": []},
    "can": {"label": "CAN", "fn": decode_can, "channels": [{"id": "can", "label": "CAN RX", "required": True, "hint": ["can"]}],
            "options": [{"id": "bitrate", "label": "Bit rate", "type": "text", "default": "auto", "help": "a number or auto. Classic CAN only: CAN FD frames are marked, not decoded; with CAN FD on the bus set the nominal rate, since auto would find the faster data phase"}, {"id": "sample_point", "label": "Sample point %", "type": "number", "default": 70, "min": 30, "max": 95}]},
    "simpleserial": {"label": "SimpleSerial", "fn": decode_simpleserial, "channels": [{"id": "rx", "label": "Target RX", "required": False, "hint": ["uart rx", "rx", "io2"]}, {"id": "tx", "label": "Target TX", "required": False, "hint": ["uart tx", "tx", "io1"]}],
                     "options": [{"id": "version", "label": "Version", "type": "select", "values": ["auto", "1", "2"], "default": "auto"}] + _UART_OPTS},
}


for _spec in DECODERS.values():
    _spec["options"].append(dict(GLITCH_OPT))


def registry() -> Dict[str, Dict[str, Any]]:
    """The decoders with their channels and options, without the functions (for the API and the UI)."""
    return {k: {kk: vv for kk, vv in v.items() if kk != "fn"} for k, v in DECODERS.items()}


def guess_channels(kind: str, cap: LogicCapture) -> Dict[str, int]:
    """A best guess of the channel mapping from channel names (``UART TX`` for tx and so on)."""
    spec = DECODERS[kind]
    names = [c.name.lower() for c in cap.channels]
    out = {}
    for ch in spec["channels"]:
        for h in ch.get("hint", []):
            hit = next((i for i, n in enumerate(names) if n == h), None)
            if hit is None:
                hit = next((i for i, n in enumerate(names) if h in n and i not in out.values()), None)
            if hit is not None and hit not in out.values():
                out[ch["id"]] = hit
                break
    return out


def deglitch(c: Channel, min_samples: int) -> Channel:
    """A copy of a channel without pulses shorter than ``min_samples`` (both edges of each short pulse are dropped, so the levels around it are unchanged)."""
    e = c.edges
    if min_samples <= 1 or e.shape[0] < 2:
        return c
    short = np.flatnonzero(np.diff(e.astype(np.int64)) < min_samples)
    if short.shape[0] == 0:
        return c
    keep = np.ones(e.shape[0], bool)
    last = -1
    for i in short.tolist():
        if i <= last:
            continue
        keep[i] = keep[i + 1] = False
        last = i + 1
    return Channel(c.name, c.init, e[keep], c.color, c.hidden, c.group)


def _filtered(cap: LogicCapture, chans: Dict[str, Any], glitch_ns: float) -> LogicCapture:
    """The capture as the decoder sees it with the glitch filter: the mapped channels without pulses shorter than ``glitch_ns``."""
    w = int(np.ceil(float(glitch_ns) * 1e-9 * cap.samplerate))
    if w <= 1:
        return cap
    out = copy.copy(cap)
    out.channels = list(cap.channels)
    for v in chans.values():
        if v is None or v == "" or v == -1:
            continue
        try:
            ci = cap.ch_index(v)
        except KeyError:
            continue
        out.channels[ci] = deglitch(cap.channels[ci], w)
    return out


def decode(cap: LogicCapture, kind: str, chans: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Result:
    spec = DECODERS.get(kind)
    if spec is None:
        raise DecodeError(f"unknown decoder {kind!r}; choose one of {', '.join(DECODERS)}")
    o = {x["id"]: x["default"] for x in spec["options"]}
    o.update({k: v for k, v in (options or {}).items() if v is not None})
    chans = dict(chans or {})
    try:
        g = float(o.get("glitch_ns") or 0)
    except (TypeError, ValueError):
        raise DecodeError("the glitch filter is a time in ns") from None
    if g < 0:
        raise DecodeError("the glitch filter is a time in ns")
    if g > 0:
        cap = _filtered(cap, chans, g)
    return spec["fn"](cap, chans, o)
