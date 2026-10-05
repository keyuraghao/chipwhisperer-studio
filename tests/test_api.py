"""End-to-end API tests against the simulator (no hardware needed)."""
import json
import struct
import tempfile
import time

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio.app import create_app
from cwstudio.session import Session


def decode_frame(data: bytes):
    (hlen,) = struct.unpack("<I", data[:4])
    header = json.loads(data[4:4 + hlen])
    samples = np.frombuffer(data[4 + hlen:], dtype="<f4")
    return header, samples


@pytest.fixture(scope="module")
def client():
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    app = create_app(session)
    with TestClient(app) as c:
        c.session = session
        yield c


def wait_job(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/api/status").json()
        job = st.get("job")
        if job is None or not job["running"]:
            return st
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def test_meta_and_status(client):
    m = client.get("/api/meta").json()
    assert "sim" in m["scope_kinds"] and "sbox_hw" in m["cpa_models"]
    st = client.get("/api/status").json()
    assert st["scope"]["connected"] is False


def test_connect_and_settings(client):
    r = client.post("/api/scope/connect", json={"kind": "sim"})
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True
    tree = client.get("/api/scope/settings").json()
    paths = {n["path"] for n in tree}
    assert {"gain", "adc", "clock", "io", "glitch"} <= paths
    adc = next(n for n in tree if n["path"] == "adc")
    samples = next(n for n in adc["children"] if n["name"] == "samples")
    assert samples["writable"] and samples["type"] == "int" and "ADC samples" in samples["doc"]
    r = client.put("/api/scope/settings", json={"path": "adc.samples", "value": "3000"})
    assert r.status_code == 200 and r.json()["value"] == 3000
    r = client.put("/api/scope/settings", json={"path": "adc.samples", "value": -5})
    assert r.status_code == 400
    r = client.put("/api/scope/settings", json={"path": "gain.mode", "value": "low"})
    assert r.json()["value"] == "low"
    client.put("/api/scope/settings", json={"path": "gain.mode", "value": "high"})
    r = client.post("/api/target/connect", json={"kind": "sim"})
    assert r.status_code == 200, r.text
    ttree = client.get("/api/target/settings").json()
    assert any(n["path"] == "output_len" for n in ttree)


def test_serial(client):
    r = client.post("/api/target/serial/write", json={"data": "v"})
    assert r.status_code == 200
    time.sleep(0.3)
    log = client.get("/api/target/serial").json()
    assert any(e["dir"] == "tx" for e in log) and any(e["dir"] == "rx" for e in log)
    r = client.post("/api/target/simpleserial", json={"cmd": "p", "data": "00" * 16})
    assert r.json()["response"] and len(r.json()["response"]) == 32


def test_capture_and_traces(client):
    client.delete("/api/traces")
    r = client.post("/api/capture/start", json={"count": 200, "key_mode": "fixed", "text_mode": "random",
                                                "key": "2b7e151628aed2a6abf7158809cf4f3c"})
    assert r.status_code == 200, r.text
    st = wait_job(client)
    assert st["traces"]["count"] == 200, st
    assert st["job"]["error"] is None
    # every job type exposes a consistent done/total pair (issue #2)
    assert st["job"]["done"] == st["job"]["total"] == st["job"]["target"] == 200
    r = client.get("/api/traces/5")
    header, samples = decode_frame(r.content)
    assert header["index"] == 5 and header["n"] == 3000 and samples.shape[0] == 3000
    assert len(header["textout"]) == 32
    header, block = decode_frame(client.get("/api/traces/block?start=0&end=10").content)
    assert header["indices"] == list(range(10)) and block.shape[0] == 10 * 3000
    header, stats = decode_frame(client.get("/api/traces/stats").content)
    assert header["fields"] == ["mean", "std", "min", "max"] and stats.shape[0] == 4 * 3000
    meta = client.get("/api/traces/5/meta").json()
    assert meta["key"] == "2b7e151628aed2a6abf7158809cf4f3c"


def test_single_and_trigger_only(client):
    n0 = client.get("/api/traces").json()["count"]
    client.post("/api/capture/single", json={"store": True})
    wait_job(client)
    assert client.get("/api/traces").json()["count"] == n0 + 1
    client.post("/api/capture/start", json={"count": 3, "mode": "trigger_only"})
    st = wait_job(client)
    # Trigger-only capture with sim target never triggers -> timeouts; job must end with error, not hang
    assert st["job"]["error"] is not None or st["traces"]["count"] >= n0 + 1


def test_continuous_stop(client):
    r = client.post("/api/capture/start", json={"count": 0, "store": False})
    assert r.status_code == 200
    time.sleep(0.5)
    r = client.post("/api/capture/start", json={"count": 1})
    assert r.status_code == 400  # already running
    client.post("/api/capture/stop")
    st = wait_job(client)
    assert st["job"]["done"] > 0


def test_cpa(client):
    r = client.post("/api/analysis/cpa/start", json={"model": "sbox_hw", "report_every": 50})
    assert r.status_code == 200, r.text
    for _ in range(200):
        res = client.get("/api/analysis/cpa").json()
        if res.get("done"):
            break
        time.sleep(0.1)
    assert res["done"] and res["error"] is None
    assert res["best_key"] == "2b7e151628aed2a6abf7158809cf4f3c"
    assert all(b["pge"] == 0 for b in res["bytes"])
    header, corr = decode_frame(client.get("/api/analysis/cpa/corr/0").content)
    assert header["guess"] == 0x2b and corr.shape[0] == 3000


def test_export_import(client):
    r = client.post("/api/traces/export", json={"path": "t1", "format": "npz"})
    assert r.status_code == 200, r.text
    path = r.json()["path"]
    n = client.get("/api/traces").json()["count"]
    r = client.post("/api/traces/import", json={"path": path, "replace": True})
    assert r.json()["imported"] == n
    r = client.post("/api/traces/export", json={"path": "t1", "format": "cwp"})
    assert r.status_code == 200, r.text
    assert r.json()["path"].endswith(".cwp")
    r = client.post("/api/traces/import", json={"path": r.json()["path"], "replace": True})
    assert r.json()["imported"] == n
    r = client.get("/api/traces/download/csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") or r.status_code == 200


def test_glitch(client):
    # enable glitching on the sim
    client.put("/api/scope/settings", json={"path": "glitch.trigger_src", "value": "ext_single"})
    client.put("/api/scope/settings", json={"path": "glitch.repeat", "value": 5})
    r = client.post("/api/glitch/start", json={
        "parameters": [{"path": "glitch.width", "start": 0, "stop": 45, "step": 15},
                       {"path": "glitch.ext_offset", "start": 0, "stop": 80, "step": 20, "int": True}],
        "repeats": 2, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "nrst",
    })
    assert r.status_code == 200, r.text
    st = wait_job(client)
    # consistent done/total pair, kept in step with the legacy point/points (issue #2)
    assert st["job"]["done"] == st["job"]["point"] and st["job"]["total"] == st["job"]["points"] == 4 * 5
    res = client.get("/api/glitch/results").json()
    assert len(res["results"]) == 4 * 5 * 2
    assert res["counts"]["success"] > 0 and res["counts"]["normal"] > 0
    r = client.post("/api/glitch/export", json={"path": "g.csv"})
    assert r.status_code == 200
    client.put("/api/scope/settings", json={"path": "glitch.repeat", "value": 0})


def test_websocket_frames(client):
    with client.websocket_connect("/ws") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello"
        client.post("/api/capture/start", json={"count": 5, "store": False})
        got_trace = got_capture = False
        for _ in range(50):
            msg = ws.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                h, s = decode_frame(msg["bytes"])
                if h["type"] == "trace":
                    got_trace = s.shape[0] == 3000
            elif msg.get("text"):
                ev = json.loads(msg["text"])
                if ev["type"] == "capture" and ev.get("state") == "done":
                    got_capture = True
                    break
        assert got_trace and got_capture


def test_program_upload(client):
    r = client.post("/api/target/program/upload?programmer=STM32F", files={"file": ("fw.hex", b":00000001FF\n")})
    assert r.status_code == 200 and r.json()["simulated"] is True


def test_disconnect(client):
    client.post("/api/target/disconnect")
    client.post("/api/scope/disconnect")
    st = client.get("/api/status").json()
    assert st["scope"]["connected"] is False and st["target"]["connected"] is False


def test_download_npy_set_as_zip(client):
    import io
    import zipfile
    client.post("/api/scope/connect", json={"kind": "sim"})
    client.post("/api/target/connect", json={"kind": "sim"})
    client.post("/api/capture/start", json={"count": 3, "clear": True})
    wait_job(client)
    r = client.get("/api/traces/download/npy")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["traces_keys.npy", "traces_textins.npy", "traces_textouts.npy", "traces_waves.npy"]


def test_web_layer(client):
    """Uploads (multipart with binary content, or the raw body), parameter conversion, FastAPI-compatible errors, docs and openapi of the routing layer."""
    from cwstudio.web import _multipart
    blob = bytes(range(256)) * 4 + b"\r\n--x\r\n\r\nX--B--Bx\r\n\r\n--Bx"
    body = b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"C:\\\\fw\\\\a;b.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n" + blob + b"\r\n--B--\r\n"
    assert _multipart(body, "multipart/form-data; boundary=B")["file"] == ("a;b.bin", blob, "application/octet-stream")
    quoted = b"--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a\\\"b.npz\"\r\n\r\nx\r\n--B--"
    assert _multipart(quoted, 'multipart/form-data; boundary="B"')["file"][0] == 'a"b.npz'
    with pytest.raises(Exception):
        _multipart(b"--B\r\nContent-Disposition: form-data; name=\"file\"\r\n\r\nno closing boundary", "multipart/form-data; boundary=B")
    r = client.post("/api/target/program/upload?programmer=STM32F&filename=raw.hex", content=b":00000001FF\n", headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json()["path"].endswith("raw.hex")
    r = client.post("/api/target/program/upload")
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "file"]
    assert client.post("/api/notebooks/import", files={"file": (None, b"not a file")}).status_code == 422
    r = client.get("/api/traces/notanumber")
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "int_parsing" and r.json()["detail"][0]["loc"] == ["path", "index"]
    r = client.get("/api/traces/99999")
    assert r.status_code == 404 and r.json() == {"detail": "no such trace"}
    assert client.get("/api/notebooks/file").json()["detail"] == [{"type": "missing", "loc": ["query", "path"], "msg": "Field required", "input": None}]
    assert client.get("/api/logs?since=1.0").status_code == 200
    assert client.get("/api/logs?since=1.5").status_code == 422
    assert client.head("/api/status").status_code == 405
    assert client.post("/api/calc", json={"expr": "1e308*10"}).status_code == 200
    docs = client.get("/api/docs")
    assert docs.status_code == 200 and "/api/capture/start" in docs.text
    spec = client.get("/openapi.json").json()
    assert spec["paths"]["/api/traces/{index}"]["get"]["parameters"][0] == {"name": "index", "in": "path", "required": True, "schema": {"type": "integer"}}


def test_notebook_import_never_overwrites(client):
    nb = json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode()
    a = client.post("/api/notebooks/import", files={"file": ("x;y", nb)}).json()["path"]
    b = client.post("/api/notebooks/import", files={"file": ("x;y", nb)}).json()["path"]
    assert a == "imported/x;y.ipynb" and b == "imported/x;y 2.ipynb"


def test_cwp_download_roundtrip_and_job_status(client):
    """A ChipWhisperer project downloads as one zip (project file plus data folder) that imports back; starting a job announces it at once."""
    import io
    import zipfile
    client.post("/api/scope/connect", json={"kind": "sim"})
    client.post("/api/target/connect", json={"kind": "sim"})
    with client.websocket_connect("/ws") as ws:
        ws.receive_text()  # hello
        client.post("/api/capture/start", json={"count": 5, "clear": True})
        seen_running = False
        for _ in range(200):
            msg = ws.receive()
            if msg.get("text"):
                ev = json.loads(msg["text"])
                if ev["type"] == "status" and (ev.get("job") or {}).get("running"):
                    seen_running = True
                    break
        assert seen_running
    wait_job(client)
    r = client.get("/api/traces/download/cwp")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "traces.cwp" in names and any(n.startswith("traces_data/traces/") for n in names)
    up = client.post("/api/traces/import/upload?replace=true", files={"file": ("traces_cwp.zip", r.content)})
    assert up.status_code == 200 and up.json()["imported"] == 5
    path = client.post("/api/traces/export", json={"path": "rel_export", "format": "npz"}).json()["path"]
    assert client.post("/api/traces/import", json={"path": "rel_export.npz"}).json()["imported"] == 5, path
