"""The logic viewer's visible-range query stays fast on a large capture: 10 channels of 10 million samples, from fully zoomed out to a few samples, through the HTTP API (JSON included)."""
import tempfile
import time

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio.app import create_app
from cwstudio.logic.model import Channel, LogicCapture
from cwstudio.session import Session

N = 10_000_000
LIMIT = 0.2  # seconds per visible-range query


def big_capture() -> LogicCapture:
    rng = np.random.default_rng(0)
    chans = [Channel("clk", 0, np.arange(1, N, dtype=np.int32))]  # toggles every sample: 10 million edges
    chans.append(Channel("clk/8", 0, np.arange(4, N, 4, dtype=np.int32)))
    for k in range(2, 10):
        gaps = rng.integers(1, 10 ** (k // 2 + 1), size=int(N / 10 ** (k // 2 + 1) * 2) + 10)
        e = np.cumsum(gaps)
        chans.append(Channel(f"rnd{k}", int(k & 1), e[e < N].astype(np.int32)))
    return LogicCapture(chans, N, 100e6, trigger=N // 2, source="perf")


def test_visible_range_query_10ch_10M():
    cap = big_capture()
    assert len(cap.channels) == 10 and cap.n == N and sum(c.edges.shape[0] for c in cap.channels) > 12_000_000
    s = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(s)) as c:
        s.logic.add_capture(cap)
        c.put("/api/la/channels", json={"buses": [{"name": "b", "channels": [0, 1, 2, 3]}]})
        c.post("/api/la/view", json={"a": 0, "b": 1000, "px": 100})  # warm up
        times = {}
        for a, b in [(0, N), (N // 3, N // 3 + N // 10), (4_000_000, 4_100_000), (5_000_000, 5_002_000), (N - 50, N)]:
            t0 = time.perf_counter()
            r = c.post("/api/la/view", json={"a": a, "b": b, "px": 2000})
            times[(a, b)] = time.perf_counter() - t0
            assert r.status_code == 200
            v = r.json()
            assert len(v["channels"]) == 10
            for ch in v["channels"]:
                assert len(ch.get("edges", [])) <= 2000 and len(ch.get("b", "")) <= 2000
        assert max(times.values()) < LIMIT, times
        # a decoder on a capture this big runs in the background: the view answers right away and says it is pending
        t0 = time.perf_counter()
        d = c.post("/api/la/decoders", json={"type": "uart", "channels": {"rx": "clk/8"}, "options": {"baud": 1e6}}).json()
        v = c.post("/api/la/view", json={"a": 0, "b": N, "px": 2000}).json()
        assert time.perf_counter() - t0 < LIMIT and d["pending"]
        for _ in range(600):
            v = c.post("/api/la/view", json={"a": 0, "b": N, "px": 2000}).json()
            if not v["decoders"][0].get("pending"):
                break
            time.sleep(0.05)
        assert "rows" in v["decoders"][0] and not v["decoders"][0].get("pending")
        # the model query alone is a few milliseconds
        t0 = time.perf_counter()
        for _ in range(20):
            cap.view(0, N, 3000)
        assert (time.perf_counter() - t0) / 20 < 0.05
        # measurements on the densest channel stay quick too
        t0 = time.perf_counter()
        m = cap.measure("clk/8")
        assert m["frequency"] == pytest.approx(100e6 / 8) and time.perf_counter() - t0 < 2.0
