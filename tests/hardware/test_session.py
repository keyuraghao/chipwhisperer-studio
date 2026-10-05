"""Staged real-hardware session: run with CWSTUDIO_HW=husky (or sim) pytest tests/hardware -v -s. Stages run in order; a failed stage skips the stages that depend on it."""
import ctypes.util
import json
import os
import platform
import struct
import subprocess
import sys
import time

import numpy as np
import pytest

from conftest import HW, REPO, cfg, scope_kind, ss_proto, ss_ver, target_kind
from cwstudio.aes import encrypt_block

KEY = "2b7e151628aed2a6abf7158809cf4f3c"
PT = "00112233445566778899aabbccddeeff"
SIM = HW == "sim"
C = cfg()
stage = pytest.mark.stage


def frame(content):
    (hlen,) = struct.unpack("<I", content[:4])
    return json.loads(content[4:4 + hlen]), np.frombuffer(content[4 + hlen:], dtype="<f4")


def connect(api, **extra):
    body = {"kind": scope_kind(), "sn": C["sn"], **extra}
    return api.post("/api/scope/connect", body)


def ensure_target(api):
    st = api.get("/api/status")
    if not st["scope"].get("connected"):
        connect(api)
    if not api.get("/api/status")["target"].get("connected"):
        api.post("/api/target/connect", {"kind": target_kind()})


def hw_only(why="needs real hardware"):
    if SIM:
        pytest.skip(f"sim: {why}")


# ----- 1. environment ---------------------------------------------------------------------------
@stage("environment")
def test_01_libusb_loads(report):
    if SIM:
        pytest.skip("sim: no USB access needed")
    import usb1
    with usb1.USBContext() as ctx:
        n = sum(1 for _ in ctx.getDeviceIterator(skip_on_error=True))
    report("environment", "usb_devices", n)
    report("environment", "libusb", ctypes.util.find_library("usb-1.0"))


@stage("environment")
def test_02_udev_and_groups(report):
    if SIM or platform.system() != "Linux":
        pytest.skip("only checked on Linux with hardware")
    rules = [f for f in os.listdir("/etc/udev/rules.d") if "newae" in f.lower() or "chipwhisperer" in f.lower()]
    groups = subprocess.run(["id", "-nG"], capture_output=True, text=True).stdout.split()
    report("environment", "udev_rules", rules)
    report("environment", "groups_ok", bool({"chipwhisperer", "plugdev"} & set(groups)))
    assert rules, "no NewAE udev rule in /etc/udev/rules.d"
    assert {"chipwhisperer", "plugdev"} & set(groups), "user is in neither the chipwhisperer nor the plugdev group"


@stage("environment")
def test_03_studio_meta(api, report):
    m = api.get("/api/meta")
    assert "husky" in m["scope_kinds"] and "SAM4S" in m["programmers"]
    report("environment", "studio_version", m["version"])
    report("environment", "data_dir", m["data_dir"])


# ----- 2. discovery -----------------------------------------------------------------------------
@stage("discovery")
def test_10_devices_lists_husky(api, report):
    hw_only("the simulator is not a USB device")
    devs = api.get("/api/devices")
    report("discovery", "devices", devs)
    errors = [d for d in devs if d.get("error")]
    assert not errors, f"device entries with errors: {errors}"
    husky = [d for d in devs if d.get("kind") == "husky"]
    assert len(husky) == 1, f"expected exactly one Husky, got {devs}"
    assert husky[0]["sn"], "the Husky has no serial number"
    if C["sn"]:
        assert husky[0]["sn"] == C["sn"]


# ----- 3. scope ---------------------------------------------------------------------------------
@stage("scope")
def test_20_connect_auto(api, report):
    if SIM:
        pytest.skip("sim: auto connects to real hardware only")
    info = api.post("/api/scope/connect", {"kind": "auto"})
    assert info["type"] == "cwhusky", info
    api.post("/api/scope/disconnect")


@stage("scope")
def test_21_connect_explicit(api, report):
    t0 = time.time()
    info = connect(api)
    report("scope", "connect_s", round(time.time() - t0, 2))
    report("scope", "fw_version", info.get("fw_version"))
    report("scope", "name", info.get("name"))
    report("scope", "sn", info.get("sn"))
    assert info["connected"] and info.get("name")
    assert info["type"] == ("cwsim" if SIM else "cwhusky")
    assert info.get("fw_version")
    assert info["warnings"] == [], info["warnings"]
    st = api.get("/api/status")["scope"]
    assert st["connected"] and not st.get("pending") and st.get("sn") == info.get("sn")


@stage("scope")
def test_22_devices_while_connected(api, report):
    hw_only("no USB device to list")
    t0 = time.time()
    devs = api.get("/api/devices")
    report("scope", "devices_while_connected_s", round(time.time() - t0, 3))
    husky = [d for d in devs if d.get("kind") == "husky"]
    assert husky and husky[0].get("in_use"), devs
    assert not any(d.get("error") for d in devs), devs


@stage("scope")
def test_23_settings_roundtrip(api, report):
    tree = api.get("/api/scope/settings")
    assert tree
    for path, value in (("gain.db", 20), ("adc.samples", 3000), ("clock.clkgen_freq", 7.37e6)):
        got = api.put("/api/scope/settings", {"path": path, "value": value})["value"]
        report("scope", f"set_{path}", got)
        assert abs(float(got) - value) <= max(1.0, 0.01 * value), (path, got)
    if not SIM:
        api.put("/api/scope/settings", {"path": "clock.adc_mul", "value": 4})


@stage("scope")
def test_24_reconnect_cycle(api, report):
    api.post("/api/scope/disconnect")
    assert not api.get("/api/status")["scope"]["connected"]
    connect(api)
    api.post("/api/scope/disconnect")
    info = connect(api, force=True)
    report("scope", "force_connect_type", info["type"])
    assert info["connected"]
    sn = C["sn"] or info.get("sn")
    info = connect(api, sn=sn)
    assert info["sn"] == sn


# ----- 4. firmware ------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def firmware(api, report):
    """Build simpleserial-aes for the platform through the build API and return (hex path, programmer)."""
    cat = api.get("/api/firmware")
    if not cat["sources"]["valid"]:
        pytest.skip("no firmware sources in the data folder (run tools/hw_session.sh --prepare)")
    body = {"project": "simpleserial-aes", "platform": C["platform"], "ss_ver": ss_ver(), "crypto_target": "TINYAES128C", "compiler": "gcc"}
    t0 = time.time()
    api.post("/api/firmware/build", body)
    while True:
        st = api.get("/api/firmware/build")
        if st["state"] != "running":
            break
        assert time.time() - t0 < 600, "build took over 10 minutes"
        time.sleep(0.5)
    if st["state"] != "ok":
        tail = api.get("/api/firmware/build/log")["lines"][-15:]
        pytest.fail(f"build failed: {st.get('error')}\n" + "\n".join(tail))
    report("firmware", "build_s", round(time.time() - t0, 1))
    report("firmware", "hex", st["hex"])
    report("firmware", "size", st.get("size"))
    return st["hex"], C["programmer"] or st["programmer"]


@stage("firmware")
def test_30_build(firmware):
    path, programmer = firmware
    assert os.path.isfile(path) and programmer


@stage("firmware")
def test_31_program(api, firmware, report):
    if C["skip_program"]:
        pytest.skip("CWSTUDIO_HW_SKIP_PROGRAM=1")
    path, programmer = firmware
    t0 = time.time()
    res = api.post("/api/firmware/program", {"path": path, "programmer": programmer})
    report("firmware", "program_s", round(time.time() - t0, 1))
    report("firmware", "programmer", programmer)
    report("firmware", "program_result", res)
    assert res.get("ok")


# ----- 5. target --------------------------------------------------------------------------------
@stage("target")
def test_40_aes_known_answer(api, report):
    ensure_target(api)
    api.post("/api/target/simpleserial", {"cmd": "k", "data": KEY, "read_cmd": "e", "read_len": 1})
    r = api.post("/api/target/simpleserial", {"cmd": "p", "data": PT})
    want = encrypt_block(bytes.fromhex(KEY), bytes.fromhex(PT)).hex()
    report("target", "ciphertext", r["response"])
    assert r["response"] == want, f"got {r['response']}, AES says {want}"


# ----- 6. capture -------------------------------------------------------------------------------
@stage("capture")
@pytest.mark.parametrize("count", [50, 500])
def test_50_capture(api, report, count):
    ensure_target(api)
    api.put("/api/scope/settings", {"path": "adc.samples", "value": 3000})
    t0 = time.time()
    api.post("/api/capture/start", {"count": count, "clear": True, "key": KEY, "key_mode": "fixed", "text_mode": "random"})
    st = api.wait_job()
    dt = time.time() - t0
    job = st["job"]
    report("capture", f"rate_{count}_per_s", round(count / dt, 1))
    report("capture", f"timeouts_{count}", job.get("timeouts"))
    assert job["error"] is None, job
    assert st["traces"]["count"] == count, "dropped traces"
    h, w = frame(api.get("/api/traces/block?start=0&end=10").content)
    w = w.reshape(len(h["indices"]), h["samples"])
    assert h["samples"] == 3000
    assert float(w.std(axis=1).min()) > 1e-4, "flat traces: no signal"
    meta = api.get(f"/api/traces/{count - 1}/meta")
    assert meta["key"] == KEY and meta["textout"] == encrypt_block(bytes.fromhex(KEY), bytes.fromhex(meta["textin"])).hex()


@stage("capture")
def test_51_export_import(api, report):
    n = api.get("/api/traces")["count"]
    assert n
    path = api.post("/api/traces/export", {"path": "hw_session/traces", "format": "npz"})["path"]
    assert api.post("/api/traces/import", {"path": path, "replace": True})["imported"] == n
    report("capture", "export", path)


@stage("capture")
def test_52_websocket_events(studio, api):
    from websockets.sync.client import connect as wsconnect
    ensure_target(api)
    with wsconnect(studio.url.replace("http", "ws") + "/ws", max_size=None) as ws:
        assert json.loads(ws.recv(timeout=10))["type"] == "hello"
        api.post("/api/capture/start", {"count": 5, "store": False})
        got_trace = got_done = False
        end = time.time() + 30
        while time.time() < end and not got_done:
            msg = ws.recv(timeout=10)
            if isinstance(msg, bytes):
                got_trace = got_trace or frame(msg)[0]["type"] == "trace"
            else:
                ev = json.loads(msg)
                got_done = ev["type"] == "capture" and ev.get("state") == "done"
        assert got_trace and got_done
    api.wait_job()


# ----- 7. analysis ------------------------------------------------------------------------------
@stage("analysis")
def test_60_cpa_recovers_key(api, report):
    ensure_target(api)
    n = 500 if SIM else 3000
    api.post("/api/capture/start", {"count": n, "clear": True, "key": KEY})
    st = api.wait_job(1800)
    assert st["traces"]["count"] == n, st["job"]
    t0 = time.time()
    api.post("/api/analysis/cpa/start", {"model": "sbox_hw", "report_every": 250})
    while True:
        res = api.get("/api/analysis/cpa")
        if res.get("done"):
            break
        assert time.time() - t0 < 600
        time.sleep(0.5)
    pge = [b["pge"] for b in res["bytes"]]
    report("analysis", "traces", n)
    report("analysis", "best_key", res["best_key"])
    report("analysis", "pge", pge)
    report("analysis", "cpa_s", res["elapsed"])
    assert res["error"] is None
    correct = sum(p == 0 for p in pge)
    assert correct >= (16 if SIM else 14), f"only {correct}/16 bytes at PGE 0"


# ----- 8. glitch --------------------------------------------------------------------------------
# Both sweeps target the AES firmware already on the board: command p with a known plaintext, so a valid but different ciphertext is a successful fault, and no answer is a reset. Settings stay deliberately gentle (narrow clock glitches, a few cycles of the low-power MOSFET only), and every test restores the scope and checks the target still answers correctly afterwards.
GLITCH_OFF = [("glitch.trigger_src", "manual"), ("glitch.width", 0), ("glitch.enabled", False), ("io.glitch_lp", False), ("io.glitch_hp", False), ("io.hs2", "clkgen"), ("glitch.output", "clock_xor"), ("glitch.repeat", 1), ("glitch.ext_offset", 0)]


def _set(api, path, value):
    return api.put("/api/scope/settings", {"path": path, "value": value})


def _restore_glitch(api):
    for path, value in GLITCH_OFF:
        r = api._do("PUT", "/api/scope/settings", {"path": path, "value": value}, ok=False)
        if r.status_code >= 300:
            print(f"[glitch] restore {path} -> {r.status_code}: {r.text[:200]}")


def _sweep(studio, api, report, name, parameters, repeats=2):
    ct = encrypt_block(bytes.fromhex(KEY), bytes.fromhex(PT)).hex()
    api.post("/api/target/simpleserial", {"cmd": "k", "data": KEY, "read_cmd": "e", "read_len": 1})
    from websockets.sync.client import connect as wsconnect
    events = []
    with wsconnect(studio.url.replace("http", "ws") + "/ws", max_size=None) as ws:
        assert json.loads(ws.recv(timeout=10))["type"] == "hello"
        api.post("/api/glitch/start", {"parameters": parameters, "repeats": repeats, "command": "p", "data": PT, "expected": ct, "output_len": 16, "reset": "nrst", "reset_on": "reset"})
        end = time.time() + 300
        while time.time() < end:
            msg = ws.recv(timeout=60)
            if isinstance(msg, bytes):
                continue
            ev = json.loads(msg)
            if ev.get("type") in ("glitch", "glitch_result"):
                events.append(ev)
            if ev.get("type") == "glitch" and ev.get("state") in ("done", "stopped", "error"):
                break
    api.wait_job(timeout=60)
    res = api.get("/api/glitch/results")
    points = 1
    for prm in parameters:
        points *= len(prm["values"])
    assert not res.get("error"), res.get("error")
    assert not res.get("running")
    assert len(res["results"]) == points * repeats, f"{len(res['results'])} results for {points} points x {repeats}"
    assert sum(res["counts"].values()) == points * repeats
    assert set(res["counts"]) <= {"normal", "success", "reset"}
    assert any(e.get("type") == "glitch_result" for e in events), "no glitch_result events reached the websocket"
    assert any(e.get("type") == "glitch" and e.get("state") == "done" for e in events), "no final glitch event"
    per_point = {}
    for r in res["results"]:
        k = ",".join(str(v) for v in r["values"])
        per_point.setdefault(k, {"normal": 0, "success": 0, "reset": 0})[r["result"]] += 1
    report("glitch", f"{name}_counts", res["counts"])
    report("glitch", f"{name}_per_point", per_point)
    exported = api.post("/api/glitch/export", {"path": f"hw_{name}.csv"})["path"]
    with open(exported) as f:
        assert sum(1 for _ in f) == points * repeats + 1
    return res


def _target_still_works(api, report, name):
    """After a sweep: reset the target, then the AES known answer and a short capture must work."""
    ct = encrypt_block(bytes.fromhex(KEY), bytes.fromhex(PT)).hex()
    for attempt in range(3):
        _set(api, "io.nrst", "low")
        time.sleep(0.05)
        _set(api, "io.nrst", "high_z")
        time.sleep(0.3)
        api.post("/api/target/simpleserial", {"cmd": "k", "data": KEY, "read_cmd": "e", "read_len": 1})
        r = api.post("/api/target/simpleserial", {"cmd": "p", "data": PT})
        if r.get("response") == ct:
            break
    report("glitch", f"{name}_recovery_attempts", attempt + 1)
    assert r.get("response") == ct, f"target does not answer correctly after the {name} sweep: {r}"
    api.post("/api/capture/start", {"count": 5, "store": False})
    st = api.wait_job(timeout=120)
    assert not (st.get("job") or {}).get("error"), st


@stage("glitch")
def test_70_glitch_capabilities(api, report):
    ensure_target(api)
    caps = api.get("/api/capabilities")
    report("glitch", "capabilities", {k: v for k, v in caps.items() if "glitch" in k.lower()} if isinstance(caps, dict) else caps)
    tree = api.get("/api/scope/settings")
    assert "glitch" in json.dumps(tree), "the scope settings have no glitch module"


@stage("glitch")
def test_71_clock_glitch_sweep(studio, api, report):
    ensure_target(api)
    try:
        for path, value in [("glitch.enabled", True), ("glitch.clk_src", "pll"), ("glitch.output", "clock_xor"), ("glitch.trigger_src", "ext_single"), ("glitch.repeat", 1), ("io.hs2", "glitch")]:
            _set(api, path, value)
        _sweep(studio, api, report, "clock", [{"path": "glitch.width", "values": [10, 25, 40], "int": True}, {"path": "glitch.ext_offset", "values": [0, 30], "int": True}])
    finally:
        _restore_glitch(api)
    _target_still_works(api, report, "clock")


@stage("glitch")
def test_72_voltage_glitch_sweep(studio, api, report):
    ensure_target(api)
    try:
        for path, value in [("glitch.enabled", True), ("glitch.clk_src", "pll"), ("glitch.output", "enable_only"), ("glitch.trigger_src", "ext_single"), ("io.glitch_hp", False), ("io.glitch_lp", True)]:
            _set(api, path, value)
        _sweep(studio, api, report, "voltage", [{"path": "glitch.repeat", "values": [1, 3, 5], "int": True}, {"path": "glitch.ext_offset", "values": [0, 30], "int": True}])
    finally:
        _restore_glitch(api)
    _target_still_works(api, report, "voltage")


# ----- 9. Husky specific ------------------------------------------------------------------------
@stage("husky")
def test_80_capabilities(api, report):
    c = api.get("/api/capabilities")
    report("husky", "model", c["model"])
    report("husky", "unavailable", {k: v["reason"] for k, v in c.items() if isinstance(v, dict) and "available" in v and not v["available"]})
    assert c["model"] == "husky"
    assert c["logic_analyzer"]["native"]["available"], c["logic_analyzer"]["native"]


@stage("husky")
def test_81_logic_analyser(api, report):
    ensure_target(api)
    r = api.post("/api/la/capture", {"source": "native", "settings": {"group": "CW 20-pin", "trigger": "capture", "fire": "simpleserial", "depth": 4000}, "wait": True, "timeout": 30})
    report("husky", "la_samples", r["samples"])
    report("husky", "la_edges", r["edges"])
    report("husky", "la_warnings", r.get("warnings"))
    assert r["ok"] and r["samples"] > 0 and r["edges"] > 0, "no edges on the 20-pin header (UART and trigger should toggle)"


@stage("husky")
def test_82_triggers(api, report):
    ensure_target(api)
    out = {}
    for kind, body in (("uart_pattern", {"pin": "tio1", "pattern": "'r'", "baud": 38400}), ("edge_counter", {"pin": "tio4", "edges": 1})):
        api.put("/api/interfaces/trigger", {"kind": kind, **body})
        api.post("/api/capture/start", {"count": 5, "store": False, "max_timeouts": 5})
        job = api.wait_job()["job"]
        out[kind] = {"done": job["done"], "timeouts": job["timeouts"], "error": job["error"]}
    api.put("/api/interfaces/trigger", {"kind": "sequencer", "pins": ["tio4", "tio3"]})
    api.put("/api/interfaces/trigger", {"kind": "basic", "pins": ["tio4"]})
    report("husky", "triggers", out)
    api.post("/api/capture/start", {"count": 3, "store": False})
    job = api.wait_job()["job"]
    assert job["error"] is None and job["done"] == 3, "basic trigger broken after the trigger tests"
    assert out["edge_counter"]["done"] == 5, out


@stage("husky")
def test_83_sad_trigger(api, report):
    ensure_target(api)
    if api.get("/api/traces")["count"] == 0:
        api.post("/api/capture/start", {"count": 1})
        api.wait_job()
    try:
        # Husky's SAD threshold maxes out at 64 (the Pro allows up to 100000); keep it in range.
        api.put("/api/interfaces/trigger", {"kind": "sad", "threshold": 32, "start": 0})
        api.post("/api/capture/start", {"count": 5, "store": False, "max_timeouts": 5})
        job = api.wait_job()["job"]
        report("husky", "sad", {"done": job["done"], "timeouts": job["timeouts"]})
    finally:
        # always restore the basic trigger so later capture stages are not left on SAD
        api.put("/api/interfaces/trigger", {"kind": "basic", "pins": ["tio4"]})


@stage("husky")
def test_84_uart_and_gpio(api, report):
    ensure_target(api)
    u = api.get("/api/interfaces/uart")
    report("husky", "uart", u)
    assert u["rx"] and u["tx"]
    from cwstudio.codemap.emu import SimpleSerial
    raw = SimpleSerial(ss_proto()).frame("p", bytes.fromhex(PT))
    t0 = time.time()
    api.post("/api/target/serial/write", {"data": raw.hex(), "hex": True})
    time.sleep(0.5)
    rx = "".join(e["hex"] for e in api.get(f"/api/target/serial?since={t0 - 0.01}") if e["dir"] == "rx")
    ct = encrypt_block(bytes.fromhex(KEY), bytes.fromhex(PT))
    report("husky", "uart_rx", rx)
    if target_kind() == "SimpleSerial":
        # v1 answers in ASCII: 'r' + the ciphertext as uppercase hex text + '\n'
        txt = bytes.fromhex(rx).decode("ascii", "replace")
        assert "r" in txt and ct.hex() in txt.lower(), f"raw UART reply {txt!r} does not hold the 'r' response with ciphertext {ct.hex()}"
    else:
        assert b"\x00" not in ct  # COBS leaves a ciphertext without zero bytes as it is, so it shows up verbatim
        assert "72" in rx and ct.hex() in rx, f"raw UART reply {rx} does not hold the 'r' response with ciphertext {ct.hex()}"
    g = api.get("/api/interfaces/gpio")
    report("husky", "gpio", {p: v["level"] for p, v in g["pins"].items()})
    api.post("/api/interfaces/gpio/pulse", {"pin": "nrst", "ms": 50})
    time.sleep(0.3)
    api.post("/api/target/simpleserial", {"cmd": "k", "data": KEY, "read_cmd": "e", "read_len": 1})
    r = api.post("/api/target/simpleserial", {"cmd": "p", "data": PT})
    assert r["response"] == ct.hex(), "target did not answer after the nRST pulse"
    us = api.get("/api/interfaces/userio")
    report("husky", "userio", us)


# ----- 10. notebook -----------------------------------------------------------------------------
@stage("notebook")
def test_90_notebook_scope(api, report):
    ensure_target(api)
    n0 = api.get("/api/traces")["count"]
    code = ("import chipwhisperer as cw\nscope = cw.scope()\ntarget = cw.target(scope)\n"
            f"key = bytearray.fromhex('{KEY}')\nfor i in range(10):\n    tr = cw.capture_trace(scope, target, bytearray(16), key)\n"
            "print('fw', scope.fw_version, 'last', tr.textout.hex())")
    r = api.post("/api/kernel/run", {"code": code, "timeout": 120})
    text = "".join(o.get("text", "") for o in r["outputs"] if o.get("output_type") == "stream")
    report("notebook", "output", text.strip())
    assert r["ok"], r["outputs"]
    assert api.get("/api/traces")["count"] == n0 + 10
    assert encrypt_block(bytes.fromhex(KEY), bytes(16)).hex() in text


# ----- MCP --------------------------------------------------------------------------------------
@stage("mcp")
def test_95_mcp_agent(studio, api, report):
    ensure_target(api)
    env = {**os.environ, "PYTHONPATH": os.path.join(REPO, "src")}
    p = subprocess.Popen([sys.executable, "-m", "cwstudio", "mcp", "--url", studio.url, "--no-embed"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    try:
        def ask(i, method, params=None):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            while True:
                msg = json.loads(p.stdout.readline())
                if msg.get("id") == i:
                    return msg

        def tool(i, name, **args):
            r = ask(i, "tools/call", {"name": name, "arguments": args})["result"]
            assert not r["isError"], r
            return r["structuredContent"]["result"]
        ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hw", "version": "0"}})
        p.stdin.write(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n')
        st = tool(2, "studio_status")
        assert st["scope"]["connected"]
        cap = tool(3, "capture_start", count=20, clear=True, key=KEY)
        assert cap["traces"]["count"] == 20
        tr = tool(4, "trace_get", index=0, max_points=200)
        assert tr["key"] == KEY and tr["std"] > 0
        ss = tool(5, "simpleserial", cmd="p", data=PT)
        report("mcp", "ciphertext", ss["response"])
        assert ss["response"] == encrypt_block(bytes.fromhex(KEY), bytes.fromhex(PT)).hex()
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(10)


# ----- 11. robustness ---------------------------------------------------------------------------
@stage("robustness")
def test_97_devices_does_not_reopen(api, report):
    hw_only("no USB device to list")
    for _ in range(5):
        devs = api.get("/api/devices")
        assert any(d.get("in_use") for d in devs)
    api.post("/api/capture/start", {"count": 3, "store": False})
    assert api.wait_job()["job"]["error"] is None, "capture failed after listing devices while connected"


@stage("robustness")
def test_98_manual_unplug(api, report):
    hw_only("unplug test")
    if not C["manual"]:
        pytest.skip("set CWSTUDIO_HW_MANUAL=1 for the operator unplug test")
    print("\n>>> UNPLUG the Husky now (waiting up to 60 s)", flush=True)
    end = time.time() + 60
    while time.time() < end and api.get("/api/status")["scope"].get("connected"):
        time.sleep(1)
    st = api.get("/api/status")["scope"]
    report("robustness", "lost", st.get("lost"))
    assert not st["connected"] and st.get("lost"), "Studio did not notice the unplug"
    print(">>> PLUG the Husky back in (waiting up to 60 s)", flush=True)
    end = time.time() + 60
    while time.time() < end and not any(d.get("kind") == "husky" for d in api.get("/api/devices")):
        time.sleep(1)
    time.sleep(2)
    assert connect(api)["connected"]
    api.post("/api/target/connect", {"kind": target_kind()})


# ----- 12. disconnect ---------------------------------------------------------------------------
@stage("disconnect")
def test_99_disconnect(api):
    api.post("/api/target/disconnect")
    api.post("/api/scope/disconnect")
    st = api.get("/api/status")
    assert not st["scope"]["connected"] and not st["target"]["connected"]
