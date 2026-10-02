"""The code map of one Studio session and its HTTP API (``/api/codemap/*``).

Build: pick a firmware ELF (the one programmed in this session, the newest build, or any ELF plus a source folder), emulate it for the inputs of a captured trace (its key and plaintext), and turn the run into a timeline of functions and source lines. Map: cycles to ADC samples from the scope's clocks and ADC settings, refined by aligning the emulated power model with the stored traces. Query: what code a range of samples represents, and where a function or source line ran.
"""
from __future__ import annotations

import glob
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

from cwstudio.capabilities import Unsupported
from cwstudio.codemap import cycles as cy
from cwstudio.codemap import power
from cwstudio.codemap.emu import EmuError, Firmware
from cwstudio.codemap.program import Program, ProgramError, find_objdump
from cwstudio.codemap.timeline import Timeline
from cwstudio.web import HTTPException, Request, UploadFile

log = logging.getLogger("cwstudio.codemap")

DEFAULT_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")


def _check_mapping(m: power.Mapping) -> None:
    """Reject settings that make the cycle to sample mapping meaningless (before they are applied)."""
    if not 0.2 < m.scale < 5:
        raise ValueError("scale must stay between 0.2 and 5")
    if (m.adc_freq is not None and m.adc_freq <= 0) or (m.target_freq is not None and m.target_freq <= 0):
        raise ValueError("adc_freq and target_freq must be positive (Hz)")
    if m.decimate < 1:
        raise ValueError("decimate must be 1 or more")
    if m.adc_offset < 0 or m.presamples < 0:
        raise ValueError("adc_offset and presamples cannot be negative")
    if not np.isfinite(m.shift):
        raise ValueError("shift must be a number")


def engine_status() -> Dict[str, Any]:
    """Which architectures can be emulated here (Unicorn is needed for Arm and RISC-V)."""
    try:
        import unicorn
        uc = getattr(unicorn, "__version__", None) or "installed"
    except ImportError:
        uc = None
    try:
        import elftools  # noqa: F401
        elf = True
    except ImportError:
        elf = False
    reason_uc = None if uc else "the Unicorn engine is not installed (pip install unicorn)"
    reason_elf = None if elf else "pyelftools is not installed (pip install pyelftools)"
    return {"elf": {"available": elf, "reason": reason_elf},
            "arm": {"available": bool(uc and elf), "reason": reason_elf or reason_uc, "cores": list(cy.ARM_CORES)},
            "riscv": {"available": bool(uc and elf), "reason": reason_elf or reason_uc, "cores": list(cy.RISCV_CORES)},
            "avr": {"available": elf, "reason": reason_elf, "cores": list(cy.AVR_CORES)},
            "unicorn": uc}


class CodeMapService:
    def __init__(self, session):
        self.s = session
        self._lock = threading.RLock()
        self._programs: Dict[str, Program] = {}
        self._firmwares: Dict[tuple, Firmware] = {}
        self.prog: Optional[Program] = None
        self.fw: Optional[Firmware] = None
        self.timeline: Optional[Timeline] = None
        self.mapping = power.Mapping()
        self.alignment: Optional[Dict[str, Any]] = None
        self.P: Optional[np.ndarray] = None
        self.meta: Dict[str, Any] = {}
        self.source_roots: List[str] = []
        self.pctrace: Optional[Dict[str, Any]] = None

    # --- helpers ------------------------------------------------------------------------------------
    def publish(self) -> None:
        self.s.bus.publish("codemap", self.status())

    def _need(self) -> Timeline:
        if self.timeline is None:
            raise LookupError("no code map yet: build one first (POST /api/codemap/build)")
        return self.timeline

    def elfs(self) -> List[Dict[str, Any]]:
        """Candidate ELFs: the firmware programmed in this session first, then Studio's builds (newest first)."""
        out: List[Dict[str, Any]] = []
        seen = set()
        pr = getattr(self.s, "programmed", None)
        if pr and pr.get("elf") and os.path.isfile(pr["elf"]):
            out.append({"path": pr["elf"], "name": os.path.basename(pr["elf"]), "source": "programmed", "emulated": pr.get("emulated"), "mtime": os.path.getmtime(pr["elf"])})
            seen.add(os.path.abspath(pr["elf"]))
        bdir = self.s.firmware.builds_dir
        files = sorted(glob.glob(os.path.join(bdir, "*.elf")), key=lambda p: -os.path.getmtime(p))
        for p in files:
            if os.path.abspath(p) in seen:
                continue
            seen.add(os.path.abspath(p))
            out.append({"path": p, "name": os.path.basename(p), "source": "build", "mtime": os.path.getmtime(p)})
        if self.prog is not None and os.path.abspath(self.prog.path) not in seen:
            out.insert(0, {"path": self.prog.path, "name": os.path.basename(self.prog.path), "source": "chosen", "mtime": os.path.getmtime(self.prog.path) if os.path.isfile(self.prog.path) else 0})
        return out

    def default_elf(self) -> str:
        c = self.elfs()
        pr = [e for e in c if e["source"] in ("programmed", "chosen")]
        if pr:
            return pr[0]["path"]
        job = self.s.firmware.build_status()
        if job.get("hex"):
            elf = os.path.splitext(job["hex"])[0] + ".elf"
            if os.path.isfile(elf):
                return elf
        if c:
            return c[0]["path"]
        raise FileNotFoundError("no firmware ELF yet: build one in the Firmware tab, program one, or choose an .elf file")

    def program(self, path: str, sources: Optional[List[str]] = None) -> Program:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):  # a project folder: its newest ELF, and the folder for the sources
            found = [p for pat in ("*.elf", "*/*.elf", "*/*/*.elf") for p in glob.glob(os.path.join(path, pat))]
            if not found:
                raise FileNotFoundError(f"no .elf in {path}: build the firmware first (Firmware tab) or give the .elf")
            if sources is None and not self.source_roots:
                sources = [path]
            path = max(found, key=os.path.getmtime)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such file: {path}")
        if not path.lower().endswith(".elf"):
            cand = os.path.splitext(path)[0] + ".elf"
            if os.path.isfile(cand):
                path = cand
        st = os.stat(path)
        key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
        p = self._programs.pop(key, None)
        if p is not None:
            self._programs[key] = p  # most recently used last
        if p is None:
            eng = engine_status()
            if not eng["elf"]["available"]:
                raise Unsupported(eng["elf"]["reason"])
            p = Program(path)
            p.objdump = find_objdump(self.s.toolchains, p.arch) if p.arch else None
            self._programs = {k: v for k, v in self._programs.items() if not k.startswith(path + ":")}
            self._programs[key] = p
            while len(self._programs) > 6:  # parsed debug information of a few recent ELFs, not of every file ever opened
                self._programs.pop(next(iter(self._programs)))
        if sources is not None:
            p.set_source_roots(sources)
        return p

    def _firmware(self, prog: Program, core: Optional[str], protocol: Optional[str], ws: int, options: Dict[str, Any]) -> Firmware:
        if prog.arch not in ("arm", "riscv", "avr"):
            raise Unsupported(f"{prog.machine_name} firmware cannot be emulated; the code map supports Arm Cortex-M, RISC-V RV32 and AVR/XMEGA")
        eng = engine_status()[prog.arch]
        if not eng["available"]:
            raise Unsupported(f"{prog.arch} emulation: {eng['reason']}")
        key = (prog.sha256, core, protocol, ws, repr(sorted(options.items())))
        fw = self._firmwares.get(key)
        if fw is None:
            try:
                fw = Firmware(prog, core=core, protocol=protocol, wait_states=ws, options=options)
                fw.machine
            except EmuError as e:
                raise Unsupported(str(e)) from e
            self._firmwares = {k: v for k, v in self._firmwares.items() if k[0] == prog.sha256}
            self._firmwares[key] = fw
        return fw

    def _scope_mapping(self, over: Dict[str, Any]) -> power.Mapping:
        sc = self.s.scope
        if sc is not None:
            try:
                m = self.s.worker.call(power.Mapping.from_scope, sc, timeout=20)
            except Exception as e:  # noqa: BLE001
                log.debug("scope mapping: %s", e)
                m = power.Mapping.from_scope(None)
        else:
            m = power.Mapping.from_scope(None)
        adc = over.get("adc_freq")
        tgt = over.get("target_freq")
        if adc or tgt:
            m.adc_freq = float(adc or m.adc_freq)
            m.target_freq = float(tgt or m.target_freq)
        for k in ("adc_offset", "presamples", "decimate"):
            if over.get(k) is not None:
                setattr(m, k, int(over[k]))
        for k in ("scale", "shift"):
            if over.get(k) is not None:
                setattr(m, k, float(over[k]))
        _check_mapping(m)
        m.spc = m.adc_freq / m.target_freq / max(1, m.decimate)
        return m

    # --- build ------------------------------------------------------------------------------------
    def save_upload(self, filename: str, content: bytes) -> str:
        d = os.path.join(self.s.data_dir, "codemap", "uploads")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, os.path.basename(filename) or "firmware.elf")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def build(self, p: Dict[str, Any]) -> Dict[str, Any]:
        t_start = time.time()
        with self._lock:
            elf = p.get("elf") or self.default_elf()
            sources = p.get("sources")
            if isinstance(sources, str):
                sources = [x for x in sources.split(os.pathsep) if x]
            try:
                prog = self.program(elf, sources if sources is not None else (self.source_roots or None))
            except ProgramError as e:
                raise Unsupported(str(e)) from e
            if sources is not None:
                self.source_roots = list(sources)
            core = p.get("core") or None
            fw = self._firmware(prog, core, p.get("protocol") or None, int(p.get("wait_states") or 0), dict(p.get("options") or {}))
            # inputs: a stored trace's key and plaintext, or given ones
            store = self.s.store
            idx = p.get("trace")
            textout = None
            if idx is None and len(store) and not (p.get("key") or p.get("text")):
                idx = len(store) - 1
            if idx is not None and len(store):
                idx = int(idx) if int(idx) >= 0 else len(store) + int(idx)
                if not 0 <= idx < len(store):
                    raise LookupError(f"no trace {idx} (the store has {len(store)})")
                _w, tin, tout, key = store.get(idx)
                textout = tout
            else:
                idx = None
                tin = key = b""
            key = bytes.fromhex(p["key"].replace(" ", "")) if p.get("key") else (key or DEFAULT_KEY)
            text = bytes.fromhex(p["text"].replace(" ", "")) if p.get("text") is not None else (tin or bytes(16))  # "" sends a command without data
            cmd = p.get("cmd") or "p"
            raw = p.get("raw")
            try:
                if raw is not None:  # firmware that does not speak SimpleSerial (basic-passwdcheck): these serial bytes go in as they are
                    raw_b = raw.encode() if p.get("raw_text") else bytes.fromhex(str(raw).replace(" ", ""))
                    run, _snap = fw.command_raw(raw_b)
                    resp = run.output
                    key, text, cmd, textout = b"", raw_b, "raw", None
                else:
                    run, resp = fw.encrypt(key, text, cmd)
            except EmuError as e:
                raise Unsupported(f"emulation failed: {e}") from e
            tl = Timeline.build(prog, run)
            mapping = self._scope_mapping(dict(p.get("mapping") or {}))  # before anything changes: a bad mapping leaves the previous code map as it was
            self.prog, self.fw, self.timeline = prog, fw, tl
            self.P = power.cycle_power(run)
            self.mapping = mapping
            self.alignment = None
            expect = None
            if cmd == "p" and len(text) == 16 and len(key) == 16:
                from cwstudio.aes import encrypt_block
                expect = encrypt_block(key, text)
            hi, lo = run.trigger_window()
            self.meta = {"elf": prog.path, "name": os.path.basename(prog.path), "trace": idx, "key": key.hex(), "text": text.hex(), "cmd": cmd,
                         "response": resp.hex() if resp is not None else None, "aes_ok": (resp == expect) if expect is not None and resp is not None else None,
                         "stored_textout": textout.hex() if textout else None, "matches_stored": (resp == textout) if textout and resp is not None else None,
                         "instructions": run.instructions, "cycles": run.total_cycles, "trigger_cycles": (lo - hi) if hi is not None and lo is not None else None,
                         "trigger_source": run.trigger_source, "halted": fw.machine.where(run.halted) if run.halted is not None else None, "output": run.output[:512].hex(),
                         "built": time.time(), "seconds": None, "firmware": fw.info(), "program": prog.summary(),
                         "sim_emulates": self._sim_emulates(prog)}
            want = p.get("align", "auto")
            if want is True or (want == "auto" and len(store) >= 1):
                try:
                    self._align({"source": p.get("align_source") or "mean"})
                except Exception as e:  # noqa: BLE001
                    self.alignment = {"error": str(e)}
            self.meta["seconds"] = round(time.time() - t_start, 3)
        self.publish()
        return self.status(full=True)

    def _sim_emulates(self, prog: Program) -> Optional[bool]:
        sc = self.s.scope
        if sc is None or getattr(sc, "_getCWType", lambda: "")() != "cwsim":
            return None
        fs = getattr(sc, "firmware", None)
        return bool(fs is not None and fs.prog.sha256 == prog.sha256)

    # --- status -------------------------------------------------------------------------------------
    def status(self, full: bool = False) -> Dict[str, Any]:
        st: Dict[str, Any] = {"ready": self.timeline is not None, "engines": engine_status(), "mapping": self.mapping.to_json(), "alignment": self.alignment,
                              "cores": {k: cy.CORE_LABELS[k] for k in cy.ALL_CORES}, "source_roots": self.source_roots, "programmed": getattr(self.s, "programmed", None)}
        st.update(self.meta)
        if full and self.timeline is not None:
            st["band"] = self.timeline.to_json()
        return st

    def band(self) -> Dict[str, Any]:
        tl = self._need()
        return {"band": tl.to_json(), "mapping": self.mapping.to_json(), "alignment": self.alignment, "name": self.meta.get("name"), "trace": self.meta.get("trace")}

    # --- mapping & alignment ------------------------------------------------------------------------
    def set_mapping(self, p: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            m = power.Mapping(**self.mapping.__dict__)  # changes apply only once all of them are valid
            for k in ("shift", "scale"):
                if p.get(k) is not None:
                    setattr(m, k, float(p[k]))
            for k in ("dshift", "dscale"):
                if p.get(k) is not None:
                    if k == "dshift":
                        m.shift += float(p[k])
                    else:
                        m.scale *= 1.0 + float(p[k])
            for k in ("adc_offset", "presamples", "decimate"):
                if p.get(k) is not None:
                    setattr(m, k, int(p[k]))
            for k in ("adc_freq", "target_freq"):
                if p.get(k):
                    setattr(m, k, float(p[k]))
            if p.get("reset"):
                m.shift, m.scale = 0.0, 1.0
            _check_mapping(m)
            m.spc = (m.adc_freq or 4 * 7.37e6) / (m.target_freq or 7.37e6) / max(1, m.decimate)
            self.mapping = m
        self.publish()
        return {"mapping": m.to_json(), "alignment": self.alignment}

    def _align(self, p: Dict[str, Any]) -> Dict[str, Any]:
        tl = self._need()
        store = self.s.store
        src = p.get("source") or "mean"
        if src not in ("mean", "trace"):
            raise ValueError("source must be 'mean' or 'trace'")
        if not len(store):
            raise LookupError("no stored traces to align with: capture some first")
        if src == "trace":
            i = p.get("trace")
            i = int(i) if i is not None else (self.meta.get("trace") if self.meta.get("trace") is not None else len(store) - 1)
            if not -len(store) <= i < len(store):
                raise LookupError(f"no trace {i} (the store has {len(store)})")
            x = store.get(i % len(store))[0]
        else:
            st = store.stats(0, int(p["traces"]) if p.get("traces") else None)
            x = st.get("mean")
            if x is None:
                raise LookupError("no stored traces to align with")
        sr = (float(p.get("scale_min") or 0.9), float(p.get("scale_max") or 1.1))
        if not 0.2 < sr[0] <= sr[1] < 5:
            raise ValueError("need 0.2 < scale_min <= scale_max < 5")
        ms = int(p["max_shift"]) if p.get("max_shift") else None
        if ms is not None and not 1 <= ms <= 200_000:
            raise ValueError("max_shift must be 1 to 200000 samples")
        res = power.align(np.asarray(x), self.P, tl.t0, self.mapping, max_shift=ms, scale_range=sr)
        res["source"] = src
        res["samples"] = int(len(x))
        apply = p.get("apply", "auto")
        if apply == "auto":  # a low confidence fit is more likely wrong than the nominal mapping from the scope settings: keep the mapping
            apply = res["label"] != "low"
        res["applied"] = bool(apply)
        if apply:
            self.mapping.shift, self.mapping.scale = res["shift"], res["scale"]
        self.alignment = res
        return res

    def align(self, p: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            res = self._align(p)
        self.publish()
        return {"alignment": res, "mapping": self.mapping.to_json()}

    def model(self, n: Optional[int] = None) -> np.ndarray:
        tl = self._need()
        if n is None:
            n = self.s.store.summary().get("samples") or 5000
        n = int(n)
        if not 0 <= n <= 2_000_000:
            raise ValueError("n must be 0 to 2000000 samples")
        return power.model_samples(self.P, tl.t0, self.mapping, n).astype(np.float32)

    # --- queries -------------------------------------------------------------------------------------
    def _with_samples(self, d: Dict[str, Any]) -> Dict[str, Any]:
        m = self.mapping
        smp = lambda c: round(float(m.sample(c)), 2)  # noqa: E731
        for f in d.get("functions", []):
            f["samples"] = [smp(f["first"]), smp(f["last"])]
            f["sample_spans"] = [[smp(a), smp(b), depth] for a, b, depth in f.get("spans", [])]
        for ln in d.get("lines", []) + d.get("inlined", []):
            ln["samples"] = [smp(ln["first"]), smp(ln["last"])]
        return d

    def region(self, p: Dict[str, Any]) -> Dict[str, Any]:
        tl = self._need()
        if "start" not in p or "end" not in p:
            raise ValueError("give start and end (sample indices)")
        s0, s1 = float(p["start"]), float(p["end"])
        if not (np.isfinite(s0) and np.isfinite(s1)):
            raise ValueError("start and end must be finite sample indices")
        c0, c1 = (float(x) for x in self.mapping.cycle([s0, s1]))
        limit = int(p["limit"]) if p.get("limit") is not None else 400
        out = tl.region(c0, c1, limit=max(1, limit))
        out["samples"] = [s0, s1]
        return self._with_samples(out)

    def lookup(self, p: Dict[str, Any]) -> Dict[str, Any]:
        tl = self._need()
        m = self.mapping
        if p.get("function"):
            rng = tl.function_ranges(p["function"])
            f = tl.prog.function(p["function"])
            if f is None:  # inlined: point at the line it was inlined from
                info = next((i for i in tl.prog.inline_info if i[0] == p["function"]), None)
                return {"function": p["function"], "inlined": True, "file": None, "file_index": -1, "line": 0, "call_file": tl.prog.short_label(info[1]) if info and info[1] >= 0 else None, "call_line": info[2] if info else 0,
                        "ranges": [{"cycles": [a, b], "samples": [round(float(m.sample(a)), 2), round(float(m.sample(b)), 2)], "depth": d} for a, b, d in rng]}
            return {"function": p["function"], "file": tl.prog.short_label(f.file) if f and f.file >= 0 else None, "file_index": f.file if f else -1, "line": f.line if f else 0,
                    "ranges": [{"cycles": [a, b], "samples": [round(float(m.sample(a)), 2), round(float(m.sample(b)), 2)], "depth": d} for a, b, d in rng]}
        if p.get("file") is not None and p.get("line") is not None:
            fi = p["file"]
            fi = int(fi) if isinstance(fi, int) or (isinstance(fi, str) and fi.lstrip("-").isdigit()) else tl.prog.find_file(str(fi))
            if not 0 <= fi < len(tl.prog.files):
                raise LookupError(f"no source file {p['file']!r} in the debug information")
            line = int(p["line"])
            rng = tl.line_ranges(fi, line)
            return {"file": tl.prog.short_label(fi), "file_index": fi, "path": tl.prog.file_label(fi), "line": line,
                    "ranges": [{"cycles": [a, b], "samples": [round(float(m.sample(a)), 2), round(float(m.sample(b)), 2)]} for a, b in rng]}
        raise ValueError("give function, or file and line")

    def at(self, sample: float) -> Optional[Dict[str, Any]]:
        tl = self._need()
        if not np.isfinite(sample):
            raise ValueError("sample must be a finite number")
        c = float(self.mapping.cycle(sample))
        d = tl.at(c)
        if d:
            d["sample"] = sample
        return d

    def source(self, file: Any) -> Dict[str, Any]:
        tl = self._need()
        prog = tl.prog
        fi = int(file) if str(file).lstrip("-").isdigit() else prog.find_file(str(file))
        if fi < 0 or fi >= len(prog.files):
            raise LookupError(f"no source file {file!r}")
        text = prog.source(fi)
        executed: Dict[int, float] = {}
        sel = tl.file[tl.lr_first] == fi
        if sel.any():
            lines = tl.line[tl.lr_first][sel]
            dur = (tl.lr_end - tl.lr_start)[sel]
            for ln, d in zip(lines.tolist(), dur.tolist()):
                executed[ln] = executed.get(ln, 0) + d
        return {"file_index": fi, "path": prog.file_label(fi), "resolved": prog.resolve_source(fi), "name": prog.short_label(fi), "text": text,
                "code_lines": prog.lines_in_file(fi), "executed": {str(k): v for k, v in sorted(executed.items())},
                "reason": None if text is not None else "source file not found: choose the source folder"}

    def disasm(self, p: Dict[str, Any]) -> Dict[str, Any]:
        tl = self._need()
        prog = tl.prog
        if p.get("lo") is not None and p.get("hi") is not None:
            ranges = [(int(p["lo"]), int(p["hi"]))]
        elif p.get("function"):
            f = prog.function(p["function"])
            if f is None:
                raise LookupError(f"no function {p['function']!r}")
            ranges = [(f.lo, f.hi)]
        elif p.get("file") is not None and p.get("line") is not None:
            fi = p.get("file")
            fi = int(fi) if str(fi).lstrip("-").isdigit() else prog.find_file(str(fi))
            if not 0 <= fi < len(prog.files):
                raise LookupError(f"no source file {p['file']!r} in the debug information")
            ranges = prog.ranges_for_line(fi, int(p["line"]))
        else:
            raise ValueError("give function, file and line, or lo and hi (addresses)")
        out = []
        counts = dict(zip(*np.unique(tl.run.pcs, return_counts=True))) if len(tl.run.pcs) else {}
        for lo, hi in ranges[:16]:
            for ins in prog.disassemble(lo, hi):
                ins["executed"] = int(counts.get(ins["addr"], 0))
                out.append(ins)
        return {"ranges": [[lo, hi] for lo, hi in ranges], "instructions": out}

    # --- exact mode (Husky Arm trace) ------------------------------------------------------------------
    def pc_trace(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """Record real program counter samples with the Husky's trace port for the code map's inputs and compare them with the emulation (on the simulator: from the emulated run through the same decoder)."""
        from cwstudio.capabilities import capabilities, require
        from cwstudio.codemap import swo
        tl = self._need()
        sc = self.s.scope
        caps = capabilities(sc, self.s.target)
        require(caps.get("trace"), "Arm trace (exact mode)")
        if tl.prog.arch != "arm":
            raise Unsupported("exact mode records Arm SWO trace; this firmware is " + str(tl.prog.arch))
        interval = int(p.get("interval") or 64)
        if caps.get("simulated"):
            fake = swo.FakeTraceWhisperer(tl.run, interval=interval)
            stream = swo.raw_bytes(fake.read_capture_data())
            samples = swo.decode_pc_samples(stream)
            res = swo.compare(samples, tl)
            res.update({"mode": "simulated", "interval": interval, "bytes": len(stream)})
        else:
            if "set_pcsample_params" not in tl.prog.symbols:
                raise Unsupported("exact mode needs firmware with ChipWhisperer's simpleserial-trace commands (PC sampling setup); build simpleserial-trace for this platform")
            if self.s.target is None:
                raise Unsupported("connect the target first")
            rec = swo.HuskyPCTrace(sc, self.s.target, swo_div=int(p.get("swo_div") or 8), postinit=max(0, interval // 64 - 1))
            key = bytes.fromhex(self.meta["key"])
            text = bytes.fromhex(self.meta["text"])

            def _do():
                rec.setup()
                return rec.capture(key, text)
            got = self.s.worker.call(_do, timeout=60)
            res = swo.compare(got["samples"], tl)
            res.update({"mode": "husky", "interval": interval, "bytes": got["bytes"], "textout": got["textout"]})
        m = self.mapping
        for r in res.get("rows", []):
            r["sample"] = round(float(m.sample(r["cycle"] * (res.get("scale") or 1.0) + (res.get("offset") or 0.0))), 2)
        self.pctrace = res
        return res


# --- HTTP routes ---------------------------------------------------------------------------------------
def register_routes(app, session) -> None:
    """Add the /api/codemap/* routes (called from ``create_app``)."""
    import asyncio
    from cwstudio.events import trace_event
    from cwstudio.web import Response

    svc = getattr(session, "codemap", None)
    if svc is None:
        svc = CodeMapService(session)
        session.codemap = svc

    async def run(fn, *args):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args))
        except HTTPException:
            raise
        except LookupError as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)  # KeyError's str() is the repr of its message
            raise HTTPException(status_code=404, detail=f"{type(e).__name__}: {msg}")
        except Exception as e:  # noqa: BLE001
            log.debug("codemap API error", exc_info=True)
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")

    async def body(req: Request) -> Dict[str, Any]:
        try:
            return (await req.json()) or {}
        except Exception:  # noqa: BLE001
            return {}

    @app.get("/api/codemap")
    async def codemap_status():
        """Code map state: firmware (ELF, architecture, core, SimpleSerial version), the inputs it ran with, whether the emulated response is the correct AES and matches the stored trace, the cycle to sample mapping and the last alignment."""
        return await run(svc.status)

    @app.get("/api/codemap/elfs")
    async def codemap_elfs():
        """ELF files to choose from: the one programmed in this session, then Studio's firmware builds."""
        return await run(svc.elfs)

    @app.post("/api/codemap/build")
    async def codemap_build(req: Request):
        """Body: elf (path; default the programmed firmware or the newest build), sources (folder), trace (index; default the newest), key/text (hex, instead of a trace; text "" sends the command without data), cmd ('p'), raw (hex serial bytes fed as they are, for firmware that is not SimpleSerial; raw_text true: raw is text), core, protocol ('2.1', '1.1'), wait_states, mapping {adc_freq, target_freq, adc_offset, presamples, decimate, scale, shift}, align (true, false or 'auto'), options {skip: [...], getch: [...], putch: [...], trigger_high: [...], trigger_low: [...], max_instructions (per command, default 4000000)}."""
        return await run(svc.build, await body(req))

    @app.post("/api/codemap/upload")
    async def codemap_upload(file: UploadFile):
        """Upload an ELF from the browser; returns its path on the Studio machine (give it to build as elf)."""
        content = await file.read()
        if content[:4] != b"\x7fELF":
            raise HTTPException(status_code=400, detail="Unsupported: not an ELF file (the code map needs the .elf with symbols, not the .hex)")
        return {"path": await run(svc.save_upload, file.filename or "firmware.elf", content)}

    @app.get("/api/codemap/band")
    async def codemap_band():
        """The timeline for the waveform's Code band: function spans (flame chart) and source line runs in cycles from the trigger, plus the mapping to samples."""
        return await run(svc.band)

    @app.post("/api/codemap/region")
    async def codemap_region(req: Request):
        """Body: start, end (sample indices). The functions (self cycles, inclusive spans) and source lines (cycles, executions, source text) that ran in those samples."""
        return await run(svc.region, await body(req))

    @app.post("/api/codemap/lookup")
    async def codemap_lookup(req: Request):
        """Body: function, or file (name, path or index) and line. Where it ran: cycle and sample ranges."""
        return await run(svc.lookup, await body(req))

    @app.post("/api/codemap/align")
    async def codemap_align(req: Request):
        """Body: source ('mean' or 'trace'), trace, scale_min, scale_max, max_shift, apply (true, false or 'auto': apply unless the confidence is low). Fits shift and scale of the mapping by correlating the emulated power model with the traces."""
        return await run(svc.align, await body(req))

    @app.put("/api/codemap/mapping")
    async def codemap_mapping(req: Request):
        """Body: shift, scale (absolute), dshift (samples) or dscale (fraction) to nudge, adc_offset, presamples, decimate, adc_freq, target_freq, reset."""
        return await run(svc.set_mapping, await body(req))

    @app.get("/api/codemap/source")
    async def codemap_source(file: str):
        """A source file of the firmware (index, path or name): text, lines with code and the executed lines with their cycles."""
        return await run(svc.source, file)

    @app.get("/api/codemap/disasm")
    async def codemap_disasm(file: Optional[str] = None, line: Optional[int] = None, function: Optional[str] = None, lo: Optional[int] = None, hi: Optional[int] = None):
        """Disassembly of a source line (file and line), a function, or an address range (lo, hi), with how often each instruction ran."""
        return await run(svc.disasm, {"file": file, "line": line, "function": function, "lo": lo, "hi": hi})

    @app.get("/api/codemap/at")
    async def codemap_at(sample: float):
        """The instruction (address, function, file:line, cycle) at a sample index."""
        return await run(svc.at, sample)

    @app.get("/api/codemap/model")
    async def codemap_model(n: Optional[int] = None):
        """The emulated power model resampled to the trace's samples with the current mapping (binary frame like a trace)."""
        x = await run(svc.model, n)
        return Response(content=trace_event("codemap_model", x, {"samples": int(len(x))}).frame(), media_type="application/octet-stream")

    @app.post("/api/codemap/pctrace")
    async def codemap_pctrace(req: Request):
        """Exact mode: record program counter samples through the Husky's Arm trace port (SWO) and compare them with the emulation. Body: interval (cycles between samples), swo_div."""
        return await run(svc.pc_trace, await body(req))
