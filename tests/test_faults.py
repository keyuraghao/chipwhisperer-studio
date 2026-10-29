"""Fault work on the simulator: the faulted AES and inverse key schedule, differential fault analysis, pluggable glitch checkers, and captures streamed to disk."""
import os
import tempfile
import time

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio import checkers, dfa
from cwstudio.aes import encrypt_block, encrypt_block_faulted, expand_key, last_round_key, master_key_from_round_key
from cwstudio.app import create_app
from cwstudio.session import Session
from cwstudio.traces import DiskTraceWriter, TraceStore

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
PT = bytes.fromhex("00112233445566778899aabbccddeeff")


@pytest.fixture(scope="module")
def client():
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        assert c.post("/api/scope/connect", json={"kind": "sim", "sim_model": "husky"}).status_code == 200
        assert c.post("/api/target/connect", json={"kind": "sim"}).status_code == 200
        yield c


def wait_job(client, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/api/status").json()
        if not (st.get("job") or {}).get("running"):
            return st
        time.sleep(0.05)
    raise TimeoutError


# --- AES helpers -----------------------------------------------------------
def test_faulted_aes_and_inverse_key_schedule():
    ct = encrypt_block(KEY, PT)
    assert encrypt_block_faulted(KEY, PT, 5, 0) == ct  # a zero fault is no fault
    f = encrypt_block_faulted(KEY, PT, 5, 0x3c)
    assert sum(1 for i in range(16) if f[i] != ct[i]) == 4  # one byte entering round 9 spreads to one column
    assert dfa.column_of(ct, f) is not None
    f10 = encrypt_block_faulted(KEY, PT, 5, 0x3c, round_=10)
    assert sum(1 for i in range(16) if f10[i] != ct[i]) == 1 and dfa.column_of(ct, f10) is None
    assert master_key_from_round_key(last_round_key(KEY)) == KEY
    for k in (bytes(16), bytes(range(16)), os.urandom(16)):
        assert master_key_from_round_key(bytes(expand_key(k)[10])) == k


def test_dfa_recovers_the_key_and_reports_unusable_faults():
    ct = encrypt_block(KEY, PT)
    rng = np.random.default_rng(3)
    faults = [encrypt_block_faulted(KEY, PT, int(rng.integers(16)), int(rng.integers(1, 256))) for _ in range(24)]
    r = dfa.analyse(ct, faults)
    assert r["key"] == KEY.hex() and r["round_key_10"] == last_round_key(KEY).hex() and r["solved_columns"] == 4
    assert all(c["candidates"] == 1 and c["pairs"] >= 2 for c in r["columns"])
    one = dfa.analyse(ct, faults[:1])  # one fault narrows one column to a few hundred tuples, the rest stay open
    assert "key" not in one and one["solved_columns"] == 0 and "??" in one["round_key_10_partial"]
    assert sum(c["candidates"] for c in one["columns"]) > 1
    bad = dfa.analyse(ct, [ct, encrypt_block_faulted(KEY, PT, 2, 0x11, round_=10), encrypt_block_faulted(KEY, PT, 2, 0x11, round_=8), b"\x00" * 5])
    assert len(bad["unusable"]) == 4 and bad["faulty_used"] == 0
    assert "identical" in bad["unusable"][0]["why"] and "1 byte" in bad["unusable"][1]["why"] and "not 16" in bad["unusable"][3]["why"]
    g = dfa.from_glitch_results([{"result": "normal", "response": ct.hex()}] * 3 + [{"result": "success", "response": f.hex()} for f in faults] + [{"result": "reset", "response": None}], PT, None)
    assert g["key"] == KEY.hex() and g["correct"] == ct.hex() and g["replies"] == 27 and g["faulty"] == 24  # the most common reply is the correct one when no expected value is given
    with pytest.raises(ValueError):
        dfa.from_glitch_results([{"result": "reset", "response": None}], None, None)


# --- checkers ------------------------------------------------------------------
def _ctx(resp, expected=None, outcome="success", **extra):
    d = {"resp": resp, "hex": resp.hex() if resp else None, "valid": resp is not None, "timed_out": False, "expected": expected,
         "value": int.from_bytes(resp, "little") if resp else None, "expected_value": int.from_bytes(expected, "little") if expected else None,
         "fired": None, "outcome": outcome, "n": 0}
    d.update(extra)
    return d


def test_builtin_checkers():
    exp = (2500).to_bytes(4, "little")
    lc = checkers.BUILTIN["loop_counter"][1]
    assert checkers.apply(lc, _ctx(exp, exp, "normal")) == ("normal", None)
    out, detail = checkers.apply(lc, _ctx((2470).to_bytes(4, "little"), exp))
    assert out == "success" and "30 skipped" in detail
    assert checkers.apply(lc, _ctx(None, exp, "reset")) == ("reset", None)
    ct = encrypt_block(KEY, PT)
    ad = checkers.BUILTIN["aes_dfa"][1]
    assert checkers.apply(ad, _ctx(ct, ct, "normal")) == ("normal", None)
    out, detail = checkers.apply(ad, _ctx(encrypt_block_faulted(KEY, PT, 7, 0x80), ct))
    assert out == "success" and detail.startswith("usable fault, column")
    out, detail = checkers.apply(ad, _ctx(bytes(b ^ 0xFF for b in ct), ct))
    assert out == "garbled" and "16 bytes differ" in detail
    out, detail = checkers.apply(ad, _ctx(ct[:4], ct))
    assert out == "garbled" and "4-byte reply" in detail
    fl = checkers.BUILTIN["flag"][1]
    assert checkers.apply(fl, _ctx(b"\x00", None, "normal"))[0] == "normal" and checkers.apply(fl, _ctx(b"\x01", None, "normal"))[0] == "success"
    ex = checkers.make_expr("('success', 'short') if value < expected_value else False")
    assert checkers.apply(ex, _ctx((10).to_bytes(4, "little"), exp)) == ("success", "short")
    assert checkers.apply(ex, _ctx(exp, exp)) == ("normal", None)
    assert checkers.apply(checkers.make_expr("None"), _ctx(exp, exp, "garbled")) == ("garbled", None)
    assert checkers.apply(checkers.make_expr("'bogus'"), _ctx(exp, exp))[0] == "garbled"
    out, detail = checkers.apply(checkers.make_expr("1 / 0"), _ctx(exp, exp))  # a failing checker does not stop the sweep
    assert out == "garbled" and "ZeroDivisionError" in detail
    with pytest.raises(ValueError):
        checkers.make_expr("")
    with pytest.raises(ValueError):
        checkers.resolve("nope", None, tempfile.mkdtemp())


def test_user_checker_files_load_and_report_errors():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "checkers"))
    with open(os.path.join(d, "checkers", "parity.py"), "w") as f:
        f.write('"""Success when the reply has odd parity."""\n\ndef check(ctx):\n    return sum(ctx["resp"] or b"") % 2 == 1\n')
    with open(os.path.join(d, "checkers", "broken.py"), "w") as f:
        f.write("def check(ctx:\n    pass\n")
    rows = {r["name"]: r for r in checkers.available(d)}
    assert rows["parity"]["doc"] == "Success when the reply has odd parity." and not rows["parity"]["builtin"]
    assert rows["broken"]["doc"].startswith("broken: SyntaxError")
    assert {"expected", "loop_counter", "aes_dfa", "flag", "expr"} <= set(rows)
    fn = checkers.resolve("parity", None, d)
    assert checkers.apply(fn, _ctx(b"\x01\x02", None))[0] == "success" and checkers.apply(fn, _ctx(b"\x01\x01", None))[0] == "normal"
    out, detail = checkers.apply(checkers.resolve("broken", None, d), _ctx(b"\x01", None))
    assert out == "garbled" and "failed to load" in detail


# --- the whole chain on the simulator ---------------------------------------------
def test_aes_fault_campaign_and_dfa_over_the_api(client):
    r = client.get("/api/glitch/checkers").json()
    assert [c["name"] for c in r["checkers"]][:5] == ["expected", "loop_counter", "aes_dfa", "flag", "expr"] and "ctx is a dict" in r["help"]
    assert client.post("/api/target/simpleserial", json={"cmd": "k", "data": KEY.hex(), "read_len": 0}).status_code == 200
    ct = client.post("/api/target/simpleserial", json={"cmd": "p", "data": PT.hex(), "read_cmd": "r", "read_len": 16}).json()["response"]
    assert ct == encrypt_block(KEY, PT).hex()
    assert client.post("/api/scope/action/vglitch_lp").status_code == 200
    client.put("/api/scope/settings", json={"path": "glitch.width", "value": 1148})  # 25 percent of a period: inside the simulator's success window
    r = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.ext_offset", "start": 20, "stop": 60, "step": 1, "int": True}], "repeats": 3,
                                              "command": "p", "data": PT.hex(), "expected": ct, "output_len": 16, "reset": "nrst", "checker": "aes_dfa"})
    assert r.status_code == 200, r.text
    st = wait_job(client)
    assert st["job"]["error"] is None and st["job"]["checker"] == "aes_dfa"
    res = client.get("/api/glitch/results").json()
    assert res["counts"]["success"] >= 8 and res["checker"] == "aes_dfa" and res["expected"] == ct
    assert all(r["detail"].startswith("usable fault") for r in res["results"] if r["result"] == "success")
    assert not any(r["result"] == "garbled" for r in res["results"])  # every simulated fault lands on one byte entering round 9
    d = client.post("/api/analysis/dfa", json={"source": "glitch"}).json()
    assert d["key"] == KEY.hex(), d
    assert d["correct"] == ct and d["solved_columns"] == 4 and d["plaintext"] == PT.hex()
    # the same from pasted ciphertexts, and a refusal without any
    faulty = [r["response"] for r in res["results"] if r["result"] == "success"]
    d2 = client.post("/api/analysis/dfa", json={"correct": ct, "faulty": faulty}).json()
    assert d2["key"] == KEY.hex()
    assert client.post("/api/analysis/dfa", json={"correct": ct, "faulty": []}).status_code == 400
    r = client.post("/api/glitch/export", json={"path": "faults.csv"})
    assert r.status_code == 200
    with open(r.json()["path"]) as f:
        assert "detail" in f.readline()
    client.post("/api/scope/action/glitch_disable")


def test_expr_checker_and_refusals_over_the_api(client):
    assert client.post("/api/scope/action/vglitch_lp").status_code == 200
    client.put("/api/scope/settings", json={"path": "glitch.width", "value": 1148})
    r = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.ext_offset", "values": [30, 40]}], "repeats": 4, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "nrst",
                                              "checker": "expr", "checker_expr": "('success', f'{expected_value - value} skipped') if value is not None and value < expected_value else None"})
    assert r.status_code == 200, r.text
    wait_job(client)
    res = client.get("/api/glitch/results").json()
    assert res["counts"]["success"] >= 1 and all("skipped" in r["detail"] for r in res["results"] if r["result"] == "success")
    bad = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.ext_offset", "values": [30]}], "command": "g", "checker": "no_such"})
    assert bad.status_code == 400 and "unknown checker" in bad.text
    bad = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.ext_offset", "values": [30]}], "command": "g", "checker": "expr", "checker_expr": ""})
    assert bad.status_code == 400 and "needs an expression" in bad.text
    client.post("/api/scope/action/glitch_disable")


# --- captures streamed to disk ------------------------------------------------------
def test_disk_trace_writer_roundtrip():
    d = tempfile.mkdtemp()
    w = DiskTraceWriter(os.path.join(d, "run1_waves.npy"))
    for i in range(7):
        w.append(np.full(1000, i, np.float32), bytes([i]) * 16, bytes([i + 1]) * 16, KEY)
    assert w.count == 7 and w.samples == 1000 and w.nbytes == 7 * 4000
    with pytest.raises(RuntimeError):
        w.append(np.zeros(999, np.float32), b"", b"", b"")
    info = w.close()
    assert info["path"] == os.path.join(d, "run1_waves.npy") and info["closed"]
    W = np.load(info["path"], mmap_mode="r")
    assert W.shape == (7, 1000) and W.dtype == np.float32 and float(W[6, 0]) == 6.0
    s = TraceStore()
    assert s.import_file(info["path"]) == 7
    assert s.textins[3] == bytes([3]) * 16 and s.textouts[3] == bytes([4]) * 16 and s.keys[3] == KEY


def test_streamed_capture_to_disk_over_the_api(client):
    client.delete("/api/traces")
    data_dir = client.session.data_dir
    client.put("/api/scope/settings", json={"path": "adc.stream", "value": True})
    r = client.put("/api/scope/settings", json={"path": "adc.samples", "value": 300000})  # above the Husky's 137209 in one capture: only streaming allows it
    assert r.status_code == 200, r.text
    try:
        r = client.post("/api/capture/start", json={"count": 5, "store": False, "disk": "stream_test", "key": KEY.hex()})
        assert r.status_code == 200, r.text
        st = wait_job(client)
        job = st["job"]
        assert job["error"] is None and job["done"] == 5, job
        assert job["disk"]["count"] == 5 and job["disk"]["samples"] == 300000 and job["disk"]["closed"] and job["disk"]["bytes"] == 5 * 300000 * 4
        path = os.path.join(data_dir, "stream_test_waves.npy")
        assert job["disk"]["path"] == path and os.path.getsize(path) == 128 + 5 * 300000 * 4
        assert st["traces"]["count"] == 0  # nothing kept in memory
        W = np.load(path, mmap_mode="r")
        assert W.shape == (5, 300000)
        r = client.post("/api/traces/import", json={"path": "stream_test_waves.npy"}).json()
        assert r["imported"] == 5 and r["samples"] == 300000
        assert client.get("/api/traces/2/meta").json()["key"] == KEY.hex()
        # a sample count change mid-capture ends the capture with the reason instead of corrupting the file
        r = client.post("/api/capture/start", json={"count": 0, "store": False, "disk": "stream_test2"})
        assert r.status_code == 200
        time.sleep(0.4)
        client.put("/api/scope/settings", json={"path": "adc.samples", "value": 200000})
        st = wait_job(client)
        assert st["job"]["error"] and "samples" in st["job"]["error"], st["job"]
    finally:
        client.put("/api/scope/settings", json={"path": "adc.samples", "value": 3000})
        client.put("/api/scope/settings", json={"path": "adc.stream", "value": False})
        client.delete("/api/traces")
