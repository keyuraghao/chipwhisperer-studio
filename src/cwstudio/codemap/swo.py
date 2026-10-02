"""Exact mode on the ChipWhisperer-Husky: real program counter samples from the target's Arm trace port (SWO), recorded by the Husky's TraceWhisperer.

The target's DWT samples the program counter every N cycles and sends each sample as a 5-byte hardware source packet (header 0x17, then the 32-bit PC) over SWO; the Husky timestamps every received byte in target clock cycles from the trigger. This module configures that capture through the chipwhisperer library (``scope.trace``), decodes the ITM/DWT packet stream from the raw capture FIFO and compares the samples with the emulated timeline (how many samples land in the function the emulation predicts, and the cycle scale between emulated and real execution).

It needs Arm firmware with ChipWhisperer's ``simpleserial-trace`` command set (``set_pcsample_params``, command 'c') and the SWO pin wired to the Husky (TDO to USERIO D2 through the 20-pin connector). :class:`FakeTraceWhisperer` produces the same raw FIFO data from an emulated run, so the whole path runs (and is tested) on the simulator; only real hardware shows the real timing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

CMD_DATA, CMD_STAT, CMD_TIME, CMD_STRM = 0, 1, 2, 3
PC_HEADER = 0x17  # DWT hardware source packet, discriminator 2 (periodic PC sample), 4-byte payload
PC_SLEEP = 0x15  # PC sample while sleeping, 1-byte payload


def raw_bytes(raw: Sequence[Sequence[int]], stat: int = CMD_STAT, time_cmd: int = CMD_TIME) -> List[Tuple[int, int]]:
    """(time, byte) for every trace byte in a TraceWhisperer raw capture (``read_capture_data()`` entries of 3 bytes)."""
    t = 0
    out = []
    for e in raw:
        cmd = e[2] & 3
        if cmd == stat:
            t += e[0]
            out.append((t, e[1]))
        elif cmd == time_cmd:
            t += e[0] + (e[1] << 8)
    return out


def decode_pc_samples(stream: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Decode an ITM/DWT packet stream ((time, byte) pairs) into (time of the packet, PC) for every periodic PC sample."""
    out = []
    i, n = 0, len(stream)
    while i < n:
        t, h = stream[i]
        if h == 0x00 or h == 0x80 or h == 0x70:  # synchronisation bytes, overflow
            i += 1
            continue
        if h & 3:  # source packet with a 1, 2 or 4 byte payload
            size = {1: 1, 2: 2, 3: 4}[h & 3]
            if h == PC_HEADER and i + 4 < n:
                b = [stream[i + k][1] for k in range(1, 5)]
                out.append((t, b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)))
            i += 1 + size
            continue
        # timestamp or extension packet: header, then payload bytes while the continuation bit is set
        i += 1
        if h & 0x80:
            while i < n and stream[i][1] & 0x80:
                i += 1
            i += 1
    return out


def compare(samples: Sequence[Tuple[int, int]], timeline) -> Dict[str, Any]:
    """How well real PC samples (cycle from the trigger, PC) agree with the emulated timeline: fitted cycle scale and offset (from PCs that ran exactly once), and the share of samples whose function matches the emulation at the fitted cycle."""
    prog = timeline.prog
    if not samples:
        return {"samples": 0, "agreement": None, "scale": None, "offset": None}
    pcs = timeline.run.pcs
    start = timeline.start
    cyc = np.array([s[0] for s in samples], np.float64)
    pc = np.array([s[1] & ~1 for s in samples], np.int64)
    uniq, counts = np.unique(pcs, return_counts=True)
    once = set(uniq[counts == 1].tolist())
    pairs = [(c, float(start[int(np.flatnonzero(pcs == p)[0])])) for c, p in zip(cyc, pc) if int(p) in once]
    if len(pairs) >= 2:
        a = np.array(pairs)
        k, b = np.polyfit(a[:, 0], a[:, 1], 1)
    elif len(pairs) == 1:
        k, b = 1.0, pairs[0][1] - pairs[0][0]
    else:
        k, b = 1.0, 0.0
    lk = prog.lookup(pc)
    hits = 0
    rows = []
    for j in range(len(pc)):
        emu = timeline.at(cyc[j] * k + b)
        fn = prog.functions[int(lk["func"][j])].name if lk["func"][j] >= 0 else None
        ok = emu is not None and emu["func"] == fn
        hits += ok
        rows.append({"cycle": float(cyc[j]), "pc": int(pc[j]), "func": fn, "file": prog.short_label(int(lk["file"][j])) if lk["file"][j] >= 0 else None, "line": int(lk["line"][j]), "emulated_func": emu["func"] if emu else None, "match": bool(ok)})
    return {"samples": len(pc), "agreement": round(hits / len(pc), 4), "scale": float(k), "offset": float(b), "rows": rows[:2000]}


class HuskyPCTrace:
    """Record periodic PC samples with a Husky (``scope.trace``) while the target runs one command."""

    def __init__(self, scope, target, swo_div: int = 8, acpr: int = 0, cyctap: int = 0, postinit: int = 1):
        self.scope, self.target = scope, target
        self.swo_div, self.acpr, self.cyctap, self.postinit = swo_div, acpr, cyctap, postinit

    def setup(self) -> None:
        tr = self.scope.trace
        tr.target = self.target
        try:
            self.scope.userio.mode = "trace"
        except Exception:  # noqa: BLE001
            pass
        tr.clock.fe_clock_src = "target_clock"
        tr.set_trace_mode("swo", swo_div=self.swo_div, acpr=self.acpr)
        tr.jtag_to_swd()
        tr.capture.trigger_source = "firmware trigger"
        tr.capture.mode = "while_trig"
        tr.capture.raw = True
        tr.capture.use_husky_arm = True
        tr.set_periodic_pc_sampling(enable=1, cyctap=self.cyctap, postinit=self.postinit)

    def capture(self, key: bytes, pt: bytes) -> Dict[str, Any]:
        import chipwhisperer as cw
        trace = cw.capture_trace(self.scope, self.target, bytearray(pt), bytearray(key))
        raw = self.scope.trace.read_capture_data()
        tw = self.scope.trace
        stream = raw_bytes(raw, getattr(tw, "FE_FIFO_CMD_STAT", CMD_STAT), getattr(tw, "FE_FIFO_CMD_TIME", CMD_TIME))
        return {"samples": decode_pc_samples(stream), "bytes": len(stream), "textout": bytes(trace.textout).hex() if trace is not None and trace.textout else None}


class FakeTraceWhisperer:
    """Stands in for ``scope.trace`` on the simulator: turns the emulated run of the last command into the raw FIFO entries a Husky would record (PC sample packets every ``interval`` cycles, each byte taking ``byte_cycles`` on the SWO line)."""

    FE_FIFO_CMD_STAT, FE_FIFO_CMD_TIME = CMD_STAT, CMD_TIME

    def __init__(self, run, interval: int = 64, byte_cycles: int = 10):
        self.run, self.interval, self.byte_cycles = run, interval, byte_cycles

    def read_capture_data(self) -> List[List[int]]:
        run = self.run
        hi, lo = run.trigger_window()
        t0 = int(hi or 0)
        end = int(lo if lo is not None else run.total_cycles)
        out: List[List[int]] = []
        last = 0
        busy_until = 0
        for c in range(t0, end, self.interval):
            i = int(np.searchsorted(run.start, c, side="right")) - 1
            if i < 0:
                continue
            pc = int(run.pcs[i])
            t = max(c - t0, busy_until)
            for k, b in enumerate([PC_HEADER, pc & 0xFF, (pc >> 8) & 0xFF, (pc >> 16) & 0xFF, (pc >> 24) & 0xFF]):
                tb = t + k * self.byte_cycles
                d = tb - last
                while d > 255:
                    step = min(d - 255, 0xFFFF)
                    out.append([step & 0xFF, step >> 8, CMD_TIME])
                    d -= step
                out.append([d, b, CMD_STAT])
                last = tb
            busy_until = t + 5 * self.byte_cycles
        return out
