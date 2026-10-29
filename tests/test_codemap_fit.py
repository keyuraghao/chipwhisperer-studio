"""Code map fit and progress: the piecewise warp on the mapping, the local refinement following a drift, chunked emulation with progress, the build phases, and the session's analysis activity (header chip)."""
import tempfile
import time

import numpy as np
import pytest

from cwstudio.activity import Activity
from cwstudio.codemap import power

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")


def _fwbuild():
    try:
        from tests import fwbuild
    except ImportError:
        import fwbuild
    return fwbuild


def elf_or_skip(*args, **kw):
    try:
        return _fwbuild().build_elf(*args, **kw)
    except RuntimeError as e:
        pytest.skip(f"firmware build not available: {str(e).splitlines()[0]}")


def wait_job(api, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = api.get("/api/status").json()
        if not (st.get("job") or {}).get("running"):
            return st
        time.sleep(0.05)
    raise TimeoutError


# ----- mapping with a warp ---------------------------------------------------------------------------------------
def test_mapping_warp_roundtrip_and_json():
    m = power.Mapping(spc=4.0, scale=1.01, shift=3.0, adc_offset=40, presamples=10)
    assert m.to_json().get("warp") is None
    m.knots = [[0.0, 0.0], [1000.0, 8.0], [2000.0, -4.0]]
    c = np.array([-500.0, 0.0, 250.0, 1000.0, 1500.0, 2000.0, 9000.0])
    s = m.sample(c)
    lin = c * m.a + m.b
    assert s[0] == pytest.approx(lin[0]) and s[1] == pytest.approx(lin[1])  # flat before the first knot (delta 0)
    assert s[2] == pytest.approx(lin[2] + 2.0) and s[3] == pytest.approx(lin[3] + 8.0) and s[4] == pytest.approx(lin[4] + 2.0)
    assert s[6] == pytest.approx(lin[6] - 4.0)  # flat after the last knot
    assert np.allclose(m.cycle(s), c)  # the inverse goes through the knots and the flat ends
    j = m.to_json()
    assert j["warp"] == {"knots": 3, "max_abs": 8.0, "span": [-4.0, 8.0]} and j["knots"] == m.knots
    assert np.allclose(power.Mapping(**{**m.__dict__, "knots": None}).sample(c), lin)


def _drifted(rng, P, stretch, L=24400):
    """A mean trace of ``P`` where the middle third of the run took ``stretch`` times longer than the model says."""
    cyc = np.arange(len(P), dtype=float)
    warped = np.concatenate([cyc[:2000], 2000 + (cyc[2000:4000] - 2000) * stretch, 2000 + 2000 * stretch + (cyc[4000:] - 4000)])
    Pw = np.interp(np.arange(0, warped[-1], 1.0), warped, P)
    return np.mean([power.synth_trace(Pw, 200, power.Mapping(spc=4.0), L, rng) for _ in range(8)], axis=0)


@pytest.mark.parametrize("stretch", [1.0, 1.02, 1.05])
def test_refine_follows_the_drift(stretch):
    rng = np.random.default_rng(7)
    P = 1.0 + 0.6 * rng.random(6000) + np.repeat(rng.random(120) > 0.7, 50) * 1.5
    mean = _drifted(rng, P, stretch)
    g = power.align(mean, P, 200, power.Mapping(spc=4.0))
    lin = power.Mapping(spc=4.0, scale=g["scale"], shift=g["shift"])
    t = time.time()
    r = power.refine(mean, P, 200, lin)
    assert time.time() - t < 1.0
    assert r["matched"] >= 20 and len(r["windows"]) >= 20 and {"corr_linear", "corr_warped"} <= set(r)
    assert all({"start", "end", "r", "lag", "used"} <= set(w) for w in r["windows"])
    truth = (2000 + 2000 * stretch + 1000 - 200) * 4  # where cycle 5000 really is
    if stretch == 1.0:
        # no drift: the warp cannot beat the straight line by the margin, so it is correctly not applied
        assert not r["applied"] and r["knots"] == [] and r["corr_warped"] == pytest.approx(r["corr_linear"]) and "did not improve" in r["reason"]
    else:
        assert r["applied"] and r["reason"] is None and r["corr_warped"] > r["corr_linear"] + 0.3
        warped = power.Mapping(**{**lin.__dict__, "knots": r["knots"]})
        assert abs(float(warped.sample(4800)) - truth) < 2 < abs(float(lin.sample(4800)) - truth)
        assert np.allclose(warped.cycle(warped.sample(np.array([0.0, 2500.0, 5000.0]))), [0.0, 2500.0, 5000.0])


def test_refine_refuses_without_matches():
    rng = np.random.default_rng(2)
    P = 1.0 + 0.6 * rng.random(4000)
    noise = rng.normal(0, 0.05, 16000)
    r = power.refine(noise, P, 100, power.Mapping(spc=4.0))
    assert not r["applied"] and "windows match" in r["reason"] and r["knots"] == []


def test_align_is_fast_on_long_traces():
    rng = np.random.default_rng(3)
    P = 1.0 + 0.6 * rng.random(30000) + np.repeat(rng.random(600) > 0.7, 50) * 1.5
    truth = power.Mapping(spc=4.0, scale=1.01, shift=33.0)
    mean = np.mean([power.synth_trace(P, 200, truth, 137000, rng) for _ in range(4)], axis=0)
    t = time.time()
    res = power.align(mean, P, 200, power.Mapping(spc=4.0))
    assert time.time() - t < 6.0  # was 4.6 s before the two-resolution search on this machine, now under 2
    assert res["scale"] == pytest.approx(1.01, abs=0.0025) and res["shift"] == pytest.approx(33.0, abs=2.0) and res["label"] == "high"


# ----- activity tracker -------------------------------------------------------------------------------------------
def test_activity_tracker():
    events = []
    a = Activity(publish=lambda kind, data: events.append((kind, data)))
    assert a.status() == {"active": [], "busy": False, "last": None}
    i = a.begin("CPA", "running", "sbox_hw on 100 traces")
    a.update(i, progress=0.5, detail="50/100 traces")
    st = a.status()
    assert st["busy"] and st["active"][0]["name"] == "CPA" and st["active"][0]["progress"] == 0.5 and st["active"][0]["detail"] == "50/100 traces"
    a.end(i, detail="100 traces")
    st = a.status()
    assert not st["busy"] and st["last"]["name"] == "CPA" and st["last"]["state"] == "done" and st["last"]["seconds"] >= 0
    with pytest.raises(ZeroDivisionError):
        with a.track("DFA") as update:
            update(phase="solving")
            assert a.status()["active"][0]["phase"] == "solving"
            1 / 0
    assert a.status()["last"]["state"] == "error" and "ZeroDivisionError" in a.status()["last"]["error"]
    with a.track("t-test"):
        pass
    assert a.status()["last"] == {**a.status()["last"], "name": "t-test", "state": "done"}
    assert events and all(k == "analysis" for k, _ in events)
    a.update(999, progress=1.0)  # unknown ids are ignored
    a.end(999)


# ----- chunked emulation with progress ------------------------------------------------------------------------------------
@pytest.mark.parametrize("platform,core_chunk", [("CWLITEARM", 3000), ("CWLITEXMEGA", 3000)])
def test_emulation_in_chunks_reports_progress_and_matches(platform, core_chunk):
    from cwstudio.codemap.emu import Firmware
    from cwstudio.codemap.program import Program
    elf = elf_or_skip(platform, "gcc", "TINYAES128C", "SS_VER_2_1")
    fw = Firmware(Program(elf))
    pt = bytes(range(16))
    run0, resp0 = fw.encrypt(KEY, pt)
    m = fw.machine
    old = m.CHUNK
    seen = []
    m.CHUNK = core_chunk
    m.progress = seen.append
    try:
        run1, resp1 = fw.encrypt(KEY, pt)
    finally:
        m.CHUNK = old
        m.progress = None
    assert resp0 == resp1 and run1.instructions == run0.instructions and run1.total_cycles == run0.total_cycles
    assert np.array_equal(run0.pcs, run1.pcs)
    assert seen and seen == sorted(seen) and seen[0] >= core_chunk and seen[-1] < run0.instructions  # reported between chunks, in order, never after the end


# ----- the build's phases and the header's analysis state over the API ------------------------------------------------------
@pytest.fixture(scope="module")
def api():
    from starlette.testclient import TestClient
    from cwstudio.app import create_app
    from cwstudio.session import Session
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def test_build_phases_refinement_and_activity(api):
    elf = elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    assert api.get("/api/codemap").json()["phase"]["phase"] == "idle"
    assert api.get("/api/status").json()["analysis"] == {"active": [], "busy": False, "last": None}
    api.post("/api/scope/connect", json={"kind": "sim"})
    api.post("/api/target/connect", json={"kind": "sim"})
    assert api.post("/api/target/program", json={"programmer": "STM32F", "path": elf}).json()["emulation"]["emulated"]
    api.post("/api/capture/start", json={"count": 30, "clear": True})
    wait_job(api)
    svc = api.session.codemap
    phases = []
    orig = svc._set_phase

    def record(phase, message=None, **extra):
        phases.append(phase)
        orig(phase, message, **extra)
    svc._set_phase = record
    try:
        st = api.post("/api/codemap/build", json={"elf": elf}).json()
    finally:
        svc._set_phase = orig
    assert phases[0] == "loading" and phases[-1] == "ready" and [p for p in phases if p != "emulating"] == ["loading", "timeline", "aligning", "refining", "ready"] and "emulating" in phases
    ph = st["phase"]
    assert ph["phase"] == "ready" and ph["message"].startswith("built in") and "aligned" in ph["message"] and ph["elapsed"] is not None and ph["instructions"] == st["instructions"]
    al = st["alignment"]
    assert al["label"] in ("high", "medium") and "refine" in al and al["refine"]["windows"] and all({"r", "lag", "used"} <= set(w) for w in al["refine"]["windows"])
    assert {"corr_linear", "corr_warped", "matched"} <= set(al["refine"])
    # the simulator runs the exact emulation it is matched against, so there is no drift: the window fit is near perfect but the warp is correctly NOT applied (it cannot beat the straight line), and no knots are added
    assert al["refine"]["matched"] >= 20 and al["refine"]["corr_linear"] > 0.9 and not al["refine"]["applied"] and st["mapping"]["knots"] is None
    an = api.get("/api/status").json()["analysis"]
    assert not an["busy"] and an["last"]["name"] == "code map" and an["last"]["state"] == "done" and an["last"]["seconds"] >= 0
    # clear_warp and reset are no-ops here (no warp) but keep the fit; lookups are stable across them
    sb = api.post("/api/codemap/lookup", json={"function": "SubBytes"}).json()["ranges"][0]["samples"]
    m = api.put("/api/codemap/mapping", json={"clear_warp": True}).json()["mapping"]
    assert m["knots"] is None and "warp" not in m and m["shift"] == pytest.approx(al["shift"]) and m["scale"] == pytest.approx(al["scale"])
    sb2 = api.post("/api/codemap/lookup", json={"function": "SubBytes"}).json()["ranges"][0]["samples"]
    assert abs(sb2[0] - sb[0]) < 4
    # (the warp-applies-on-drift path is covered by test_refine_follows_the_drift above)
    # align without refinement: no refine report, the linear fit stands
    r = api.post("/api/codemap/align", json={"source": "mean", "refine": False}).json()
    assert r["mapping"]["knots"] is None and "refine" not in r["alignment"] and api.get("/api/codemap").json()["phase"]["message"] == "aligned (" + r["alignment"]["label"] + ")"
    r = api.post("/api/codemap/align", json={"source": "mean", "windows": 1}).json()
    assert not r["alignment"]["refine"]["applied"] and "windows must be" in r["alignment"]["refine"]["reason"]
    # the window diagnostic still comes back, with correlations, even though the warp is not applied on the clean simulator
    r = api.post("/api/codemap/align", json={"source": "trace", "windows": 12}).json()
    assert r["alignment"]["refine"]["windows"] and "corr_linear" in r["alignment"]["refine"] and r["mapping"]["knots"] is None
    assert api.put("/api/codemap/mapping", json={"reset": True}).json()["mapping"]["knots"] is None
    # a failed build lands in the error phase and in the activity as a failure
    r = api.post("/api/codemap/build", json={"elf": elf, "options": {"max_instructions": 10}})
    assert r.status_code == 400
    ph = api.get("/api/codemap").json()["phase"]
    assert ph["phase"] == "error" and ph["error"] and "Unsupported" in ph["error"]
    assert api.get("/api/status").json()["analysis"]["last"]["state"] == "error"


def test_cpa_and_leakage_show_in_the_activity(api):
    api.post("/api/scope/connect", json={"kind": "sim"})
    api.post("/api/target/connect", json={"kind": "sim"})
    api.post("/api/capture/start", json={"count": 60, "clear": True, "key": KEY.hex()})
    wait_job(api)
    assert api.post("/api/analysis/cpa/start", json={"model": "sbox_hw", "report_every": 20}).status_code == 200
    an = None
    for _ in range(600):  # the result is marked done a moment before the finishing callback ends the activity
        an = api.get("/api/status").json()["analysis"]
        if not an["busy"] and an["last"] and an["last"]["name"] == "CPA":
            break
        assert not an["busy"] or an["active"][0]["name"] == "CPA"
        time.sleep(0.05)
    assert an and not an["busy"] and an["last"]["name"] == "CPA" and an["last"]["state"] == "done" and "traces" in an["last"]["detail"]
    api.post("/api/analysis/snr", json={"byte": 0, "known_key": KEY.hex()})
    assert api.get("/api/status").json()["analysis"]["last"]["name"] == "SNR"
    r = api.post("/api/analysis/dfa", json={"correct": "00" * 16, "faulty": []})
    assert r.status_code == 400
    last = api.get("/api/status").json()["analysis"]["last"]
    assert last["name"] == "DFA" and last["state"] == "error"
