"""Every ChipWhisperer model looks and refuses like its hardware: the profile (profiles.py) and the simulator's per-model settings shape."""
import tempfile
import time

import pytest
from starlette.testclient import TestClient

from cwstudio import settings as cws
from cwstudio.app import create_app
from cwstudio.session import Session
from cwstudio.capabilities import Unsupported, capabilities, check_setting, gate_settings, setting_gates
from cwstudio.profiles import MODEL_PROFILES, glitch_params, profile_for, sample_limit
from cwstudio.simulator import SimScope, SimTarget

MODELS = ["nano", "lite", "pro", "husky", "huskyplus"]


def tree_paths(scope):
    return set(cws.flatten(gate_settings(cws.describe(scope), scope)))


@pytest.mark.parametrize("model", MODELS)
def test_profile_is_complete(model):
    p = MODEL_PROFILES[model]
    for key in ("label", "brain", "adc", "clock", "trigger", "glitch", "monitors", "firmware", "best_for", "avoid"):
        assert key in p, key
    assert p["adc"]["bits"] in p["adc"]["bits_choices"] and p["adc"]["max_samples"][p["adc"]["bits"]] > 0
    assert p["glitch"]["params"] and all({"path", "label", "unit", "min", "max", "int"} <= set(row) for row in p["glitch"]["params"])
    c = capabilities(SimScope(sim_model=model))
    assert c["profile"]["label"] == p["label"] and c["profile"]["adc"]["max_samples"] == p["adc"]["max_samples"]


def test_profiles_carry_the_hardware_numbers():
    assert MODEL_PROFILES["nano"]["adc"]["max_samples"] == {8: 100000}
    assert MODEL_PROFILES["lite"]["adc"]["max_samples"] == {10: 24400}
    assert MODEL_PROFILES["pro"]["adc"]["max_samples"] == {10: 96000} and MODEL_PROFILES["pro"]["adc"]["stream"]
    assert MODEL_PROFILES["husky"]["adc"]["max_samples"] == {12: 137209, 8: 204796}
    assert MODEL_PROFILES["huskyplus"]["adc"]["max_samples"] == {12: 346246, 8: 518344}
    assert MODEL_PROFILES["husky"]["trigger"]["sequencer_slots"] == 2 and MODEL_PROFILES["huskyplus"]["trigger"]["sequencer_slots"] == 4
    assert MODEL_PROFILES["husky"]["glitch"]["max_glitches"] == 32
    assert [r["path"] for r in glitch_params("nano")] == ["glitch.ext_offset", "glitch.repeat"]
    assert [r["path"] for r in glitch_params("lite")][:2] == ["glitch.width", "glitch.offset"] and glitch_params("lite")[0]["unit"].startswith("%")
    assert glitch_params("husky")[0]["unit"] == "phase steps" and any(r["path"] == "glitch.num_glitches" for r in glitch_params("husky"))


@pytest.mark.parametrize("model", MODELS)
def test_simulated_model_has_its_hardware_shape(model):
    s = SimScope(sim_model=model)
    paths = tree_paths(s)
    if model == "nano":
        assert {"adc.samples", "adc.clk_freq", "adc.clk_src", "io.clkout", "io.tio3", "io.nrst", "glitch.ext_offset", "glitch.repeat"} <= paths
        for gone in ("adc.presamples", "adc.decimate", "adc.basic_mode", "gain.gain", "clock.clkgen_freq", "trigger.module", "io.hs2", "io.target_pwr", "glitch.width", "glitch.offset", "glitch.trigger_src"):
            assert gone not in paths, gone
    else:
        assert {"adc.presamples", "adc.decimate", "adc.basic_mode", "gain.gain", "clock.clkgen_freq", "trigger.module", "io.hs2", "glitch.width", "glitch.offset", "glitch.ext_offset", "glitch.repeat"} <= paths
        assert "io.clkout" not in paths and "adc.clk_freq" not in paths
    husky = model in ("husky", "huskyplus")
    for p in ("adc.bits_per_sample", "adc.segments", "adc.max_samples", "adc.errors", "glitch.num_glitches", "glitch.phase_shift_steps", "glitch.enabled"):
        assert (p in paths) == husky, p
    for p in ("glitch.width_fine", "glitch.offset_fine"):
        assert (p in paths) == (model in ("lite", "pro")), p
    assert ("adc.stream" in paths) == (model in ("pro", "husky", "huskyplus"))
    assert ("clock.adc_mul" in paths) == husky


@pytest.mark.parametrize("model", MODELS)
def test_sample_limits_follow_the_model(model):
    s = SimScope(sim_model=model)
    limit = {"nano": 100000, "lite": 24400, "pro": 96000, "husky": 137209, "huskyplus": 346246}[model]
    assert sample_limit(s, model) == limit
    cws.set_value(s, "adc.samples", limit)
    with pytest.raises(ValueError):
        cws.set_value(s, "adc.samples", limit + 1)
    with pytest.raises(Unsupported, match=f"at most {limit}"):
        check_setting(s, "adc.samples", limit + 1)
    if model in ("pro", "husky", "huskyplus"):
        s.adc.stream = True
        assert sample_limit(s, model) is None
        cws.set_value(s, "adc.samples", 2_000_000)  # streaming lifts the limit
        check_setting(s, "adc.samples", 2_000_000)
    if model in ("husky", "huskyplus"):
        s.adc.stream = False
        s.adc.bits_per_sample = 8
        assert sample_limit(s, model) == {"husky": 204796, "huskyplus": 518344}[model] == s.adc.max_samples
        with pytest.raises(ValueError):
            s.adc.presamples = 1
        s.adc.presamples = 26000
        with pytest.raises(ValueError):
            s.adc.presamples = 10 ** 6


def test_nano_refuses_what_it_lacks():
    s = SimScope(sim_model="nano")
    gates = setting_gates(s)
    for path in ("adc.presamples", "gain.gain", "clock.clkgen_freq", "glitch.width", "io.hs2"):
        assert "unavailable" in gates[path], path
        with pytest.raises(Unsupported):
            check_setting(s, path, 1)
    assert cws.set_value(s, "adc.clk_freq", 14e6) == 15e6  # nearest of the seven divisions of 240 MHz
    assert cws.set_value(s, "io.clkout", 8e6) == 7.5e6
    cws.set_value(s, "glitch.repeat", 0)
    assert s._spc() == 2  # 15 MHz ADC against a 7.5 MHz target clock


def test_lite_and_pro_refuse_husky_settings():
    for model in ("lite", "pro"):
        s = SimScope(sim_model=model)
        gates = setting_gates(s)
        for path in ("adc.segments", "adc.bits_per_sample", "glitch.num_glitches"):
            assert "unavailable" in gates[path], (model, path)
        assert ("adc.stream" in gates and "unavailable" in gates["adc.stream"]) == (model == "lite")
        assert "extclk_aux_io" in gates["clock.clkgen_src"]["disabled"]
    assert "extclk_aux_io" not in setting_gates(SimScope(sim_model="husky")).get("clock.clkgen_src", {}).get("disabled", {})


def test_glitch_units_per_model():
    nano = SimScope(sim_model="nano")
    nano.glitch.repeat = 4
    assert nano.glitch.width_percent() == 25.0
    with pytest.raises(ValueError):
        nano.glitch.repeat = -1
    lite = SimScope(sim_model="lite")
    lite.glitch.width = 20
    assert lite.glitch.width_percent() == 20.0
    with pytest.raises(ValueError):
        lite.glitch.width = 60
    husky = SimScope(sim_model="husky")
    assert husky.glitch.phase_shift_steps == 4592
    husky.glitch.width = 1148
    assert round(husky.glitch.width_percent()) == 25
    with pytest.raises(ValueError):
        husky.glitch.width = 3000
    with pytest.raises(ValueError):
        husky.glitch.num_glitches = 33


@pytest.mark.parametrize("model", MODELS)
def test_glitch_outcomes_and_setups_per_model(model):
    s = SimScope(sim_model=model)
    t = SimTarget(s)
    t.con(s)
    s.vglitch_setup("lp")
    assert t._glitch_active()  # armed on every model (the Nano with repeat 1, the others on ext_single); a zero width still gives a normal outcome
    assert t._glitch_outcome() == "normal"
    if model == "nano":
        s.glitch.ext_offset, s.glitch.repeat = 40, 4  # 25 percent, inside the window
    else:
        s.glitch.trigger_src = "ext_single"
        s.glitch.repeat = 1
        s.glitch.ext_offset = 40
        s.glitch.width = 1148 if model in ("husky", "huskyplus") else 25
    assert t._glitch_active()
    outcomes = {t._glitch_outcome() for _ in range(40)}
    assert "success" in outcomes
    s.glitch_disable()
    assert not t._glitch_active()
    if model != "nano":
        s.cglitch_setup()
        assert s.io.hs2 == "glitch" and not s.io.glitch_lp
    else:
        with pytest.raises(AttributeError):
            s.cglitch_setup()


def test_husky_reports_clip_errors_and_glitch_counts():
    s = SimScope(sim_model="husky")
    t = SimTarget(s)
    t.con(s)
    s.gain.gain = 75
    s.arm()
    t.simpleserial_write("p", bytes(16))
    assert not s.capture()
    assert "clip" in s.adc.errors
    s.adc.errors = 0
    assert s.adc.errors == ""
    s.vglitch_setup(default_setup=False)
    s.glitch.num_glitches = 3
    s.glitch.repeat = 1
    s.glitch.width = 100
    s.arm()
    t.simpleserial_write("p", bytes(16))
    s.capture()
    assert s.glitch.actual_num_glitches == 3
    s.glitch.reset_glitch_counter()
    assert s.glitch.actual_num_glitches == 0
    live = profile_for(s, "husky")
    assert live["adc"]["live"]["max_samples_now"] == 137209 and live["glitch"]["phase_shift_steps"] == 4592


@pytest.fixture(scope="module")
def client():
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def use(client, model):
    assert client.post("/api/scope/connect", json={"kind": "sim", "sim_model": model}).json()["sim_model"] == model
    assert client.post("/api/target/connect", json={"kind": "sim"}).status_code == 200


def wait_job(client, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/api/status").json()
        if not (st.get("job") or {}).get("running"):
            return st
        time.sleep(0.05)
    raise TimeoutError


def flat_settings(client):
    out = {}

    def walk(ns):
        for n in ns:
            if n.get("kind") == "group":
                walk(n.get("children") or [])
            else:
                out[n["path"]] = n.get("value")
    walk(client.get("/api/scope/settings").json())
    return out


@pytest.mark.parametrize("model", MODELS)
def test_glitch_presets_per_model(client, model):
    """The Glitch tab's presets call the library setup functions on every model: clock glitch (not on the Nano), voltage glitch with the chosen MOSFET, disable."""
    use(client, model)
    caps = client.get("/api/capabilities").json()
    prof = caps["profile"]["glitch"]
    assert [r["path"] for r in prof["params"]] == [r["path"] for r in glitch_params(model)]
    r = client.post("/api/scope/action/cglitch_setup")
    if model == "nano":
        assert r.status_code == 400 and not prof["clock_glitch"]
    else:
        assert r.status_code == 200, r.text
        f = flat_settings(client)
        assert f["io.hs2"] == "glitch" and f["glitch.trigger_src"] == "ext_single" and f["io.glitch_lp"] is False
    assert client.post("/api/scope/action/vglitch_hp").status_code == 200
    f = flat_settings(client)
    if model == "nano":
        assert f["glitch.repeat"] == 1
    else:
        assert f["io.glitch_hp"] is True and f["io.glitch_lp"] is False and f["glitch.output"] == "glitch_only"
    assert client.post("/api/scope/action/vglitch_both").status_code == 200
    if model != "nano":
        f = flat_settings(client)
        assert f["io.glitch_hp"] is True and f["io.glitch_lp"] is True
    assert client.post("/api/scope/action/glitch_disable").status_code == 200
    f = flat_settings(client)
    if model == "nano":
        assert f["glitch.repeat"] == 0
    else:
        assert f["io.glitch_hp"] is False and f["io.glitch_lp"] is False and f["glitch.trigger_src"] == "manual"


def test_glitch_sweep_classes_on_the_husky(client):
    """A Husky sweep reports how many glitches each point fired: a point that fired none is 'not fired', not 'normal'."""
    use(client, "husky")
    assert client.post("/api/scope/action/vglitch_lp").status_code == 200
    client.put("/api/scope/settings", json={"path": "glitch.trigger_src", "value": "manual"})  # armed but never triggered: nothing fires
    r = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.width", "values": [918]}, {"path": "glitch.ext_offset", "values": [40]}], "repeats": 3, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "none"})
    assert r.status_code == 200, r.text
    wait_job(client)
    res = client.get("/api/glitch/results").json()
    assert res["counts"]["not_fired"] == 3 and all(x["fired"] == 0 for x in res["results"])
    client.put("/api/scope/settings", json={"path": "glitch.trigger_src", "value": "ext_single"})
    r = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.width", "values": [918]}, {"path": "glitch.ext_offset", "values": [40]}], "repeats": 12, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "nrst"})
    assert r.status_code == 200, r.text
    wait_job(client)
    res = client.get("/api/glitch/results").json()
    assert res["counts"]["not_fired"] == 0 and res["counts"]["success"] > 0 and all(x["fired"] == 1 for x in res["results"])
    assert client.post("/api/scope/action/glitch_disable").status_code == 200


def test_glitch_sweep_on_the_nano(client):
    """The Nano sweeps its two firmware parameters; the result rows carry no fired count (the Nano has no counter)."""
    use(client, "nano")
    r = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.ext_offset", "values": [40]}, {"path": "glitch.repeat", "values": [4]}], "repeats": 8, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "nrst"})
    assert r.status_code == 200, r.text
    wait_job(client)
    res = client.get("/api/glitch/results").json()
    assert len(res["results"]) == 8 and res["counts"]["success"] > 0 and "fired" not in res["results"][0]
    r = client.post("/api/glitch/start", json={"parameters": [{"path": "glitch.width", "values": [10]}], "command": "g"})
    assert r.status_code == 400 and "ext_offset and repeat" in r.json()["detail"]  # refused up front with the model's reason


def test_segmented_capture_on_the_husky(client):
    """adc.segments on a Husky: one arm, N commands, one download, N stored traces with their own plaintexts; CPA still recovers the key from them."""
    use(client, "husky")
    assert client.put("/api/scope/settings", json={"path": "adc.segments", "value": 4}).status_code == 200
    r = client.post("/api/capture/start", json={"count": 60, "clear": True, "key": "2b7e151628aed2a6abf7158809cf4f3c"})
    assert r.status_code == 200 and r.json()["segments"] == 4
    st = wait_job(client)
    assert st["job"]["done"] == 60 and not st["job"]["error"] and st["traces"]["count"] == 60 and st["traces"]["samples"] == 5000
    metas = [client.get(f"/api/traces/{i}/meta").json() for i in range(8)]
    assert len({m["textin"] for m in metas}) == 8 and all(m["textout"] for m in metas)
    assert client.post("/api/analysis/cpa/start", json={"model": "sbox_hw", "report_every": 60}).status_code == 200
    for _ in range(200):
        res = client.get("/api/analysis/cpa").json()
        if res.get("done"):
            break
        time.sleep(0.1)
    assert res["best_key"] == "2b7e151628aed2a6abf7158809cf4f3c"
    client.put("/api/scope/settings", json={"path": "adc.segments", "value": 1})


def test_segments_refused_elsewhere(client):
    for model in ("lite", "pro", "nano"):
        use(client, model)
        r = client.put("/api/scope/settings", json={"path": "adc.segments", "value": 4})
        assert r.status_code == 400 and "Husky" in r.json()["detail"], (model, r.text)
        r = client.post("/api/capture/start", json={"count": 2, "clear": True})
        assert r.status_code == 200 and r.json()["segments"] == 1
        assert wait_job(client)["job"]["done"] == 2


def test_capture_errors_reach_the_job(client):
    """A Husky capture error (the simulator flags a clip when the gain saturates) lands in the job progress and the log, and clears on write."""
    use(client, "husky")
    client.put("/api/scope/settings", json={"path": "gain.gain", "value": 75})
    client.post("/api/capture/start", json={"count": 3, "clear": True})
    st = wait_job(client)
    assert "clip" in (st["job"]["hw_errors"] or "") and st["job"]["hw_error_count"] == 3 and not st["job"]["error"]
    assert any("capture error" in (e.get("msg") or "") for e in client.get("/api/logs").json())
    assert client.put("/api/scope/settings", json={"path": "adc.errors", "value": 0}).status_code == 200
    client.put("/api/scope/settings", json={"path": "gain.gain", "value": 30})
    client.post("/api/capture/start", json={"count": 2, "clear": True})
    assert wait_job(client)["job"]["hw_errors"] is None
    use(client, "lite")
    client.post("/api/capture/start", json={"count": 1, "clear": True})
    assert wait_job(client)["job"]["hw_errors"] is None


def test_health_per_model(client):
    """The health reading follows the model's monitors: XADC and capture errors on a Husky, locks on a Lite, serial overflows only on a Nano; problems clear through the clear route."""
    use(client, "husky")
    h = client.get("/api/scope/health?refresh=true").json()
    assert h["model"] == "husky" and h["xadc"]["temp"] == 48.2 and h["xadc"]["rails"]["vccint"] == 1.0 and h["locked"] == {"clkgen_locked": True, "adc_locked": True} and h["problems"] == []
    client.put("/api/scope/settings", json={"path": "gain.gain", "value": 75})
    client.post("/api/capture/start", json={"count": 2, "clear": True})
    wait_job(client)
    client.session.scope.XADC.temp = 90  # over the user threshold: the alarm latches
    h = client.get("/api/scope/health?refresh=true").json()
    assert "clip" in h["adc_errors"] and h["xadc"]["alarms"] and len(h["problems"]) == 2
    assert client.get("/api/status").json()["health"]["problems"] == h["problems"]
    client.session.scope.XADC.temp = 48  # cooled down, the alarm stays latched until cleared
    assert len(client.get("/api/scope/health?refresh=true").json()["problems"]) == 2
    r = client.post("/api/scope/health/clear").json()
    assert "errors" in r["cleared"] and r["health"]["problems"] == [] and r["health"]["adc_errors"] == ""
    client.put("/api/scope/settings", json={"path": "gain.gain", "value": 30})
    use(client, "lite")
    h = client.get("/api/scope/health?refresh=true").json()
    assert "xadc" not in h and h["locked"] == {"clkgen_locked": True, "adc_locked": True} and "trig_count" in h and h["problems"] == []
    use(client, "nano")
    h = client.get("/api/scope/health?refresh=true").json()
    assert "xadc" not in h and "locked" not in h and h["sam_errors"] == "" and h["problems"] == []
    client.session.scope.errors.sam_errors = "serial rx overflow"
    h = client.get("/api/scope/health?refresh=true").json()
    assert h["problems"] and "overflow" in h["problems"][0]
    assert client.post("/api/scope/health/clear").json()["health"]["problems"] == []


def test_sad_engines_per_model(client):
    """The SAD trigger scores as each engine does: the Pro sums differences over 128 samples, the Husky counts samples over an interval across 256 (512 extended) with a mask; the preview says where it fires before arming."""
    from cwstudio import sadmodel
    assert sadmodel.reference_length("pro") == 128 and sadmodel.reference_length("husky") == 256 and sadmodel.reference_length("husky", True) == 512
    assert sadmodel.parse_mask("0-9,20", 32) == [True] * 10 + [False] * 10 + [True] + [False] * 11
    with pytest.raises(ValueError):
        sadmodel.parse_mask("0-40", 32)
    for model in ("husky", "pro"):
        use(client, model)
        client.post("/api/capture/start", json={"count": 20, "clear": True, "key": "2b7e151628aed2a6abf7158809cf4f3c"})
        wait_job(client)
        body = {"trace": 0, "start": 100, "threshold": 20 if model == "husky" else 3000, "interval": 12}
        pv = client.post("/api/interfaces/trigger/sad/preview", json=body).json()
        assert pv["reference_samples"] == (256 if model == "husky" else 128) and pv["mode"] == ("interval" if model == "husky" else "sum")
        assert pv["rows"][0]["fires"] and pv["rows"][0]["at"] == 100 + pv["reference_samples"]  # the reference matches itself exactly
        assert pv["fires"] >= 1 and pv["suggested_threshold"] >= 0
        r = client.put("/api/interfaces/trigger", json={"kind": "sad", **body, "mask": "0-199" if model == "husky" else None, "emode": False})
        assert r.status_code == 200, r.text
        assert r.json()["applied"]["samples"] == pv["reference_samples"]
        bad = client.put("/api/interfaces/trigger", json={"kind": "sad", "threshold": 100000 if model == "husky" else 10 ** 7, "start": 0})
        assert bad.status_code == 400
        client.post("/api/capture/start", json={"count": 3, "clear": False, "max_timeouts": 3})
        st = wait_job(client)
        assert st["job"]["done"] + st["job"]["timeouts"] == 3  # the simulated engine scored the capture (it fires on the reference's own trace shape or times out, never crashes)
        client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio4"]})


def test_trigger_outputs_per_model(client):
    use(client, "husky")
    r = client.get("/api/interfaces/outputs").json()
    assert r["available"] and set(r["ports"]) == {"trig_mcx", "aux_mcx"} and r["ports"]["trig_mcx"]["value"] == "trigger"
    assert client.put("/api/interfaces/outputs", json={"port": "trig_mcx", "value": "glitch"}).json()["ports"]["trig_mcx"]["value"] == "glitch"
    assert client.put("/api/interfaces/outputs", json={"port": "aux_mcx", "value": "hs2"}).status_code == 200
    assert client.put("/api/interfaces/outputs", json={"port": "aux", "value": "true"}).status_code == 400
    use(client, "pro")
    r = client.get("/api/interfaces/outputs").json()
    assert set(r["ports"]) == {"aux"} and r["ports"]["aux"]["value"] is False
    assert client.put("/api/interfaces/outputs", json={"port": "aux", "value": "true"}).json()["ports"]["aux"]["value"] is True
    for model in ("lite", "nano"):
        use(client, model)
        r = client.get("/api/interfaces/outputs").json()
        assert not r["available"] and r["reason"]


def test_userio_modes(client):
    use(client, "husky")
    for mode in ("trace", "target_debug_swd", "fpga_debug", "normal"):
        r = client.put("/api/interfaces/userio", json={"mode": mode})
        assert r.status_code == 200 and r.json()["mode"] == mode, r.text
    assert client.put("/api/interfaces/userio", json={"mode": "bogus"}).status_code == 400
    use(client, "lite")
    assert client.put("/api/interfaces/userio", json={"mode": "trace"}).status_code == 400


def test_sequencer_steps_per_model(client):
    """The sequencer takes N steps of any trigger kind (2 slots on a Husky, 4 on a Plus) with a window between consecutive steps, and the simulator chains them on what the target does."""
    use(client, "huskyplus")
    client.post("/api/capture/start", json={"count": 2, "clear": True})
    wait_job(client)
    body = {"kind": "sequencer", "steps": [{"kind": "basic", "pins": ["tio4"]}, {"kind": "uart_pattern", "pin": "tio1", "pattern": "'r'", "baud": 38400}, {"kind": "basic", "pins": ["tio1"]}], "windows": [[0, 0], [0, 0]], "edge": "falling_edge"}
    r = client.put("/api/interfaces/trigger", json=body)
    assert r.status_code == 200, r.text
    ap = r.json()["applied"]
    assert ap["enabled"] and [s["kind"] for s in ap["steps"]] == ["basic", "uart_pattern", "basic"] and ap["windows"] == [[0, 0], [0, 0]]
    client.post("/api/capture/start", json={"count": 2, "clear": True, "max_timeouts": 2})
    st = wait_job(client)
    assert st["job"]["done"] == 2 and not st["job"]["error"]  # TIO4 falls, the 'r' reply is seen, then a falling edge on TIO1 after it
    tight = dict(body, windows=[[0, 1], [0, 0]])
    assert client.put("/api/interfaces/trigger", json=tight).status_code == 200
    client.post("/api/capture/start", json={"count": 1, "clear": True, "max_timeouts": 1})
    st = wait_job(client)
    assert st["job"]["timeouts"] == 1  # the reply cannot come within one cycle of the trigger fall
    five = dict(body, steps=body["steps"] + [{"kind": "basic", "pins": ["tio3"]}, {"kind": "basic", "pins": ["tio2"]}])
    r = client.put("/api/interfaces/trigger", json=five)
    assert r.status_code == 400 and "2 to 4" in r.json()["detail"]
    use(client, "husky")
    r = client.put("/api/interfaces/trigger", json=body)
    assert r.status_code == 400 and "2 to 2" in r.json()["detail"]
    r = client.put("/api/interfaces/trigger", json={"kind": "sequencer", "pins": ["tio4", "tio1"], "window_end": 0})  # legacy two-pin form
    assert r.status_code == 200 and len(r.json()["applied"]["steps"]) == 2
    client.post("/api/capture/start", json={"count": 1, "clear": True, "max_timeouts": 2})
    assert wait_job(client)["job"]["done"] == 1
    r = client.put("/api/interfaces/trigger", json={"kind": "sequencer", "steps": [{"kind": "sad", "threshold": 20}, {"kind": "sad", "threshold": 20}]})
    assert r.status_code == 400 and "once" in r.json()["detail"]
    client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio4"]})
    use(client, "lite")
    assert client.put("/api/interfaces/trigger", json=body).status_code == 400


def test_firmware_status_and_upgrade(client):
    """Status shows the scope's SAM firmware against the library's bundled one and NewAE's upstream; an outdated scope offers the upgrade, which the simulator performs in place."""
    use(client, "husky")
    fw = client.get("/api/status").json()["scope"]["firmware"]
    assert fw["version"] == "1.7.0" and fw["library"] == "1.7.0" and fw["upstream"] == "1.8.0" and not fw["upgrade_available"]
    client.session.scope.fw_version = {"major": 1, "minor": 5, "debug": 0}
    client.session.worker.call(client.session._refresh_scope_info)
    fw = client.get("/api/status").json()["scope"]["firmware"]
    assert fw["version"] == "1.5.0" and fw["upgrade_available"]
    r = client.post("/api/scope/firmware/upgrade").json()
    assert r == {"upgraded": True, "before": "1.5.0", "after": "1.7.0", "replug": False}
    assert not client.get("/api/status").json()["scope"]["firmware"]["upgrade_available"]
    use(client, "nano")
    fw = client.get("/api/status").json()["scope"]["firmware"]
    assert fw["version"] == "0.65.0" and fw["upstream"] == "0.66.0"


def test_recipes_per_model(client):
    from cwstudio.recipes import GOALS
    for model in MODELS:
        use(client, model)
        rs = client.get("/api/recipes").json()
        assert [r["id"] for r in rs] == GOALS
        byid = {r["id"]: r for r in rs}
        assert byid["trigger_without_gpio"]["available"] == (model in ("pro", "husky", "huskyplus"))
        assert byid["clock_glitch"]["available"] == (model != "nano")
        a = client.post("/api/recipes/fast_capture/apply")
        assert a.status_code == 200, a.text
        if model in ("husky", "huskyplus"):
            assert "adc.segments = 20" in a.json()["applied"] and flat_settings(client)["adc.segments"] == 20
            client.put("/api/scope/settings", json={"path": "adc.segments", "value": 1})
        a = client.post("/api/recipes/voltage_glitch/apply")
        assert a.status_code == 200 and "action vglitch_lp" in a.json()["applied"]
        assert client.post("/api/scope/action/glitch_disable").status_code == 200
        if model == "nano":
            r = client.post("/api/recipes/clock_glitch/apply")
            assert r.status_code == 400 and "crowbar" in r.json()["detail"]
    po = client.get("/api/pinout").json()
    assert po["best_for"] and po["avoid"] and po["brain"]


def test_usb_pipe_hint():
    from cwstudio.hardware import usb_pipe_hint

    class USBErrorPipe(Exception):
        pass
    try:
        try:
            raise USBErrorPipe("LIBUSB_ERROR_PIPE [-9]")
        except USBErrorPipe as inner:
            raise RuntimeError("request failed") from inner
    except RuntimeError as e:
        assert "firmware" in usb_pipe_hint(e)
    assert usb_pipe_hint(ValueError("x")) is None
