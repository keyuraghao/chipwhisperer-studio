"""Logic capture files: VCD, CSV (Saleae Logic and generic) and sigrok ``.sr`` sessions, in both directions.

* VCD: 1-bit wires and vectors (split into one channel per bit). The sample rate comes from the timescale and the greatest common divisor of the timestamps, or from the ``$comment cwstudio samplerate=... trigger=...`` line Studio writes.
* CSV: a header row, then either a time column in seconds (Saleae ``Time [s]``, or ``ms``/``us``/``ns`` units in the header) or a sample index column (``Sample``/``index``), then one 0/1 column per channel. Rows may be every sample or only changes. Files without a time column are one row per sample.
* ``.sr``: a zip with ``version``, an INI ``metadata`` file (samplerate, probe names, unitsize) and ``logic-1-N`` chunks of packed samples.
"""
from __future__ import annotations

import configparser
import csv
import io
import math
import os
import re
import zipfile
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from cwstudio.logic.model import Channel, LogicCapture, edge_dtype

FORMATS = {"vcd": "Value Change Dump (.vcd)", "csv": "CSV (.csv, Saleae Logic style)", "sr": "sigrok session (.sr)"}
_UNITS = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15}


def detect_format(path: str, head: Optional[bytes] = None) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in ("vcd", "csv", "sr"):
        return ext
    if head is None:
        with open(path, "rb") as f:
            head = f.read(512)
    if head.startswith(b"PK"):
        return "sr"
    if b"$" in head[:200] and (b"$timescale" in head or b"$var" in head or b"$date" in head or b"$version" in head):
        return "vcd"
    return "csv"


def load(path: str, fmt: Optional[str] = None, samplerate: Optional[float] = None) -> LogicCapture:
    fmt = (fmt or detect_format(path)).lower().lstrip(".")
    if fmt == "vcd":
        cap = read_vcd(path)
    elif fmt == "csv":
        cap = read_csv(path, samplerate=samplerate)
    elif fmt == "sr":
        cap = read_sr(path)
    else:
        raise ValueError(f"unknown logic file format {fmt!r}; use vcd, csv or sr")
    cap.name = os.path.basename(path)
    cap.meta.setdefault("file", path)
    return cap


def save(cap: LogicCapture, path: str, fmt: Optional[str] = None, channels: Optional[Sequence[int]] = None) -> str:
    fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "vcd").lower()
    if not path.lower().endswith("." + fmt):
        path += "." + fmt
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    {"vcd": write_vcd, "csv": write_csv, "sr": write_sr}[fmt](cap, path, channels)
    return path


def _from_changes(names: List[str], pos: np.ndarray, vals: np.ndarray, n: int, samplerate: float, trigger: int, source: str) -> LogicCapture:
    """Channels from rows of (sample position, values per channel); rows are sorted, positions may repeat (last wins)."""
    order = np.argsort(pos, kind="stable")
    pos, vals = pos[order], vals[order]
    last = np.ones(pos.shape[0], bool)
    last[:-1] = pos[1:] != pos[:-1]
    pos, vals = pos[last], vals[last]
    chans = []
    dt = edge_dtype(n)
    for k, name in enumerate(names):
        v = vals[:, k].astype(np.int8)
        if v.shape[0] == 0:
            chans.append(Channel(name, 0, np.zeros(0, dt)))
            continue
        ch = np.flatnonzero(v[1:] != v[:-1]) + 1
        chans.append(Channel(name, int(v[0]), pos[ch].astype(dt)))
    return LogicCapture(chans, n, samplerate, trigger, source=source)


# ----- VCD ---------------------------------------------------------------------------------------------------
def read_vcd(path: str) -> LogicCapture:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    hdr_end = text.find("$enddefinitions")
    if hdr_end < 0:
        raise ValueError("not a VCD file: no $enddefinitions")
    header = text[:hdr_end]
    body_start = text.find("$end", hdr_end) + 4
    body = text[body_start:]
    m = re.search(r"\$timescale\s+(\d+)\s*([a-zµ]+)\s+\$end", header)
    ts = 1e-9
    if m:
        ts = int(m.group(1)) * _UNITS.get(m.group(2), 1e-9)
    sr_c, trig_c, n_c = None, None, None
    for cm in re.finditer(r"\$comment(.*?)\$end", header, re.S):
        ms = re.search(r"cwstudio\s+samplerate=([\d.eE+-]+)(?:\s+trigger=(-?\d+))?(?:\s+samples=(\d+))?", cm.group(1))
        if ms:
            sr_c = float(ms.group(1))
            trig_c = int(ms.group(2)) if ms.group(2) else 0
            n_c = int(ms.group(3)) if ms.group(3) else None
    vars_: List[Tuple[str, int, str]] = []  # (id, width, name)
    for vm in re.finditer(r"\$var\s+\S+\s+(\d+)\s+(\S+)\s+(.+?)\s+\$end", header):
        width, ident, name = int(vm.group(1)), vm.group(2), vm.group(3).strip()
        name = re.sub(r"\s*\[\d+(:\d+)?\]\s*$", "", name)
        vars_.append((ident, width, name))
    if not vars_:
        raise ValueError("the VCD file declares no variables")
    # one channel per bit, vectors MSB first
    chan_of: Dict[str, List[int]] = {}
    names: List[str] = []
    for ident, width, name in vars_:
        if ident in chan_of:
            continue
        if width == 1:
            chan_of[ident] = [len(names)]
            names.append(name)
        else:
            idxs = []
            for b in range(width - 1, -1, -1):
                idxs.append(len(names))
                names.append(f"{name}[{b}]")
            chan_of[ident] = idxs
    times: List[int] = []
    ch_ev: List[List[Tuple[int, int]]] = [[] for _ in names]
    t = 0
    tok = body.split()
    i = 0
    nt = len(tok)
    while i < nt:
        w = tok[i]
        c0 = w[0]
        if c0 == "#":
            t = int(w[1:])
            times.append(t)
        elif c0 in "01xXzZ" and len(w) > 1:
            ids = chan_of.get(w[1:])
            if ids:
                ch_ev[ids[0]].append((t, 1 if c0 == "1" else 0))
        elif c0 in "bB":
            val = w[1:]
            i += 1
            ids = chan_of.get(tok[i]) if i < nt else None
            if ids:
                val = val.rjust(len(ids), "0" if val[0] in "01" else val[0])[-len(ids):]
                for k, ci in enumerate(ids):
                    ch_ev[ci].append((t, 1 if val[k] == "1" else 0))
        elif c0 in "rR":
            i += 1
        i += 1
    if not times:
        times = [0]
    tarr = np.asarray(times, np.int64)
    t0, t1 = int(tarr.min()), int(tarr.max())
    if sr_c:
        sr = sr_c
        to_idx = lambda tt: np.round((np.asarray(tt, np.float64) - t0) * ts * sr).astype(np.int64)  # noqa: E731
        trigger = int(trig_c or 0)
        n = n_c or int(to_idx([t1])[0]) + 1
    else:
        rel = np.unique(tarr - t0)
        unit = int(np.gcd.reduce(rel[rel > 0])) if (rel > 0).any() else 1
        sr = 1.0 / (ts * unit)
        to_idx = lambda tt: (np.asarray(tt, np.int64) - t0) // unit  # noqa: E731
        trigger = int(-t0 // unit) if t0 <= 0 else 0
        n = int((t1 - t0) // unit) + 1
    n = max(n, 1)
    chans = []
    dt = edge_dtype(n)
    for ci, name in enumerate(names):
        ev = ch_ev[ci]
        if not ev:
            chans.append(Channel(name, 0, np.zeros(0, dt)))
            continue
        tt = np.fromiter((e[0] for e in ev), np.int64, len(ev))
        vv = np.fromiter((e[1] for e in ev), np.int8, len(ev))
        pos = to_idx(tt)
        last = np.ones(pos.shape[0], bool)
        last[:-1] = pos[1:] != pos[:-1]
        pos, vv = pos[last], vv[last]
        init = int(vv[0]) if pos[0] <= 0 else 0
        prev = np.concatenate(([init], vv[:-1]))
        keep = (vv != prev) & (pos > 0)
        chans.append(Channel(name, init, pos[keep].astype(dt)))
    return LogicCapture(chans, n, sr, trigger, source="file")


def _vcd_ids(n: int) -> List[str]:
    out = []
    for i in range(n):
        s, k = "", i
        while True:
            s += chr(33 + k % 94)
            k //= 94
            if k == 0:
                break
        out.append(s)
    return out


def _timescale(sr: float) -> Tuple[str, int, float]:
    """A VCD timescale for a sample period: ("10 ns", time units per sample, seconds per unit), exact when the period is a whole number of some unit."""
    period = Fraction(1) / Fraction(sr).limit_denominator(10 ** 9)
    for exp, name in ((0, "s"), (3, "ms"), (6, "us"), (9, "ns"), (12, "ps"), (15, "fs")):
        for mult in (100, 10, 1):
            unit = Fraction(mult, 10 ** exp)
            q = period / unit
            if q.denominator == 1 and q.numerator >= 1:
                return f"{mult} {name}", int(q.numerator), float(unit)
    return "1 fs", max(1, int(round(float(period) * 1e15))), 1e-15


def write_vcd(cap: LogicCapture, path: str, channels: Optional[Sequence[int]] = None) -> None:
    chans = list(range(len(cap.channels))) if channels is None else list(channels)
    ids = _vcd_ids(len(chans))
    tsname, mult, _ = _timescale(cap.samplerate)
    lines = ["$date", "  " + __import__("time").strftime("%Y-%m-%d %H:%M:%S"), "$end", "$version ChipWhisperer Studio $end",
             f"$comment cwstudio samplerate={cap.samplerate!r} trigger={cap.trigger} samples={cap.n} $end", f"$timescale {tsname} $end", "$scope module logic $end"]
    for ident, ci in zip(ids, chans):
        name = re.sub(r"\s+", "_", cap.channels[ci].name) or f"ch{ci}"
        lines.append(f"$var wire 1 {ident} {name} $end")
    lines += ["$upscope $end", "$enddefinitions $end", "#0", "$dumpvars"]
    for ident, ci in zip(ids, chans):
        lines.append(f"{cap.channels[ci].init}{ident}")
    lines.append("$end")
    pos_l, ch_l, val_l = [], [], []
    for k, ci in enumerate(chans):
        e = cap.channels[ci].edges.astype(np.int64)
        pos_l.append(e)
        ch_l.append(np.full(e.shape[0], k, np.int32))
        val_l.append((cap.channels[ci].init ^ ((np.arange(e.shape[0]) + 1) & 1)).astype(np.int8))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
        if pos_l:
            pos = np.concatenate(pos_l)
            chs = np.concatenate(ch_l)
            vals = np.concatenate(val_l)
            order = np.argsort(pos, kind="stable")
            pos, chs, vals = pos[order], chs[order], vals[order]
            out: List[str] = []
            last = -1
            for p, c, v in zip(pos.tolist(), chs.tolist(), vals.tolist()):
                if p != last:
                    out.append(f"#{p * mult}")
                    last = p
                out.append(f"{v}{ids[c]}")
                if len(out) > 200000:
                    f.write("\n".join(out) + "\n")
                    out = []
            if out:
                f.write("\n".join(out) + "\n")
        if cap.n > 1:
            f.write(f"#{(cap.n - 1) * mult}\n")


# ----- CSV ---------------------------------------------------------------------------------------------------
def _num(s: str) -> float:
    return float(s.strip())


def read_csv(path: str, samplerate: Optional[float] = None) -> LogicCapture:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(f, dialect) if r and any(x.strip() for x in r)]
    if not rows:
        raise ValueError("the CSV file is empty")
    def is_num(x):
        try:
            float(x)
            return True
        except ValueError:
            return False
    header = None
    if not all(is_num(x) for x in rows[0]):
        header = [x.strip() for x in rows[0]]
        rows = rows[1:]
    if not rows:
        raise ValueError("the CSV file has a header but no data")
    data = np.asarray([[float(x) if x.strip() else 0.0 for x in r] for r in rows], np.float64)
    ncol = data.shape[1]
    first = (header[0].lower() if header else "")
    kind = None  # "time", "index" or "dense"
    scale = 1.0
    if header:
        if re.match(r"^(time|t|timestamp|seconds)\b", first) or "time" in first:
            kind = "time"
            um = re.search(r"[\[(]\s*(s|ms|us|µs|ns|ps)\s*[\])]", header[0], re.I)
            if um:
                scale = _UNITS.get(um.group(1).lower(), 1.0)
        elif re.match(r"^(sample|samples|index|idx|n|sample number)\b", first):
            kind = "index"
        else:
            kind = "dense" if set(np.unique(data[:, 0])) <= {0.0, 1.0} else "time"
    else:
        kind = "dense" if set(np.unique(data)) <= {0.0, 1.0} else "time"
    if kind == "dense":
        names = header if header else [f"D{i}" for i in range(ncol)]
        pos = np.arange(data.shape[0], dtype=np.int64)
        vals = data
        sr = float(samplerate or 1.0)
        return _from_changes(names, pos, (vals != 0).astype(np.int8), data.shape[0], sr, 0, "file")
    names = header[1:] if header else [f"D{i}" for i in range(ncol - 1)]
    vals = (data[:, 1:] != 0).astype(np.int8)
    col = data[:, 0]
    if kind == "index":
        pos = np.round(col).astype(np.int64)
        p0 = int(pos.min())
        sr = float(samplerate or 1.0)
        trigger = -p0 if p0 < 0 else 0
        pos = pos - min(p0, 0)
        n = int(pos.max()) + 1
        return _from_changes(names, pos, vals, n, sr, trigger, "file")
    tsec = col * scale
    t0 = float(tsec.min())
    if samplerate:
        sr = float(samplerate)
    else:
        sr = _estimate_rate(tsec - t0)
    pos = np.round((tsec - t0) * sr).astype(np.int64)
    trigger = int(round(-t0 * sr))
    n = int(pos.max()) + 1
    return _from_changes(names, pos, vals, n, sr, trigger, "file")


def _estimate_rate(rel: np.ndarray) -> float:
    """Sample rate from change times: the smallest step that every time is a whole multiple of."""
    d = np.diff(np.unique(rel))
    d = d[d > 0]
    if d.shape[0] == 0:
        return 1.0
    m = float(d.min())
    for k in range(1, 17):
        step = m / k
        ok = True
        # refine the step on a growing prefix so rounding in the printed times does not add up over long captures
        for limit in (1e3, 1e5, None):
            part = rel if limit is None else rel[rel <= limit * step]
            q = np.round(part / step)
            if np.any(np.abs(part / step - q) > 0.1):
                ok = False
                break
            if q.any():
                step = float(part @ q / (q @ q))
        if ok and np.all(np.abs(rel / step - np.round(rel / step)) < 0.05):
            return float(f"{1.0 / step:.10g}")
    ps = np.round(rel * 1e12).astype(np.int64)
    g = int(np.gcd.reduce(ps[ps > 0])) if (ps > 0).any() else 1
    return 1e12 / max(g, 1)


def write_csv(cap: LogicCapture, path: str, channels: Optional[Sequence[int]] = None) -> None:
    """Saleae Logic style: ``Time [s]`` then one column per channel, one row at the start, one per change and one at the last sample."""
    chans = list(range(len(cap.channels))) if channels is None else list(channels)
    pos = np.unique(np.concatenate([cap.channels[ci].edges.astype(np.int64) for ci in chans] + [np.array([0, max(cap.n - 1, 0)], np.int64)]))
    cols = [cap.channels[ci].value_at(pos) for ci in chans]
    t = (pos - cap.trigger) / cap.samplerate
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("Time [s]," + ",".join(cap.channels[ci].name.replace(",", " ") for ci in chans) + "\n")
        mat = np.stack(cols, 1) if cols else np.zeros((pos.shape[0], 0), np.int8)
        chunk = 100000
        for s in range(0, pos.shape[0], chunk):
            buf = io.StringIO()
            for tt, row in zip(t[s:s + chunk].tolist(), mat[s:s + chunk].tolist()):
                buf.write(f"{tt:.12g}," + ",".join(map(str, row)) + "\n")
            f.write(buf.getvalue())


# ----- sigrok .sr ----------------------------------------------------------------------------------------------
def parse_rate(s: str) -> float:
    m = re.match(r"^\s*([\d.]+)\s*([kKMG]?)\s*(Hz)?\s*$", str(s))
    if not m:
        raise ValueError(f"cannot read the sample rate {s!r}")
    return float(m.group(1)) * {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]


def format_rate(sr: float) -> str:
    v = int(round(sr))
    for div, unit in ((10 ** 9, "GHz"), (10 ** 6, "MHz"), (10 ** 3, "kHz")):
        if v >= div and v % div == 0:
            return f"{v // div} {unit}"
    return f"{v} Hz"


def read_sr(path: str) -> LogicCapture:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "metadata" not in names:
            raise ValueError("not a sigrok session: no metadata file")
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read_string(z.read("metadata").decode("utf-8", "replace"))
        dev = next((s for s in cp.sections() if s.startswith("device")), None)
        if dev is None:
            raise ValueError("the sigrok session has no device section")
        d = cp[dev]
        total = int(d.get("total probes", "0"))
        unitsize = int(d.get("unitsize", str(max(1, (total + 7) // 8))))
        sr = parse_rate(d.get("samplerate", "1 Hz"))
        prefix = d.get("capturefile", "logic-1")
        probes = [d.get(f"probe{i}", None) for i in range(1, total + 1)]
        trigger = 0
        if "cwstudio" in cp.sections():
            trigger = int(cp["cwstudio"].get("trigger", "0"))
            sr = float(cp["cwstudio"].get("samplerate", sr))
        chunks = sorted((n for n in names if n == prefix or re.fullmatch(re.escape(prefix) + r"-\d+", n)), key=lambda n: int(n.rsplit("-", 1)[1]) if n != prefix else 0)
        live = [(i, p) for i, p in enumerate(probes) if p]
        edges: Dict[int, List[np.ndarray]] = {i: [] for i, _ in live}
        last: Dict[int, int] = {}
        init: Dict[int, int] = {}
        off = 0
        carry = b""
        for cn in chunks:
            raw = carry + z.read(cn)
            usable = len(raw) - len(raw) % unitsize
            carry = raw[usable:]
            if usable == 0:
                continue
            arr = np.frombuffer(raw[:usable], np.uint8).reshape(-1, unitsize)
            for i, _ in live:
                bits = (arr[:, i // 8] >> (i % 8)) & 1
                if i not in init:
                    init[i] = int(bits[0])
                    last[i] = int(bits[0])
                ch = np.flatnonzero(bits[1:] != bits[:-1]) + 1
                if bits[0] != last[i]:
                    ch = np.concatenate(([0], ch))
                edges[i].append(ch.astype(np.int64) + off)
                last[i] = int(bits[-1])
            off += arr.shape[0]
    n = max(off, 1)
    dt = edge_dtype(n)
    chans = [Channel(p, init.get(i, 0), (np.concatenate(edges[i]) if edges[i] else np.zeros(0, np.int64)).astype(dt)) for i, p in live]
    return LogicCapture(chans, n, sr, trigger, source="file")


def write_sr(cap: LogicCapture, path: str, channels: Optional[Sequence[int]] = None, chunk: int = 1 << 20) -> None:
    chans = list(range(len(cap.channels))) if channels is None else list(channels)
    if cap.n > 2_000_000_000:
        raise ValueError("this capture is too long for a .sr file")
    if len(chans) > 64:
        raise ValueError("a .sr file holds at most 64 channels here")
    unitsize = max(1, (len(chans) + 7) // 8)
    meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1", f"total probes={len(chans)}", f"samplerate={format_rate(cap.samplerate)}", "total analog=0"]
    for k, ci in enumerate(chans):
        name = re.sub(r"[=\n\r]", "_", cap.channels[ci].name)
        meta.append(f"probe{k + 1}={name}")
    meta.append(f"unitsize={unitsize}")
    meta += ["", "[cwstudio]", f"trigger={cap.trigger}", f"samplerate={cap.samplerate!r}", ""]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version", "2")
        z.writestr("metadata", "\n".join(meta))
        part = 1
        for s in range(0, cap.n, chunk):
            e = min(cap.n, s + chunk)
            words = cap.packed(s, e, chans).astype(np.uint64)
            buf = np.zeros((e - s, unitsize), np.uint8)
            for b in range(unitsize):
                buf[:, b] = ((words >> np.uint64(8 * b)) & np.uint64(0xFF)).astype(np.uint8)
            z.writestr(f"logic-1-{part}", buf.tobytes())
            part += 1


# ----- decoded results --------------------------------------------------------------------------------------------
def annotations_csv(rows: List[Dict[str, Any]], path_or_buf) -> None:
    """Decoded annotations as CSV: start/end in seconds and samples, decoder, row, channel, kind, text, value."""
    import json as _json
    own = isinstance(path_or_buf, str)
    f = open(path_or_buf, "w", encoding="utf-8", newline="") if own else path_or_buf
    try:
        w = csv.writer(f)
        w.writerow(["start_s", "end_s", "start_sample", "end_sample", "decoder", "row", "channel", "kind", "text", "value"])
        for r in rows:
            v = r.get("value")
            if isinstance(v, (dict, list)):
                v = _json.dumps(v)
            w.writerow([f"{r['t']:.12g}", f"{r['t_end']:.12g}", r["s"], r["e"], r.get("decoder", ""), r.get("label", r.get("row", "")), r.get("channel_name", ""), r["kind"], r["text"], "" if v is None else v])
    finally:
        if own:
            f.close()


def _isfinite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
