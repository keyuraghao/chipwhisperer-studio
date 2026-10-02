"""Code map regression tests from the verification pass: firmware other than simpleserial-aes (glitch, password check, RSA and the tests' own cm-odd project that polls, sleeps, traps and halts), the Unicorn IT block workaround, halting and instruction limits with their hints, fault injection and glitches on the simulator's emulated firmware, the bounded run cache, the alignment search window and confidence, the decimated ADC offset, the source folder index and the HTTP API's input checks.

Tests that need a firmware build use ``tests/fwbuild.py`` and are skipped with the reason when no toolchain is available.
"""
import collections
import os
import tempfile
import time

import numpy as np
import pytest

from cwstudio.codemap import power

try:
    from tests.test_codemap import elf_or_skip
except ImportError:
    from test_codemap import elf_or_skip


def _fw(elf, **options):
    from cwstudio.codemap.emu import Firmware
    from cwstudio.codemap.program import Program
    return Firmware(Program(elf), options=options or None)


def _resp(fw, run, cmd="r"):
    return fw.ss.response(run.output, cmd)


ODD = dict(project="cm-odd", crypto=None, ss_ver="SS_VER_1_1")


# ----- firmware behaviour -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("platform", ["CWLITEARM", "CWNANO", "CWLITEXMEGA", "CW308_NEORV32", "CW312_IBEX"])
def test_glitch_firmware_commands(platform):
    fw = _fw(elf_or_skip(platform, project="simpleserial-glitch", crypto=None, ss_ver="SS_VER_2_1"))
    run, _ = fw.command("g", b"")
    assert _resp(fw, run) == (2500).to_bytes(4, "little")
    hi, lo = run.trigger_window()
    assert lo - hi > 20000  # 2500 volatile loop iterations between the trigger edges
    assert _resp(fw, fw.command("c", b"\xa2")[0]) == b"\x01" and _resp(fw, fw.command("c", b"\x00")[0]) == b"\x00"
    assert _resp(fw, fw.command("p", b"touch")[0]) == b"\x01" and _resp(fw, fw.command("p", b"touce")[0]) == b"\x00"


@pytest.mark.parametrize("platform", ["CWLITEARM", "CWLITEXMEGA", "CW308_NEORV32"])
def test_password_firmware_halts_after_answering(platform):
    # basic-passwdcheck is not SimpleSerial and ends in "while (1);": the emulator stops there with the output instead of running into the instruction limit
    elf = elf_or_skip(platform, project="basic-passwdcheck", crypto="NONE", ss_ver=None)
    t = time.time()
    fw = _fw(elf)
    run, snap = fw.command_raw(b"h0px3\n")
    assert run.output == b"Access granted, Welcome!\n" and run.halted is not None and snap is None
    bad, _ = fw.command_raw(b"wrong\n")
    assert bad.output == b"PASSWORD FAIL\n" and bad.instructions > 20 * run.instructions  # the failure path waits in delay loops
    assert time.time() - t < 20


def test_rsa_firmware_heap_survives_it_blocks():
    # Unicorn bug: a load or store skipped inside an IT block (with memory hooks installed) left its IT state behind, so the unconditional return after "itt ne; strne" in newlib's malloc was skipped and every malloc grabbed fresh heap; the RSA key set-up then ran out of memory and signing failed with MBEDTLS_ERR_MPI_ALLOC_FAILED
    from elftools.elf.elffile import ELFFile
    elf = elf_or_skip("CWLITEARM", project="simpleserial-rsa", crypto=None, ss_ver=None)
    with open(elf, "rb") as f:
        syms = {s.name: s["st_value"] for s in ELFFile(f).get_section_by_name(".symtab").iter_symbols()}
    fw = _fw(elf)
    heap_end = int.from_bytes(bytes(fw.machine.uc.mem_read(syms["heap_end.0"], 4)), "little")
    assert 0 < heap_end - syms["end"] < 0x2000, hex(heap_end - syms["end"])  # a few kilobytes of key material, not the whole 22 KB up to the stack


def test_odd_firmware_behaviour_and_messages():
    from cwstudio.codemap.emu import EmuError
    for platform in ("CWLITEARM", "CW308_NEORV32", "CWLITEXMEGA"):
        fw = _fw(elf_or_skip(platform, **ODD), max_instructions=300_000)
        run, _ = fw.command("s", b"\x01\x02\x03\x04")
        assert _resp(fw, run) == b"\x0a" and None not in run.trigger_window()
        run, _ = fw.command("c", b"\x07")  # answers, raises the trigger and halts in while (1)
        assert _resp(fw, run) == b"\x07" and run.halted is not None
        run, _ = fw.command("s", b"\x01\x02\x03\x05")  # the next command still sees its trigger edge (the XMEGA port watcher state is part of the snapshot)
        assert _resp(fw, run) == b"\x0b" and None not in run.trigger_window(), platform
        run, _ = fw.command("d", b"\x01")  # WFI / sleep: an interrupt would wake the core, so the emulation goes on
        assert _resp(fw, run) == b"\x01"
        with pytest.raises(EmuError, match=r"poll_flag \(cm-odd\.c:\d+\).*options\.skip"):
            fw.command("a", b"\x01")  # polls a peripheral flag that never comes: the limit error names the loop
        if platform != "CWLITEXMEGA":  # AVR's BREAK is a no-op without a debugger
            with pytest.raises(EmuError, match="supervisor call|ECALL"):
                fw.command("e", b"\x01")
        if platform == "CWLITEARM":
            with pytest.raises(EmuError, match=r"at 0x0 .*last instruction .* in null_call"):
                fw.command("f", b"\x01")


def test_missing_getch_hint():
    from cwstudio.codemap.emu import EmuError
    with pytest.raises(EmuError, match="no getch function.*-flto.*getch, putch"):
        _fw(elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1"), getch=["no_such_function"])


# ----- fault injection and glitches -------------------------------------------------------------------------------
def test_instruction_skip_changes_the_glitch_loop():
    fw = _fw(elf_or_skip("CWLITEARM", project="simpleserial-glitch", crypto=None, ss_ver="SS_VER_2_1"), max_instructions=200_000)
    run, _ = fw.command("g", b"")
    i0 = int(np.searchsorted(run.start, run.trigger_window()[0]))
    seen = collections.Counter()
    for k in range(i0, i0 + 120):
        try:
            g, _ = fw.command("g", b"", skip=(k, 1))
        except Exception:  # noqa: BLE001
            seen["crash"] += 1
            continue
        assert np.array_equal(g.pcs[:k], run.pcs[:k])  # identical up to the glitch
        r = _resp(fw, g)
        seen["normal" if r == run_resp(fw, run) else "changed"] += 1
    assert seen["changed"] > 10 and seen["normal"] > 10


def run_resp(fw, run):
    return _resp(fw, run)


def _sim(elf, seed=3):
    from cwstudio.simulator import SimScope, SimTarget
    sc = SimScope(seed=seed)
    sc.default_setup()
    tg = SimTarget(sc)
    tg.con(sc)
    assert sc.load_firmware(elf)["emulated"]
    return sc, tg


def test_simulator_glitches_emulated_firmware():
    sc, tg = _sim(elf_or_skip("CWLITEARM", project="simpleserial-glitch", crypto=None, ss_ver="SS_VER_2_1"))
    g = sc.glitch
    g.enabled, g.trigger_src, g.repeat = True, "ext_single", 1

    def sweep(width):
        c = collections.Counter()
        g.width = width
        for off in range(0, 160, 4):
            g.ext_offset = off
            sc.arm()
            tg.simpleserial_write("g", b"")
            assert not sc.capture()  # the trigger fired: no capture timeout
            v = tg.simpleserial_read_witherrors("r", 4)
            c["reset" if not v["valid"] else "normal" if bytes(v["payload"]) == (2500).to_bytes(4, "little") else "success"] += 1
        return c
    assert sweep(0.5) == {"normal": 40}
    mid = sweep(20.0)
    assert mid["success"] >= 8 and mid["normal"] >= 4, mid
    assert sweep(49.0) == {"reset": 40}
    g.enabled, g.repeat = False, 0
    assert sweep(20.0) == {"normal": 40}


def test_simulator_run_cache_is_bounded():
    from cwstudio.codemap.sim import FirmwareSim
    fs = FirmwareSim(elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1"))
    fs.CACHE_BYTES = 200_000  # a few runs
    rng = np.random.default_rng(1)
    for _ in range(40):
        run, resp, P = fs.command("p", rng.bytes(16), bytes(16))
        assert resp is not None and P.dtype == np.float32 and len(run.pcs) == 0  # the per-instruction arrays are not kept
    assert fs._bytes <= fs.CACHE_BYTES and 1 <= len(fs._cache) < 40
    assert fs._bytes == sum(p.nbytes + len(r.output) + 256 for r, _resp, p in fs._cache.values())


# ----- mapping and alignment --------------------------------------------------------------------------------------
def test_mapping_offset_counts_adc_cycles():
    m = power.Mapping(spc=2.0, adc_offset=1000, presamples=30, decimate=2)
    assert m.b == pytest.approx(-500 + 30) and m.to_json()["intercept"] == pytest.approx(-470)
    assert m.cycle(m.sample(1234.0)) == pytest.approx(1234.0)


def test_alignment_does_not_jump_a_round():
    # repetitive code (ten AES-like rounds) seen from the middle: a search over the whole trace length used to lock onto a round early or late
    rng = np.random.default_rng(5)
    rnd = 1.0 + 0.6 * rng.random(700) + np.repeat(rng.random(35) > 0.7, 20) * 1.2
    P = np.concatenate([np.ones(300)] + [rnd + 0.05 * rng.random(700) for _ in range(10)] + [np.ones(300)])
    truth = power.Mapping(spc=4.0, adc_offset=9000)
    mean = np.mean([power.synth_trace(P, 300, truth, 8000, rng, noise=0.02) for _ in range(20)], axis=0)
    res = power.align(mean, P, 300, power.Mapping(spc=4.0, adc_offset=9000))
    assert abs(res["shift"]) < 1.0 and res["scale"] == pytest.approx(1.0, abs=0.003) and res["label"] == "high"
    # the model of other code does not align with any confidence
    other = 1.0 + 0.6 * rng.random(len(P))
    assert power.align(mean, other, 300, power.Mapping(spc=4.0, adc_offset=9000))["label"] == "low"


def test_source_folder_is_walked_once(tmp_path, monkeypatch):
    from cwstudio.codemap import program as prog_mod
    p = prog_mod.Program(elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1"))
    (tmp_path / "deep" / "er").mkdir(parents=True)
    (tmp_path / "deep" / "er" / "aes.c").write_text("/* moved */\n")
    walks = []
    real_walk, real_isfile = os.walk, os.path.isfile
    monkeypatch.setattr(prog_mod.os, "walk", lambda *a, **k: (walks.append(a[0]), real_walk(*a, **k))[1])
    monkeypatch.setattr(prog_mod.os.path, "isfile", lambda x: str(x).startswith(str(tmp_path)) and real_isfile(x))  # as if the build folder had moved away
    p.set_source_roots([str(tmp_path)])
    for i in range(len(p.files)):
        p.resolve_source(i)
    assert len(walks) == 1  # one walk for all the files not found under their build paths, not one per file
    assert p.source(p.find_file("aes.c")) == "/* moved */\n"


# ----- HTTP API ------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def api():
    from starlette.testclient import TestClient
    from cwstudio.app import create_app
    from cwstudio.session import Session
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def test_api_input_checks(api):
    elf = elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    api.post("/api/scope/connect", json={"kind": "sim"})
    api.post("/api/target/connect", json={"kind": "sim"})
    assert api.post("/api/target/program", json={"programmer": "STM32F", "path": elf}).json()["emulation"]["emulated"]
    api.post("/api/capture/start", json={"count": 20, "clear": True})
    for _ in range(300):
        if not api.get("/api/status").json()["job"]["running"]:
            break
        time.sleep(0.05)
    st = api.post("/api/codemap/build", json={}).json()
    assert st["alignment"]["applied"] and abs(st["alignment"]["shift"]) < 0.5  # the model is in phase with the simulator's traces
    m0 = api.get("/api/codemap").json()["mapping"]
    for body in ({"scale": 0}, {"target_freq": -5}, {"decimate": 0}, {"adc_offset": -3}, {"shift": 3, "scale": 99}):
        assert api.put("/api/codemap/mapping", json=body).status_code == 400
    assert api.get("/api/codemap").json()["mapping"] == m0  # a rejected change leaves the mapping as it was
    checks = [("post", "/api/codemap/lookup", {"json": {"file": 9999, "line": 3}}, 404, "no source file"),
              ("get", "/api/codemap/disasm", {}, 400, "give function"),
              ("get", "/api/codemap/at", {"params": {"sample": "nan"}}, 400, "finite"),
              ("get", "/api/codemap/model", {"params": {"n": -5}}, 400, "n must be"),
              ("post", "/api/codemap/align", {"json": {"source": "bogus"}}, 400, "source must be"),
              ("post", "/api/codemap/align", {"json": {"scale_min": 2, "scale_max": 1}}, 400, "scale_min"),
              ("post", "/api/codemap/align", {"json": {"max_shift": 10**9}}, 400, "max_shift"),
              ("post", "/api/codemap/align", {"json": {"source": "trace", "trace": 999}}, 404, "no trace 999"),
              ("post", "/api/codemap/region", {"content": b'{"start": 0, "end": Infinity}', "headers": {"content-type": "application/json"}}, 400, "finite"),
              ("post", "/api/codemap/lookup", {"json": {"function": "nope"}}, 404, "KeyError: no function 'nope'")]
    for method, path, kw, code, text in checks:
        r = getattr(api, method)(path, **kw)
        assert r.status_code == code, (path, kw, r.text)
        if text:
            assert text in r.json()["detail"], r.json()["detail"]
    reg = api.post("/api/codemap/region", json={"start": 1000, "end": 1000}).json()  # zero width: the instruction at that sample
    assert reg["instructions"] == 1
    assert api.post("/api/codemap/region", json={"start": 10**7, "end": 10**7 + 5}).json()["functions"] == []  # after the end
    pre = api.post("/api/codemap/region", json={"start": -400, "end": -10}).json()  # before the trigger: the command parsing
    assert pre["instructions"] > 0 and all(f["last"] <= 0 for f in pre["functions"] if f["self_cycles"] > 0)
    # the wrong firmware for these traces: a low confidence fit is not applied
    xm = elf_or_skip("CWLITEXMEGA", "gcc", "TINYAES128C", "SS_VER_2_1")
    st = api.post("/api/codemap/build", json={"elf": xm}).json()
    assert st["alignment"]["label"] == "low" and st["alignment"]["applied"] is False and st["mapping"]["shift"] == 0 and st["mapping"]["scale"] == 1


def test_api_build_other_firmware(api):
    g = elf_or_skip("CWLITEARM", project="simpleserial-glitch", crypto=None, ss_ver="SS_VER_2_1")
    st = api.post("/api/codemap/build", json={"elf": g, "cmd": "g", "text": "", "align": False}).json()
    assert st["response"] == "c4090000" and st["text"] == "" and st["trigger_cycles"] > 20000
    pw = elf_or_skip("CWLITEARM", project="basic-passwdcheck", crypto="NONE", ss_ver=None)
    st = api.post("/api/codemap/build", json={"elf": pw, "raw": "h0px3\n", "raw_text": True, "align": False}).json()
    assert bytes.fromhex(st["response"]) == b"Access granted, Welcome!\n" and "main" in st["halted"] and st["cmd"] == "raw"
    r = api.post("/api/codemap/build", json={"elf": g, "cmd": "i", "text": "", "options": {"max_instructions": 100000}})
    assert r.status_code == 400 and "infinite_loop" in r.json()["detail"] and "100,000" in r.json()["detail"]


def test_mcp_build_passes_command_raw_and_limit():
    from cwstudio.mcp_server import build_server
    sent = []

    class FakeClient:
        base = "http://fake"

        def post(self, path, body=None, timeout=None):
            sent.append((path, body))
            return {"ready": True, "band": {"t1": None, "total": 0}, "mapping": {}}

        def get(self, path, **params):
            return {}

        put = post

    m = build_server(FakeClient())
    r = m._tools_call({"name": "code_map_build", "arguments": {"cmd": "g", "text": "", "raw": None, "max_instructions": 9000000}})
    assert not r["isError"]
    path, body = sent[0]
    assert path == "/api/codemap/build" and body["cmd"] == "g" and body["text"] == "" and body["options"] == {"max_instructions": 9000000} and "raw" not in body
