"""Turn an emulated run into a timeline of code: which function and source line ran during which clock cycles.

Cycles are relative to the trigger (the cycle ``trigger_high`` was called, or the start of the stand-in window), so cycle 0 is where a captured trace starts before its offset and pre-trigger samples are applied (see :mod:`cwstudio.codemap.power`).

* Line runs: consecutive instructions from the same function, file and line, merged.
* Spans: function activations as in a flame chart (a call opens a span one level deeper, its return closes it; a tail call or fall through into another function closes the span and opens one at the same depth), so a parent span covers its callees.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from cwstudio.codemap import cycles as cy
from cwstudio.codemap.emu import Run
from cwstudio.codemap.program import Program


@dataclass
class Timeline:
    prog: Program
    run: Run
    t0: int  # absolute cycle of the trigger
    t1: Optional[int]  # absolute cycle the trigger went low
    # per instruction (cycles relative to t0)
    start: np.ndarray
    end: np.ndarray
    func: np.ndarray
    file: np.ndarray
    line: np.ndarray
    inline: np.ndarray
    # line runs
    lr_start: np.ndarray
    lr_end: np.ndarray
    lr_first: np.ndarray  # index of the run's first instruction
    lr_count: np.ndarray
    # spans
    sp_start: np.ndarray
    sp_end: np.ndarray
    sp_depth: np.ndarray
    sp_func: np.ndarray

    @classmethod
    def build(cls, prog: Program, run: Run) -> "Timeline":
        hi, lo = run.trigger_window()
        t0 = int(hi) if hi is not None else 0
        t1 = int(lo) if lo is not None else None
        n = len(run.pcs)
        lk = prog.lookup(run.pcs) if n else {k: np.zeros(0, np.int32) for k in ("func", "file", "line", "inline", "outer")}
        start = run.start - t0
        end = start + run.dur
        func, file, line, inline = lk["func"], lk["file"], lk["line"], lk["inline"]
        # line runs
        if n:
            key_change = np.ones(n, bool)
            key_change[1:] = (func[1:] != func[:-1]) | (file[1:] != file[:-1]) | (line[1:] != line[:-1]) | (inline[1:] != inline[:-1])
            first = np.flatnonzero(key_change)
            last = np.append(first[1:] - 1, n - 1)
            lr_start, lr_end = start[first], end[last]
            lr_count = last - first + 1
        else:
            first = lr_start = lr_end = lr_count = np.zeros(0, np.int64)
        sp = _spans(run.cls, func, start, end)
        tl = cls(prog, run, t0, t1, start, end, func, file, line, inline, lr_start, lr_end, first, lr_count, *sp)
        tl.outer = lk["outer"]
        tl._inline_runs()
        return tl

    def _inline_runs(self) -> None:
        """Runs of instructions from inlined functions, one level below the call they were inlined into (so the band can draw them like calls)."""
        n = len(self.start)
        depth = np.zeros(n, np.int64)
        for k in np.argsort(self.sp_depth, kind="stable"):
            i0 = int(np.searchsorted(self.start, self.sp_start[k], "left"))
            i1 = int(np.searchsorted(self.start, self.sp_end[k], "left"))
            depth[i0:i1] = self.sp_depth[k]
        parts = []
        # the outermost inlined function one level below its caller, a nested one (if different) another level down
        for arr, extra in ((self.outer, 1), (np.where(self.inline != self.outer, self.inline, -1), 2)):
            if not n:
                break
            ch = np.ones(n, bool)
            ch[1:] = arr[1:] != arr[:-1]
            first = np.flatnonzero(ch)
            last = np.append(first[1:] - 1, n - 1)
            keep = arr[first] >= 0
            first, last = first[keep], last[keep]
            parts.append((self.start[first], self.end[last], arr[first], depth[first] + extra))
        if parts:
            self.in_start, self.in_end, self.in_name, self.in_depth = (np.concatenate([p[k] for p in parts]) for k in range(4))
            order = np.argsort(self.in_start, kind="stable")
            self.in_start, self.in_end, self.in_name, self.in_depth = self.in_start[order], self.in_end[order], self.in_name[order], self.in_depth[order]
        else:
            z = np.zeros(0, np.int64)
            self.in_start = self.in_end = self.in_name = self.in_depth = z

    # --- output for the UI ------------------------------------------------------------------------
    def functions_table(self) -> List[Dict[str, Any]]:
        out = []
        for i, f in enumerate(self.prog.functions):
            out.append({"i": i, "name": f.name, "file": self.prog.short_label(f.file) if f.file >= 0 else None, "file_index": f.file, "line": f.line, "lo": f.lo, "hi": f.hi})
        return out

    def to_json(self) -> Dict[str, Any]:
        used_files = sorted(set(np.unique(self.file[self.file >= 0]).tolist()))
        lf = self.file[self.lr_first]
        return {
            "t0": self.t0, "t1": None if self.t1 is None else self.t1 - self.t0, "total": int(self.end[-1]) if len(self.end) else 0,
            "start_cycle": int(self.start[0]) if len(self.start) else 0, "instructions": int(len(self.start)),
            "functions": self.functions_table(),
            "files": [{"i": i, "path": self.prog.file_label(i), "name": self.prog.short_label(i)} for i in used_files],
            "spans": {"start": self.sp_start.tolist(), "end": self.sp_end.tolist(), "depth": self.sp_depth.tolist(), "func": self.sp_func.tolist()},
            "lines": {"start": self.lr_start.tolist(), "end": self.lr_end.tolist(), "func": self.func[self.lr_first].tolist(), "file": lf.tolist(), "line": self.line[self.lr_first].tolist(),
                      "inline": self.inline[self.lr_first].tolist(), "pc": self.run.pcs[self.lr_first].tolist(), "count": self.lr_count.tolist()},
            "inlines": [{"name": n, "call_file": self.prog.short_label(f) if f >= 0 else None, "call_line": ln} for n, f, ln in self.prog.inline_info],
            "inline_runs": {"start": self.in_start.tolist(), "end": self.in_end.tolist(), "inline": self.in_name.tolist(), "depth": self.in_depth.tolist()},
        }

    # --- queries --------------------------------------------------------------------------------------
    def region(self, c0: float, c1: float, limit: int = 400) -> Dict[str, Any]:
        """Functions and source lines that executed between cycles ``c0`` and ``c1`` (relative to the trigger)."""
        if c1 < c0:
            c0, c1 = c1, c0
        m = (self.end > c0) & (self.start < c1)
        idx = np.flatnonzero(m)
        prog = self.prog
        if not len(idx):
            return {"cycles": [c0, c1], "functions": [], "lines": [], "instructions": 0}
        s = np.maximum(self.start[idx], c0)
        e = np.minimum(self.end[idx], c1)
        part = (e - s).astype(np.float64)
        funcs: Dict[int, Dict[str, Any]] = {}
        f_idx = self.func[idx]
        for fi in np.unique(f_idx).tolist():
            sel = f_idx == fi
            name = prog.functions[fi].name if fi >= 0 else "(unknown)"
            fobj = prog.functions[fi] if fi >= 0 else None
            funcs[fi] = {"func": fi, "name": name, "file": prog.short_label(fobj.file) if fobj is not None and fobj.file >= 0 else None, "file_index": fobj.file if fobj is not None else -1,
                         "line": fobj.line if fobj is not None else 0, "self_cycles": float(part[sel].sum()), "first": float(s[sel].min()), "last": float(e[sel].max()), "instructions": int(sel.sum())}
        # inclusive spans overlapping the region
        span_m = (self.sp_end > c0) & (self.sp_start < c1)
        for k in np.flatnonzero(span_m).tolist():
            fi = int(self.sp_func[k])
            d = funcs.get(fi)
            if d is None:
                fobj = prog.functions[fi] if fi >= 0 else None
                d = funcs[fi] = {"func": fi, "name": fobj.name if fobj else "(unknown)", "file": prog.short_label(fobj.file) if fobj is not None and fobj.file >= 0 else None, "file_index": fobj.file if fobj else -1,
                                 "line": fobj.line if fobj else 0, "self_cycles": 0.0, "first": float(max(self.sp_start[k], c0)), "last": float(min(self.sp_end[k], c1)), "instructions": 0}
            d.setdefault("spans", []).append([float(max(self.sp_start[k], c0)), float(min(self.sp_end[k], c1)), int(self.sp_depth[k])])
            d["depth"] = min(d.get("depth", 99), int(self.sp_depth[k]))
        # functions the compiler inlined: DWARF still knows which instructions came from them
        inl = self.inline[idx]
        out_ = self.outer[idx]
        inlined: Dict[str, Dict[str, Any]] = {}
        names_at: Dict[str, set] = {}
        for ii in set(np.unique(inl[inl >= 0]).tolist()) | set(np.unique(out_[out_ >= 0]).tolist()):
            names_at.setdefault(prog.inline_names[ii], set()).add(ii)
        for name, ids in names_at.items():
            sel = np.isin(inl, list(ids)) | np.isin(out_, list(ids))  # instructions of this function, including functions inlined into it
            d = inlined.setdefault(name, {"name": name, "cycles": 0.0, "first": float("inf"), "last": float("-inf"), "into": set()})
            d["cycles"] += float(part[sel].sum())
            d["first"] = min(d["first"], float(s[sel].min()))
            d["last"] = max(d["last"], float(e[sel].max()))
            d["into"].update(prog.functions[f].name for f in np.unique(f_idx[sel]).tolist() if f >= 0)
        total = float(c1 - c0) or 1.0
        inl_list = sorted(({**d, "into": sorted(d["into"]), "share": round(d["cycles"] / total, 4)} for d in inlined.values()), key=lambda d: d["first"])
        flist = sorted(funcs.values(), key=lambda d: (d["first"], -d["self_cycles"]))
        for d in flist:
            d["share"] = round(d["self_cycles"] / total, 4)
            d.setdefault("spans", [])
            d.setdefault("depth", 0)
        # lines
        fl = self.file[idx].astype(np.int64)
        ln = self.line[idx].astype(np.int64)
        keys = fl * 1_000_000 + ln
        lines = []
        uk, inv = np.unique(keys, return_inverse=True)
        cyc_sum = np.bincount(inv, weights=part)
        firsts = np.full(len(uk), np.inf)
        np.minimum.at(firsts, inv, s.astype(np.float64))
        lasts = np.full(len(uk), -np.inf)
        np.maximum.at(lasts, inv, e.astype(np.float64))
        counts = np.bincount(inv)
        lr_m = (self.lr_end > c0) & (self.lr_start < c1)
        lr_keys = self.file[self.lr_first[lr_m]].astype(np.int64) * 1_000_000 + self.line[self.lr_first[lr_m]].astype(np.int64)
        exec_count = {int(k): int(v) for k, v in zip(*np.unique(lr_keys, return_counts=True))}
        any_inst = np.zeros(len(uk), np.int64)
        any_inst[inv] = idx
        src_lines: Dict[int, List[str]] = {}
        for j in range(len(uk)):
            fi = int(uk[j] // 1_000_000)
            li = int(uk[j] % 1_000_000)
            i0 = int(any_inst[j])
            f = int(self.func[i0])
            text = None
            if fi >= 0 and fi not in src_lines:
                src_lines[fi] = (prog.source(fi) or "").splitlines()
            sl = src_lines.get(fi) or []
            if 0 < li <= len(sl):
                text = sl[li - 1].strip()[:200]
            inl = int(self.inline[i0])
            lines.append({"file": prog.short_label(fi) if fi >= 0 else None, "file_index": fi, "path": prog.file_label(fi) if fi >= 0 else None, "line": li, "cycles": float(cyc_sum[j]),
                          "instructions": int(counts[j]), "executions": exec_count.get(int(uk[j]), 0), "first": float(firsts[j]), "last": float(lasts[j]),
                          "func": prog.functions[f].name if f >= 0 else None, "inline": prog.inline_names[inl] if inl >= 0 else None, "text": text})
        lines.sort(key=lambda d: d["first"])
        return {"cycles": [float(c0), float(c1)], "functions": flist, "inlined": inl_list, "lines": lines[:limit], "lines_total": len(lines), "instructions": int(len(idx))}

    def function_ranges(self, name: str) -> List[Tuple[float, float, int]]:
        prog = self.prog
        ids = {i for i, f in enumerate(prog.functions) if f.name == name}
        if not ids:
            inl = {i for i, n in enumerate(prog.inline_names) if n == name}
            if not inl:
                raise KeyError(f"no function {name!r} in {prog.summary()['name']}")
            # an inlined function: the runs of instructions DWARF attributes to it (depth -1: no call of its own)
            m = np.isin(self.inline, list(inl)) | np.isin(self.outer, list(inl))
            if not m.any():
                return []
            k = np.flatnonzero(m)
            br = np.flatnonzero(np.diff(k) != 1)
            starts, ends = np.concatenate([[0], br + 1]), np.concatenate([br, [len(k) - 1]])
            return [(float(self.start[k[a]]), float(self.end[k[b]]), -1) for a, b in zip(starts, ends)]
        out = [(float(self.sp_start[k]), float(self.sp_end[k]), int(self.sp_depth[k])) for k in range(len(self.sp_func)) if int(self.sp_func[k]) in ids]
        return out

    def line_ranges(self, file_index: int, line: int) -> List[Tuple[float, float]]:
        m = (self.file[self.lr_first] == file_index) & (self.line[self.lr_first] == line)
        k = np.flatnonzero(m)
        return [(float(self.lr_start[j]), float(self.lr_end[j])) for j in k.tolist()]

    def at(self, c: float) -> Optional[Dict[str, Any]]:
        """The instruction executing at cycle ``c``."""
        i = int(np.searchsorted(self.start, c, side="right")) - 1
        if i < 0 or i >= len(self.start) or c >= self.end[i]:
            return None
        prog = self.prog
        f, fi, li = int(self.func[i]), int(self.file[i]), int(self.line[i])
        return {"pc": int(self.run.pcs[i]), "cycle": float(self.start[i]), "cycles": int(self.run.dur[i]), "class": cy.CLASS_NAMES[int(self.run.cls[i])] if 0 <= int(self.run.cls[i]) < len(cy.CLASS_NAMES) else None,
                "func": prog.functions[f].name if f >= 0 else None, "file": prog.short_label(fi) if fi >= 0 else None, "line": li}


def _spans(cls: np.ndarray, func: np.ndarray, start: np.ndarray, end: np.ndarray):
    n = len(func)
    if not n:
        z = np.zeros(0, np.int64)
        return z, z, z, z
    ev = np.zeros(n, bool)
    ev[:-1] = (cls[:-1] == cy.CALL) | (cls[:-1] == cy.RET) | (func[1:] != func[:-1])
    stack: List[List[int]] = [[int(func[0]), int(start[0]), 0]]
    out_s, out_e, out_d, out_f = [], [], [], []

    def close(fr, e):
        out_f.append(fr[0])
        out_s.append(fr[1])
        out_e.append(e)
        out_d.append(fr[2])
    for i in np.flatnonzero(ev).tolist():
        c = cls[i]
        nf, ns, ce = int(func[i + 1]), int(start[i + 1]), int(end[i])
        if c == cy.CALL:
            stack.append([nf, ns, stack[-1][2] + 1])
        elif c == cy.RET:
            top = stack.pop()
            close(top, ce)
            if not stack:
                stack.append([nf, ns, top[2] - 1])
            elif stack[-1][0] != nf:
                fr = stack[-1]
                close(fr, ce)
                stack[-1] = [nf, ns, fr[2]]
        else:
            fr = stack[-1]
            close(fr, ce)
            stack[-1] = [nf, ns, fr[2]]
    last = int(end[-1])
    while stack:
        close(stack.pop(), last)
    d = np.asarray(out_d, np.int64)
    if len(d):
        d -= d.min()
    order = np.lexsort((d, np.asarray(out_s)))
    return (np.asarray(out_s, np.int64)[order], np.asarray(out_e, np.int64)[order], d[order], np.asarray(out_f, np.int64)[order])
