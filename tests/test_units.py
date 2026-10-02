import os
import threading
import time

import numpy as np
import pytest

from cwstudio import settings as cws
from cwstudio.simulator import SimScope, SimTarget
from cwstudio.worker import HardwareWorker, LongJob
from cwstudio.traces import TraceStore
from cwstudio.capture import KeyTextGen


def test_describe_and_set_sim_scope():
    s = SimScope()
    nodes = cws.describe(s)
    flat = cws.flatten(nodes)
    assert flat["adc.samples"] == 5000 and flat["gain.mode"] == "high"
    adc = next(n for n in nodes if n["path"] == "adc")
    leaf = next(n for n in adc["children"] if n["name"] == "samples")
    assert leaf["writable"] and leaf["type"] == "int"
    trig = next(n for n in adc["children"] if n["name"] == "trig_count")
    assert not trig["writable"]
    assert cws.set_value(s, "adc.samples", "1234") == 1234
    assert cws.set_value(s, "clock.clkgen_freq", "7.37e6") == 7370000
    assert cws.set_value(s, "io.target_pwr", "false") is False
    assert cws.set_value(s, "gain.mode", "low") == "low"
    with pytest.raises(ValueError):
        cws.set_value(s, "adc.samples", "-1")
    with pytest.raises(ValueError):
        cws.set_value(s, "adc.basic_mode", "bogus")


def test_coerce():
    assert cws.coerce("0x10", 5) == 16
    assert cws.coerce("3.5", 1.0) == 3.5
    assert cws.coerce("None", "high_z", [None, "high_z"]) is None
    assert cws.coerce("true", False) is True
    assert cws.coerce(7, 3) == 7
    assert cws.coerce(2.0, 3) == 2
    assert cws.coerce("[1, 2]", [0]) == [1, 2]


def test_worker_short_and_long_jobs():
    w = HardwareWorker()
    w.start()
    try:
        assert w.call(lambda a, b: a + b, 2, 3) == 5
        with pytest.raises(ZeroDivisionError):
            w.call(lambda: 1 / 0)

        class Counter(LongJob):
            name = "counter"

            def __init__(self):
                super().__init__()
                self.n = 0

            def step(self):
                self.n += 1
                time.sleep(0.001)
                return self.n >= 50

        j = Counter()
        assert w.start_long_job(j)
        assert not w.start_long_job(Counter())  # only one at a time
        # short jobs are serviced while the long job runs
        assert w.call(lambda: "ok", timeout=5) == "ok"
        assert j.finished.wait(5)
        assert j.n == 50 and j.error is None

        j2 = Counter()
        j2.step = lambda: (time.sleep(0.005), False)[1]
        w.start_long_job(j2)
        time.sleep(0.05)
        w.stop_long_job()
        assert j2.finished.wait(5)
    finally:
        w.stop()


def test_trace_store_arrays_and_stats(tmp_path):
    st = TraceStore()
    for i in range(10):
        st.append(np.full(100, i, np.float32), bytes([i] * 16), bytes(16), bytes(range(16)))
    W, tin, tout, key = st.as_arrays()
    assert W.shape == (10, 100) and tin.shape == (10, 16) and key[0].tolist() == list(range(16))
    s = st.stats()
    assert s["mean"][0] == pytest.approx(4.5) and s["max"][0] == 9
    p = st.export(str(tmp_path / "x"), "npz")
    st2 = TraceStore()
    assert st2.import_file(p) == 10
    assert st2.get(3)[1] == bytes([3] * 16)
    p = st.export(str(tmp_path / "y"), "csv")
    assert open(p).read().count("\n") == 11


def test_keytextgen():
    g = KeyTextGen("fixed", "random", key=bytes(16), seed=1)
    k1, t1 = g.next_pair()
    k2, t2 = g.next_pair()
    assert k1 == k2 == bytes(16) and t1 != t2
    g = KeyTextGen("fixed", "counter", text=bytes(16))
    assert g.next_pair()[1][-1] == 1 and g.next_pair()[1][-1] == 2


def test_sim_glitch_outcomes():
    s = SimScope(seed=3)
    t = SimTarget()
    t.con(s)
    s.glitch.trigger_src = "ext_single"
    s.glitch.repeat = 5
    s.glitch.width = 20
    s.glitch.ext_offset = 40
    succ = 0
    for _ in range(40):
        s.arm()
        t.simpleserial_write("g", b"")
        s.capture()
        r = t.simpleserial_read_witherrors("r", 4)
        if r["valid"] and bytes(r["payload"]) != (2500).to_bytes(4, "little"):
            succ += 1
    assert succ > 10
    s.glitch.ext_offset = 500
    normal = 0
    for _ in range(20):
        s.arm()
        t.simpleserial_write("g", b"")
        s.capture()
        r = t.simpleserial_read_witherrors("r", 4)
        normal += bool(r["valid"] and bytes(r["payload"]) == (2500).to_bytes(4, "little"))
    assert normal >= 15


def test_running_stats_match_numpy_and_follow_changes():
    """Whole-set statistics come from running sums updated with new traces only; they match numpy and reset on clear and import."""
    import numpy as np
    from cwstudio.traces import TraceStore
    st = TraceStore()
    rng = np.random.default_rng(3)
    for _ in range(700):
        st.append((rng.standard_normal(300) * 0.1).astype(np.float32), b"", b"", b"")
    st.stats()
    for _ in range(123):  # added after the first call: folded in incrementally
        st.append((rng.standard_normal(300) * 0.1).astype(np.float32), b"", b"", b"")
    got, W = st.stats(), np.stack(st.waves)
    assert np.allclose(got["mean"], W.mean(0), atol=1e-6) and np.allclose(got["std"], W.std(0), atol=1e-6)
    assert np.array_equal(got["min"], W.min(0)) and np.array_equal(got["max"], W.max(0))
    part = st.stats(10, 20)  # a sub-range is computed directly
    assert np.allclose(part["mean"], W[10:20].mean(0))
    st.clear()
    assert st.stats() == {}
    st.append(np.full(5, 2, np.float32), b"", b"", b"")
    assert np.array_equal(st.stats()["mean"], np.full(5, 2, np.float32))
    st.append(np.full(4, 4, np.float32), b"", b"", b"")  # mixed lengths: truncated to the shortest, as before
    assert np.array_equal(st.stats()["mean"], np.full(4, 3, np.float32))


def test_npz_export_is_readable_and_round_trips(tmp_path):
    import json
    import numpy as np
    from cwstudio.traces import TraceStore
    st = TraceStore()
    st.meta = {"scope": "sim"}
    for i in range(20):
        st.append(np.arange(50, dtype=np.float32) * i, bytes([i]) * 16, bytes(16), bytes(range(16)))
    path = st.export(str(tmp_path / "t"), "npz")
    d = np.load(path, allow_pickle=False)
    assert d["waves"].shape == (20, 50) and json.loads(str(d["meta"])) == {"scope": "sim"} and not os.path.exists(path + ".part")
    st2 = TraceStore()
    assert st2.import_file(path) == 20 and np.array_equal(np.stack(st2.waves), np.stack(st.waves)) and st2.textins == st.textins


def test_event_history_keeps_only_log_kinds():
    """Only log, capture and glitch events are kept for /api/logs; notebook outputs with figures are not."""
    from cwstudio.events import EventBus
    bus = EventBus()
    bus.publish("nb", {"data": {"image/png": "x" * 100000}})
    bus.publish("log", {"msg": "hello"})
    bus.publish("cpa", {"history": list(range(1000))})
    assert [e["type"] for e in bus.history] == ["log"]


def test_sim_trigger_honours_configured_pins():
    """The simulated target only drives TIO4: a trigger on other pins times out like real hardware, and the default stays unchanged."""
    s = SimScope(seed=1)
    t = SimTarget()
    t.con(s)
    s.adc.timeout = 0.05
    cases = [("tio4", False), ("TIO4", False), ("tio1 OR tio4", False), ("tio4 AND tio1", False), ("tio1 NAND tio4", False),
             ("tio1", True), ("nrst", True), ("sma", True), ("userio_d0", True), ("tio4 AND tio3", True), ("tio1 AND tio2", True)]
    for trig, timeout in cases:
        s.trigger.triggers = trig
        s.arm()
        t.simpleserial_write("p", bytes(16))
        assert s.capture() is timeout, trig
        t.simpleserial_read("r", 16)
    # a timed-out capture leaves no stale trigger behind: the next capture on TIO4 waits for its own command
    s.trigger.triggers = "tio1"
    s.arm()
    t.simpleserial_write("p", bytes(16))
    assert s.capture() is True
    t.simpleserial_read("r", 16)
    s.trigger.triggers = "tio4"
    s.arm()
    assert s.capture() is True
    s.default_setup()
    assert s.trigger.triggers == "tio4" and s.trigger_driven()


def test_sim_adc_clock_follows_adc_src_and_adc_mul():
    """The simulated ADC rate is the target clock times the multiplier set by adc_src (CW-Lite, Pro) or adc_mul (Husky); the AES leakage moves with it, and the default stays 4 samples per cycle."""
    import pytest
    s = SimScope(seed=2)
    t = SimTarget()
    t.con(s)
    s.default_setup()
    assert (s.clock.adc_src, s.clock.adc_mul, s.clock.adc_freq) == ("clkgen_x4", 4, 29480000)

    def leak_center():
        s.adc.samples = 3000
        s.noise = 0.0
        waves = []
        for pt in (bytes(16), bytes([0x55] * 16)):
            s.arm()
            t.simpleserial_write("p", pt)
            s.capture()
            t.simpleserial_read("r", 16)
            waves.append(s.get_last_trace().copy())
        return int(np.argmax(np.abs(waves[1] - waves[0])[:600]))  # first key-dependent bump: S-box byte 0

    at4, trig4 = leak_center(), s.adc.trig_count
    s.clock.adc_src = "clkgen_x1"
    assert s.clock.adc_mul == 1 and s.clock.adc_freq == 7370000
    at1, trig1 = leak_center(), s.adc.trig_count
    assert abs(at1 - at4 / 4) <= 2 and trig1 * 4 == pytest.approx(trig4, rel=0.05)
    s.clock.adc_mul = 2
    assert s.clock.adc_freq == 14740000
    s.clock.clkgen_freq = 10e6
    assert s.clock.adc_freq == 20000000 and s.clock.freq_ctr == 10000000
    s.clock.adc_mul = 4
    assert s.clock.adc_src == "clkgen_x4" and s.clock.adc_freq == 40000000
    s.clock.adc_src = "extclk_x1"
    assert s.clock.adc_mul == 1 and s.clock.adc_freq == 10000000
    for bad in (("adc_src", "pll"), ("adc_mul", 0), ("adc_mul", 17)):
        with pytest.raises(ValueError):
            setattr(s.clock, *bad)
    s.default_setup()
    assert s.clock.adc_mul == 4 and leak_center() == at4


def test_ui_numbers_use_latin_digits():
    """The UI formats numbers and clock times through api.js fmtNum/fmtClock (Latin digits in every locale), never with toLocaleString, toLocaleTimeString or Intl directly."""
    import pathlib
    import re
    js = pathlib.Path(__file__).resolve().parents[1] / "src" / "cwstudio" / "static" / "js"
    bad = [f"{p.name}:{i}" for p in sorted(js.glob("*.js")) if p.name != "api.js" for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if re.search(r"\.toLocale(String|TimeString|DateString)\(|\bIntl\.", line)]
    assert not bad, bad
