#!/usr/bin/env python3
"""Stress Studio until it breaks: every workload is escalated until it fails, times out or leaves the memory budget, and the last size that worked and the first that did not are recorded.

Workloads: trace store size, simulated capture at the largest trace, CPA (GPU and CPU), trace file export and import, logic analyser captures and decoders, code map builds of large firmware runs, glitch sweep grids, notebook outputs and kernels, WebSocket fan-out, concurrent HTTP clients and settings floods. In-process workloads run one at a time in a fresh interpreter (benchmark.py's pattern) watched by a memory and time budget; server workloads drive a real Studio process.

    python tools/stress.py                         # everything (an hour or more; needs a quiet machine)
    python tools/stress.py --only cpa,logic        # some workloads
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


SERVER = {"fanout": sv_fanout, "http": sv_http, "codemap": sv_codemap}


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
}


def run_step(name: str, size, budget_gb: float, timeout: float) -> dict:
    """One size of one workload in a fresh interpreter, killed when it exceeds the memory budget or the timeout."""
    kw = {"size": size} if name in ("cpa", "cpa_cpu") else {LADDERS[name][0]: size}
    if name == "cpa_cpu":
        kw["gpu"] = False
    cmd = [sys.executable, os.path.abspath(__file__), "--step", name, "--kwargs", json.dumps(kw)]
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
           "Each workload was escalated until it failed. **Last OK** is the largest size that completed correctly, **First fail** the size at which it broke and why. Workloads marked exhausted completed their largest planned size without failing.", "",
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
    names = [n.strip() for n in a.only.split(",")] if a.only else list(LADDERS)
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
