"""Model Context Protocol server for ChipWhisperer Studio.

Every tool maps onto Studio's HTTP API, so an AI agent gets the same features and options as the UI: connecting scopes and targets, every scope and target setting, programming, serial and SimpleSerial I/O, capture, trace access and export, CPA, glitch sweeps, compiler toolchains and firmware builds.

``cw-studio mcp`` attaches to a Studio that is already running (so the agent and the browser share one session) or, if none answers at ``--url``, starts a headless Studio inside the MCP process. Use ``--transport streamable-http`` to serve MCP over HTTP instead of stdio.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Literal, Optional

import numpy as np

log = logging.getLogger("cwstudio.mcp")

INSTRUCTIONS = (
    "ChipWhisperer Studio controls NewAE ChipWhisperer side-channel and fault-injection hardware (Nano, Lite, Pro, Husky) or a built-in simulator. "
    "Typical flow: studio_status, then scope_connect (kind='sim' when no hardware), target_connect, optionally toolchains_list, toolchain_install, firmware_fetch_sources, firmware_build and firmware_program, "
    "then capture_start with wait=true, traces_summary, cpa_start with wait=true and cpa_result. For fault injection configure glitch.* with scope_set_setting, then glitch_start and glitch_results. "
    "Settings are addressed by dotted paths from scope_get_settings / target_get_settings (e.g. 'gain.db', 'adc.samples', 'clock.clkgen_freq', 'glitch.width'). "
    "For custom experiments, notebook_run_code runs Python in Studio's shared notebook kernel (or, with notebook_path, in that notebook's own kernel) with cw bound to the connected hardware; notebook_run runs a whole stored notebook (e.g. NewAE's tutorials after tutorials_fetch). "
    "Long jobs (capture, glitch, CPA, builds, downloads) run in the background; pass wait=true or poll the matching status tool. Only one hardware job runs at a time; capture_stop stops it."
)


class StudioError(RuntimeError):
    pass


class StudioClient:
    """Minimal JSON/binary client for the Studio HTTP API."""

    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any = None, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise StudioError(f"{method} {path} failed ({e.code}): {detail}") from None
        except urllib.error.URLError as e:
            raise StudioError(f"cannot reach ChipWhisperer Studio at {self.base}: {e.reason}") from None
        if "application/json" in ctype:
            return json.loads(raw.decode("utf-8")) if raw else None
        return raw

    def get(self, _path, **params):  # positional name chosen so a query parameter called "path" does not clash
        return self._request("GET", _path, params=params)

    def post(self, path, body=None, timeout=None):
        return self._request("POST", path, body if body is not None else {}, timeout=timeout)

    def put(self, path, body):
        return self._request("PUT", path, body)

    def delete(self, path):
        return self._request("DELETE", path)

    def binary(self, path, **params):
        raw = self._request("GET", path, params=params)
        (hlen,) = struct.unpack_from("<I", raw, 0)
        header = json.loads(raw[4:4 + hlen].decode("utf-8"))
        samples = np.frombuffer(raw[4 + hlen:], dtype="<f4")
        return header, samples

    def alive(self) -> bool:
        try:
            self._request("GET", "/api/status", timeout=2)
            return True
        except StudioError:
            return False


def _decimate(x: np.ndarray, max_points: int) -> Dict[str, Any]:
    """Summarise a waveform and return at most ``max_points`` samples (min/max pairs when decimating so peaks survive)."""
    n = int(x.shape[0])
    out: Dict[str, Any] = {"n": n}
    if n == 0:
        return dict(out, samples=[])
    out.update({"min": float(x.min()), "max": float(x.max()), "mean": float(x.mean()), "std": float(x.std()), "argmin": int(x.argmin()), "argmax": int(x.argmax())})
    max_points = max(2, int(max_points))
    if n <= max_points:
        out.update({"step": 1, "samples": [round(float(v), 6) for v in x]})
        return out
    bins = max_points // 2
    step = int(np.ceil(n / bins))
    pad = (-n) % step
    xp = np.concatenate([x, np.full(pad, x[-1], dtype=x.dtype)]) if pad else x
    blocks = xp.reshape(-1, step)
    mm = np.stack([blocks.min(axis=1), blocks.max(axis=1)], axis=1).reshape(-1)
    out.update({"step": step, "decimation": "min/max per block", "samples": [round(float(v), 6) for v in mm]})
    return out


def start_embedded(host: str, port: int, simulate: bool, data_dir: Optional[str]) -> str:
    """Run a headless Studio (API + UI) in a background thread and return its URL."""
    import uvicorn
    from cwstudio.app import create_app
    from cwstudio.cli import _free_port, _wait_ready
    from cwstudio.session import Session

    session = Session(simulate=simulate, data_dir=data_dir)
    app = create_app(session)
    port = _free_port(host, port)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", ws_max_size=64 * 1024 * 1024, timeout_graceful_shutdown=3, log_config=None)
    server = uvicorn.Server(config)
    app.state.server = server
    threading.Thread(target=server.run, name="studio-embedded", daemon=True).start()
    if not _wait_ready(host, port, 30):
        raise StudioError("embedded Studio did not start")
    return f"http://{host}:{port}"


def build_server(client: StudioClient, url_note: str = ""):
    from cwstudio import __version__
    from cwstudio.mcplite import Server

    mcp = Server(name="chipwhisperer-studio", title="ChipWhisperer Studio", version=__version__, instructions=INSTRUCTIONS + (" " + url_note if url_note else ""))
    RO = {"readOnlyHint": True, "openWorldHint": False}
    HW = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
    NET = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}

    def wait_job(timeout: float, name: Optional[str] = None) -> Dict[str, Any]:
        end = time.time() + timeout
        st = client.get("/api/status")
        while time.time() < end:
            st = client.get("/api/status")
            job = st.get("job") or {}
            if not job.get("running") and (name is None or job.get("name") == name):
                break
            time.sleep(0.5)
        return st

    # ----- general -------------------------------------------------------------
    @mcp.tool(annotations=RO)
    def studio_status() -> Dict[str, Any]:
        """Current state: connected scope and target, running job and its progress, stored trace count, CPA summary, data folder and the Studio URL."""
        st = client.get("/api/status")
        st["studio_url"] = client.base
        return st

    @mcp.tool(annotations=RO)
    def studio_options() -> Dict[str, Any]:
        """Every choice Studio accepts: scope kinds, target kinds, programmers, CPA leakage models, crypto targets, SimpleSerial versions, host platform and version."""
        return client.get("/api/meta")

    @mcp.tool(annotations=RO)
    def list_devices() -> Any:
        """List ChipWhisperer USB devices attached to the machine running Studio (name, serial number, whether in use)."""
        return client.get("/api/devices")

    @mcp.tool(annotations=RO)
    def get_logs(since_seq: int = 0, limit: int = 200, level: Optional[Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]] = None) -> List[Dict[str, Any]]:
        """Recent Studio and ChipWhisperer log messages (also capture and glitch events), optionally only those after sequence number since_seq or at a given level."""
        items = client.get("/api/logs", since=since_seq)
        if level:
            items = [i for i in items if i.get("type") == "log" and i.get("level") == level]
        return items[-limit:]

    @mcp.tool(annotations=RO)
    def open_ui(open_browser: bool = False) -> Dict[str, Any]:
        """Return the URL of the Studio web UI (live waveform, settings tree) and optionally open it in the default browser on this machine."""
        if open_browser:
            import webbrowser
            webbrowser.open(client.base + "/")
        return {"url": client.base + "/", "api_docs": client.base + "/api/docs"}

    # ----- scope -----------------------------------------------------------------
    @mcp.tool(annotations=HW)
    def scope_connect(kind: Literal["auto", "lite", "pro", "nano", "husky", "huskyplus", "sim"] = "auto", sn: Optional[str] = None, force: bool = False, default_setup: bool = True, sim_model: Optional[Literal["husky", "huskyplus", "pro", "lite", "nano"]] = None) -> Dict[str, Any]:
        """Connect to a capture scope. kind='sim' uses the built-in simulator (no hardware); sim_model picks which ChipWhisperer it stands in for (default husky), which decides the protocols, triggers and programmers offered. sn picks a device by serial number, force reconnects a device another program left open, default_setup applies ChipWhisperer's recommended settings for the default target."""
        return client.post("/api/scope/connect", {"kind": kind, "sn": sn, "force": force, "default_setup": default_setup, "sim_model": sim_model})

    @mcp.tool(annotations=HW)
    def scope_disconnect() -> Dict[str, Any]:
        """Disconnect the scope (and release the USB device)."""
        return client.post("/api/scope/disconnect")

    @mcp.tool(annotations=RO)
    def scope_get_settings(filter: Optional[str] = None, include_docs: bool = False) -> List[Dict[str, Any]]:
        """All scope settings as a flat list of {path, value, type, writable, choices} read back from the hardware; filter keeps paths containing the text (e.g. 'glitch', 'adc', 'clock'); include_docs adds each setting's documentation."""
        return _flatten(client.get("/api/scope/settings"), filter, include_docs)

    @mcp.tool(annotations=HW)
    def scope_set_setting(path: str, value: Any) -> Dict[str, Any]:
        """Set one scope setting by dotted path (e.g. 'gain.db'=25, 'adc.samples'=5000, 'adc.offset'=0, 'trigger.triggers'='tio4', 'clock.clkgen_freq'=7.37e6, 'glitch.width'=10). Returns the value read back from the hardware."""
        return client.put("/api/scope/settings", {"path": path, "value": value})

    @mcp.tool(annotations=HW)
    def scope_set_settings(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Set several scope settings in order, given as {path: value}; stops at the first error and reports what was applied."""
        out = []
        for k, v in settings.items():
            out.append(client.put("/api/scope/settings", {"path": k, "value": v}))
        return out

    @mcp.tool(annotations=HW)
    def scope_action(action: Literal["default_setup", "arm_capture", "reset_fpga"]) -> Any:
        """Run a scope action: default_setup (recommended defaults), arm_capture (arm and wait for one trigger without talking to the target, to test triggering) or reset_fpga."""
        return client.post(f"/api/scope/action/{action}")

    # ----- target ----------------------------------------------------------------
    @mcp.tool(annotations=HW)
    def target_connect(kind: Literal["SimpleSerial2", "SimpleSerial", "SimpleSerial2_CDC", "CW305", "sim"] = "SimpleSerial2", options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Connect the target interface. SimpleSerial2 matches current ChipWhisperer firmware, SimpleSerial is the legacy v1 protocol, sim is the simulated AES target. options are passed to chipwhisperer.target() (e.g. {'bitfile': ...} for CW305)."""
        return client.post("/api/target/connect", {"kind": kind, **(options or {})})

    @mcp.tool(annotations=HW)
    def target_disconnect() -> Dict[str, Any]:
        """Disconnect the target interface."""
        return client.post("/api/target/disconnect")

    @mcp.tool(annotations=RO)
    def target_get_settings(filter: Optional[str] = None, include_docs: bool = False) -> List[Dict[str, Any]]:
        """All target interface settings (baud rate, protocol options...) as a flat list, same format as scope_get_settings."""
        return _flatten(client.get("/api/target/settings"), filter, include_docs)

    @mcp.tool(annotations=HW)
    def target_set_setting(path: str, value: Any) -> Dict[str, Any]:
        """Set one target interface setting by dotted path (e.g. 'baud'=38400)."""
        return client.put("/api/target/settings", {"path": path, "value": value})

    @mcp.tool(annotations=DESTRUCTIVE)
    def target_program(path: str, programmer: Literal["STM32F", "XMEGA", "AVR", "SAM4S", "NEORV32", "iCE40", "XC7A35T"] = "STM32F") -> Dict[str, Any]:
        """Erase and program the target microcontroller with a .hex/.bin file on the machine running Studio, using the chosen programmer (STM32F for CWLITEARM/Nano/STM32 targets, XMEGA for CWLITEXMEGA, AVR for ATmega, SAM4S for Husky's target, NEORV32 for the soft core), or load an FPGA bitstream (iCE40, XC7A35T on CW312T boards). Programmers the connected model does not support are refused with the reason (see hardware_capabilities)."""
        return client.post("/api/target/program", {"programmer": programmer, "path": path}, timeout=900)

    @mcp.tool(annotations=HW)
    def serial_write(data: str, hex: bool = False, newline: bool = True, eol: Optional[Literal["none", "lf", "cr", "crlf"]] = None) -> Dict[str, Any]:
        """Write raw data to the target serial port: text, or hex bytes when hex=true; newline appends a line feed, or eol picks the line ending (none, lf, cr, crlf)."""
        return client.post("/api/target/serial/write", {"data": data, "hex": hex, "newline": newline, "eol": eol})

    @mcp.tool(annotations=RO)
    def serial_read(since: float = 0, limit: int = 200) -> List[Dict[str, Any]]:
        """Serial traffic seen by Studio (both directions) after timestamp 'since', each entry {t, dir, data, hex}."""
        return client.get("/api/target/serial", since=since)[-limit:]

    @mcp.tool(annotations=HW)
    def simpleserial(cmd: str = "p", data: str = "", read_cmd: str = "r", read_len: Optional[int] = None) -> Dict[str, Any]:
        """Send one SimpleSerial command with a hex payload and read the reply, e.g. cmd='k' with a 16 byte key, cmd='p' with a plaintext (read_len=16 for AES output). read_len=0 skips reading."""
        return client.post("/api/target/simpleserial", {"cmd": cmd, "data": data, "read_cmd": read_cmd, "read_len": read_len})

    # ----- capture -----------------------------------------------------------------
    @mcp.tool(annotations=HW)
    def capture_start(count: int = 100, mode: Literal["simpleserial", "trigger_only"] = "simpleserial", key_mode: Literal["fixed", "random"] = "fixed", text_mode: Literal["random", "fixed", "counter"] = "random", key: Optional[str] = None, text: Optional[str] = None, length: int = 16, seed: Optional[int] = None, store: bool = True, clear: bool = False, ack: bool = True, max_timeouts: int = 10, max_rate: float = 0, wait: bool = True, timeout_s: float = 600) -> Dict[str, Any]:
        """Capture power traces. count=0 runs until capture_stop. mode='simpleserial' sends key/plaintext to the target each trace, 'trigger_only' just arms and waits for the target's own trigger. key_mode/text_mode choose fixed, random or counter data (key/text give fixed values in hex, length is the block size, seed makes random data repeatable). store=false only displays; clear empties the trace store first; ack waits for the SimpleSerial ack; max_timeouts aborts after that many consecutive timeouts; max_rate limits traces/s (0 = unlimited). wait=true returns when the capture ends."""
        params = {"count": count, "mode": mode, "key_mode": key_mode, "text_mode": text_mode, "key": key, "text": text, "length": length, "seed": seed, "store": store, "clear": clear, "ack": ack, "max_timeouts": max_timeouts, "max_rate": max_rate}
        res = client.post("/api/capture/start", {k: v for k, v in params.items() if v is not None})
        if wait and count > 0:
            st = wait_job(timeout_s)
            return {"started": res, "job": st.get("job"), "traces": st.get("traces")}
        return res

    @mcp.tool(annotations=HW)
    def capture_single(key: Optional[str] = None, text: Optional[str] = None, mode: Literal["simpleserial", "trigger_only"] = "simpleserial", store: bool = True, max_points: int = 1000) -> Dict[str, Any]:
        """Capture exactly one trace and return it (summary statistics plus at most max_points samples)."""
        params = {"mode": mode, "store": store, "key": key, "text": text}
        if key:
            params["key_mode"] = "fixed"
        if text:
            params["text_mode"] = "fixed"
        client.post("/api/capture/single", {k: v for k, v in params.items() if v is not None})
        st = wait_job(60)
        n = (st.get("traces") or {}).get("count", 0)
        out: Dict[str, Any] = {"job": st.get("job"), "traces": st.get("traces")}
        if store and n:
            out["trace"] = trace_get(n - 1, max_points)
        return out

    @mcp.tool(annotations=HW)
    def capture_stop() -> Dict[str, Any]:
        """Stop the running hardware job (capture or glitch sweep)."""
        return client.post("/api/capture/stop")

    @mcp.tool(annotations=RO)
    def job_wait(timeout_s: float = 60) -> Dict[str, Any]:
        """Wait until the running capture or glitch job finishes (or the timeout passes) and return the final status."""
        return wait_job(timeout_s)

    # ----- traces --------------------------------------------------------------------
    @mcp.tool(annotations=RO)
    def traces_summary() -> Dict[str, Any]:
        """How many traces are stored, samples per trace and memory used."""
        return client.get("/api/traces")

    @mcp.tool(annotations=RO)
    def trace_get(index: int, max_points: int = 1000) -> Dict[str, Any]:
        """One stored trace: its plaintext, ciphertext and key (hex), summary statistics (min, max, mean, std, argmin, argmax) and at most max_points samples (min/max decimated for long traces)."""
        meta = client.get(f"/api/traces/{index}/meta")
        _h, samples = client.binary(f"/api/traces/{index}")
        return {**meta, **_decimate(samples, max_points)}

    @mcp.tool(annotations=RO)
    def traces_stats(start: int = 0, end: Optional[int] = None, max_points: int = 1000) -> Dict[str, Any]:
        """Per-sample statistics over stored traces start..end (mean, min, max, std...) each decimated to at most max_points, useful to find where the target is computing."""
        header, samples = client.binary("/api/traces/stats", start=start, end=end)
        n = int(header.get("samples") or 0)
        out: Dict[str, Any] = {"traces": header.get("traces", header.get("count")), "samples": n}
        for i, f in enumerate(header.get("fields") or []):
            out[f] = _decimate(samples[i * n:(i + 1) * n], max_points)
        return out

    @mcp.tool(annotations=DESTRUCTIVE)
    def traces_clear() -> Dict[str, Any]:
        """Delete all stored traces from memory (exports on disk are kept)."""
        return client.delete("/api/traces")

    @mcp.tool(annotations=HW)
    def traces_export(path: str = "traces", format: Literal["npz", "cwp", "csv"] = "npz") -> Dict[str, Any]:
        """Save stored traces with their inputs/outputs/keys: npz (numpy), cwp (ChipWhisperer project, opens in the Python API) or csv. Relative paths go into Studio's data folder."""
        return client.post("/api/traces/export", {"path": path, "format": format})

    @mcp.tool(annotations=HW)
    def traces_import(path: str, replace: bool = True) -> Dict[str, Any]:
        """Load traces from an .npz or ChipWhisperer .cwp file on the machine running Studio; replace=false appends to the stored traces."""
        return client.post("/api/traces/import", {"path": path, "replace": replace})

    # ----- analysis ----------------------------------------------------------------------
    @mcp.tool(annotations=HW)
    def cpa_start(model: Literal["sbox_hw", "ptkey_hw", "invsbox_hw", "lastround_hd", "lastround_hw"] = "sbox_hw", trace_start: int = 0, trace_end: Optional[int] = None, point_start: Optional[int] = None, point_end: Optional[int] = None, bytes: Optional[List[int]] = None, known_key: Optional[str] = None, use_stored_key: bool = True, report_every: int = 50, wait: bool = True, timeout_s: float = 600) -> Dict[str, Any]:
        """Run a correlation power analysis attack on the stored traces. model is the leakage model (sbox_hw for software AES such as TINYAES128C, lastround_hd for hardware AES). trace_start/trace_end and point_start/point_end limit traces and sample window; bytes limits key bytes (0-15); known_key (hex) or the stored key enables partial guessing entropy; report_every sets progress granularity. wait=true returns the final result."""
        params: Dict[str, Any] = {"model": model, "trace_start": trace_start, "trace_end": trace_end, "known_key": known_key, "use_stored_key": use_stored_key, "report_every": report_every, "bytes": bytes}
        if point_start is not None or point_end is not None:
            params["point_range"] = [point_start or 0, point_end]
        client.post("/api/analysis/cpa/start", {k: v for k, v in params.items() if v is not None})
        if not wait:
            return {"started": True}
        end = time.time() + timeout_s
        res: Dict[str, Any] = {}
        while time.time() < end:
            res = client.get("/api/analysis/cpa") or {}
            if res and (res.get("done") or res.get("error")):
                break
            time.sleep(0.5)
        return res

    @mcp.tool(annotations=HW)
    def cpa_stop() -> Dict[str, Any]:
        """Stop a running CPA attack (the partial result stays available)."""
        return client.post("/api/analysis/cpa/stop")

    @mcp.tool(annotations=RO)
    def cpa_result() -> Dict[str, Any]:
        """Latest CPA result: best key guess, per-byte ranking with correlation values, PGE per byte when the key is known, progress."""
        return client.get("/api/analysis/cpa") or {}

    @mcp.tool(annotations=RO)
    def cpa_correlation(byte: int, max_points: int = 1000) -> Dict[str, Any]:
        """Correlation versus sample index for the best guess of one key byte (0-15), decimated, which shows where in the trace the leakage happens."""
        _h, samples = client.binary(f"/api/analysis/cpa/corr/{byte}")
        return {"byte": byte, **_decimate(samples, max_points)}

    # ----- glitch ----------------------------------------------------------------------
    @mcp.tool(annotations=HW)
    def glitch_start(parameters: List[Dict[str, Any]], command: str = "g", data: str = "", expected: Optional[str] = None, output_len: int = 4, repeats: int = 1, order: Literal["nested", "random"] = "nested", reset: Literal["none", "nrst", "pdic"] = "nrst", reset_on: Literal["never", "reset", "always"] = "reset", reset_delay: float = 0.05, glitch_timeout: int = 1000, arm_scope: bool = True, wait: bool = False, timeout_s: float = 1800) -> Dict[str, Any]:
        """Sweep glitch parameters. parameters is a list of {path, start, stop, step} or {path, values:[...]} over scope settings (e.g. glitch.width, glitch.offset, glitch.ext_offset, glitch.repeat); every combination is tried 'repeats' times in nested or random order. Each point sends SimpleSerial command with hex data and reads output_len bytes; a valid reply different from expected (hex) counts as success, no reply as reset. reset chooses how to recover the target (nrst pin, pdic, none), reset_on when (never, after a reset result, always), reset_delay in seconds. Configure glitch.clk_src/output/trigger_src first with scope_set_setting."""
        params = {"parameters": parameters, "command": command, "data": data, "expected": expected, "output_len": output_len, "repeats": repeats, "order": order, "reset": reset, "reset_on": reset_on, "reset_delay": reset_delay, "glitch_timeout": glitch_timeout, "arm_scope": arm_scope}
        res = client.post("/api/glitch/start", {k: v for k, v in params.items() if v is not None})
        if wait:
            wait_job(timeout_s)
            return client.get("/api/glitch/results")
        return res

    @mcp.tool(annotations=RO)
    def glitch_results(only: Optional[Literal["success", "reset", "normal"]] = None, limit: int = 500) -> Dict[str, Any]:
        """Results of the current or last glitch sweep: counts per outcome and each point's parameter values, outcome and response; only filters by outcome."""
        res = client.get("/api/glitch/results") or {}
        rows = res.get("results") or []
        if only:
            rows = [r for r in rows if r.get("result") == only]
        res["results"] = rows[-limit:]
        return res

    @mcp.tool(annotations=HW)
    def glitch_export(path: str = "glitch_results.csv") -> Dict[str, Any]:
        """Save the glitch sweep results as CSV (relative paths go into Studio's data folder)."""
        return client.post("/api/glitch/export", {"path": path})

    # ----- toolchains ----------------------------------------------------------------------
    @mcp.tool(annotations=RO)
    def toolchains_list() -> Dict[str, Any]:
        """Compiler toolchains Studio knows (GCC for Arm, AVR and RISC-V, clang, make for Windows, plus custom ones): version, download size, whether installed, bin folder, a system copy on PATH, and any running download."""
        return client.get("/api/toolchains")

    @mcp.tool(annotations=NET)
    def toolchain_install(toolchain_id: str, wait: bool = True, timeout_s: float = 1800) -> Dict[str, Any]:
        """Download, verify (SHA-256) and unpack a toolchain by id (arm-gcc, avr-gcc, riscv-gcc, clang, win-build-tools or a custom id). Works offline afterwards."""
        res = client.post(f"/api/toolchains/{toolchain_id}/install")
        end = time.time() + timeout_s
        while wait and time.time() < end:
            st = next((t for t in client.get("/api/toolchains")["toolchains"] if t["id"] == toolchain_id), None)
            job = (st or {}).get("job") or {}
            if job.get("state") not in ("downloading", "extracting"):
                return st or res
            time.sleep(1)
        return res

    @mcp.tool(annotations=HW)
    def toolchain_cancel(toolchain_id: str) -> Dict[str, Any]:
        """Cancel a running toolchain download."""
        return client.post(f"/api/toolchains/{toolchain_id}/cancel")

    @mcp.tool(annotations=DESTRUCTIVE)
    def toolchain_remove(toolchain_id: str) -> Dict[str, Any]:
        """Delete an installed toolchain from disk (it can be installed again later)."""
        return client.delete(f"/api/toolchains/{toolchain_id}")

    @mcp.tool(annotations=HW)
    def toolchain_add_custom(name: str, compiler: Literal["gcc", "clang"] = "gcc", arch: Optional[List[str]] = None, prefix: str = "", url: Optional[str] = None, sha256: Optional[str] = None, path: Optional[str] = None, version: Optional[str] = None, bin: str = "bin") -> Dict[str, Any]:
        """Register another toolchain, e.g. for targets Studio has no download for (TriCore, PowerPC, RX) or a specific GCC version: give an archive url (+ sha256) to download, or path to an existing install. arch lists the architectures it serves (arm, avr, riscv, tricore, ppc, rx...), prefix is the tool prefix (e.g. 'arm-none-eabi-')."""
        return client.post("/api/toolchains/custom", {"name": name, "compiler": compiler, "arch": arch or [], "prefix": prefix, "url": url, "sha256": sha256, "path": path, "version": version, "bin": bin})

    @mcp.tool(annotations=DESTRUCTIVE)
    def toolchain_remove_custom(toolchain_id: str) -> Dict[str, Any]:
        """Remove a custom toolchain entry (and its download, if Studio installed it)."""
        return client.delete(f"/api/toolchains/custom/{toolchain_id}")

    @mcp.tool(annotations=NET)
    def toolchains_refresh() -> Dict[str, Any]:
        """Fetch the latest toolchain list published in the Studio repository, so newer compiler versions become available without updating Studio."""
        return client.post("/api/toolchains/refresh")

    # ----- firmware ----------------------------------------------------------------------
    @mcp.tool(annotations=RO)
    def firmware_catalogue() -> Dict[str, Any]:
        """Firmware sources state (folder, upstream repo, channel, installed commit, update check) plus buildable projects (simpleserial-aes, simpleserial-glitch...), every platform with its architecture, MCU and programmer, crypto targets and SimpleSerial versions."""
        return client.get("/api/firmware")

    @mcp.tool(annotations=HW)
    def firmware_set_channel(channel: str = "develop") -> Dict[str, Any]:
        """Choose which upstream newaetech/chipwhisperer version firmware sources follow: a branch (develop), 'latest-release', or any tag or commit. Apply it with firmware_fetch_sources."""
        return client.put("/api/firmware/channel", {"channel": channel})

    @mcp.tool(annotations=NET)
    def firmware_check_updates() -> Dict[str, Any]:
        """Ask GitHub for the newest commit on the chosen channel and report whether the downloaded firmware sources are out of date."""
        return client.post("/api/firmware/sources/check")

    @mcp.tool(annotations=NET)
    def firmware_fetch_sources(ref: Optional[str] = None, wait: bool = True, timeout_s: float = 900) -> Dict[str, Any]:
        """Download or update the ChipWhisperer firmware sources (firmware/mcu and the fw-extra HALs) from newaetech's GitHub at the channel's current commit, or at ref (branch, tag or commit) if given."""
        res = client.post("/api/firmware/sources/fetch", {"ref": ref})
        end = time.time() + timeout_s
        while wait and time.time() < end:
            st = client.get("/api/firmware")["sources"]
            if (st.get("job") or {}).get("state") not in ("resolving", "downloading", "extracting"):
                return st
            time.sleep(1)
        return res

    @mcp.tool(annotations=HW)
    def firmware_set_folder(root: Optional[str] = None) -> Dict[str, Any]:
        """Build from your own firmware folder (firmware/mcu of a ChipWhisperer checkout, e.g. with local changes) instead of the downloaded sources; root=None goes back to the downloaded sources."""
        return client.put("/api/firmware/sources", {"root": root})

    @mcp.tool(annotations=RO)
    def firmware_plan(project: str = "simpleserial-aes", platform: str = "CWLITEARM", compiler: Literal["gcc", "clang"] = "gcc", crypto_target: Optional[str] = None, ss_ver: Optional[str] = None, cflags: Optional[str] = None, make_args: Optional[str] = None) -> Dict[str, Any]:
        """Show exactly how a build would run (make command, toolchains chosen, output file, programmer) without building."""
        return client.post("/api/firmware/plan", {"project": project, "platform": platform, "compiler": compiler, "crypto_target": crypto_target, "ss_ver": ss_ver, "cflags": cflags, "make_args": make_args})

    @mcp.tool(annotations=HW)
    def firmware_build(project: str = "simpleserial-aes", platform: str = "CWLITEARM", compiler: Literal["gcc", "clang"] = "gcc", crypto_target: Optional[str] = "TINYAES128C", ss_ver: Optional[Literal["SS_VER_2_1", "SS_VER_1_1", "SS_VER_1_0"]] = "SS_VER_2_1", cflags: Optional[str] = None, make_args: Optional[str] = None, clean: bool = True, jobs: Optional[int] = None, wait: bool = True, timeout_s: float = 900, log_tail: int = 40) -> Dict[str, Any]:
        """Compile a ChipWhisperer firmware project for a platform with GCC or clang using ChipWhisperer's makefiles. crypto_target picks the AES/crypto implementation (TINYAES128C, AVRCRYPTOLIB, MBEDTLS, HWAES...), ss_ver the SimpleSerial protocol (SS_VER_2_1 for current Studio/ChipWhisperer, SS_VER_1_1 legacy), cflags adds compiler flags, make_args adds make variables (e.g. 'OPT=2 EXTRA_OPTS=...'), clean rebuilds from scratch. Returns the .hex path, sizes and programmer, plus the end of the build log."""
        res = client.post("/api/firmware/build", {"project": project, "platform": platform, "compiler": compiler, "crypto_target": crypto_target, "ss_ver": ss_ver, "cflags": cflags, "make_args": make_args, "clean": clean, "jobs": jobs})
        end = time.time() + timeout_s
        while wait and time.time() < end and res.get("state") == "running":
            time.sleep(0.5)
            res = client.get("/api/firmware/build")
        if wait and log_tail:
            res["log_tail"] = client.get("/api/firmware/build/log")["lines"][-log_tail:]
        return res

    @mcp.tool(annotations=RO)
    def firmware_build_log(since: int = 0, limit: int = 400) -> Dict[str, Any]:
        """Build output lines after line number 'since' (use 'next' from the reply to continue)."""
        res = client.get("/api/firmware/build/log", since=since)
        res["lines"] = res["lines"][-limit:]
        return res

    @mcp.tool(annotations=HW)
    def firmware_build_cancel() -> Dict[str, Any]:
        """Stop a running firmware build."""
        return client.post("/api/firmware/build/cancel")

    @mcp.tool(annotations=RO)
    def firmware_builds() -> List[Dict[str, Any]]:
        """Firmware images built so far (newest first) with their paths, ready for firmware_program or target_program."""
        return client.get("/api/firmware/builds")

    @mcp.tool(annotations=DESTRUCTIVE)
    def firmware_program(path: Optional[str] = None, programmer: Optional[Literal["STM32F", "XMEGA", "AVR", "SAM4S", "NEORV32"]] = None) -> Dict[str, Any]:
        """Program the target with the last successful build (or path) using the platform's programmer (or the one given). Needs a connected scope."""
        return client.post("/api/firmware/program", {"path": path, "programmer": programmer}, timeout=900)

    # ----- notebooks ----------------------------------------------------------------------
    def _summarise_outputs(outputs: List[Dict[str, Any]], max_chars: int = 4000) -> Dict[str, Any]:
        text, images, errors = [], 0, []
        for o in outputs or []:
            t = o.get("output_type")
            if t == "stream":
                text.append(o.get("text", ""))
            elif t == "error":
                errors.append(f"{o.get('ename')}: {o.get('evalue')}")
                text.append("".join(o.get("traceback") or [])[-1500:])
            else:
                d = o.get("data") or {}
                if "image/png" in d or "image/jpeg" in d or "image/svg+xml" in d:
                    images += 1
                if "text/plain" in d:
                    text.append(d["text/plain"] + "\n")
        joined = "".join(text)
        if len(joined) > max_chars:
            joined = joined[:max_chars // 2] + "\n...\n" + joined[-max_chars // 2:]
        return {"text": joined, "images": images, "errors": errors}

    @mcp.tool(annotations=HW)
    def notebook_run_code(code: str, notebook_path: Optional[str] = None, timeout_s: float = 600) -> Dict[str, Any]:
        """Run Python in a Studio notebook kernel and return its output. Without notebook_path it runs in the shared default kernel (one persistent namespace for agent code); with notebook_path (relative to the notebooks folder) it runs in that notebook's own kernel, the namespace its tab in the Notebook tab uses, with the notebook's folder as working directory. Inside, `import chipwhisperer as cw` gives cw.scope()/cw.target() bound to Studio's connected devices, cw.capture_trace() stores traces in the Capture tab, IPython magics and !shell commands work, and `studio` offers studio.traces, studio.add_trace(), studio.build_firmware(), studio.program(). Cells of all kernels run one at a time."""
        r = client.post("/api/kernel/run", {"code": code, "path": notebook_path, "kernel": notebook_path, "timeout": timeout_s}, timeout=timeout_s + 30)
        return {"ok": r.get("ok"), "execution_count": r.get("execution_count"), "kernel": r.get("kernel"), **_summarise_outputs(r.get("outputs"))}

    @mcp.tool(annotations=RO)
    def notebook_list() -> Dict[str, Any]:
        """Notebooks stored in Studio (paths relative to the notebooks folder) and the state of the ChipWhisperer tutorial download."""
        return client.get("/api/notebooks")

    @mcp.tool(annotations=RO)
    def notebook_read(path: str, include_outputs: bool = True) -> Dict[str, Any]:
        """A notebook's cells (type, source and, optionally, a text summary of the outputs)."""
        nb = client.get("/api/notebooks/file", path=path)
        cells = []
        for i, c in enumerate(nb.get("cells", [])):
            item = {"index": i, "id": c["id"], "type": c["cell_type"], "source": c["source"]}
            if include_outputs and c["cell_type"] == "code":
                item["execution_count"] = c.get("execution_count")
                item["outputs"] = _summarise_outputs(c.get("outputs"), 1500)
            cells.append(item)
        return {"path": path, "cells": cells}

    @mcp.tool(annotations=HW)
    def notebook_write(path: str, cells: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create or overwrite a notebook from a list of cells, each {"type": "code" or "markdown", "source": "..."}; the user sees it in the Notebook tab."""
        nb = {"cells": [{"cell_type": c.get("type") or c.get("cell_type") or "code", "source": c.get("source", "")} for c in cells]}
        return client.put("/api/notebooks/file", {"path": path, "notebook": nb})

    @mcp.tool(annotations=HW)
    def notebook_run(path: str, stop_on_error: bool = True, timeout_s: float = 1800) -> Dict[str, Any]:
        """Run every code cell of a stored notebook in order (like Run all) in that notebook's own kernel, save the outputs into the notebook and return a per-cell summary. Its variables stay available to notebook_run_code(notebook_path=path) until kernel_shutdown."""
        r = client.post("/api/notebooks/run", {"path": path, "stop_on_error": stop_on_error, "timeout": timeout_s}, timeout=timeout_s + 60)
        cells = []
        for c in r["notebook"]["cells"]:
            if c["cell_type"] == "code" and c.get("execution_count") is not None:
                cells.append({"id": c["id"], "first_line": (c["source"].strip().splitlines() or [""])[0][:80], **_summarise_outputs(c.get("outputs"), 800)})
        return {"path": path, "ok": r["ok"], "cells_run": r["cells_run"], "failed_cell": r["failed_cell"], "cells": cells}

    def _kernel_query(notebook_path: Optional[str]) -> Dict[str, Any]:
        return {"kernel": notebook_path} if notebook_path else {}

    @mcp.tool(annotations=RO)
    def kernel_variables(notebook_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Variables defined in a notebook kernel (name, type, shape, short repr): the shared default kernel, or the kernel of the notebook at notebook_path."""
        return client.get("/api/kernel/variables", **_kernel_query(notebook_path))

    @mcp.tool(annotations=RO)
    def kernel_list() -> List[Dict[str, Any]]:
        """Running notebook kernels: id (the notebook path, or "default"), busy, queued cells, execution count, number of variables and how many Studio windows have the notebook open."""
        return client.get("/api/kernels")

    @mcp.tool(annotations=HW)
    def kernel_interrupt(notebook_path: Optional[str] = None) -> Dict[str, Any]:
        """Interrupt the running cell of a kernel and drop its queued cells: the shared default kernel, the kernel of the notebook at notebook_path, or every kernel with notebook_path="all"."""
        return client.post("/api/kernel/interrupt", _kernel_query(notebook_path))

    @mcp.tool(annotations=DESTRUCTIVE)
    def kernel_restart(notebook_path: Optional[str] = None) -> Dict[str, Any]:
        """Restart a kernel, clearing its variables (Studio's hardware connection is kept): the shared default kernel, or the kernel of the notebook at notebook_path. Other notebooks are not affected."""
        return client.post("/api/kernel/restart", _kernel_query(notebook_path))

    @mcp.tool(annotations=DESTRUCTIVE)
    def kernel_shutdown(notebook_path: str) -> Dict[str, Any]:
        """Shut down the kernel of the notebook at notebook_path and free its memory (it starts again, empty, when the notebook next runs a cell). Use it after notebook_run on notebooks you no longer need."""
        return client.post("/api/kernels/shutdown", {"kernel": notebook_path})

    @mcp.tool(annotations=NET)
    def tutorials_fetch(wait: bool = True, timeout_s: float = 900) -> Dict[str, Any]:
        """Download NewAE's tutorial notebooks (chipwhisperer-jupyter, matched to the installed firmware sources) into the notebooks folder, with firmware paths linked so their build cells work."""
        r = client.post("/api/notebooks/tutorials/fetch")
        end = time.time() + timeout_s
        while wait and time.time() < end:
            r = client.get("/api/notebooks")["tutorials"]
            if (r.get("job") or {}).get("state") not in ("resolving", "downloading", "extracting"):
                break
            time.sleep(1)
        return r

    # ----- notes and calculator -------------------------------------------------------------
    @mcp.tool(annotations=RO)
    def notes_list() -> List[Dict[str, Any]]:
        """Text notes in Studio's notes pad."""
        return client.get("/api/notes")

    @mcp.tool(annotations=RO)
    def note_read(name: str) -> Dict[str, Any]:
        """Read one note."""
        return client.get(f"/api/notes/{urllib.parse.quote(name, safe='')}")

    @mcp.tool(annotations=HW)
    def note_write(name: str, text: str, append: bool = True) -> Dict[str, Any]:
        """Write to a note (created if missing); append=true adds the text at the end, for example to record a recovered key or working glitch settings."""
        try:
            cur = client.get(f"/api/notes/{urllib.parse.quote(name, safe='')}")["text"]
        except StudioError:
            client.post("/api/notes", {"name": name})
            cur = ""
        new = (cur.rstrip("\n") + "\n" + text if cur and append else text)
        return client.put(f"/api/notes/{urllib.parse.quote(name, safe='')}", {"text": new})

    @mcp.tool(annotations=RO)
    def calculate(expression: str) -> Dict[str, Any]:
        """Evaluate a calculator expression: arithmetic, bitwise (^ is XOR), hex/bin, math functions, mean/median/std/rms, and side-channel helpers hw(x), hd(a, b), sbox(x). Variables persist (x = 3), ans is the last result."""
        return client.post("/api/calc", {"expr": expression})

    @mcp.tool(annotations=RO)
    def selection_stats(values: Optional[List[float]] = None, trace_index: Optional[int] = None, start: Optional[int] = None, end: Optional[int] = None, sample: Optional[int] = None) -> Dict[str, Any]:
        """Statistics (count, sum, mean, median, min, max, peak to peak, std, variance, RMS) of explicit values, of samples start..end of a stored trace (trace_index, -1 = newest), or of one sample index across all stored traces."""
        if values is not None:
            return client.post("/api/calc/stats", {"values": values})
        if sample is not None:
            return client.post("/api/calc/stats", {"source": "sample", "sample": sample})
        return client.post("/api/calc/stats", {"source": "trace", "index": -1 if trace_index is None else trace_index, "start": start, "end": end})

    from cwstudio.mcp_interfaces import register_interface_tools
    register_interface_tools(mcp, client, RO, HW, DESTRUCTIVE)  # protocols and interfaces: UART, SPI, GPIO, triggers, bit-banger, 1-Wire, OpenOCD
    from cwstudio.mcp_logic import register_logic_tools
    register_logic_tools(mcp, client, RO, HW)  # logic analyser: sources, capture, import/export, decoders, measurements, channels

    # ----- resources and prompts --------------------------------------------------------------
    @mcp.resource("studio://status", mime_type="application/json")
    def status_resource() -> str:
        """Live Studio status as JSON."""
        return json.dumps(client.get("/api/status"), default=str)

    @mcp.resource("studio://firmware", mime_type="application/json")
    def firmware_resource() -> str:
        """Firmware projects, platforms and source state as JSON."""
        return json.dumps(client.get("/api/firmware"), default=str)

    @mcp.prompt()
    def cpa_attack(platform: str = "CWLITEARM", traces: int = 50, simulate: bool = False) -> str:
        """Step by step CPA key recovery on a ChipWhisperer target."""
        scope = "sim" if simulate else "auto"
        target = "sim" if simulate else "SimpleSerial2"
        steps = [f"Connect the scope with scope_connect(kind='{scope}') and the target with target_connect(kind='{target}')."]
        if not simulate:
            steps.append(f"Make sure the GCC toolchain for {platform} is installed (toolchains_list, toolchain_install), fetch firmware sources if needed, then firmware_build(project='simpleserial-aes', platform='{platform}') and firmware_program().")
        steps += [f"Capture {traces} traces with capture_start(count={traces}, key_mode='fixed', text_mode='random', clear=true).", "Look at traces_stats to check the traces are not clipped (min/max well inside -0.5..0.5); adjust gain.db with scope_set_setting and recapture if needed.", "Run cpa_start(model='sbox_hw') and report cpa_result: the recovered key, per-byte correlation and PGE."]
        return " ".join(f"{i + 1}. {s}" for i, s in enumerate(steps))

    @mcp.prompt()
    def glitch_search(parameter_ranges: str = "glitch.width -45..45 step 5, glitch.offset -45..45 step 5") -> str:
        """Guided clock glitch parameter search against simpleserial-glitch."""
        return (f"1. Build and program simpleserial-glitch for the target. 2. Configure clock glitching with scope_set_setting: glitch.clk_src='clkgen', glitch.output='clock_xor', glitch.trigger_src='ext_single', io.hs2='glitch'. "
                f"3. Run glitch_start over {parameter_ranges} with command 'g', output_len 4 and reset='nrst'. 4. Summarise glitch_results(only='success') and propose a narrower second sweep around the successes.")

    return mcp


def _flatten(nodes: List[Dict[str, Any]], flt: Optional[str], docs: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def walk(ns):
        for n in ns or []:
            if n.get("kind") == "group" or n.get("children"):
                walk(n.get("children"))
                continue
            if flt and flt.lower() not in n.get("path", "").lower():
                continue
            item = {k: n.get(k) for k in ("path", "value", "type", "writable", "choices") if n.get(k) is not None}
            if docs and n.get("doc"):
                item["doc"] = n["doc"]
            out.append(item)
    walk(nodes)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cw-studio mcp", description="ChipWhisperer Studio MCP server")
    ap.add_argument("--url", default=os.environ.get("CWSTUDIO_URL", "http://127.0.0.1:8765"), help="Studio to attach to; if it does not answer, a headless Studio is started")
    ap.add_argument("--no-embed", action="store_true", help="fail instead of starting a headless Studio")
    ap.add_argument("--simulate", action="store_true", help="embedded Studio: pre-select the simulator")
    ap.add_argument("--data-dir", default=None, help="embedded Studio: data folder")
    ap.add_argument("--port", type=int, default=8765, help="embedded Studio: preferred HTTP port")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"])
    ap.add_argument("--mcp-host", default="127.0.0.1", help="streamable-http/sse: bind address")
    ap.add_argument("--mcp-port", type=int, default=8766, help="streamable-http/sse: port")
    ap.add_argument("--log-level", default="warning", choices=["debug", "info", "warning", "error"])
    args = ap.parse_args(argv)

    # stdout carries the MCP protocol on stdio, so all logging goes to stderr.
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = StudioClient(args.url)
    note = ""
    if not client.alive():
        if args.no_embed:
            print(f"ChipWhisperer Studio is not running at {args.url}", file=sys.stderr)
            return 1
        url = start_embedded("127.0.0.1", args.port, args.simulate, args.data_dir)
        client = StudioClient(url)
        note = f"A headless Studio was started for this session; its UI is at {url}/ ."
        print(f"cw-studio mcp: started headless Studio at {url}", file=sys.stderr)
    server = build_server(client, note)
    server.run(args.transport, host=args.mcp_host, port=args.mcp_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
