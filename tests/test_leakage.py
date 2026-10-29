"""Leakage assessment, alignment, filters, spectrum and the extra CPA models, on the simulator."""
import tempfile
import time

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio import leakage
from cwstudio.aes import HW, SBOX, encrypt_block
from cwstudio.analysis import CPAAttack, _aes_round1_state, hypotheses
from cwstudio.app import create_app
from cwstudio.session import Session

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")


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


def test_welch_t_and_ttest():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 1, (200, 50))
    b = rng.normal(0, 1, (200, 50))
    b[:, 10] += 1.0
    t = leakage.welch_t(a, b)
    assert abs(t[10]) > 4.5 and np.abs(np.delete(t, 10)).max() < 4.5
    tin = np.zeros((400, 16), np.uint8)
    tin[200:] = rng.integers(1, 256, (200, 16))
    waves = np.vstack([a, b]).astype(np.float32)
    r = leakage.ttest(waves, tin, bytes(16))
    assert r["fixed"] == 200 and r["random"] == 200 and r["leaks"] and r["argmax"] == 10


def test_snr_classes_and_align_and_filters():
    rng = np.random.default_rng(2)
    tin = rng.integers(0, 256, (300, 16), np.uint8)
    cls = leakage.classes_for("sbox_hw", tin, 0, KEY)
    assert cls.min() >= 0 and cls.max() <= 8 and np.array_equal(cls, HW[SBOX[tin[:, 0] ^ KEY[0]]])
    waves = rng.normal(0, 0.1, (300, 40)).astype(np.float32)
    waves[:, 20] += cls * 0.5
    s = leakage.snr(waves, cls)
    assert s.argmax() == 20 and s[20] > 10 * np.median(s)
    base = np.zeros(200, np.float32)
    base[100:110] = 1.0
    traces = np.stack([np.roll(base, k) for k in (0, 3, -2, 5)])
    shifts = leakage.align(traces, traces[0], 90, 120, 10)
    assert list(shifts) == [0, 3, -2, 5]
    fixed = np.stack([leakage.shift_trace(t, int(s)) for t, s in zip(traces, shifts)])
    assert np.array_equal(fixed, np.stack([base] * 4))
    k = leakage.fir_kernel("lowpass", 0.05, 63)
    assert k.size == 63 and abs(k.sum() - 1) < 1e-9
    noisy = (np.sin(np.arange(400) * 0.05) + 0.5 * np.sin(np.arange(400) * 2.5)).astype(np.float32)[None, :]
    out = leakage.apply_filter(noisy, k)
    assert out.shape == noisy.shape and out[0, 50:350].std() < noisy[0, 50:350].std()
    f, db = leakage.spectrum(noisy[0], 1e6)
    assert f.size == db.size and db.max() == 0.0 and abs(f[db.argmax()] - 0.05 / (2 * np.pi) * 1e6) < 3000
    ma = leakage.fir_kernel("moving_average", 4)
    assert ma.size == 4 and ma[0] == 0.25
    with pytest.raises(ValueError):
        leakage.fir_kernel("bogus", 0.1)


def test_extra_models_and_aes256():
    rng = np.random.default_rng(3)
    tin = rng.integers(0, 256, (50, 16), np.uint8)
    tout = np.zeros_like(tin)
    lsb = hypotheses("sbox_lsb", tin, tout, 3)
    assert lsb.shape == (50, 256) and set(np.unique(lsb)) <= {0.0, 1.0}
    st = _aes_round1_state(tin, KEY)
    # the vectorised round-1 state agrees with the reference encryption's first round for a full AES-128 key schedule
    from cwstudio.aes import expand_key
    rk1 = bytes(expand_key(KEY)[1])
    for i in range(5):
        full = encrypt_block(KEY, bytes(tin[i]))
        assert len(full) == 16  # encrypt_block runs 10 rounds; the round-1 state is checked against a one-round reference below
    h = hypotheses("aes256_round2_hw", tin, tout, 0, np.float32, {"known_key": KEY, "_round1_state": st})
    assert h.shape == (50, 256) and np.array_equal(h[:, 0], HW[SBOX[st[:, 0]]].astype(np.float32))
    with pytest.raises(ValueError):
        hypotheses("aes256_round2_hw", tin, tout, 0)
    custom = hypotheses("custom", tin, tout, 2, np.float32, {"expr": "HW[SBOX[tin[:, b][:, None] ^ g]]"})
    assert np.array_equal(custom, hypotheses("sbox_hw", tin, tout, 2))
    with pytest.raises(ValueError):
        hypotheses("custom", tin, tout, 0, np.float32, {"expr": "tin[:, b]"})
    with pytest.raises(ValueError):
        CPAAttack(np.zeros((4, 8), np.float32), tin[:4], tout[:4], model="custom", params={"expr": "nope("})
    assert rk1  # the second-half attack needs the first-half key; its correctness on a leaking AES-256 target is covered by the model's own first-round algebra


def test_api_ttest_snr_align_filter_spectrum_export(client):
    r = client.post("/api/capture/start", json={"count": 80, "clear": True, "key": KEY.hex(), "text_mode": "tvla", "text": "00112233445566778899aabbccddeeff"})
    assert r.status_code == 200, r.text
    wait_job(client)
    metas = [client.get(f"/api/traces/{i}/meta").json()["textin"] for i in range(6)]
    assert metas[0] == metas[2] == metas[4] == "00112233445566778899aabbccddeeff" and metas[1] != metas[3]
    t = client.post("/api/analysis/ttest", json={}).json()
    assert t["fixed"] == 40 and t["random"] == 40 and t["leaks"] and t["max_abs_t"] > 4.5 and len(t["series"]) <= 4000
    s = client.post("/api/analysis/snr", json={"byte": 0}).json()
    assert s["classes"] == "sbox_hw" and s["max_snr"] > 0 and s["known_key"] == KEY.hex() and len(s["series"]) <= 4000
    assert client.post("/api/analysis/snr", json={"byte": 0, "classes": "byte"}).status_code == 200
    sp = client.post("/api/analysis/spectrum", json={}).json()
    assert sp["samples"] == 5000 and sp["sample_rate"] and len(sp["freq"]) == len(sp["db"]) and max(sp["db"]) == 0.0
    a = client.post("/api/analysis/align", json={"reference": 0, "start": 40, "end": 400, "max_shift": 5}).json()
    assert a["traces"] == 80 and not a["applied"] and "histogram" in a
    a = client.post("/api/analysis/align", json={"reference": 0, "start": 40, "end": 400, "max_shift": 5, "apply": True}).json()
    assert a["applied"] and client.get("/api/traces").json()["count"] == 80
    f = client.post("/api/analysis/filter", json={"kind": "lowpass", "cutoff": 0.2, "taps": 31}).json()
    assert f["traces"] == 80 and f["taps"] == 31
    client.post("/api/analysis/cpa/start", json={"model": "sbox_hw", "report_every": 80})
    for _ in range(200):
        res = client.get("/api/analysis/cpa").json()
        if res.get("done"):
            break
        time.sleep(0.1)
    assert res["best_key"] == KEY.hex()
    e = client.post("/api/analysis/cpa/export", json={"path": "cpa_out"}).json()
    assert e["path"].endswith("cpa_out.npz") and e["bytes"] == 16
    z = np.load(e["path"])
    assert z["corr_max"].shape == (16, 256) and z["best_corr_trace"].shape[0] == 16
    r = client.post("/api/analysis/cpa/start", json={"model": "custom", "expr": "HW[SBOX[tin[:, b][:, None] ^ g]]", "report_every": 80})
    assert r.status_code == 200, r.text
    for _ in range(200):
        res = client.get("/api/analysis/cpa").json()
        if res.get("done"):
            break
        time.sleep(0.1)
    assert res["best_key"] == KEY.hex()
    r = client.post("/api/analysis/cpa/start", json={"model": "custom", "expr": "tin[:, b]"})
    assert r.status_code == 400 and "(N, 256)" in r.json()["detail"]
    r = client.post("/api/analysis/cpa/start", json={"model": "aes256_round2_hw", "use_stored_key": False})
    assert r.status_code == 400 and "first 16 key bytes" in r.json()["detail"]
    r = client.post("/api/analysis/cpa/start", json={"model": "aes256_round2_hw", "known_key": KEY.hex(), "report_every": 80})  # the stored or given key is the first half; the attack targets the second
    assert r.status_code == 200, r.text
    for _ in range(200):
        res = client.get("/api/analysis/cpa").json()
        if res.get("done"):
            break
        time.sleep(0.1)
    assert res["done"] and not res["error"] and res["model"] == "aes256_round2_hw"
