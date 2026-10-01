#!/usr/bin/env python3
"""Stress tests and performance benchmarks for ChipWhisperer Studio.

Measures how Studio behaves with very large trace sets, large trace files, long sessions and many clients, and checks for memory leaks. Every scenario runs in a fresh Python process (or against a fresh Studio server), so memory figures are not polluted by earlier scenarios. Results go to a JSON file and to Markdown tables for the README and the wiki.

    python tools/benchmark.py                  # everything (about 30 minutes)
    python tools/benchmark.py --quick          # smaller sizes and shorter leak runs (a few minutes)
    python tools/benchmark.py --only store,cpa # some groups: store, files, cpa, capture, server, leaks, ui
    python tools/benchmark.py --report build/bench/results.json   # only render the Markdown from saved results
    python tools/benchmark.py --publish build/bench/results.json  # keep the run in docs/benchmarks and rebuild the wiki Performance page and the README section

Linux only (memory is read from /proc). The UI group needs Playwright with Chromium.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
MB = 1024 * 1024
KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")


# ----------------------------------------------------------------------------
# memory helpers (Linux /proc)
# ----------------------------------------------------------------------------
def _status(pid="self"):
    out = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k in ("VmRSS", "VmHWM"):
                out[k] = int(v.split()[0]) * 1024
    return out


def rss(pid="self") -> int:
    return _status(pid)["VmRSS"]


def peak_reset(pid="self"):
    """Reset the peak RSS counter (VmHWM) so the next phase reports its own peak."""
    try:
        with open(f"/proc/{pid}/clear_refs", "w") as f:
            f.write("5")
    except OSError:
        pass


def peak(pid="self") -> int:
    return _status(pid)["VmHWM"]


class Phase:
    """Time a block and record the extra memory it needed at its peak (above the RSS before it)."""

    def __init__(self):
        self.seconds = 0.0
        self.peak_extra = 0

    def __enter__(self):
        import gc
        gc.collect()
        self.base = rss()
        peak_reset()
        self.t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.t
        self.peak_extra = max(0, peak() - self.base)


# ----------------------------------------------------------------------------
# data generators
# ----------------------------------------------------------------------------
def fill_store(store, n: int, s: int, seed: int = 1, leak: bool = True):
    """Append n traces of s samples with a first-round S-box Hamming weight leak, like a real AES capture. Returns the append time."""
    import numpy as np
    from cwstudio.aes import HW, SBOX
    rng = np.random.default_rng(seed)
    hw = np.array([HW[SBOX[x]] for x in range(256)], np.float32) if leak else None
    # Values on a 10-bit ADC grid like a CW-Lite or Nano capture, so file compression behaves as with real traces
    base = (np.round((rng.standard_normal((64, s)) * 0.05 + 0.5) * 1024) / 1024 - 0.5).astype(np.float32)
    pts = rng.integers(0, 256, (n, 16), dtype=np.uint8)
    pos = [min(s - 1, 40 + 25 * b) for b in range(16)]
    t = time.perf_counter()
    for i in range(n):
        w = base[i % 64].copy()  # a new array per trace, as the scope returns
        if leak:
            p = pts[i]
            for b in range(16):
                w[pos[b]] += 0.01 * hw[p[b] ^ KEY[b]]
        store.append(w, p.tobytes() if leak else bytes(16), bytes(16), KEY)
    return time.perf_counter() - t


# ----------------------------------------------------------------------------
# in-process scenarios (each runs in its own subprocess)
# ----------------------------------------------------------------------------
def sc_store(n: int, s: int):
    """Trace store at scale: append rate, memory per trace, summary, statistics, matrix and UI fetches."""
    import numpy as np
    from cwstudio.session import Session
    d = tempfile.mkdtemp(prefix="cwbench-")
    sess = Session(simulate=True, data_dir=d)
    sess.store.max_traces = n + 10
    import gc
    gc.collect()
    base = rss()
    t_append = fill_store(sess.store, n, s, leak=False)
    data = n * s * 4
    held = rss() - base
    out = {"traces": n, "samples": s, "data_mb": data / MB, "append_traces_per_s": n / t_append, "rss_mb": held / MB, "overhead_bytes_per_trace": (held - data) / n}
    t = time.perf_counter()
    for _ in range(1000):
        sess.store.summary()
    out["summary_us"] = (time.perf_counter() - t) / 1000 * 1e6
    with Phase() as p:
        sess.stats_bytes(0, None)
    out.update(stats_s=p.seconds, stats_peak_extra_mb=p.peak_extra / MB)
    with Phase() as p:
        W = sess.store.as_arrays()[0]
    out.update(matrix_s=p.seconds, matrix_peak_extra_mb=p.peak_extra / MB)
    del W
    with Phase() as p:
        for i in range(0, n, max(1, n // 200)):
            sess.trace_bytes(i)
    out["trace_fetch_ms"] = p.seconds / len(range(0, n, max(1, n // 200))) * 1000
    with Phase() as p:
        sess.traces_block(0, min(n, 200), 1)
    out["block_200_ms"] = p.seconds * 1000
    sess.close()
    shutil.rmtree(d, ignore_errors=True)
    del np
    return out


def sc_files(n: int, s: int, fmt: str, folder: str):
    """Export a large trace set to a file and import it back: time, file size and memory."""
    from cwstudio.traces import TraceStore
    store = TraceStore(max_traces=n + 10)
    fill_store(store, n, s, leak=False)
    path = os.path.join(folder, f"bench_{n}x{s}")
    with Phase() as p:
        out_path = store.export(path, fmt)
    files = [out_path] if "*" not in out_path else [out_path.replace("*", k) for k in ("waves", "textins", "textouts", "keys")]
    size = sum(os.path.getsize(f) for f in files)
    res = {"traces": n, "samples": s, "format": fmt, "data_mb": n * s * 4 / MB, "file_mb": size / MB, "export_s": p.seconds, "export_mb_per_s": n * s * 4 / MB / p.seconds, "export_peak_extra_mb": p.peak_extra / MB}
    store.clear()
    import gc
    gc.collect()
    imp = out_path if fmt != "npy" else out_path.replace("*", "waves")
    with Phase() as p:
        got = store.import_file(imp, True)
    res.update(import_s=p.seconds, import_mb_per_s=n * s * 4 / MB / p.seconds, import_peak_extra_mb=p.peak_extra / MB, imported=got)
    for f in files:
        os.remove(f)
    return res


def sc_cpa(n: int, s: int, report_every: int):
    """CPA through the session (as the UI runs it): time, key recovery and memory."""
    from cwstudio.session import Session
    d = tempfile.mkdtemp(prefix="cwbench-")
    sess = Session(simulate=True, data_dir=d)
    sess.store.max_traces = n + 10
    fill_store(sess.store, n, s)
    with Phase() as p:
        sess.start_cpa({"model": "sbox_hw", "report_every": report_every})
        while not sess.cpa.result.done:
            time.sleep(0.05)
    r = sess.cpa.result.to_json()
    sess.close()
    shutil.rmtree(d, ignore_errors=True)
    return {"traces": n, "samples": s, "report_every": report_every, "seconds": p.seconds, "traces_per_s": n / p.seconds, "peak_extra_mb": p.peak_extra / MB, "data_mb": n * s * 4 / MB, "key_ok": r.get("best_key") == KEY.hex(), "error": r.get("error")}


def sc_capture(samples: int, count: int, store: bool):
    """End-to-end simulated capture (scope, target, AES, event publishing, store): traces per second."""
    from cwstudio.session import Session
    d = tempfile.mkdtemp(prefix="cwbench-")
    sess = Session(simulate=True, data_dir=d)
    sess.store.max_traces = count + 10
    sess.connect_scope("sim")
    sess.connect_target("sim")
    sess.set_scope_setting("adc.samples", samples)
    with Phase() as p:
        sess.start_capture({"count": count, "clear": True, "store": store})
        while sess.worker.long_job is not None and not sess.worker.long_job.finished.is_set():
            time.sleep(0.02)
    stored = len(sess.store)
    sess.close()
    shutil.rmtree(d, ignore_errors=True)
    return {"samples": samples, "count": count, "store": store, "seconds": p.seconds, "traces_per_s": count / p.seconds, "mb_per_s": count * samples * 4 / MB / p.seconds, "stored": stored, "peak_extra_mb": p.peak_extra / MB}


SCENARIOS = {"store": sc_store, "files": sc_files, "cpa": sc_cpa, "capture": sc_capture}


def run_isolated(name: str, **kw):
    """Run one scenario in a fresh interpreter and return its JSON result."""
    cmd = [sys.executable, os.path.abspath(__file__), "--scenario", name, "--kwargs", json.dumps(kw)]
    t = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=SRC))
    if p.returncode != 0:
        return {"error": (p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) else f"exit {p.returncode}", **kw}
    res = json.loads(p.stdout.strip().splitlines()[-1])
    res["wall_s"] = time.time() - t
    return res


# ----------------------------------------------------------------------------
# server scenarios: a real Studio process, measured from outside
# ----------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self):
        self.port = free_port()
        self.dir = tempfile.mkdtemp(prefix="cwbench-srv-")
        self.proc = subprocess.Popen([sys.executable, "-m", "cwstudio", "--no-browser", "--simulate", "--port", str(self.port), "--data-dir", self.dir, "--log-level", "warning"], env=dict(os.environ, PYTHONPATH=SRC), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(300):
            try:
                urllib.request.urlopen(self.base + "/api/status", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        self.call("POST", "/api/scope/connect", {"kind": "sim"})
        self.call("POST", "/api/target/connect", {"kind": "sim"})

    def call(self, method, path, body=None, timeout=600):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") and raw else raw

    def rss(self) -> int:
        return rss(self.proc.pid)

    def cpu_s(self) -> float:
        with open(f"/proc/{self.proc.pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")

    def idle(self, timeout=600):
        end = time.time() + timeout
        while time.time() < end:
            if not (self.call("GET", "/api/status").get("job") or {}).get("running"):
                return
            time.sleep(0.1)

    def capture(self, count, **kw):
        self.call("POST", "/api/capture/start", {"count": count, **kw})
        self.idle()

    def close(self):
        try:
            self.call("POST", "/api/shutdown", {})
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))] if xs else 0.0


def ws_client(url, stop, counter, idx):
    from websockets.sync.client import connect
    with connect(url, max_size=None, open_timeout=10) as ws:
        while not stop.is_set():
            try:
                ws.recv(timeout=0.5)
                counter[idx] += 1
            except TimeoutError:
                continue


def sv_latency(traces: int, seconds: float, workers: int):
    """API response times while a continuous capture runs and several clients poll the API."""
    srv = Server()
    try:
        srv.capture(traces, clear=True)
        srv.call("POST", "/api/capture/start", {"count": 0, "store": False})
        paths = {"status": "/api/status", "one trace": f"/api/traces/{traces // 2}", "trace block (200)": "/api/traces/block?start=0&end=200", "mean/min/max": "/api/traces/stats", "settings tree": "/api/scope/settings"}
        lat = {k: [] for k in paths}
        end = time.time() + seconds

        def worker(i):
            keys = list(paths)
            j = i
            while time.time() < end:
                k = keys[j % len(keys)]
                j += 1
                t = time.perf_counter()
                srv.call("GET", paths[k])
                lat[k].append((time.perf_counter() - t) * 1000)
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(worker, range(workers)))
        srv.call("POST", "/api/capture/stop", {})
        srv.idle()
        return {"traces": traces, "workers": workers, "seconds": seconds, "endpoints": {k: {"n": len(v), "p50_ms": pct(v, 50), "p95_ms": pct(v, 95), "p99_ms": pct(v, 99)} for k, v in lat.items()}}
    finally:
        srv.close()


def sv_fanout(clients: int, count: int, samples: int):
    """Capture rate and frames delivered with many browser windows (WebSocket clients) open."""
    srv = Server()
    try:
        srv.call("PUT", "/api/scope/settings", {"path": "adc.samples", "value": samples})
        stop, counter = threading.Event(), [0] * clients
        threads = [threading.Thread(target=ws_client, args=(srv.base.replace("http", "ws") + "/ws", stop, counter, i), daemon=True) for i in range(clients)]
        for t in threads:
            t.start()
        time.sleep(1)
        cpu0, t0 = srv.cpu_s(), time.time()
        srv.capture(count, clear=True)
        el = time.time() - t0
        cpu = srv.cpu_s() - cpu0
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(5)
        return {"clients": clients, "count": count, "samples": samples, "traces_per_s": count / el, "frames_per_client": (sum(counter) / clients) if clients else 0, "server_cpu_pct": cpu / el * 100}
    finally:
        srv.close()


def sample_rss(srv, until, every, series):
    while time.time() < until:
        series.append((time.time(), srv.rss()))
        time.sleep(every)


def growth(series, warm_frac=0.25):
    """RSS change after warm-up, and its slope in MB per minute from a least-squares fit."""
    if len(series) < 4:
        return 0.0, 0.0
    s = series[int(len(series) * warm_frac):]
    t0 = s[0][0]
    xs = [(t - t0) / 60 for t, _ in s]
    ys = [r / MB for _, r in s]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs) or 1
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return ys[-1] - ys[0], slope


def lk_capture(minutes: float):
    """Long continuous capture (not storing traces) with two live windows open."""
    srv = Server()
    try:
        stop, counter = threading.Event(), [0, 0]
        for i in range(2):
            threading.Thread(target=ws_client, args=(srv.base.replace("http", "ws") + "/ws", stop, counter, i), daemon=True).start()
        srv.call("POST", "/api/capture/start", {"count": 0, "store": False})
        series = []
        sample_rss(srv, time.time() + minutes * 60, 5, series)
        done = (srv.call("GET", "/api/status").get("job") or {}).get("done", 0)
        srv.call("POST", "/api/capture/stop", {})
        srv.idle()
        stop.set()
        g, slope = growth(series)
        return {"what": "continuous capture, 2 live windows", "minutes": minutes, "operations": done, "unit": "traces", "start_mb": series[0][1] / MB, "end_mb": series[-1][1] / MB, "growth_after_warmup_mb": g, "slope_mb_per_min": slope}
    finally:
        srv.close()


def _cycles(srv, n, op, what, unit, per=1):
    series = [(time.time(), srv.rss())]
    for i in range(n):
        op(i)
        series.append((time.time(), srv.rss()))
    g, slope = growth(series)
    return {"what": what, "operations": n * per, "unit": unit, "start_mb": series[0][1] / MB, "end_mb": series[-1][1] / MB, "growth_after_warmup_mb": g, "slope_mb_per_min": slope, "max_mb": max(r for _, r in series) / MB}


def lk_ws(cycles: int):
    """Windows opening and closing: WebSocket connect/disconnect cycles."""
    from websockets.sync.client import connect
    srv = Server()
    try:
        srv.call("POST", "/api/capture/start", {"count": 0, "store": False, "max_rate": 200})
        url = srv.base.replace("http", "ws") + "/ws"

        def op(_):
            for _ in range(10):
                with connect(url, max_size=None) as ws:
                    ws.recv(timeout=5)
        r = _cycles(srv, cycles // 10, op, "browser windows opening and closing during a capture", "WebSocket connections", 10)
        time.sleep(1.5)
        r["subscribers_left"] = srv.call("GET", "/api/status")["clients"]
        srv.call("POST", "/api/capture/stop", {})
        return r
    finally:
        srv.close()


def lk_api(requests: int):
    """Many API calls of every common kind."""
    srv = Server()
    try:
        srv.capture(500, clear=True)
        paths = ["/api/status", "/api/traces/10", "/api/traces/block?start=0&end=100", "/api/traces/stats", "/api/scope/settings", "/api/logs", "/api/meta"]

        def op(i):
            for j in range(100):
                srv.call("GET", paths[(i + j) % len(paths)])
        return _cycles(srv, requests // 100, op, "API requests of 7 kinds", "requests", 100)
    finally:
        srv.close()


def lk_clear(cycles: int, traces: int):
    """Capture a trace set, clear it, repeat: memory should return to the same level every time."""
    srv = Server()
    try:
        def op(_):
            srv.capture(traces, clear=True)
            srv.call("DELETE", "/api/traces")
        r = _cycles(srv, cycles, op, f"capture {traces} traces then clear", "cycles")
        return r
    finally:
        srv.close()


def lk_cpa(runs: int, traces: int):
    """Repeated CPA attacks on the same traces."""
    srv = Server()
    try:
        srv.capture(traces, clear=True)

        def op(_):
            srv.call("POST", "/api/analysis/cpa/start", {"model": "sbox_hw", "report_every": 250})
            while not srv.call("GET", "/api/analysis/cpa").get("done"):
                time.sleep(0.05)
        return _cycles(srv, runs, op, f"CPA on {traces} traces", "attacks")
    finally:
        srv.close()


def lk_notebook(cells: int):
    """Notebook cells that capture, compute and draw a matplotlib figure."""
    srv = Server()
    try:
        code = "import chipwhisperer as cw, numpy as np, matplotlib.pyplot as plt\nscope = cw.scope(); target = cw.target(scope)\nfor _ in range(5): cw.capture_trace(scope, target, bytearray(np.random.bytes(16)), bytearray(16))\nplt.plot(np.random.rand(5000)); plt.show()\nx = np.random.rand(200000).sum()"

        def op(_):
            for _ in range(10):
                r = srv.call("POST", "/api/kernel/run", {"code": code})
                assert r["ok"], r
            srv.call("DELETE", "/api/traces")
        return _cycles(srv, cells // 10, op, "notebook cells with a capture and a figure", "cells", 10)
    finally:
        srv.close()


def lk_mcp(calls: int):
    """An AI agent making many MCP tool calls over stdio (measured on the MCP process)."""
    srv = Server()
    try:
        srv.capture(200, clear=True)
        p = subprocess.Popen([sys.executable, "-m", "cwstudio", "mcp", "--url", srv.base, "--no-embed"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONPATH=SRC))
        n = [0]

        def ask(method, params=None):
            n[0] += 1
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            return json.loads(p.stdout.readline())
        ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bench", "version": "1"}})
        tools = [("studio_status", {}), ("trace_get", {"index": 5, "max_points": 500}), ("traces_summary", {}), ("scope_get_settings", {"filter": "adc"}), ("calculate", {"expression": "hw(0x53) ^ 7"})]
        series = [(time.time(), rss(p.pid))]
        t0 = time.time()
        for i in range(calls // 100):
            for j in range(100):
                name, args = tools[(i + j) % len(tools)]
                r = ask("tools/call", {"name": name, "arguments": args})
                assert not r["result"]["isError"], r
            series.append((time.time(), rss(p.pid)))
        el = time.time() - t0
        p.stdin.close()
        p.wait(20)
        g, slope = growth(series)
        return {"what": "MCP tool calls over stdio (5 kinds)", "operations": calls, "unit": "tool calls", "calls_per_s": calls / el, "start_mb": series[0][1] / MB, "end_mb": series[-1][1] / MB, "growth_after_warmup_mb": g, "slope_mb_per_min": slope}
    finally:
        srv.close()


def ui_browser(samples: int, minutes: float):
    """The browser UI during a long live capture: frame rate and JavaScript heap over time (catches frontend leaks)."""
    from playwright.sync_api import sync_playwright
    srv = Server()
    try:
        srv.call("PUT", "/api/scope/settings", {"path": "adc.samples", "value": samples})
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--enable-precise-memory-info"])
            page = b.new_page(viewport={"width": 1600, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(srv.base + "/")
            page.wait_for_timeout(1500)
            page.click('[data-tab="capture"]')
            page.select_option("#wave-toolbar select >> nth=1", "10")
            page.locator("#wave-toolbar label", has_text="mean").locator("input").check()
            cdp = page.context.new_cdp_session(page)
            cdp.send("Performance.enable")
            page.evaluate("""() => { window.__frames = 0; const tick = () => { window.__frames++; requestAnimationFrame(tick); }; requestAnimationFrame(tick); }""")
            srv.call("POST", "/api/capture/start", {"count": 0, "store": False})
            heap, fps = [], []
            end = time.time() + minutes * 60
            last_f, last_t = page.evaluate("window.__frames"), time.time()
            while time.time() < end:
                page.wait_for_timeout(10000)
                cdp.send("HeapProfiler.collectGarbage")
                m = {x["name"]: x["value"] for x in cdp.send("Performance.getMetrics")["metrics"]}
                f, t = page.evaluate("window.__frames"), time.time()
                fps.append((f - last_f) / (t - last_t))
                last_f, last_t = f, t
                heap.append((t, m["JSHeapUsedSize"]))
            srv.call("POST", "/api/capture/stop", {})
            b.close()
        g, slope = growth(heap)
        return {"samples": samples, "minutes": minutes, "fps_median": statistics.median(fps), "fps_min": min(fps), "heap_start_mb": heap[0][1] / MB, "heap_end_mb": heap[-1][1] / MB, "heap_growth_after_warmup_mb": g, "heap_slope_mb_per_min": slope, "page_errors": len(errors), "server_rss_mb": srv.rss() / MB}
    finally:
        srv.close()


# ----------------------------------------------------------------------------
# orchestration and report
# ----------------------------------------------------------------------------
def environment():
    import numpy
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next(line.split(":", 1)[1].strip() for line in f if line.startswith("model name"))
    except (OSError, StopIteration):
        cpu = platform.processor()
    with open("/proc/meminfo") as f:
        mem = int(f.readline().split()[1]) * 1024
    from cwstudio import __version__
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "?"
    return {"date": datetime.date.today().isoformat(), "studio": __version__, "commit": commit, "cpu": cpu, "cores": os.cpu_count(), "ram_gb": round(mem / 1024 ** 3), "os": f"{platform.system()} {platform.release()}", "python": platform.python_version(), "numpy": numpy.__version__}


def run_all(groups, quick: bool, folder: str):
    R = {"environment": environment(), "quick": quick}
    q = quick

    def log(*a):
        print(time.strftime("%H:%M:%S"), *a, flush=True)
    if "store" in groups:
        R["store"] = []
        for n, s in ([(10_000, 5_000), (2_000, 100_000)] if q else [(10_000, 5_000), (100_000, 5_000), (200_000, 5_000), (10_000, 100_000), (1_000, 1_000_000)]):
            log("store", n, s)
            R["store"].append(run_isolated("store", n=n, s=s))
    if "files" in groups:
        R["files"] = []
        for n, s in ([(10_000, 5_000)] if q else [(10_000, 5_000), (100_000, 5_000)]):
            for fmt in ("npz", "npy"):
                log("files", n, s, fmt)
                R["files"].append(run_isolated("files", n=n, s=s, fmt=fmt, folder=folder))
    if "cpa" in groups:
        R["cpa"] = []
        for n, s, rep in ([(5_000, 5_000, 50), (20_000, 5_000, 1000)] if q else [(5_000, 5_000, 50), (5_000, 5_000, 5_000), (50_000, 5_000, 1_000), (100_000, 5_000, 5_000), (20_000, 50_000, 5_000)]):
            log("cpa", n, s, rep)
            R["cpa"].append(run_isolated("cpa", n=n, s=s, report_every=rep))
    if "capture" in groups:
        R["capture"] = []
        for samples, count in ([(5_000, 5_000), (100_000, 300)] if q else [(5_000, 20_000), (24_400, 5_000), (100_000, 1_000), (131_070, 1_000)]):
            for store in (True, False):
                log("capture", samples, count, store)
                R["capture"].append(run_isolated("capture", samples=samples, count=count, store=store))
    if "server" in groups:
        log("server latency")
        R["latency"] = sv_latency(5_000 if q else 50_000, 10 if q else 30, 8)
        R["fanout"] = []
        for c in ([0, 5] if q else [0, 1, 5, 20]):
            log("fanout", c)
            R["fanout"].append(sv_fanout(c, 2_000 if q else 5_000, 5_000))
    if "leaks" in groups:
        R["leaks"] = []
        jobs = [(lk_capture, (1 if q else 5,)), (lk_ws, (200 if q else 2_000,)), (lk_api, (2_000 if q else 30_000,)), (lk_clear, (5 if q else 30, 5_000)), (lk_cpa, (5 if q else 30, 2_000)), (lk_notebook, (30 if q else 300,)), (lk_mcp, (500 if q else 5_000,))]
        for fn, args in jobs:
            log("leak", fn.__name__, args)
            try:
                R["leaks"].append(fn(*args))
            except Exception as e:  # noqa: BLE001
                R["leaks"].append({"what": fn.__name__, "error": f"{type(e).__name__}: {e}"})
    if "ui" in groups:
        R["ui"] = []
        for samples in ([5_000] if q else [5_000, 100_000, 131_070]):
            log("ui", samples)
            try:
                R["ui"].append(ui_browser(samples, 1 if q else 4))
            except Exception as e:  # noqa: BLE001
                R["ui"].append({"samples": samples, "error": f"{type(e).__name__}: {e}"})
    return R


def fmt_n(x, d=0):
    if x is None:
        return "?"
    if isinstance(x, float) and d:
        return f"{x:,.{d}f}"
    return f"{int(round(x)):,}"


def size(mb):
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:,.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def report(R) -> str:
    """Markdown tables of the results (the wiki Performance page body)."""
    e = R["environment"]
    L = [f"Measured on **{e['date']}** with Studio **{e['studio']}** (commit `{e['commit']}`) on {e['cpu']} ({e['cores']} threads), {e['ram_gb']} GB RAM, {e['os']}, Python {e['python']}, numpy {e['numpy']}. All captures use the built-in simulator; with real hardware, capture speed is set by the scope and the target, not by Studio.", ""]
    if R.get("store"):
        L += ["### Large trace sets in memory", "", "| Traces x samples | Data | Memory used | Overhead per trace | Append rate | Mean/min/max (time, extra memory) | Trace matrix for CPA (time, extra memory) | One trace to the UI |", "|---|---|---|---|---|---|---|---|"]
        for r in R["store"]:
            if "error" in r:
                L.append(f"| {fmt_n(r['n'])} x {fmt_n(r['s'])} | | | | | error: {r['error']} | | |")
                continue
            L.append(f"| {fmt_n(r['traces'])} x {fmt_n(r['samples'])} | {size(r['data_mb'])} | {size(r['rss_mb'])} | {fmt_n(r['overhead_bytes_per_trace'])} B | {fmt_n(r['append_traces_per_s'])} traces/s | {r['stats_s']:.2f} s, +{size(r['stats_peak_extra_mb'])} | {r['matrix_s']:.2f} s, +{size(r['matrix_peak_extra_mb'])} | {r['trace_fetch_ms']:.2f} ms |")
        L.append("")
    if R.get("files"):
        L += ["### Large trace files", "", "| Traces x samples | Format | Data | File size | Export (speed, extra memory) | Import (speed, extra memory) |", "|---|---|---|---|---|---|"]
        for r in R["files"]:
            if "error" in r:
                L.append(f"| {fmt_n(r['n'])} x {fmt_n(r['s'])} | {r['fmt']} | | | error: {r['error']} | |")
                continue
            L.append(f"| {fmt_n(r['traces'])} x {fmt_n(r['samples'])} | .{r['format']} | {size(r['data_mb'])} | {size(r['file_mb'])} | {r['export_s']:.1f} s ({fmt_n(r['export_mb_per_s'])} MB/s), +{size(r['export_peak_extra_mb'])} | {r['import_s']:.1f} s ({fmt_n(r['import_mb_per_s'])} MB/s), +{size(r['import_peak_extra_mb'])} |")
        L.append("")
    if R.get("cpa"):
        L += ["### CPA key recovery", "", "| Traces x samples | Data | Progress every | Time | Throughput | Extra memory | Key recovered |", "|---|---|---|---|---|---|---|"]
        for r in R["cpa"]:
            if "error" in r and r.get("seconds") is None:
                L.append(f"| {fmt_n(r['n'])} x {fmt_n(r['s'])} | | {fmt_n(r['report_every'])} | error: {r['error']} | | | |")
                continue
            L.append(f"| {fmt_n(r['traces'])} x {fmt_n(r['samples'])} | {size(r['data_mb'])} | {fmt_n(r['report_every'])} traces | {r['seconds']:.1f} s | {fmt_n(r['traces_per_s'])} traces/s | +{size(r['peak_extra_mb'])} | {'yes' if r['key_ok'] else 'NO ' + str(r.get('error') or '')} |")
        L.append("")
    if R.get("capture"):
        L += ["### Capture throughput (simulator, end to end)", "", "| Samples per trace | Traces | Stored | Rate | Data rate | Extra memory |", "|---|---|---|---|---|---|"]
        for r in R["capture"]:
            if "error" in r:
                L.append(f"| {fmt_n(r['samples'])} | {fmt_n(r['count'])} | {r['store']} | error: {r['error']} | | |")
                continue
            L.append(f"| {fmt_n(r['samples'])} | {fmt_n(r['count'])} | {'yes' if r['store'] else 'no'} | {fmt_n(r['traces_per_s'])} traces/s | {fmt_n(r['mb_per_s'])} MB/s | +{size(r['peak_extra_mb'])} |")
        L.append("")
    if R.get("latency"):
        lt = R["latency"]
        L += [f"### API response times under load", "", f"{lt['workers']} clients polling for {lt['seconds']:.0f} s while a continuous capture runs, with {fmt_n(lt['traces'])} stored traces of 5,000 samples.", "", "| Endpoint | Requests | Median | 95th percentile | 99th percentile |", "|---|---|---|---|---|"]
        for k, v in lt["endpoints"].items():
            L.append(f"| {k} | {fmt_n(v['n'])} | {v['p50_ms']:.1f} ms | {v['p95_ms']:.1f} ms | {v['p99_ms']:.1f} ms |")
        L.append("")
    if R.get("fanout"):
        L += ["### Many open windows", "", "| Browser windows (WebSocket clients) | Capture rate | Frames each window received | Studio CPU |", "|---|---|---|---|"]
        for r in R["fanout"]:
            L.append(f"| {r['clients']} | {fmt_n(r['traces_per_s'])} traces/s | {fmt_n(r['frames_per_client'])} | {fmt_n(r['server_cpu_pct'])}% |")
        L.append("")
    if R.get("leaks"):
        L += ["### Memory leak tests", "", "Studio's resident memory is sampled during each run; growth is measured after a warm-up quarter (allocators keep some memory after the first runs). A leak shows up as steady growth that scales with the number of operations.", "", "| Workload | Operations | Memory at start | Memory at end | Growth after warm-up | Verdict |", "|---|---|---|---|---|---|"]
        for r in R["leaks"]:
            if "error" in r:
                L.append(f"| {r['what']} | | | | | error: {r['error']} |")
                continue
            per = r["growth_after_warmup_mb"]
            verdict = "no leak" if per < 15 else "investigate"
            extra = f" ({r['subscribers_left']} subscribers left)" if "subscribers_left" in r else ""
            L.append(f"| {r['what']} | {fmt_n(r['operations'])} {r['unit']}{extra} | {size(r['start_mb'])} | {size(r['end_mb'])} | {per:+.1f} MB | {verdict} |")
        L.append("")
    if R.get("ui"):
        L += ["### Browser UI during a long live capture", "", "Overlay of 10 traces and the mean on; the JavaScript heap is measured after a forced garbage collection every 10 s.", "", "| Samples per trace | Duration | Frame rate (median, lowest) | JS heap start, end | Heap growth after warm-up | Page errors |", "|---|---|---|---|---|---|"]
        for r in R["ui"]:
            if "error" in r:
                L.append(f"| {fmt_n(r['samples'])} | | error: {r['error']} | | | |")
                continue
            L.append(f"| {fmt_n(r['samples'])} | {r['minutes']:.0f} min | {r['fps_median']:.0f} fps, {r['fps_min']:.0f} fps | {size(r['heap_start_mb'])}, {size(r['heap_end_mb'])} | {r['heap_growth_after_warmup_mb']:+.1f} MB | {r['page_errors']} |")
        L.append("")
    return "\n".join(L)


def compare(old, new) -> str:
    """Markdown table of the metrics that changed between two result files (for example the previous release and this one)."""
    eo, en = old["environment"], new["environment"]
    rows = []

    def add(what, size_, a, b, unit, better="lower"):
        if a is None or b is None:
            return
        if better == "lower":
            ratio = a / b if b else float("inf")
        else:
            ratio = b / a if a else float("inf")
        rows.append(f"| {what} | {size_} | {a:,.2f} {unit} | {b:,.2f} {unit} | {ratio:,.1f}x {'better' if ratio >= 1.05 else 'same' if ratio > 0.95 else 'worse'} |")

    def by(lst, *keys):
        return {tuple(r.get(k) for k in keys): r for r in lst or [] if "error" not in r}
    so, sn = by(old.get("store"), "traces", "samples"), by(new.get("store"), "traces", "samples")
    for k in sn:
        if k in so:
            sz = f"{k[0]:,} x {k[1]:,}"
            add("Mean/min/max of all traces: time", sz, so[k]["stats_s"], sn[k]["stats_s"], "s")
            add("Mean/min/max of all traces: extra memory", sz, so[k]["stats_peak_extra_mb"], sn[k]["stats_peak_extra_mb"], "MB")
    fo, fn = by(old.get("files"), "traces", "samples", "format"), by(new.get("files"), "traces", "samples", "format")
    for k in fn:
        if k in fo and k[2] == "npz":
            sz = f"{k[0]:,} x {k[1]:,}"
            add("Export to .npz: speed", sz, fo[k]["export_mb_per_s"], fn[k]["export_mb_per_s"], "MB/s", "higher")
            add("Export to .npz: file size", sz, fo[k]["file_mb"], fn[k]["file_mb"], "MB")
    lo, ln = old.get("latency", {}).get("endpoints", {}), new.get("latency", {}).get("endpoints", {})
    for k in ln:
        if k in lo:
            add(f"API under load, {k}: median", f"{new['latency']['traces']:,} traces", lo[k]["p50_ms"], ln[k]["p50_ms"], "ms")
    ko, kn = {r["what"]: r for r in old.get("leaks") or [] if "error" not in r}, {r["what"]: r for r in new.get("leaks") or [] if "error" not in r}
    for k in kn:
        if k in ko:
            add(f"Memory after: {k}", f"{kn[k]['operations']:,} {kn[k]['unit']}", ko[k]["end_mb"], kn[k]["end_mb"], "MB")
    head = [f"Same machine and workload; before is Studio {eo['studio']} (`{eo['commit']}`, measured {eo['date']}), after is Studio {en['studio']} (`{en['commit']}`, measured {en['date']}).", "", "| Metric | Size | Before | After | Change |", "|---|---|---|---|---|"]
    return "\n".join(head + rows)


BENCH_DIR = os.path.join(ROOT, "docs", "benchmarks")
README_START, README_END = "<!-- performance:start -->", "<!-- performance:end -->"


def _runs():
    runs = []
    for name in sorted(os.listdir(BENCH_DIR)) if os.path.isdir(BENCH_DIR) else []:
        if name.endswith(".json"):
            with open(os.path.join(BENCH_DIR, name)) as f:
                runs.append((name, json.load(f)))
    return runs


def _headline(R):
    """The few numbers a reader cares about first, as (label, value) pairs."""
    out = []
    st = {(r["traces"], r["samples"]): r for r in R.get("store") or [] if "error" not in r}
    big = st.get((200_000, 5_000))
    if big:
        out.append(("Largest trace set held (200,000 x 5,000 samples)", f"{size(big['data_mb'])} of traces in {size(big['rss_mb'])} of memory"))
        out.append(("Mean/min/max of all 200,000 traces", f"{big['stats_s']:.1f} s the first time, extra memory +{size(big['stats_peak_extra_mb'])}"))
    lat = (R.get("latency") or {}).get("endpoints", {})
    if lat:
        out.append((f"API under load ({R['latency']['traces']:,} traces, {R['latency']['workers']} clients, capture running)", ", ".join(f"{k} {v['p50_ms']:.1f} ms" for k, v in lat.items()) + " (medians)"))
    cap = [r for r in R.get("capture") or [] if "error" not in r and r["store"] and r["samples"] == 5_000]
    if cap:
        out.append(("Simulated capture, 5,000 samples per trace", f"{fmt_n(cap[0]['traces_per_s'])} traces/s ({fmt_n(cap[0]['mb_per_s'])} MB/s) stored"))
    cpa = {(r["traces"], r["samples"]): r for r in R.get("cpa") or [] if r.get("seconds")}
    c = cpa.get((100_000, 5_000))
    if c:
        out.append(("CPA on 100,000 x 5,000 traces", f"{c['seconds']:.0f} s, key recovered: {'yes' if c['key_ok'] else 'no'}"))
    fl = {(r["traces"], r["format"]): r for r in R.get("files") or [] if "error" not in r}
    f = fl.get((100_000, "npz"))
    if f:
        out.append(("Export 100,000 traces (1.9 GB) to .npz", f"{f['export_s']:.0f} s ({fmt_n(f['export_mb_per_s'])} MB/s), import {f['import_s']:.0f} s"))
    lk = [r for r in R.get("leaks") or [] if "error" not in r]
    if lk:
        bad = [r for r in lk if r["growth_after_warmup_mb"] >= 15]
        out.append(("Memory leak tests", f"{len(lk) - len(bad)} of {len(lk)} workloads without growth" + (f"; to investigate: {', '.join(r['what'] for r in bad)}" if bad else "")))
    ui = [r for r in R.get("ui") or [] if "error" not in r]
    if ui:
        out.append(("Browser during a long live capture", "; ".join(f"{fmt_n(r['samples'])} samples: {r['fps_median']:.0f} fps, heap {r['heap_growth_after_warmup_mb']:+.1f} MB" for r in ui)))
    return out


def publish(results_path: str):
    """Store a run in docs/benchmarks and rebuild docs/wiki/Performance.md and the README's performance section from every stored run."""
    with open(results_path) as f:
        R = json.load(f)
    e = R["environment"]
    os.makedirs(BENCH_DIR, exist_ok=True)
    name = f"{e['date']}-v{e['studio']}.json"
    with open(os.path.join(BENCH_DIR, name), "w") as f:
        json.dump(R, f, indent=1)
    runs = _runs()
    latest = runs[-1][1]
    history = ["| Date | Studio | Commit | Machine | Highlights | Data |", "|---|---|---|---|---|---|"]
    for fname, r in reversed(runs):
        env = r["environment"]
        hl = "; ".join(f"{k}: {v}" for k, v in _headline(r)[:3])
        history.append(f"| {env['date']} | {env['studio']} | `{env['commit']}` | {env['cpu']}, {env['ram_gb']} GB | {hl} | [{fname}](https://github.com/keyuraghao/chipwhisperer-studio/blob/main/docs/benchmarks/{fname}) |")
    comp = ""
    if len(runs) >= 2:
        comp = "## Changes since the previous run\n\n" + compare(runs[-2][1], latest) + "\n\n"
    summary = ["| Test | Result |", "|---|---|"] + [f"| {k} | {v} |" for k, v in _headline(latest)]
    page = "\n".join([
        "# Performance",
        "",
        "Stress tests and benchmarks of Studio with very large trace sets, large trace files, long sessions, many open windows and AI agents, plus memory leak checks. They are produced by `tools/benchmark.py` (see [running them yourself](#running-them-yourself)); every run is kept with its date, version and machine, so results can be compared over time.",
        "",
        f"## Latest results ({latest['environment']['date']}, Studio {latest['environment']['studio']})",
        "",
        "\n".join(summary),
        "",
        comp + "## Run history",
        "",
        "\n".join(history),
        "",
        "## Detailed results",
        "",
        report(latest),
        "## What is tested",
        "",
        "| Group | What it does | What it shows |",
        "|---|---|---|",
        "| Large trace sets | Fills the trace store with up to 200,000 traces of 5,000 samples, 10,000 of 100,000 and 1,000 of 1,000,000 (each about 3.7 GB), then asks for the mean/min/max, builds the trace matrix CPA uses and fetches traces for the UI. | Memory per trace, how much extra memory each operation needs at its peak, and how long it takes. |",
        "| Large files | Exports 10,000 and 100,000 traces to `.npz` and `.npy` and imports them back. Trace values sit on a 10-bit ADC grid like a CW-Lite capture, so compression behaves as with real data. | Export and import speed, file size and memory. |",
        "| CPA | Runs the CPA attack the UI runs on up to 100,000 traces of 5,000 samples and 20,000 of 50,000, with a known key. | Time, throughput, memory and that the key is recovered. |",
        "| Capture | Captures with the simulator end to end (scope, target, AES, live events, store) at up to 131,070 samples per trace. | The rate Studio itself can sustain; real hardware is slower. |",
        "| Server under load | A real Studio process with 50,000 stored traces and a continuous capture, polled by 8 clients for 30 s; then captures with 0 to 20 browser windows connected. | Response times (median, 95th and 99th percentile) and the cost of many windows. |",
        "| Memory leaks | Long or repeated workloads against a real Studio process: 5 minutes of continuous capture, 2,000 window connections, 30,000 API requests, 30 capture and clear cycles, 30 CPA runs, 300 notebook cells with figures, 5,000 MCP tool calls. Memory is sampled throughout. | A leak shows up as memory that keeps growing with the number of operations after the warm-up. |",
        "| Browser | Chromium shows a 4 minute live capture with 10 overlaid traces and the mean, at 5,000, 100,000 and 131,070 samples per trace. The JavaScript heap is measured after a forced garbage collection every 10 s. | Frame rate and whether the page leaks memory. |",
        "",
        "## Running them yourself",
        "",
        "```bash",
        'pip install -e ".[test]" playwright websockets',
        "playwright install chromium",
        "python tools/benchmark.py --quick              # a few minutes",
        "python tools/benchmark.py                      # the full set, about 45 minutes",
        "python tools/benchmark.py --only leaks,server  # some groups",
        "python tools/benchmark.py --publish build/bench/results.json  # add the run to this page and the README",
        "```",
        "",
        "The tests read memory from `/proc`, so they run on Linux. They need about 25 GB of free memory for the largest sets (use `--quick` otherwise) and put temporary trace files in `build/bench` (use a real disk, not a RAM disk). Capture numbers use the simulator, so they measure Studio rather than the hardware: with a real scope the capture rate is set by the scope, the USB link and the target.",
        "",
    ])
    with open(os.path.join(ROOT, "docs", "wiki", "Performance.md"), "w") as f:
        f.write(page)
    readme = os.path.join(ROOT, "README.md")
    with open(readme) as f:
        text = f.read()
    e = latest["environment"]
    block = "\n".join([README_START, "", f"Measured on {e['date']} with Studio {e['studio']} on {e['cpu']} with {e['ram_gb']} GB RAM ({e['os']}). Every run, the comparison with the previous one and the detailed tables are on the [Performance](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Performance) wiki page.", ""] + summary + ["", README_END])
    if README_START in text:
        a, b = text.index(README_START), text.index(README_END) + len(README_END)
        text = text[:a] + block + text[b:]
    else:
        text = text.replace("## Architecture\n", "## Performance\n\n" + block + "\n\n## Architecture\n", 1)
    with open(readme, "w") as f:
        f.write(text)
    print(f"stored docs/benchmarks/{name}; rebuilt docs/wiki/Performance.md and the README section")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scenario", help=argparse.SUPPRESS)
    ap.add_argument("--kwargs", default="{}", help=argparse.SUPPRESS)
    ap.add_argument("--quick", action="store_true", help="smaller sizes and shorter runs")
    ap.add_argument("--only", default="store,files,cpa,capture,server,leaks,ui", help="comma-separated groups")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "bench"), help="folder for results and temporary trace files (use a real disk, not tmpfs)")
    ap.add_argument("--report", help="render Markdown from a saved results.json and exit")
    ap.add_argument("--publish", help="store a results.json in docs/benchmarks and rebuild the wiki Performance page and the README section")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="Markdown table of what changed between two results.json files")
    args = ap.parse_args()
    if args.publish:
        publish(args.publish)
        return 0
    if args.compare:
        with open(args.compare[0]) as a, open(args.compare[1]) as b:
            print(compare(json.load(a), json.load(b)))
        return 0
    if args.scenario:
        print(json.dumps(SCENARIOS[args.scenario](**json.loads(args.kwargs))))
        return 0
    if args.report:
        with open(args.report) as f:
            print(report(json.load(f)))
        return 0
    os.makedirs(args.out, exist_ok=True)
    R = run_all(set(args.only.split(",")), args.quick, args.out)
    path = os.path.join(args.out, "results.json")
    with open(path, "w") as f:
        json.dump(R, f, indent=1)
    md = report(R)
    with open(os.path.join(args.out, "results.md"), "w") as f:
        f.write(md + "\n")
    print(md)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
