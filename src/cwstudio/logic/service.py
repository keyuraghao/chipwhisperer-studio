"""The logic analyser state of one Studio session and its HTTP API (``/api/la/*``).

Captures live in memory (the newest eight; older ones can be kept as ``.sr`` files on disk). Decoders are configured once and run on whichever capture is shown, with results cached per capture. The browser never downloads a whole capture: ``POST /api/la/view`` answers with the edges or per-pixel summaries of the visible range, plus the decoded annotations there.
"""
from __future__ import annotations

import copy
import io
import itertools
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Dict, List, Optional

import numpy as np

from cwstudio.capabilities import Unsupported, require
from cwstudio.logic import decoders as dec
from cwstudio.logic import formats, sigrok, synth
from cwstudio.logic.model import AnalogChannel, Channel, LogicCapture, parse_pattern
from cwstudio.logic.sources import CLK_SOURCES, LA_TRIGGER_HELP, LA_TRIGGERS, LogicJob, adc_rate
from cwstudio.web import FileResponse, HTTPException, Request, Response, UploadFile

log = logging.getLogger("cwstudio.logic")

SOURCE_LABELS = {"native": "Husky logic analyser", "adc": "Analog input (thresholded)", "sim": "Simulator demo traffic", "sigrok": "External analyser (sigrok)", "files": "File (VCD, CSV, sigrok .sr)"}
CAP_KEY = {"native": "native", "adc": "adc", "sim": "sim", "sigrok": "external", "files": "files"}


class LogicService:
    MAX_CAPTURES = 8
    SYNC_DECODE_EDGES = 300_000  # captures with fewer edges decode inside the view request; bigger ones in the background

    def __init__(self, session):
        self.s = session
        self.captures: "OrderedDict[str, LogicCapture]" = OrderedDict()
        self.current_id: Optional[str] = None
        self.decoders: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.buses: List[Dict[str, Any]] = []
        self._results: Dict[tuple, Any] = {}
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self.job: Optional[LogicJob] = None
        self.last: Optional[Dict[str, Any]] = None
        self.dir = os.path.join(session.data_dir, "logic")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="la-decode")
        self._procs: Any = None  # the decode worker process pool, made on first use (False when it cannot run)
        self._pending: set = set()

    # --- helpers -----------------------------------------------------------------------------------
    def publish(self, kind: str, payload: Dict[str, Any]) -> None:
        self.s.bus.publish(kind, payload)

    def caps(self) -> Dict[str, Any]:
        from cwstudio.capabilities import capabilities
        if self.s.scope is None:
            return capabilities(None)["logic_analyzer"]
        return self.s.worker.call(capabilities, self.s.scope, self.s.target, timeout=30)["logic_analyzer"]

    def cap(self, cid: Optional[str] = None) -> LogicCapture:
        with self._lock:
            cid = cid or self.current_id
            if cid is None or cid not in self.captures:
                raise LookupError("no logic capture yet: capture one or import a file" if cid is None else f"no capture {cid}")
            return self.captures[cid]

    def add_capture(self, cap: LogicCapture, select: bool = True, persist: bool = False) -> Dict[str, Any]:
        cap.meta.setdefault("orig_names", [c.name for c in cap.channels])
        with self._lock:
            if cap.source not in ("file", "trace") and cap.channels:
                # a new capture from the same source and channels keeps the names, colours, hidden channels and order set on the previous one (like a repeat capture in PulseView)
                prev = next((c for c in reversed(self.captures.values()) if c.source == cap.source and c.meta.get("orig_names") == cap.meta["orig_names"]), None)
                if prev is not None:
                    for ch, old in zip(cap.channels, prev.channels):
                        ch.name, ch.color, ch.hidden = old.name, old.color, old.hidden
                    if prev.meta.get("order") and sorted(prev.meta["order"]) == list(range(len(cap.channels))):
                        cap.meta["order"] = list(prev.meta["order"])
        cap.meta.setdefault("order", list(range(len(cap.channels))))
        with self._lock:
            self.captures[cap.id] = cap
            while len(self.captures) > self.MAX_CAPTURES:
                old, _ = self.captures.popitem(last=False)
                self._results = {k: v for k, v in self._results.items() if k[0] != old}
            if select or self.current_id is None:
                self.current_id = cap.id
        if persist:
            try:
                # the capture id keeps repeat captures within the same second from overwriting each other
                path = formats.save(cap, os.path.join(self.dir, "captures", time.strftime("%Y%m%d-%H%M%S") + f"-{cap.source}-{cap.id}"), "sr")
                cap.meta["saved"] = path
            except Exception as e:  # noqa: BLE001
                cap.meta["save_error"] = str(e)
        summ = self.summary(cap)
        self.publish("la", {"kind": "capture", "capture": {k: summ[k] for k in ("id", "name", "source", "samples", "samplerate", "duration")}, "current": self.current_id})
        return summ

    def summary(self, cap: Optional[LogicCapture] = None) -> Dict[str, Any]:
        cap = cap or self.cap()
        d = cap.summary()
        d["order"] = cap.meta.get("order", list(range(len(cap.channels))))
        return d

    def captures_list(self) -> Dict[str, Any]:
        with self._lock:
            caps = list(self.captures.values())
        return {"current": self.current_id, "captures": [{"id": c.id, "name": c.name, "source": c.source, "samples": c.n, "samplerate": c.samplerate, "duration": c.duration, "channels": len(c.channels), "created": c.created} for c in caps]}

    def select(self, cid: str) -> Dict[str, Any]:
        cap = self.cap(cid)
        with self._lock:
            self.current_id = cid
        self.publish("la", {"kind": "select", "current": cid})
        return self.summary(cap)

    def delete(self, cid: str) -> Dict[str, Any]:
        with self._lock:
            self.captures.pop(cid, None)
            self._results = {k: v for k, v in self._results.items() if k[0] != cid}
            if self.current_id == cid:
                self.current_id = next(reversed(self.captures), None) if self.captures else None
        self.publish("la", {"kind": "select", "current": self.current_id})
        return self.captures_list()

    # --- sources and capture ---------------------------------------------------------------------
    def sources(self) -> Dict[str, Any]:
        c = self.caps()
        scope = self.s.scope
        out = []
        for sid in ("native", "adc", "sim", "sigrok", "files"):
            entry = dict(c.get(CAP_KEY[sid], {"available": False, "reason": "not supported"}))
            entry["id"] = sid
            entry["label"] = SOURCE_LABELS[sid]
            out.append(entry)
        info: Dict[str, Any] = {"sources": out, "la_triggers": LA_TRIGGERS, "la_trigger_help": LA_TRIGGER_HELP, "clk_sources": CLK_SOURCES, "sim_channels": synth.DEMO_CHANNELS, "decoders": dec.registry(), "formats": formats.FORMATS}
        if scope is not None:
            def probe():
                d: Dict[str, Any] = {}
                try:
                    d["adc_rate"] = adc_rate(scope)
                    d["adc_samples"] = int(scope.adc.samples)
                except Exception:  # noqa: BLE001
                    pass
                for k, path in (("clkgen_freq", ("clock", "clkgen_freq")),):
                    try:
                        d[k] = float(getattr(getattr(scope, path[0]), path[1]))
                    except Exception:  # noqa: BLE001
                        pass
                la = getattr(scope, "LA", None)
                if la is not None:
                    try:
                        d["la"] = {"clk_source": la.clk_source, "oversampling": float(la.oversampling_factor), "downsample": int(la.downsample), "group": la.capture_group, "max_depth": int(getattr(la, "max_capture_depth", 16376)),
                                   "source_clock": float(la.source_clock_frequency), "sources": {"usb": 96e6, "target": d.get("clkgen_freq"), "pll": d.get("clkgen_freq")}, "locked": bool(la.locked)}
                    except Exception as e:  # noqa: BLE001
                        d["la_error"] = str(e)
                return d
            try:
                info["scope"] = self.s.worker.call(probe, timeout=20)
            except Exception as e:  # noqa: BLE001
                info["scope"] = {"error": str(e)}
        info["target_baud"] = getattr(self.s.target, "baud", None) if self.s.target is not None else None
        info["sigrok"] = sigrok.status()
        return info

    def capture(self, source: str, settings: Optional[Dict[str, Any]] = None, wait: bool = False, timeout: float = 120) -> Dict[str, Any]:
        source = {"external": "sigrok", "simulator": "sim", "husky": "native", "analog": "adc"}.get(source, source)
        if source == "files":
            raise ValueError("files are imported with la_import (POST /api/la/import), not captured")
        if source not in ("native", "adc", "sim", "sigrok"):
            raise ValueError(f"unknown source {source!r}; choose native, adc, sim or sigrok")
        c = self.caps()
        require(c.get(CAP_KEY[source]), SOURCE_LABELS[source])
        if source != "sigrok" and self.s.scope is None:
            raise Unsupported("connect a scope first")
        settings = dict(settings or {})
        persist = bool(settings.pop("persist", False))
        job = LogicJob(source, settings, self.s.scope, self.s.target, os.path.join(self.dir, "sigrok"), lambda j: self._job_done(j, persist), self.publish)
        if not self.s.worker.start_long_job(job):
            raise RuntimeError("another hardware job is running; stop it first")
        self.job = job
        self.s.last_job = job
        self.s._push_status()
        if not wait:
            return {"started": True, "source": source}
        if not job.finished.wait(timeout):
            job.request_stop()
            raise TimeoutError(f"the capture did not finish within {timeout:g} s")
        return self.job_result(job)

    def _job_done(self, job: LogicJob, persist: bool) -> None:
        info: Dict[str, Any] = {"source": job.source, "warnings": job.warnings, "stopped": job.stopped, "error": job.error}
        if job.capture is not None and not job.error:
            job.capture.meta.setdefault("warnings", job.warnings)
            job.capture.meta["settings"] = {k: v for k, v in job.settings.items() if k != "seed"}
            info["capture"] = self.add_capture(job.capture, persist=persist)["id"]
        self.last = info
        state = "error" if job.error else "stopped" if job.stopped else "done"
        self.publish("la", {"kind": "job", "state": state, **info})

    def job_result(self, job: LogicJob) -> Dict[str, Any]:
        if job.error:
            msg = job.error
            if msg.startswith("Unsupported: "):
                raise Unsupported(msg[len("Unsupported: "):])
            raise RuntimeError(msg)
        if job.stopped or job.capture is None:
            return {"stopped": True, "source": job.source}
        return {"ok": True, "warnings": job.warnings, **self.summary(job.capture)}

    def stop(self) -> Dict[str, Any]:
        job = self.job
        if job is not None and not job.finished.is_set():
            job.request_stop()
            return {"stopping": True}
        return {"stopping": False}

    def status(self) -> Dict[str, Any]:
        job = self.job
        running = job is not None and not job.finished.is_set()
        out = {"running": running, "job": {"source": job.source, "phase": job.phase} if running else None, "last": self.last, **self.captures_list(), "decoders": list(self.decoders.values()), "buses": self.buses}
        try:
            out["capture"] = self.summary()
        except LookupError:
            out["capture"] = None
        return out

    # --- files ------------------------------------------------------------------------------------
    def import_file(self, path: str, fmt: Optional[str] = None, samplerate: Optional[float] = None, name: Optional[str] = None) -> Dict[str, Any]:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such file: {path}")
        cap = formats.load(path, fmt, samplerate)
        if name:
            cap.name = name
        cap.source = "file"
        return self.add_capture(cap)

    def export(self, fmt: str, path: Optional[str] = None, cid: Optional[str] = None, channels: Optional[List[Any]] = None) -> str:
        fmt = fmt.lower().lstrip(".")
        if fmt not in formats.FORMATS:
            raise ValueError(f"export format must be one of {', '.join(formats.FORMATS)}")
        cap = self.cap(cid)
        chans = None if channels is None else [cap.ch_index(c) for c in channels]
        if not path:
            base = re.sub(r"[^\w.-]+", "_", cap.name)[:60] or "capture"
            path = os.path.join(self.dir, "exports", base)
        return formats.save(cap, os.path.expanduser(path), fmt, chans)

    def files(self) -> List[Dict[str, Any]]:
        out = []
        for sub in ("captures", "exports", "imports", "sigrok"):
            d = os.path.join(self.dir, sub)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if f.rsplit(".", 1)[-1].lower() in ("sr", "vcd", "csv"):
                    out.append({"path": p, "name": f, "folder": sub, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return sorted(out, key=lambda x: -x["mtime"])

    def add_analog_from_trace(self, index: int = -1, cid: Optional[str] = None) -> Dict[str, Any]:
        """Show a stored power trace as an analog row on the current capture's time base (sample 0 at the trigger plus adc.offset minus presamples)."""
        wave, _tin, _tout, _key = self.s.store.get(index)
        scope = self.s.scope
        sr = adc_rate(scope) if scope is not None else 29.538e6
        off = pre = 0
        if scope is not None:
            try:
                off, pre = int(scope.adc.offset), int(getattr(scope.adc, "presamples", 0) or 0)
            except Exception:  # noqa: BLE001
                pass
        try:
            cap = self.cap(cid)
        except LookupError:
            cap = LogicCapture([], int(wave.shape[0]), sr, pre - off, source="trace", name="Power trace")
            self.add_capture(cap)
        cap.analog.append(AnalogChannel(f"trace {index if index >= 0 else len(self.s.store) + index}", np.asarray(wave, np.float32), sr, t0=(off - pre) / sr))
        cap.version += 1
        self.publish("la", {"kind": "channels", "current": cap.id})
        return self.summary(cap)

    def analog_to_waveform(self, k: int = 0, cid: Optional[str] = None) -> Dict[str, Any]:
        """Put an analog row (e.g. the Husky ADC trace captured with the logic analyser) into the trace store so the waveform view shows it."""
        cap = self.cap(cid)
        if not 0 <= k < len(cap.analog):
            raise IndexError("no such analog row")
        an = cap.analog[k]
        idx = self.s.store.append(an.data, b"", b"", b"")
        self.publish("traces", self.s.store.summary())
        return {"index": idx, "count": len(self.s.store)}

    # --- view, channels, buses ------------------------------------------------------------------------
    def view(self, p: Dict[str, Any]) -> Dict[str, Any]:
        cap = self.cap(p.get("capture"))
        a = float(p.get("a", 0))
        b = float(p.get("b", cap.n))
        px = int(p.get("px", 1000))
        chans = p.get("channels")
        if chans is None:
            chans = [i for i in cap.meta.get("order", range(len(cap.channels))) if not cap.channels[i].hidden]
        buses = p.get("buses", self.buses)
        res = cap.view(a, b, px, chans, buses=buses or None, analog=bool(p.get("analog", True)))
        res["capture"] = cap.id
        res["version"] = cap.version
        ids = p.get("decoders")
        out = []
        for did, d in list(self.decoders.items()):
            if (ids is not None and did not in ids) or not d.get("enabled", True):
                continue
            r = self.result_or_pending(did, cap)
            if r is None:
                out.append({"id": did, "type": d["type"], "name": d["name"], "pending": True, "rows": []})
            elif isinstance(r, Exception):
                out.append({"id": did, "error": str(r)})
            else:
                out.append({"id": did, "type": d["type"], "name": d["name"], "rows": r.view(a, b, px), "meta": r.meta})
        res["decoders"] = out
        res["seq"] = p.get("seq")
        return res

    def channels(self, cid: Optional[str] = None) -> Dict[str, Any]:
        cap = self.cap(cid)
        return {"capture": cap.id, "channels": [c.info(i) for i, c in enumerate(cap.channels)], "analog": [a.info(i) for i, a in enumerate(cap.analog)], "order": cap.meta.get("order"), "buses": self.buses}

    def set_channels(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """Rename, recolour, hide or reorder channels and define buses. Body: channels [{index or name, name, color, hidden}], order [indices top to bottom], buses [{name, channels (MSB first), format}], analog [{index, hidden}]."""
        cap = self.cap(p.get("capture"))
        for upd in p.get("channels") or []:
            ref = upd.get("index", upd.get("channel"))
            ch = cap.channels[cap.ch_index(ref)]
            if upd.get("name") not in (None, ""):
                ch.name = str(upd["name"])[:60]
            if upd.get("color"):
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(upd["color"])):
                    raise ValueError("colours look like #2fbf9b")
                ch.color = upd["color"]
            if "hidden" in upd and upd["hidden"] is not None:
                ch.hidden = bool(upd["hidden"])
        for upd in p.get("analog") or []:
            an = cap.analog[int(upd["index"])]
            if "hidden" in upd:
                an.hidden = bool(upd["hidden"])
            if upd.get("remove"):
                cap.analog.pop(int(upd["index"]))
        if p.get("order") is not None:
            order = [cap.ch_index(x) for x in p["order"]]
            rest = [i for i in range(len(cap.channels)) if i not in order]
            cap.meta["order"] = list(dict.fromkeys(order)) + rest
        if p.get("buses") is not None:
            buses = []
            for b in p["buses"]:
                members = [cap.ch_index(x) for x in b.get("channels", [])]
                if not members:
                    raise ValueError("a bus needs at least one channel")
                if len(members) > 32:
                    raise ValueError("a bus holds at most 32 channels")
                buses.append({"name": str(b.get("name") or "bus")[:40], "channels": members, "format": b.get("format", "hex") if b.get("format") in ("hex", "dec", "bin") else "hex"})
            self.buses = buses
        self.publish("la", {"kind": "channels", "current": cap.id})
        return self.channels(cap.id)

    # --- measurements and search ---------------------------------------------------------------------
    def _range(self, cap: LogicCapture, p: Dict[str, Any]):
        a = p.get("from")
        b = p.get("to")
        if p.get("t0") is not None:
            a = cap.idx(float(p["t0"]))
        if p.get("t1") is not None:
            b = cap.idx(float(p["t1"]))
        return (None if a is None else float(a)), (None if b is None else float(b))

    def measure(self, p: Dict[str, Any]) -> Dict[str, Any]:
        cap = self.cap(p.get("capture"))
        a, b = self._range(cap, p)
        chans = p.get("channels")
        if chans is None:
            chans = [p["channel"]] if p.get("channel") is not None else list(range(len(cap.channels)))
        res = [cap.measure(c, a, b) for c in chans]
        return {"capture": cap.id, "samplerate": cap.samplerate, "results": res} if p.get("channel") is None else res[0]

    def search(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """kind edge (channel, edge any/rising/falling), pattern (pattern like 1X0 over channels in display order or channels, optional edge_channel and edge) or decoded (text, decoder); direction next/prev from sample ``from``."""
        cap = self.cap(p.get("capture"))
        direction = -1 if str(p.get("direction", "next")).startswith("prev") else 1
        frm = float(p.get("from", -1) if p.get("from") is not None else -1)
        if direction < 0 and frm < 0:
            frm = float(cap.n)  # searching backwards with no start point: from the end
        kind = p.get("kind", "edge")
        if kind == "edge":
            hit = cap.next_edge(p["channel"], frm, direction, p.get("edge", "any"))
            return {"found": hit is not None, "index": hit, "t": cap.t(hit) if hit is not None else None}
        if kind == "pattern":
            order = [cap.ch_index(c) for c in p["channels"]] if p.get("channels") else [i for i in cap.meta.get("order", range(len(cap.channels))) if not cap.channels[i].hidden]
            pat = parse_pattern(p.get("pattern", ""), order)
            edge = None
            if p.get("edge_channel") not in (None, ""):
                edge = {"channel": p["edge_channel"], "kind": p.get("edge", "rising")}
            hit = cap.find_pattern(pat, frm, direction, edge)
            return {"found": hit is not None, "index": hit, "t": cap.t(hit) if hit is not None else None}
        if kind == "decoded":
            q = str(p.get("text", "")).strip()
            if not q:
                raise ValueError("type the decoded value to look for")
            items = self.annotations({"decoder": p.get("decoder", "all"), "q": q, "after" if direction > 0 else "before": frm, "limit": 1, "capture": cap.id})
            if items["pending"] and not items["items"]:
                raise ValueError("the decoders are still running on this capture; search again in a moment")
            it = items["items"][0] if items["items"] else None
            return {"found": it is not None, "index": it["s"] if it else None, "end": it["e"] if it else None, "t": it["t"] if it else None, "item": it}
        raise ValueError("search kind must be edge, pattern or decoded")

    # --- decoders ----------------------------------------------------------------------------------------
    def _resolve(self, cap: LogicCapture, mapping: Dict[str, Any]) -> Dict[str, int]:
        out = {}
        for k, v in (mapping or {}).items():
            if v is None or v == "":
                continue
            if isinstance(v, dict):
                name, idx = v.get("name"), v.get("index")
                hit = next((i for i, c in enumerate(cap.channels) if c.name == name), None)
                if hit is None and idx is not None and 0 <= int(idx) < len(cap.channels):
                    hit = int(idx)
                if hit is None:
                    raise dec.DecodeError(f"channel {name or idx} is not in this capture")
                out[k] = hit
            else:
                out[k] = cap.ch_index(v)
        return out

    def decoder_add(self, p: Dict[str, Any]) -> Dict[str, Any]:
        kind = p.get("type") or p.get("decoder")
        if kind == "sigrok":
            st = sigrok.status()
            if not st["available"]:
                raise Unsupported(st["reason"])
            spec = sigrok.check_spec(str((p.get("options") or {}).get("spec", "")))
        elif kind not in dec.DECODERS:
            raise ValueError(f"unknown decoder {kind!r}; choose one of {', '.join(list(dec.DECODERS) + ['sigrok'])}")
        did = p.get("id") or f"d{next(self._ids)}"
        try:
            cap = self.cap()
        except LookupError:
            cap = None
        mapping = p.get("channels")
        if mapping is None and cap is not None and kind != "sigrok":
            mapping = dec.guess_channels(kind, cap)
        stored = {}
        for k, v in (mapping or {}).items():
            if v is None or v == "":
                continue
            if cap is not None and not isinstance(v, dict):
                i = cap.ch_index(v)
                stored[k] = {"index": i, "name": cap.channels[i].name}
            else:
                stored[k] = v if isinstance(v, dict) else {"index": int(v) if str(v).isdigit() else None, "name": None if str(v).isdigit() else str(v)}
        d = {"id": did, "type": kind, "name": p.get("name") or (dec.DECODERS[kind]["label"] if kind in dec.DECODERS else f"sigrok {spec.split(':')[0]}"), "channels": stored, "options": dict(p.get("options") or {}), "enabled": bool(p.get("enabled", True)), "rev": 0}
        with self._lock:
            old = self.decoders.get(did)
            if old:
                d["rev"] = old["rev"] + 1
            self.decoders[did] = d
        self.publish("la", {"kind": "decoders"})
        if cap is not None:
            r = self.result_or_pending(did, cap)
            if r is None:
                d = dict(d, error=None, meta=None, count=None, pending=True)
            else:
                d = dict(d, error=str(r) if isinstance(r, Exception) else None, meta=None if isinstance(r, Exception) else r.meta, count=None if isinstance(r, Exception) else r.count)
        return d

    def decoder_update(self, did: str, p: Dict[str, Any]) -> Dict[str, Any]:
        if did not in self.decoders:
            raise LookupError(f"no decoder {did}")
        cur = self.decoders[did]
        merged = {"id": did, "type": cur["type"], "name": p.get("name", cur["name"]), "channels": p.get("channels", cur["channels"]), "options": p.get("options", cur["options"]), "enabled": p.get("enabled", cur["enabled"])}
        return self.decoder_add(merged)

    def decoder_delete(self, did: str) -> Dict[str, Any]:
        with self._lock:
            self.decoders.pop(did, None)
        self.publish("la", {"kind": "decoders"})
        return {"decoders": list(self.decoders.values())}

    def result_or_pending(self, did: str, cap: LogicCapture):
        """The cached result, or None while a big capture decodes in the background (a "decoded" event follows)."""
        d = self.decoders[did]
        key = (cap.id, cap.version, did, d["rev"])
        with self._lock:
            hit = self._results.get(key)
            if hit is not None:
                return hit
            if not self.big(cap):
                pass
            elif key in self._pending:
                return None
            else:
                self._pending.add(key)

                def work():
                    try:
                        self.result(did, cap, in_process=True)
                    finally:
                        with self._lock:
                            self._pending.discard(key)
                        self.publish("la", {"kind": "decoded", "decoder": did, "capture": cap.id})
                self._pool.submit(work)
                return None
        return self.result(did, cap)

    def big(self, cap: LogicCapture) -> bool:
        """Whether decoding this capture is long enough to go to the decode worker process."""
        return sum(c.edges.shape[0] for c in cap.channels) >= self.SYNC_DECODE_EDGES

    def _run_decoder(self, cap: LogicCapture, kind: str, mapping: Dict[str, int], options: Dict[str, Any], in_process: bool):
        """One decoder run, in a worker process for big captures: the decoders are Python loops that would hold the GIL for seconds and stall every other API call and the viewer's range queries."""
        if not in_process:
            return dec.decode(cap, kind, mapping, options)
        pool = self._proc_pool()
        if pool is None:
            return dec.decode(cap, kind, mapping, options)
        try:
            return pool.submit(dec.decode, _light_copy(cap, mapping.values()), kind, mapping, options).result()
        except BrokenProcessPool:
            log.warning("the decode worker process died; decoding in Studio's process instead")
            with self._lock:
                self._procs = False
            return dec.decode(cap, kind, mapping, options)

    def _proc_pool(self):
        with self._lock:
            if self._procs is None:  # the PyInstaller bundle's launcher calls multiprocessing.freeze_support(), so spawn works there too
                try:
                    import multiprocessing
                    self._procs = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
                except Exception as e:  # noqa: BLE001
                    log.warning("no decode worker process (%s); decoding in Studio's process", e)
                    self._procs = False
            return self._procs or None

    def result(self, did: str, cap: LogicCapture, in_process: bool = False):
        d = self.decoders[did]
        key = (cap.id, cap.version, did, d["rev"])
        with self._lock:
            hit = self._results.get(key)
        if hit is not None:
            return hit
        try:
            if d["type"] == "sigrok":
                r = self._sigrok_decode(cap, d["options"].get("spec", ""))
            else:
                r = self._run_decoder(cap, d["type"], self._resolve(cap, d["channels"]), d["options"], in_process)
        except (dec.DecodeError, KeyError, ValueError, RuntimeError, Unsupported) as e:
            r = e if not isinstance(e, KeyError) else dec.DecodeError(str(e).strip("'\""))
        with self._lock:
            self._results = {k: v for k, v in self._results.items() if not (k[0] == cap.id and k[2] == did)}
            self._results[key] = r
        return r

    def _sigrok_decode(self, cap: LogicCapture, spec: str) -> dec.Result:
        st = sigrok.status()
        if not st["available"]:
            raise Unsupported(st["reason"])
        res = dec.Result("sigrok")
        with tempfile.TemporaryDirectory() as td:
            tmp = LogicCapture([type(c)(f"D{i}", c.init, c.edges) for i, c in enumerate(cap.channels)], cap.n, cap.samplerate, cap.trigger)
            path = formats.save(tmp, os.path.join(td, "capture"), "sr")
            anns = sigrok.decode(path, spec)
        first = re.search(r"=D(\d+)", spec)
        ch = int(first.group(1)) if first and int(first.group(1)) < len(cap.channels) else None
        for a in anns:
            rid = res.row(a["row"], a["row"], ch)
            res.add(rid, a["s"], a["e"], a["text"], "data")
        res.meta["annotations"] = len(anns)
        return res.finalize()

    def decode_once(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """Run a decoder without adding it to the view; returns its annotations (up to limit) and summary."""
        cap = self.cap(p.get("capture"))
        kind = p.get("type") or p.get("decoder")
        if kind == "sigrok":
            r = self._sigrok_decode(cap, str((p.get("options") or {}).get("spec", "")))
        else:
            mapping = p.get("channels") or dec.guess_channels(kind, cap) if kind in dec.DECODERS else p.get("channels")
            if kind not in dec.DECODERS:
                raise dec.DecodeError(f"unknown decoder {kind!r}; choose one of {', '.join(list(dec.DECODERS) + ['sigrok'])}")
            r = self._run_decoder(cap, kind, self._resolve(cap, mapping or {}), p.get("options") or {}, self.big(cap))
        items = r.items()
        rows = p.get("rows")
        if rows:
            items = [i for i in items if i["row"] in rows]
        limit = int(p.get("limit", 500))
        names = [c.name for c in cap.channels]
        out = [{"t": cap.t(i["s"]), "t_end": cap.t(i["e"]), "s": i["s"], "e": i["e"], "row": i["row"], "channel": names[i["channel"]] if i["channel"] is not None else None, "kind": i["kind"], "text": i["text"], "value": i["value"]} for i in items[:limit]]
        return {"capture": cap.id, "decoder": kind, "count": len(items), "truncated": len(items) > limit, "meta": r.meta, "warnings": r.warnings, "annotations": out}

    def annotations(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """The results table: annotations of one decoder (or all), filtered by text, paged; after/before give the first match after (or last before) a sample."""
        cap = self.cap(p.get("capture"))
        which = p.get("decoder") or "all"
        ids = list(self.decoders) if which == "all" else [which]
        names = [c.name for c in cap.channels]
        rows: List[Dict[str, Any]] = []
        pending: List[str] = []
        for did in ids:
            if did not in self.decoders:
                raise LookupError(f"no decoder {did}")
            d = self.decoders[did]
            if not d.get("enabled", True) and which == "all":
                continue
            r = self.result_or_pending(did, cap)
            if r is None:
                pending.append(did)
                continue
            if isinstance(r, Exception):
                continue
            for it in r.items():
                it["decoder"] = d["name"]
                it["decoder_id"] = did
                rows.append(it)
        if len(ids) > 1:
            rows.sort(key=lambda x: (x["s"], x["e"]))
        q = str(p.get("q") or "").strip().lower()
        if q:
            q2 = q[2:] if q.startswith("0x") else q
            rows = [r for r in rows if q in r["text"].lower() or (q2 and q2 in r["text"].lower()) or q in r["label"].lower() or (isinstance(r["value"], int) and q.startswith("0x") and _hexmatch(q, r["value"]))]
        if p.get("row"):
            rows = [r for r in rows if r["row"] == p["row"] or r["label"] == p["row"]]
        if p.get("kinds"):
            ks = set(p["kinds"])
            rows = [r for r in rows if r["kind"] in ks]
        if p.get("after") is not None:
            a = float(p["after"])
            rows = [r for r in rows if r["s"] > a]
        if p.get("before") is not None:
            b = float(p["before"])
            rows = [r for r in rows if r["s"] < b][::-1]
        total = len(rows)
        off = int(p.get("offset", 0))
        lim = int(p.get("limit", 200))
        page = rows[off:off + lim]
        items = [{"t": cap.t(r["s"]), "t_end": cap.t(r["e"]), "s": r["s"], "e": r["e"], "decoder": r["decoder"], "decoder_id": r["decoder_id"], "row": r["row"], "label": r["label"], "channel": r["channel"], "channel_name": names[r["channel"]] if r["channel"] is not None and r["channel"] < len(names) else "", "kind": r["kind"], "text": r["text"], "value": r["value"] if not isinstance(r["value"], (bytes, bytearray)) else r["value"].hex()} for r in page]
        return {"capture": cap.id, "total": total, "offset": off, "items": items, "pending": pending}

    def annotations_csv(self, which: str = "all", q: str = "") -> bytes:
        res = self.annotations({"decoder": which, "q": q, "limit": 10 ** 9})
        if res["pending"]:
            raise ValueError("the decoders are still running on this capture; export again when they finish")
        buf = io.StringIO()
        formats.annotations_csv(res["items"], buf)
        return buf.getvalue().encode("utf-8")


def _light_copy(cap: LogicCapture, used) -> LogicCapture:
    """The capture as a decode worker needs it: the channels it reads keep their edges, the others are empty placeholders (indices stay the same), no analog rows."""
    keep = {int(i) for i in used}
    out = copy.copy(cap)
    out.channels = [c if i in keep else Channel(c.name, c.init, c.edges[:0]) for i, c in enumerate(cap.channels)]
    out.analog = []
    out.meta = {}
    return out


def _hexmatch(q: str, v: int) -> bool:
    try:
        return int(q, 16) == v
    except ValueError:
        return False


def register_routes(app, session) -> None:
    """Add the /api/la/* routes (called from ``create_app``)."""
    import asyncio

    la = getattr(session, "logic", None)
    if la is None:
        la = LogicService(session)
        session.logic = la

    async def run(fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except HTTPException:
            raise
        except LookupError as e:
            raise HTTPException(status_code=404, detail=f"{type(e).__name__}: {str(e).strip(chr(39))}")
        except Exception as e:  # noqa: BLE001
            log.debug("logic API error", exc_info=True)
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")

    async def body(req: Request) -> Dict[str, Any]:
        try:
            return (await req.json()) or {}
        except Exception:  # noqa: BLE001
            return {}

    @app.get("/api/la")
    async def la_status():
        """Logic analyser state: running job, current capture (channels, sample rate, trigger), the captures in memory, decoders and buses."""
        return await run(la.status)

    @app.get("/api/la/sources")
    async def la_sources():
        """Capture sources with availability and reason (Husky LA, analog input, simulator, sigrok, files), LA trigger and clock choices, the decoders and their options, the scope's ADC and LA clock rates."""
        return await run(la.sources)

    @app.post("/api/la/capture")
    async def la_capture(req: Request):
        """Body: source (native, adc, sim, sigrok), settings {...}, wait (bool), timeout (s). native: group, clk_source, oversampling, downsample, depth, trigger, fire, with_analog. adc: segments, samples, level (auto or volts), hysteresis, fire, invert. sim: samplerate, duration_ms or samples, pretrigger (%), channels, jitter_ns, glitches_per_ms. sigrok: device, samplerate, channels, samples or time_ms, triggers, config. persist keeps a .sr copy on disk."""
        p = await body(req)
        return await run(la.capture, p.get("source", "sim"), p.get("settings") or {}, bool(p.get("wait", False)), float(p.get("timeout", 120)))

    @app.post("/api/la/stop")
    async def la_stop():
        return await run(la.stop)

    @app.get("/api/la/captures")
    async def la_captures():
        return await run(la.captures_list)

    @app.put("/api/la/current")
    async def la_current(req: Request):
        p = await body(req)
        return await run(la.select, p.get("id"))

    @app.delete("/api/la/captures/{cid}")
    async def la_delete(cid: str):
        return await run(la.delete, cid)

    @app.get("/api/la/capture")
    async def la_capture_info(capture: Optional[str] = None):
        return await run(lambda: la.summary(la.cap(capture)))

    @app.post("/api/la/view")
    async def la_view(req: Request):
        """Body: a, b (sample range), px (width in pixels), channels (indices, default the visible ones in display order), buses, decoders (ids, default all), analog. Returns edges or per-pixel summaries per channel and the decoded annotations in range."""
        p = await body(req)
        return await run(la.view, p)

    @app.get("/api/la/channels")
    async def la_channels(capture: Optional[str] = None):
        return await run(la.channels, capture)

    @app.put("/api/la/channels")
    async def la_channels_put(req: Request):
        """Body: channels [{index or name, name, color, hidden}], order [indices], buses [{name, channels, format}], analog [{index, hidden, remove}]."""
        p = await body(req)
        return await run(la.set_channels, p)

    @app.post("/api/la/measure")
    async def la_measure(req: Request):
        """Body: channel (or channels), range as from/to sample indices or t0/t1 seconds from the trigger (default the whole capture). Returns edges, rising, falling, frequency, period, duty cycle and high/low pulse widths."""
        p = await body(req)
        return await run(la.measure, p)

    @app.post("/api/la/search")
    async def la_search(req: Request):
        p = await body(req)
        return await run(la.search, p)

    @app.get("/api/la/decoders")
    async def la_decoders():
        return {"available": dec.registry(), "decoders": list(la.decoders.values()), "sigrok": sigrok.status()}

    @app.post("/api/la/decoders")
    async def la_decoder_add(req: Request):
        """Body: type (uart, spi, i2c, onewire, jtag, swd, can, simpleserial or sigrok), channels {role: index or name} (guessed from channel names when left out), options, name."""
        p = await body(req)
        return await run(la.decoder_add, p)

    @app.put("/api/la/decoders/{did}")
    async def la_decoder_update(did: str, req: Request):
        p = await body(req)
        return await run(la.decoder_update, did, p)

    @app.delete("/api/la/decoders/{did}")
    async def la_decoder_delete(did: str):
        return await run(la.decoder_delete, did)

    @app.post("/api/la/decode")
    async def la_decode(req: Request):
        """Run a decoder once and return its annotations. Body: type, channels, options, limit, rows."""
        p = await body(req)
        return await run(la.decode_once, p)

    @app.get("/api/la/annotations")
    async def la_annotations(decoder: str = "all", q: str = "", offset: int = 0, limit: int = 200, after: Optional[float] = None, before: Optional[float] = None, row: Optional[str] = None):
        return await run(la.annotations, {"decoder": decoder, "q": q, "offset": offset, "limit": limit, "after": after, "before": before, "row": row})

    @app.get("/api/la/annotations.csv")
    async def la_annotations_csv(decoder: str = "all", q: str = ""):
        data = await run(la.annotations_csv, decoder, q)
        return Response(content=data, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="decoded.csv"'})

    @app.post("/api/la/import")
    async def la_import(req: Request):
        """Body: path (a .vcd, .csv or .sr file on the Studio machine), format (optional), samplerate (for CSV files with sample indices or no time column)."""
        p = await body(req)
        return await run(la.import_file, os.path.expanduser(str(p.get("path", ""))), p.get("format"), p.get("samplerate"), p.get("name"))

    @app.post("/api/la/import/upload")
    async def la_import_upload(file: UploadFile, format: Optional[str] = None, samplerate: Optional[float] = None):
        content = await file.read()
        d = os.path.join(la.dir, "imports")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, os.path.basename(file.filename or "capture.vcd"))
        with open(path, "wb") as f:
            f.write(content)
        return await run(la.import_file, path, format, samplerate)

    @app.post("/api/la/export")
    async def la_export(req: Request):
        """Body: format (vcd, csv, sr), path (optional; default the logic/exports folder), channels (optional subset). Returns the written path."""
        p = await body(req)
        path = await run(la.export, str(p.get("format", "vcd")), p.get("path"), p.get("capture"), p.get("channels"))
        return {"path": path, "size": os.path.getsize(path)}

    @app.get("/api/la/download/{fmt}")
    async def la_download(fmt: str):
        path = await run(la.export, fmt)
        return FileResponse(path, filename=os.path.basename(path))

    @app.get("/api/la/files")
    async def la_files():
        return await run(la.files)

    @app.post("/api/la/analog/from_trace")
    async def la_analog_from_trace(req: Request):
        """Body: index (stored trace, default the newest). Adds it as an analog row on the current capture's time base."""
        p = await body(req)
        return await run(la.add_analog_from_trace, int(p.get("index", -1)))

    @app.post("/api/la/analog/to_waveform")
    async def la_analog_to_waveform(req: Request):
        p = await body(req)
        return await run(la.analog_to_waveform, int(p.get("index", 0)))

    @app.get("/api/la/sigrok")
    async def la_sigrok(refresh: bool = False):
        return await run(sigrok.status, refresh)

    @app.get("/api/la/sigrok/scan")
    async def la_sigrok_scan():
        def scan():
            st = sigrok.status()
            if not st["available"]:
                raise Unsupported(st["reason"])
            return {"devices": sigrok.scan()}
        return await run(scan)

    @app.get("/api/la/sigrok/decoders")
    async def la_sigrok_decoders():
        def lst():
            st = sigrok.status()
            if not st["available"]:
                raise Unsupported(st["reason"])
            return {"decoders": sigrok.decoders()}
        return await run(lst)
