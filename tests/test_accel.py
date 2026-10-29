"""Hardware acceleration: backend detection with its off switch, the CPA on whichever backend is present, and the GPU path agreeing with the CPU path when a GPU is there."""
import os

import numpy as np
import pytest

from cwstudio import accel
from cwstudio.analysis import CPAAttack
from cwstudio.simulator import SimScope, SimTarget

KEY = bytes(range(16))


def _traces(n, samples, seed=0):
    s = SimScope(seed=seed)
    s.default_setup()
    s.adc.samples = samples
    t = SimTarget()
    t.con(s)
    rng = np.random.default_rng(seed)
    W, tin = np.empty((n, samples), np.float32), np.empty((n, 16), np.uint8)
    for i in range(n):
        pt = rng.integers(0, 256, 16, dtype=np.uint8)
        t.simpleserial_write("p", bytearray(pt.tobytes()))
        s.arm()
        s.capture()
        W[i], tin[i] = s.get_last_trace(), pt
    return W, tin, np.zeros((n, 16), np.uint8)


def _cpa(W, tin, tout, **kw):
    c = CPAAttack(W, tin, tout, model="sbox_hw", **kw)
    c.start()
    c._thread.join(300)
    assert c.result.done and not c.result.error, c.result.error
    return c.result


def test_backend_reports_and_off_switch(monkeypatch):
    info = accel.info()
    assert info["backend"] in ("cpu", "cuda") and info["label"]
    if info["backend"] == "cpu":
        assert info["reason"]
    monkeypatch.setenv("CWSTUDIO_GPU", "0")
    b = accel.backend(refresh=True)
    assert b["name"] == "cpu" and b["xp"] is np and "CWSTUDIO_GPU" in b["reason"]
    monkeypatch.delenv("CWSTUDIO_GPU")
    accel.backend(refresh=True)
    assert accel.to_host(np.arange(3)).tolist() == [0, 1, 2]


def test_cpa_result_names_its_backend():
    W, tin, tout = _traces(80, 1200)  # every leak position inside the window, far below the GPU threshold
    r = _cpa(W, tin, tout, report_every=30)
    assert r.backend == "cpu"  # too small for the GPU whatever the machine has
    assert r.to_json()["backend"] == "cpu" and r.to_json()["best_key"] == KEY.hex()


@pytest.mark.skipif(not accel.gpu_available(), reason="no CUDA GPU with CuPy on this machine")
def test_gpu_cpa_matches_cpu(monkeypatch):
    W, tin, tout = _traces(300, 1000)
    gpu = _cpa(W, tin, tout, report_every=100)
    assert gpu.backend == "cuda"
    monkeypatch.setenv("CWSTUDIO_GPU", "0")
    accel.backend(refresh=True)
    try:
        cpu = _cpa(W, tin, tout, report_every=100)
    finally:
        monkeypatch.delenv("CWSTUDIO_GPU")
        accel.backend(refresh=True)
    assert cpu.backend == "cpu"
    assert gpu.to_json()["best_key"] == cpu.to_json()["best_key"] == KEY.hex()
    assert np.array_equal(gpu.corr_argmax, cpu.corr_argmax)
    assert np.abs(gpu.corr_max - cpu.corr_max).max() < 1e-4
    for b in range(16):
        assert np.abs(gpu.best_corr_trace[b] - cpu.best_corr_trace[b]).max() < 1e-4
    assert [e["best_key"] for e in gpu.history] == [e["best_key"] for e in cpu.history]


@pytest.mark.skipif(not accel.gpu_available(), reason="no CUDA GPU with CuPy on this machine")
def test_gpu_failure_falls_back_to_the_cpu(monkeypatch, caplog):
    import cupy
    W, tin, tout = _traces(300, 1000)
    real = cupy.matmul
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("simulated CUDA failure")
    monkeypatch.setattr(cupy, "matmul", boom)
    try:
        r = _cpa(W, tin, tout, report_every=100)
    finally:
        monkeypatch.setattr(cupy, "matmul", real)
    assert calls["n"] == 1 and r.backend == "cpu" and r.to_json()["best_key"] == KEY.hex()
    assert any("running it on the CPU" in m for m in caplog.messages)
