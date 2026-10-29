#!/usr/bin/env python3
"""Stress Studio until it breaks: every workload is escalated until it fails, times out or leaves the memory budget, and the last size that worked and the first that did not are recorded.

Workloads: trace store size, simulated capture at the largest trace, CPA (GPU and CPU), trace file export and import, logic analyser captures and decoders, code map builds of large firmware runs, glitch sweep grids, notebook outputs and kernels, WebSocket fan-out, concurrent HTTP clients and settings floods. In-process workloads run one at a time in a fresh interpreter (benchmark.py's pattern) watched by a memory and time budget; server workloads drive a real Studio process.

Hardware workloads (``--hw``, names starting with ``hw_``) drive a real ChipWhisperer through a real Studio process: capture count and rate escalation, the largest ADC trace the scope takes, CPA on real traces (GPU and CPU), logic analyser capture runs at full depth, a settings flood against the USB device, WebSocket fan-out and HTTP concurrency during a real capture, and a long continuous capture watched for leaks. The rig is described by the same environment as tests/hardware: ``CWSTUDIO_HW=husky``, ``CWSTUDIO_HW_TARGET`` (SimpleSerial or SimpleSerial2), ``CWSTUDIO_HW_PLATFORM`` (default CW308_SAM4S), ``CWSTUDIO_HW_PROGRAMMER`` (default SAM4S) and ``CWSTUDIO_HW_HEX`` (the simpleserial-aes hex to program when the target does not answer the AES known-answer test).

    python tools/stress.py                         # every simulator workload (an hour or more; needs a quiet machine)
    python tools/stress.py --hw                    # the hardware workloads too (several hours)
    python tools/stress.py --only cpa,hw_capture   # some workloads
    python tools/stress.py --budget-gb 12 --step-timeout 900
    python tools/stress.py --report build/stress/results.json   # only render the Markdown

Linux only. Results go to build/stress/results.json and results.md.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from benchmark import MB, KEY, Phase, Server, environment, fill_store, free_port, pct, rss, ws_client  # noqa: E402

ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
GB = 1024 * MB
ELFS = "/tmp/cwstudio-codemap-elfs"


# ----------------------------------------------------------------------------
# in-process workloads: each call is one step at one size; raise to fail
# ----------------------------------------------------------------------------
def _session(max_traces=None):
    from cwstudio.session import Session
    d = tempfile.mkdtemp(prefix="cwstress-")
    sess = Session(simulate=True, data_dir=d)
    if max_traces:
        sess.store.max_traces = max_traces
    return sess, d


def st_store(gb: float, samples: int = 5000):
    """Fill the trace store with ``gb`` gigabytes of traces, then run the whole-set statistics and the matrix view."""
    sess, d = _session(max_traces=10_000_000)
    n = int(gb * GB / (samples * 4))
    try:
        with Phase() as p:
            fill_store(sess.store, n, samples)
        with Phase() as q:
            sess.store.stats()
        with Phase() as m:
            W, *_ = sess.store.as_arrays()
        return {"traces": n, "samples": samples, "data_gb": gb, "fill_s": p.seconds, "stats_s": q.seconds, "matrix_s": m.seconds, "peak_extra_gb": max(p.peak_extra, q.peak_extra, m.peak_extra) / GB, "rss_gb": rss() / GB}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_capture(count: int, samples: int = 131070):
    """A simulated capture of ``count`` traces at the largest trace the ADC allows, stored."""
    sess, d = _session(max_traces=count + 10)
    try:
        sess.connect_scope("sim")
        sess.connect_target("sim")
        sess.set_scope_setting("adc.samples", samples)
        with Phase() as p:
            sess.start_capture({"count": count, "clear": True, "store": True})
            while sess.worker.long_job is not None and not sess.worker.long_job.finished.is_set():
                time.sleep(0.02)
        job = sess.last_job
        if job.error:
            raise RuntimeError(job.error)
        if len(sess.store) != count:
            raise RuntimeError(f"stored {len(sess.store)} of {count}")
        return {"count": count, "samples": samples, "seconds": p.seconds, "traces_per_s": count / p.seconds, "mb_per_s": count * samples * 4 / MB / p.seconds, "peak_extra_gb": p.peak_extra / GB}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_cpa(n: int, s: int, gpu: bool = True):
    """CPA on ``n`` traces of ``s`` samples through the session; fails when the key is wrong or the attack errors."""
    os.environ["CWSTUDIO_GPU"] = "1" if gpu else "0"
    sess, d = _session(max_traces=n + 10)
    try:
        fill_store(sess.store, n, s)
        with Phase() as p:
            sess.start_cpa({"model": "sbox_hw", "report_every": 50})
            while not sess.cpa.result.done:
                time.sleep(0.05)
        r = sess.cpa.result.to_json()
        if r.get("error"):
            raise RuntimeError(r["error"])
        if r.get("best_key") != KEY.hex():
            raise RuntimeError(f"wrong key {r.get('best_key')}")
        return {"traces": n, "samples": s, "backend": r.get("backend"), "seconds": p.seconds, "traces_per_s": n / p.seconds, "peak_extra_gb": p.peak_extra / GB, "reports": len(r.get("history", []))}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_files(gb: float, fmt: str = "npz", samples: int = 5000):
    """Export ``gb`` gigabytes of traces to a file and import them back."""
    sess, d = _session(max_traces=10_000_000)
    n = int(gb * GB / (samples * 4))
    try:
        fill_store(sess.store, n, samples)
        path = os.path.join(d, f"traces.{fmt}")
        with Phase() as e:
            sess.export_traces(path, fmt)
        size = os.path.getsize(path)
        with Phase() as i:
            got = sess.import_traces(path, replace=True)
        if got != n:
            raise RuntimeError(f"imported {got} of {n}")
        return {"traces": n, "samples": samples, "data_gb": gb, "format": fmt, "file_gb": size / GB, "export_s": e.seconds, "import_s": i.seconds, "peak_extra_gb": max(e.peak_extra, i.peak_extra) / GB}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_logic(duration_ms: float, samplerate: float = 100e6):
    """A simulated logic capture of ``duration_ms`` at ``samplerate`` (all demo channels), then the UART and SPI decoders, the annotation list and a VCD export."""
    sess, d = _session()
    try:
        sess.connect_scope("sim")
        sess.connect_target("sim")
        from cwstudio.logic.service import LogicService
        la = LogicService(sess)
        with Phase() as c:
            res = la.capture("sim", {"samplerate": samplerate, "duration_ms": duration_ms}, wait=True, timeout=3600)
        cid = res.get("id") or res.get("capture", {}).get("id")
        n = int(res.get("samples") or res.get("capture", {}).get("samples") or 0)
        with Phase() as u:
            dec = la.decode_once({"type": "uart", "limit": 100})
        with Phase() as s:
            la.decode_once({"type": "spi", "limit": 100})
        with Phase() as x:
            out = la.export("vcd", os.path.join(d, "x.vcd"))
        return {"duration_ms": duration_ms, "samplerate": samplerate, "samples": n, "capture_s": c.seconds, "uart_s": u.seconds, "uart_rows": dec.get("count", dec.get("total")) if isinstance(dec, dict) else None, "spi_s": s.seconds, "vcd_s": x.seconds, "vcd_gb": os.path.getsize(out) / GB, "peak_extra_gb": max(c.peak_extra, u.peak_extra, s.peak_extra, x.peak_extra) / GB, "capture_id": cid}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_glitch(points: int):
    """A glitch sweep of ``points`` points on the simulator (width x ext_offset grid, one repeat)."""
    import math
    sess, d = _session()
    try:
        sess.connect_scope("sim")
        sess.connect_target("sim")
        side = max(1, int(math.sqrt(points)))
        params = {"parameters": [{"path": "glitch.width", "start": 1, "stop": 49, "step": 48 / max(1, side - 1)}, {"path": "glitch.ext_offset", "start": 0, "stop": max(1, side - 1), "step": 1}], "repeats": 1, "display": False}
        with Phase() as p:
            sess.start_glitch(params)
            while sess.worker.long_job is not None and not sess.worker.long_job.finished.is_set():
                time.sleep(0.05)
        job = sess.last_glitch
        if job.error:
            raise RuntimeError(job.error)
        r = sess.glitch_results()
        done = r.get("done") or r.get("points") or len(r.get("results", []))
        return {"points": side * side, "seconds": p.seconds, "points_per_s": side * side / p.seconds, "results": done, "peak_extra_gb": p.peak_extra / GB}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_notebook_output(chars: int):
    """One cell printing ``chars`` characters, then one returning a list of ``chars // 10`` numbers (the repr is capped at 1 MB)."""
    sess, d = _session()
    try:
        k = sess.kernels.get("stress")
        with Phase() as p:
            r = k.execute_wait(f"print('x' * {chars})", timeout=1800)
        if not r.get("ok"):
            raise RuntimeError(f"print cell failed: {r}")
        out = sum(len(o.get("text", "") or "") for o in r.get("outputs", []))
        with Phase() as q:
            r2 = k.execute_wait(f"list(range({chars // 10}))", timeout=1800)
        if not r2.get("ok"):
            raise RuntimeError(f"result cell failed: {r2}")
        return {"chars": chars, "print_s": p.seconds, "stdout_chars": out, "result_s": q.seconds, "peak_extra_gb": max(p.peak_extra, q.peak_extra) / GB}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_kernels(count: int):
    """``count`` notebook kernels each running one cell, then all shut down."""
    sess, d = _session()
    try:
        with Phase() as p:
            for i in range(count):
                r = sess.kernels.get(f"k{i}").execute_wait("a = 1 + 1", timeout=600)
                if not r.get("ok"):
                    raise RuntimeError(f"kernel {i} failed: {r}")
        with Phase() as q:
            sess.kernels.close()
        return {"kernels": count, "seconds": p.seconds, "per_kernel_ms": p.seconds / count * 1000, "shutdown_s": q.seconds, "peak_extra_gb": p.peak_extra / GB, "rss_gb": rss() / GB}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


def st_settings(writes: int):
    """``writes`` scope setting writes and reads of the whole settings tree through the hardware thread."""
    sess, d = _session()
    try:
        sess.connect_scope("sim")
        with Phase() as p:
            for i in range(writes):
                sess.set_scope_setting("adc.samples", 1000 + (i % 1000))
                if i % 100 == 0:
                    sess.scope_settings()
        return {"writes": writes, "seconds": p.seconds, "writes_per_s": writes / p.seconds}
    finally:
        sess.close()
        shutil.rmtree(d, ignore_errors=True)


INPROC = {"store": st_store, "capture": st_capture, "cpa": st_cpa, "files": st_files, "logic": st_logic, "glitch": st_glitch, "notebook_output": st_notebook_output, "kernels": st_kernels, "settings": st_settings}


# ----------------------------------------------------------------------------
# server workloads: a real Studio process
# ----------------------------------------------------------------------------
def sv_fanout(clients: int, count: int = 300, samples: int = 24400):
    """``clients`` WebSocket clients open while a capture streams traces; fails when the capture slows below a tenth of the single-client rate or a client cannot connect."""
    srv = Server()
    try:
        srv.call("PUT", "/api/scope/settings", {"path": "adc.samples", "value": samples})
        stop, counter, errors = threading.Event(), [0] * clients, []

        def client(i):
            try:
                ws_client(srv.base.replace("http", "ws") + "/ws", stop, counter, i)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
        threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(clients)]
        for t in threads:
            t.start()
        time.sleep(1 + clients / 200)
        if errors:
            raise RuntimeError(f"{len(errors)} clients failed: {errors[0]}")
        t0 = time.time()
        srv.capture(count, clear=True, display_rate=1000)
        el = time.time() - t0
        time.sleep(1)
        stop.set()
        for t in threads:
            t.join(5)
        return {"clients": clients, "count": count, "samples": samples, "traces_per_s": count / el, "frames_per_client": sum(counter) / clients, "min_frames": min(counter), "server_rss_gb": srv.rss() / GB, "errors": len(errors)}
    finally:
        srv.close()


def sv_http(workers: int, seconds: float = 15.0):
    """``workers`` clients hammering the API concurrently during a continuous capture; fails on any HTTP error or a p99 over 5 s."""
    srv = Server()
    try:
        srv.capture(500, clear=True)
        srv.call("POST", "/api/capture/start", {"count": 0, "store": False})
        paths = ["/api/status", "/api/traces/250", "/api/traces/block?start=0&end=200", "/api/traces/stats", "/api/scope/settings", "/api/capabilities", "/api/pinout", "/api/meta"]
        lat, errors = [], []
        end = time.time() + seconds

        def worker(i):
            j = i
            while time.time() < end:
                p = paths[j % len(paths)]
                j += 1
                t = time.perf_counter()
                try:
                    srv.call("GET", p, timeout=30)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{p}: {type(e).__name__}: {e}")
                    if len(errors) > 50:
                        return
                lat.append((time.perf_counter() - t) * 1000)
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(worker, range(workers)))
        srv.call("POST", "/api/capture/stop", {})
        srv.idle()
        res = {"workers": workers, "seconds": seconds, "requests": len(lat), "req_per_s": len(lat) / seconds, "p50_ms": pct(lat, 50), "p99_ms": pct(lat, 99), "max_ms": max(lat) if lat else 0, "errors": len(errors), "first_error": errors[0] if errors else None, "server_rss_gb": srv.rss() / GB}
        if errors:
            raise StepFailed(res, f"{len(errors)} failed requests: {errors[0]}")
        if res["p99_ms"] > 5000:
            raise StepFailed(res, f"p99 {res['p99_ms']:.0f} ms")
        return res
    finally:
        srv.close()


def sv_codemap(max_instructions: int, elf: str = "simpleserial-rsa-CWLITEARM-gcc-default-default.elf"):
    """Emulate up to ``max_instructions`` of the RSA firmware's decrypt command, map it on a captured trace and query the timeline."""
    path = os.path.join(ELFS, elf)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path}: build the test firmware first (tests/fwbuild.py)")
    srv = Server()
    try:
        srv.call("POST", "/api/target/program", {"programmer": "STM32F", "path": path})
        srv.call("PUT", "/api/scope/settings", {"path": "adc.samples", "value": 131070})
        srv.capture(3, clear=True)
        t = time.perf_counter()
        r = srv.call("POST", "/api/codemap/build", {"elf": path, "cmd": "t", "text": "", "align": True, "options": {"max_instructions": max_instructions}}, timeout=3600)
        build_s = time.perf_counter() - t
        if r.get("error"):
            raise RuntimeError(r["error"])
        t = time.perf_counter()
        for i in range(50):
            srv.call("GET", f"/api/codemap/at?sample={i * 2000}")
        at_ms = (time.perf_counter() - t) / 50 * 1000
        return {"max_instructions": max_instructions, "instructions": r.get("instructions") or r.get("run", {}).get("instructions"), "build_s": build_s, "at_ms": at_ms, "server_rss_gb": srv.rss() / GB, "align": r.get("align")}
    finally:
        srv.close()


# ----------------------------------------------------------------------------
# hardware workloads: a real Studio process driving a real ChipWhisperer
# ----------------------------------------------------------------------------
HW_KEY = KEY.hex()
HW_PT = "6bc1bee22e409f96e93d7e117393172a"
HW_CT = "3ad77bb40d7a3660a89ecaf32466ef97"  # NIST ECB vector under KEY
HW_DEFAULT_HEX = os.path.join(os.path.dirname(ROOT), ".studio-dev", "hw-data", "firmware", "builds", "simpleserial-aes-CW308_SAM4S-gcc.hex")


def hw_config() -> dict:
    return {"scope": os.environ.get("CWSTUDIO_HW", "husky").strip().lower() or "husky", "sn": os.environ.get("CWSTUDIO_HW_SN") or None,
            "target": os.environ.get("CWSTUDIO_HW_TARGET") or "SimpleSerial2", "platform": os.environ.get("CWSTUDIO_HW_PLATFORM") or "CW308_SAM4S",
            "programmer": os.environ.get("CWSTUDIO_HW_PROGRAMMER") or "SAM4S", "hex": os.environ.get("CWSTUDIO_HW_HEX") or HW_DEFAULT_HEX,
            "clkgen": float(os.environ.get("CWSTUDIO_HW_CLKGEN") or 7.37e6), "adc_mul": int(os.environ.get("CWSTUDIO_HW_ADC_MUL") or 4), "gain": float(os.environ.get("CWSTUDIO_HW_GAIN") or 25)}


class HwServer(Server):
    """A real Studio process connected to the real scope and target; the target is programmed with simpleserial-aes only when it fails the AES known-answer test."""

    def __init__(self, samples: int = 5000, gpu: bool = True):
        self.cfg = hw_config()
        self.port = free_port()
        self.dir = tempfile.mkdtemp(prefix="cwstress-hw-")
        self.proc = subprocess.Popen([sys.executable, "-m", "cwstudio", "--no-browser", "--port", str(self.port), "--data-dir", self.dir, "--log-level", "warning"], env=dict(os.environ, PYTHONPATH=SRC, CWSTUDIO_GPU="1" if gpu else "0"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(300):
            try:
                urllib.request.urlopen(self.base + "/api/status", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        t = time.perf_counter()
        info = self.call("POST", "/api/scope/connect", {"kind": self.cfg["scope"], "sn": self.cfg["sn"]})
        self.connect_s = time.perf_counter() - t
        if not info.get("connected") or info.get("simulated"):
            raise RuntimeError(f"no real scope: {info}")
        self.scope_info = info
        self.call("POST", "/api/target/connect", {"kind": self.cfg["target"]})
        self.programmed = False
        if not self.kat():
            if not os.path.isfile(self.cfg["hex"]):
                raise FileNotFoundError(f"target fails the AES known-answer test and there is no hex to program at {self.cfg['hex']} (CWSTUDIO_HW_HEX)")
            r = self.call("POST", "/api/firmware/program", {"path": self.cfg["hex"], "programmer": self.cfg["programmer"]})
            if not r.get("ok"):
                raise RuntimeError(f"programming failed: {r}")
            self.programmed = True
            self.call("POST", "/api/target/connect", {"kind": self.cfg["target"]})
            if not self.kat():
                raise RuntimeError("target still fails the AES known-answer test after programming")
        for path, v in (("clock.clkgen_freq", self.cfg["clkgen"]), ("clock.adc_mul", self.cfg["adc_mul"]), ("gain.db", self.cfg["gain"]), ("adc.samples", samples)):
            self.set(path, v)

    def kat(self) -> bool:
        try:
            self.call("POST", "/api/target/simpleserial", {"cmd": "k", "data": HW_KEY, "read_cmd": "e", "read_len": 1})
            return self.call("POST", "/api/target/simpleserial", {"cmd": "p", "data": HW_PT}).get("response") == HW_CT
        except Exception:  # noqa: BLE001
            return False

    def set(self, path, value):
        return self.call("PUT", "/api/scope/settings", {"path": path, "value": value})

    def get(self, path):
        node = _find_setting(self.call("GET", "/api/scope/settings"), path)
        return node.get("value") if node else None

    def status(self):
        return self.call("GET", "/api/status")

    def capture(self, count, **kw):
        """A fixed-key random-text capture of ``count`` traces; returns the finished job and raises on a capture error or dropped traces."""
        self.call("POST", "/api/capture/start", {"count": count, "key": HW_KEY, "key_mode": "fixed", "text_mode": "random", **kw})
        self.idle(timeout=3600)
        st = self.status()
        job = st["job"]
        if job.get("error"):
            raise RuntimeError(f"capture error: {job['error']}")
        if kw.get("store", True) and kw.get("clear") and st["traces"]["count"] != count:
            raise RuntimeError(f"stored {st['traces']['count']} of {count}")
        return job

    def close(self):
        try:
            self.call("POST", "/api/target/disconnect", {})
            self.call("POST", "/api/scope/disconnect", {})
        except Exception:  # noqa: BLE001
            pass
        super().close()


def _find_setting(tree, want):
    if isinstance(tree, dict):
        if tree.get("path") == want:
            return tree
        for v in tree.values():
            r = _find_setting(v, want)
            if r:
                return r
    elif isinstance(tree, list):
        for v in tree:
            r = _find_setting(v, want)
            if r:
                return r
    return None


def _hw_common(srv: HwServer) -> dict:
    return {"scope": srv.scope_info.get("type"), "connect_s": srv.connect_s, "programmed": srv.programmed, "server_rss_gb": srv.rss() / GB}


def hw_capture(count: int, samples: int = 5000):
    """``count`` real traces of ``samples`` samples stored; fails on a capture error, a dropped trace or a trace with no signal."""
    srv = HwServer(samples)
    try:
        t = time.perf_counter()
        job = srv.capture(count, clear=True, store=True)
        el = time.perf_counter() - t
        t = time.perf_counter()
        srv.call("GET", "/api/traces/stats")
        stats_s = time.perf_counter() - t
        return {"count": count, "samples": samples, "seconds": el, "traces_per_s": count / el, "timeouts": job.get("timeouts"), "job_rate": job.get("rate"), "memory_mb": srv.status()["traces"].get("memory_bytes", 0) / MB, "stats_s": stats_s, **_hw_common(srv)}
    finally:
        srv.close()


def hw_samples(samples: int, count: int = 20):
    """``count`` real traces at ``samples`` ADC samples per trace; fails when the scope refuses the setting, clamps it or the capture errors."""
    srv = HwServer(5000)
    try:
        t = time.perf_counter()
        try:
            srv.set("adc.samples", samples)
        except urllib.error.HTTPError as e:
            raise StepFailed({"samples": samples, "requested": samples, "actual": srv.get("adc.samples"), **_hw_common(srv)}, f"setting refused: HTTP {e.code} {e.read().decode(errors='replace')[:200]}") from e
        actual = srv.get("adc.samples")
        if actual != samples:
            raise StepFailed({"samples": samples, "requested": samples, "actual": actual, **_hw_common(srv)}, f"scope clamped adc.samples to {actual}")
        job = srv.capture(count, clear=True, store=True)
        el = time.perf_counter() - t
        st = srv.status()
        got = st["traces"]["samples"]
        if got != samples:
            raise StepFailed({"samples": samples, "actual": actual, "trace_samples": got, **_hw_common(srv)}, f"traces have {got} samples, not {samples}")
        return {"samples": samples, "actual": actual, "count": count, "seconds": el, "traces_per_s": count / el, "mb_per_s": count * samples * 4 / MB / el, "timeouts": job.get("timeouts"), "bits_per_sample": srv.get("adc.bits_per_sample"), **_hw_common(srv)}
    finally:
        srv.close()


def hw_cpa(traces: int, samples: int = 5000):
    """``traces`` real traces captured, then CPA on the GPU through Studio and on the CPU in this process from the exported file; fails when either misses the key (fewer than 14 bytes at PGE 0)."""
    os.environ["CWSTUDIO_GPU"] = "0"  # this process does the CPU attack; the server gets the GPU explicitly
    srv = HwServer(samples, gpu=True)
    try:
        t = time.perf_counter()
        job = srv.capture(traces, clear=True, store=True)
        cap_s = time.perf_counter() - t
        t = time.perf_counter()
        srv.call("POST", "/api/analysis/cpa/start", {"model": "sbox_hw", "report_every": 250})
        while True:
            res = srv.call("GET", "/api/analysis/cpa")
            if res.get("done"):
                break
            if time.perf_counter() - t > 3600:
                raise RuntimeError("CPA did not finish in an hour")
            time.sleep(0.2)
        gpu_s = time.perf_counter() - t
        if res.get("error"):
            raise RuntimeError(f"CPA error: {res['error']}")
        pge = [b["pge"] for b in res["bytes"]]
        gpu_ok = sum(p == 0 for p in pge)
        path = srv.call("POST", "/api/traces/export", {"path": "stress/traces", "format": "npz"})["path"]
        out = {"traces": traces, "samples": samples, "capture_s": cap_s, "traces_per_s": traces / cap_s, "timeouts": job.get("timeouts"), "backend": res.get("backend"), "gpu_s": gpu_s, "gpu_bytes_pge0": gpu_ok, "gpu_key": res.get("best_key"), "pge": pge, **_hw_common(srv)}
        from cwstudio.session import Session
        d = tempfile.mkdtemp(prefix="cwstress-cpu-")
        sess = Session(simulate=True, data_dir=d)
        try:
            sess.store.max_traces = traces + 10
            sess.import_traces(path, replace=True)
            with Phase() as p:
                sess.start_cpa({"model": "sbox_hw", "report_every": 250})
                while not sess.cpa.result.done:
                    time.sleep(0.1)
            r = sess.cpa.result.to_json()
            out.update(cpu_backend=r.get("backend"), cpu_s=p.seconds, cpu_bytes_pge0=sum(b["pge"] == 0 for b in r["bytes"]), cpu_key=r.get("best_key"), peak_extra_gb=p.peak_extra / GB)
        finally:
            sess.close()
            shutil.rmtree(d, ignore_errors=True)
        if gpu_ok < 14 or out["cpu_bytes_pge0"] < 14:
            raise StepFailed(out, f"key not recovered: GPU {gpu_ok}/16, CPU {out['cpu_bytes_pge0']}/16 bytes at PGE 0")
        return out
    finally:
        srv.close()


def hw_logic(captures: int, downsample: int = 1):
    """``captures`` logic analyser captures in a row at the full depth and rate (downsample 1, the ADC armed with 20 analog segments), then the UART decoder on the last; fails on a capture error, a timeout or a capture with no edges."""
    srv = HwServer(5000)
    try:
        cap = srv.call("GET", "/api/capabilities")["logic_analyzer"]["native"]
        if not cap.get("available"):
            raise RuntimeError(f"no native logic analyser: {cap.get('reason')}")
        depth = int(cap.get("depth") or 0)
        samples = edges = 0
        t = time.perf_counter()
        for i in range(captures):
            r = srv.call("POST", "/api/la/capture", {"source": "native", "settings": {"group": "CW 20-pin", "trigger": "capture", "fire": "simpleserial", "depth": depth, "downsample": downsample, "with_analog": True, "segments": 20}, "wait": True, "timeout": 60}, timeout=120)
            if not r.get("ok"):
                raise StepFailed({"captures": i, "error": r.get("error"), **_hw_common(srv)}, f"capture {i} failed: {r.get('error') or r}")
            if not r.get("edges"):
                raise StepFailed({"captures": i, **_hw_common(srv)}, f"capture {i} has no edges")
            samples, edges = r["samples"], r["edges"]
        el = time.perf_counter() - t
        with_dec = time.perf_counter()
        dec = srv.call("POST", "/api/la/decode", {"type": "uart", "limit": 100})
        dec_s = time.perf_counter() - with_dec
        return {"captures": captures, "depth": depth, "samples": samples, "edges": edges, "downsample": downsample, "seconds": el, "captures_per_s": captures / el, "uart_rows": dec.get("count", dec.get("total")) if isinstance(dec, dict) else None, "uart_s": dec_s, **_hw_common(srv)}
    finally:
        srv.close()


def hw_settings(writes: int):
    """``writes`` scope setting writes to the real device (adc.samples alternating, gain.db every tenth) with the whole settings tree read every 100; fails on any error."""
    srv = HwServer(5000)
    try:
        lat = []
        t = time.perf_counter()
        for i in range(writes):
            t0 = time.perf_counter()
            srv.set("adc.samples", 1000 + (i % 1000))
            if i % 10 == 0:
                srv.set("gain.db", 20 + (i // 10) % 10)
            if i % 100 == 0:
                srv.call("GET", "/api/scope/settings")
            lat.append((time.perf_counter() - t0) * 1000)
        el = time.perf_counter() - t
        if srv.get("adc.samples") != 1000 + ((writes - 1) % 1000):
            raise RuntimeError("the last write did not stick")
        job = srv.capture(3, clear=True, store=True)
        return {"writes": writes, "seconds": el, "writes_per_s": writes / el, "p50_ms": pct(lat, 50), "p99_ms": pct(lat, 99), "capture_after_ok": job.get("done") == 3, **_hw_common(srv)}
    finally:
        srv.close()


def hw_fanout(clients: int, count: int = 200, samples: int = 5000):
    """``clients`` WebSocket clients open while ``count`` real traces stream; fails when a client cannot connect, a trace is dropped or the capture runs slower than half the single-client rate (recorded from the clients=1 step in HW_FANOUT_BASE)."""
    srv = HwServer(samples)
    try:
        stop, counter, errors = threading.Event(), [0] * clients, []

        def client(i):
            try:
                ws_client(srv.base.replace("http", "ws") + "/ws", stop, counter, i)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
        threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(clients)]
        for th in threads:
            th.start()
        time.sleep(1 + clients / 200)
        if errors:
            raise RuntimeError(f"{len(errors)} clients failed: {errors[0]}")
        t0 = time.time()
        job = srv.capture(count, clear=True, display_rate=1000)
        el = time.time() - t0
        time.sleep(1)
        stop.set()
        for th in threads:
            th.join(5)
        res = {"clients": clients, "count": count, "samples": samples, "traces_per_s": count / el, "timeouts": job.get("timeouts"), "frames_per_client": sum(counter) / clients, "min_frames": min(counter), "errors": len(errors), **_hw_common(srv)}
        base = float(os.environ.get("CWSTRESS_HW_FANOUT_BASE") or 0)
        if base and res["traces_per_s"] < base / 2:
            raise StepFailed(res, f"capture slowed to {res['traces_per_s']:.1f} traces/s from {base:.1f}")
        return res
    finally:
        srv.close()


def hw_http(workers: int, seconds: float = 15.0):
    """``workers`` clients hammering the API during a continuous real capture; fails on any HTTP error, a capture error or a p99 over 5 s."""
    srv = HwServer(5000)
    try:
        srv.capture(200, clear=True)
        srv.call("POST", "/api/capture/start", {"count": 0, "store": False, "key": HW_KEY, "max_timeouts": 50})
        paths = ["/api/status", "/api/traces/100", "/api/traces/block?start=0&end=100", "/api/traces/stats", "/api/scope/settings", "/api/capabilities", "/api/pinout", "/api/meta"]
        lat, errors = [], []
        end = time.time() + seconds

        def worker(i):
            j = i
            while time.time() < end:
                p = paths[j % len(paths)]
                j += 1
                t = time.perf_counter()
                try:
                    srv.call("GET", p, timeout=30)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{p}: {type(e).__name__}: {e}")
                    if len(errors) > 50:
                        return
                lat.append((time.perf_counter() - t) * 1000)
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(worker, range(workers)))
        job = srv.status()["job"] or {}
        srv.call("POST", "/api/capture/stop", {})
        srv.idle()
        res = {"workers": workers, "seconds": seconds, "requests": len(lat), "req_per_s": len(lat) / seconds, "p50_ms": pct(lat, 50), "p99_ms": pct(lat, 99), "max_ms": max(lat) if lat else 0, "errors": len(errors), "first_error": errors[0] if errors else None, "capture_done": job.get("done"), "capture_rate": job.get("rate"), "capture_timeouts": job.get("timeouts"), "capture_error": job.get("error"), **_hw_common(srv)}
        if errors:
            raise StepFailed(res, f"{len(errors)} failed requests: {errors[0]}")
        if job.get("error"):
            raise StepFailed(res, f"capture error under load: {job['error']}")
        if res["p99_ms"] > 5000:
            raise StepFailed(res, f"p99 {res['p99_ms']:.0f} ms")
        return res
    finally:
        srv.close()


def hw_soak(minutes: float):
    """A continuous real capture (traces streamed, not stored) for ``minutes`` with the server RSS sampled every 5 s; fails on a capture error, more than 1 percent timeouts or RSS growing over 50 MB after the first quarter."""
    srv = HwServer(5000)
    try:
        srv.call("POST", "/api/capture/start", {"count": 0, "store": False, "key": HW_KEY, "max_timeouts": 100})
        series = []
        end = time.time() + minutes * 60
        t0 = time.time()
        while time.time() < end:
            series.append((time.time() - t0, srv.rss() / MB))
            job = srv.status()["job"] or {}
            if not job.get("running"):
                raise StepFailed({"minutes": minutes, "done": job.get("done"), "timeouts": job.get("timeouts"), "error": job.get("error"), **_hw_common(srv)}, f"capture stopped after {time.time() - t0:.0f} s: {job.get('error')}")
            time.sleep(5)
        job = srv.status()["job"] or {}
        srv.call("POST", "/api/capture/stop", {})
        srv.idle()
        warm = [m for t, m in series if t >= minutes * 60 / 4]
        growth_mb = (warm[-1] - warm[0]) if len(warm) > 1 else 0.0
        done = job.get("done") or 0
        res = {"minutes": minutes, "traces": done, "traces_per_s": done / (minutes * 60), "timeouts": job.get("timeouts"), "rss_start_mb": series[0][1], "rss_end_mb": series[-1][1], "rss_growth_mb": growth_mb, "error": job.get("error"), **_hw_common(srv)}
        if job.get("error"):
            raise StepFailed(res, f"capture error: {job['error']}")
        if done and (job.get("timeouts") or 0) > done / 100:
            raise StepFailed(res, f"{job.get('timeouts')} timeouts in {done} traces")
        if growth_mb > 50:
            raise StepFailed(res, f"RSS grew {growth_mb:.0f} MB after warm-up")
        return res
    finally:
        srv.close()


HARDWARE = {"hw_capture": hw_capture, "hw_samples": hw_samples, "hw_cpa": hw_cpa, "hw_logic": hw_logic, "hw_settings": hw_settings, "hw_fanout": hw_fanout, "hw_http": hw_http, "hw_soak": hw_soak}


SERVER = {"fanout": sv_fanout, "http": sv_http, "codemap": sv_codemap, **HARDWARE}


class StepFailed(Exception):
    def __init__(self, result, why):
        super().__init__(why)
        self.result = result


# ----------------------------------------------------------------------------
# escalation
# ----------------------------------------------------------------------------
# each workload: the sizes tried in order (stops at the first failure) and the keyword of the size
LADDERS = {
    "store": ("gb", [0.5, 1, 2, 4, 8, 12, 16, 24, 32]),
    "capture": ("count", [100, 500, 1000, 2000, 5000, 10000, 20000]),
    "cpa": ("size", [(2000, 5000), (10000, 5000), (5000, 24400), (20000, 24400), (50000, 24400), (5000, 131070), (20000, 131070), (50000, 131070)]),
    "cpa_cpu": ("size", [(2000, 5000), (10000, 5000), (5000, 24400), (20000, 24400), (5000, 131070), (20000, 131070)]),
    "files": ("gb", [0.25, 0.5, 1, 2, 4, 8]),
    "logic": ("duration_ms", [10, 100, 1000, 5000, 10000, 30000, 60000]),
    "glitch": ("points", [100, 1000, 10000, 40000, 100000, 250000]),
    "notebook_output": ("chars", [100_000, 1_000_000, 10_000_000, 100_000_000, 400_000_000]),
    "kernels": ("count", [10, 50, 200, 500, 1000]),
    "settings": ("writes", [1000, 10000, 50000, 100000]),
    "fanout": ("clients", [1, 10, 50, 100, 200, 400, 800]),
    "http": ("workers", [8, 32, 64, 128, 256, 512]),
    "codemap": ("max_instructions", [10_000_000, 20_000_000, 50_000_000]),  # the RSA decrypt is 9.3M instructions: a lower cap is refused by design, so this measures the build at full size
    # hardware (only with --hw or named in --only)
    "hw_capture": ("count", [100, 500, 1000, 2000, 5000, 10000, 20000, 50000]),
    "hw_samples": ("samples", [5000, 24400, 65536, 100000, 131070, 131124, 140000, 200000, 262144, 500000]),
    "hw_cpa": ("traces", [100, 500, 2000, 5000]),
    "hw_logic": ("captures", [5, 20, 50, 100, 200]),
    "hw_settings": ("writes", [100, 500, 2000, 5000, 10000]),
    "hw_fanout": ("clients", [1, 10, 50, 100, 200, 400]),
    "hw_http": ("workers", [8, 32, 64, 128, 256]),
    "hw_soak": ("minutes", [2, 5, 10, 20, 30]),
}
# workloads whose single steps legitimately run longer than the default step timeout
STEP_TIMEOUTS = {"hw_capture": 3600, "hw_soak": 2400, "hw_cpa": 2400}


def run_step(name: str, size, budget_gb: float, timeout: float) -> dict:
    """One size of one workload in a fresh interpreter, killed when it exceeds the memory budget or the timeout."""
    kw = {"size": size} if name in ("cpa", "cpa_cpu") else {LADDERS[name][0]: size}
    if name == "cpa_cpu":
        kw["gpu"] = False
    cmd = [sys.executable, os.path.abspath(__file__), "--step", name, "--kwargs", json.dumps(kw)]
    timeout = max(timeout, STEP_TIMEOUTS.get(name, 0))
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONPATH=SRC))
    peak = 0
    why = None
    while p.poll() is None:
        try:
            cur = rss(p.pid)
            peak = max(peak, cur)
            if cur > budget_gb * GB:
                why = f"memory budget exceeded ({cur / GB:.1f} GB > {budget_gb} GB)"
        except FileNotFoundError:
            break
        if time.time() - t0 > timeout:
            why = f"timed out after {timeout:.0f} s"
        if why:
            p.kill()
            break
        time.sleep(0.2)
    out, err = p.communicate()
    res = {"size": size, "wall_s": time.time() - t0, "peak_rss_gb": peak / GB}
    if why:
        res.update(ok=False, error=why)
        return res
    if p.returncode != 0:
        lines = (err or out or "").strip().splitlines()
        res.update(ok=False, error=lines[-1] if lines else f"exit {p.returncode}")
        partial = [ln for ln in out.splitlines() if ln.startswith("{")]
        if partial:
            try:
                res["partial"] = json.loads(partial[-1])
            except ValueError:
                pass
        return res
    try:
        res.update(ok=True, **json.loads(out.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        res.update(ok=False, error="no result printed")
    return res


def escalate(name: str, budget_gb: float, timeout: float, log) -> dict:
    key, sizes = LADDERS[name]
    steps = []
    for size in sizes:
        log(f"  {name} {key}={size} ...")
        r = run_step(name, size, budget_gb, timeout)
        steps.append(r)
        log(f"    {'ok' if r['ok'] else 'FAIL'} in {r['wall_s']:.0f} s, peak {r['peak_rss_gb']:.1f} GB" + ("" if r["ok"] else f": {r['error']}"))
        if not r["ok"]:
            break
        if name == "hw_fanout" and size == 1 and r.get("traces_per_s"):
            os.environ["CWSTRESS_HW_FANOUT_BASE"] = str(r["traces_per_s"])  # the later steps compare against the single-client rate
    ok = [s for s in steps if s["ok"]]
    bad = [s for s in steps if not s["ok"]]
    return {"key": key, "steps": steps, "last_ok": ok[-1]["size"] if ok else None, "first_fail": bad[0]["size"] if bad else None, "fail_reason": bad[0]["error"] if bad else None, "exhausted": not bad}


def run_one_step(name: str, kw: dict):
    """Child process entry: run one size and print its JSON result on the last line."""
    try:
        if name in ("cpa", "cpa_cpu"):
            n, s = kw.pop("size")
            res = st_cpa(n, s, **kw)
        elif name in INPROC:
            res = INPROC[name](**kw)
        else:
            res = SERVER[name](**kw)
    except StepFailed as e:
        print(json.dumps(e.result))
        print(f"StepFailed: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(res))


# ----------------------------------------------------------------------------
# report
# ----------------------------------------------------------------------------
def _fmt(v):
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:,.0f}"
    if isinstance(v, (list, tuple)):
        return " x ".join(str(x) for x in v)
    return str(v)


def report(R: dict) -> str:
    env = R.get("environment", {})
    out = [f"# Stress test results ({R.get('date', '')[:10]})", "", f"Studio {env.get('studio', '?')} on {env.get('os', '?')}, Python {env.get('python', '?')}, {env.get('cpu', '?')} ({env.get('cores', '?')} cores), {env.get('ram_gb', '?')} GB RAM" + (f", {R.get('gpu')}" if R.get("gpu") else "") + f". Memory budget per workload {R.get('budget_gb')} GB, step timeout {R.get('step_timeout_s')} s.", "",
           (f"Hardware workloads (hw_*) ran on a real {R['hardware'].get('scope')} scope with a {R['hardware'].get('platform')} target ({R['hardware'].get('target')}). " if R.get("hardware") else "") + "Each workload was escalated until it failed. **Last OK** is the largest size that completed correctly, **First fail** the size at which it broke and why. Workloads marked exhausted completed their largest planned size without failing.", "",
           "| Workload | Size | Last OK | First fail | Why |", "|---|---|---|---|---|"]
    for name, w in R["workloads"].items():
        out.append(f"| {name} | {w['key']} | {_fmt(w['last_ok']) if w['last_ok'] is not None else 'none'} | {'exhausted' if w['exhausted'] else _fmt(w['first_fail'])} | {w['fail_reason'] or ''} |")
    for name, w in R["workloads"].items():
        out += ["", f"## {name}", ""]
        keys = []
        for s in w["steps"]:
            for k in s:
                if k not in keys and k not in ("ok", "error", "partial", "size"):
                    keys.append(k)
        out.append("| " + w["key"] + " | ok | " + " | ".join(keys) + " |")
        out.append("|---|---|" + "---|" * len(keys))
        for s in w["steps"]:
            out.append(f"| {_fmt(s['size'])} | {'yes' if s['ok'] else 'FAIL: ' + str(s.get('error'))} | " + " | ".join(_fmt(s.get(k, "")) if s.get(k) is not None else "" for k in keys) + " |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma separated workloads: " + ", ".join(LADDERS))
    ap.add_argument("--hw", action="store_true", help="include the hardware workloads (a real ChipWhisperer on USB, see CWSTUDIO_HW_* above)")
    ap.add_argument("--budget-gb", type=float, default=16.0, help="kill a step whose RSS passes this (default 16)")
    ap.add_argument("--step-timeout", type=float, default=1200.0, help="seconds per step (default 1200)")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "stress"))
    ap.add_argument("--report", help="render the Markdown from a saved results.json")
    ap.add_argument("--step", help=argparse.SUPPRESS)
    ap.add_argument("--kwargs", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.step:
        run_one_step(a.step, json.loads(a.kwargs or "{}"))
        return 0
    if a.report:
        with open(a.report) as f:
            R = json.load(f)
        print(report(R))
        return 0
    if platform.system() != "Linux":
        print("Linux only (memory is read from /proc)", file=sys.stderr)
        return 2
    os.makedirs(a.out, exist_ok=True)
    names = [n.strip() for n in a.only.split(",")] if a.only else [n for n in LADDERS if a.hw or not n.startswith("hw_")]
    bad = [n for n in names if n not in LADDERS]
    if bad:
        print(f"unknown workload(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    logf = open(os.path.join(a.out, "stress.log"), "a")

    def log(msg):
        line = f"{datetime.datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()
    gpu = None
    try:
        sys.path.insert(0, SRC)
        from cwstudio import accel
        gpu = accel.info().get("label")
    except Exception:  # noqa: BLE001
        pass
    R = {"date": datetime.datetime.now().isoformat(timespec="seconds"), "environment": environment(), "gpu": gpu, "budget_gb": a.budget_gb, "step_timeout_s": a.step_timeout, "workloads": {}}
    if any(n.startswith("hw_") for n in names):
        R["hardware"] = hw_config()
        log(f"hardware rig: {R['hardware']}")
    path = os.path.join(a.out, "results.json")
    for name in names:
        log(f"== {name}")
        try:
            R["workloads"][name] = escalate(name, a.budget_gb, a.step_timeout, log)
        except Exception as e:  # noqa: BLE001
            R["workloads"][name] = {"key": LADDERS[name][0], "steps": [], "last_ok": None, "first_fail": None, "fail_reason": f"harness error: {type(e).__name__}: {e}", "exhausted": False}
            log(f"  harness error: {e}")
        with open(path, "w") as f:
            json.dump(R, f, indent=1)
        with open(os.path.join(a.out, "results.md"), "w") as f:
            f.write(report(R))
    log(f"done: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
