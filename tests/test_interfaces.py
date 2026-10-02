"""Protocols and interfaces against the simulator standing in for each ChipWhisperer model: capabilities, gating (unsupported actions refused with 400 and the reason), UART, SPI with the simulated flash, GPIO, USERIO, triggers, bit-banger and 1-Wire, programmer gating, scope settings gating, OpenOCD helpers and the OpenOCD toolchain entry."""
import json
import os
import tempfile
import time

import pytest
from starlette.testclient import TestClient

from cwstudio.app import create_app
from cwstudio.capabilities import SIM_MODELS, capabilities, check_setting, setting_gates, Unsupported
from cwstudio.session import Session
from cwstudio.simulator import SimScope


@pytest.fixture(scope="module")
def client():
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def use(client, model):
    r = client.post("/api/scope/connect", json={"kind": "sim", "sim_model": model})
    assert r.status_code == 200, r.text
    assert r.json()["sim_model"] == model
    assert client.post("/api/target/connect", json={"kind": "sim"}).status_code == 200


def refused(r, *words):
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert d.startswith("Unsupported:"), d
    for w in words:
        assert w in d, d
    return d


# ----- capabilities ------------------------------------------------------------------------------
@pytest.mark.parametrize("model", SIM_MODELS)
def test_capabilities_per_model(model):
    c = capabilities(SimScope(sim_model=model))
    husky = model in ("husky", "huskyplus")
    assert c["model"] == model and c["simulated"]
    assert c["uart"]["available"] and c["simpleserial"]["available"]
    assert c["spi"]["available"] == (model != "nano")
    assert c["userio"]["available"] == husky and c["bitbanger"]["available"] == husky and c["onewire"]["available"] == husky
    assert c["gpio"]["available"] and c["gpio"]["read"]["available"] == (model != "nano")
    for k in ("jtag", "swd"):
        assert not c[k]["available"]
        assert c[k]["model_supports"] == (model != "nano")
        assert ("needs real hardware" in c[k]["reason"]) == (model != "nano")
    t = c["triggers"]
    assert t["uart_decode"]["available"] == (model == "pro")
    assert t["uart_pattern"]["available"] == husky and t["sequencer"]["available"] == husky and t["edge_counter"]["available"] == husky
    assert t["sad"]["available"] == (model in ("pro", "husky", "huskyplus"))
    assert t["combinations"]["available"] == (model != "nano")
    assert t["basic"]["pins"] == (["tio4"] if model == "nano" else t["basic"]["pins"])
    if husky:
        assert t["uart_pattern"]["rules"] == (8 if model == "huskyplus" else 2)
        assert ("TIO1" in c["bitbanger"]["pins"]) == (model == "huskyplus")
    p = c["programmers"]
    assert p["STM32F"]["available"]
    for k in ("AVR", "NEORV32", "iCE40", "XC7A35T", "SAM4S", "XMEGA"):
        assert p[k]["available"] == (model != "nano"), k
    assert "tio4" not in c["uart"]["rx_pins"]
    assert c["uart"]["rx_pins"] == (["tio1"] if model == "nano" else ["tio1", "tio2", "tio3"])
    for entry in [c[k] for k in ("uart", "spi", "jtag", "gpio", "userio")] + list(t.values()) + list(p.values()):
        assert set(entry) >= {"available", "reason"} and (entry["available"] or entry["reason"])


def test_capabilities_endpoint_without_scope():
    s = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(s)) as c:
        caps = c.get("/api/capabilities").json()
        assert not caps["connected"] and caps["spi"]["reason"] == "connect a scope first"
        refused(c.post("/api/interfaces/spi/enable", json={}), "connect a scope first")
        st = c.get("/api/interfaces").json()
        assert st["capabilities"]["connected"] is False and st["mpsse"] is None


def test_meta_and_status(client):
    meta = client.get("/api/meta").json()
    assert set(meta["sim_models"]) == set(SIM_MODELS)
    assert "SS_VER_2_0" not in meta["ss_versions"]
    assert {"iCE40", "XC7A35T"} <= set(meta["programmers"])
    use(client, "pro")
    assert "Pro" in client.get("/api/status").json()["scope"]["name"]
    assert client.post("/api/scope/connect", json={"kind": "sim", "sim_model": "cw9000"}).status_code == 400


# ----- UART and SimpleSerial ----------------------------------------------------------------------------
def test_uart_round_trip(client):
    use(client, "husky")
    r = client.put("/api/interfaces/uart", json={"baud": 115200, "parity": "even", "stop_bits": 2, "rx": "tio3", "tx": "tio4"})
    assert r.status_code == 200, r.text
    u = client.get("/api/interfaces/uart").json()
    assert (u["baud"], u["parity"], u["stop_bits"], u["data_bits"], u["rx"], u["tx"]) == (115200, "even", 2, 8, "tio3", "tio4")
    assert u["pins"]["tio1"] == "high_z" and u["pins"]["tio2"] == "high_z"  # the old serial pins are released
    refused(client.put("/api/interfaces/uart", json={"rx": "tio4"}), "TIO4 can only transmit")
    refused(client.put("/api/interfaces/uart", json={"data_bits": 7}), "8 data bits")
    assert client.put("/api/interfaces/uart", json={"parity": "weird"}).status_code == 400
    assert client.put("/api/interfaces/uart", json={"baud": 10}).status_code == 400
    assert client.put("/api/interfaces/uart", json={"stop_bits": 3}).status_code == 400
    client.put("/api/interfaces/uart", json={"rx": "tio1", "tx": "tio2", "baud": 38400, "parity": "none", "stop_bits": 1})
    use(client, "nano")
    refused(client.put("/api/interfaces/uart", json={"rx": "tio3"}), "TIO1")
    assert client.put("/api/interfaces/uart", json={"baud": 57600}).json()["baud"] == 57600


def test_serial_line_endings(client):
    use(client, "lite")
    since = client.get("/api/target/serial").json()
    t0 = since[-1]["t"] if since else 0
    assert client.post("/api/target/serial/write", json={"data": "v", "eol": "crlf"}).json()["written"] == 3
    assert client.post("/api/target/serial/write", json={"data": "v", "eol": "none"}).json()["written"] == 1
    assert client.post("/api/target/serial/write", json={"data": "v", "eol": "bad"}).status_code == 400
    tx = [r for r in client.get(f"/api/target/serial?since={t0}").json() if r["dir"] == "tx"]
    assert tx[-2]["hex"] == "760d0a" and tx[-1]["hex"] == "76"


def test_simpleserial_versions(client):
    use(client, "husky")
    for v in ("1.0", "1.1", "2.1"):
        r = client.post("/api/interfaces/simpleserial/connect", json={"version": v})
        assert r.status_code == 200 and r.json()["version"] == v
        resp = client.post("/api/interfaces/simpleserial/send", json={"cmd": "p", "data": "00112233445566778899aabbccddeeff", "read_len": 16}).json()
        assert resp["response"] and len(resp["response"]) == 32 and resp["ack"] == (v != "1.0")
    assert client.post("/api/interfaces/simpleserial/connect", json={"version": "2.0"}).status_code == 400


# ----- SPI ---------------------------------------------------------------------------------------------------
def test_spi_flash(client):
    use(client, "lite")
    assert client.post("/api/interfaces/spi/transfer", json={"data": "9f"}).status_code == 400  # not enabled yet
    assert client.post("/api/interfaces/spi/enable", json={"speed": 2e6, "cs": "pdid"}).json()["enabled"]
    assert client.post("/api/interfaces/spi/transfer", json={"data": "9f 00 00 00"}).json()["miso"] == "ff ef 40 18"
    head = client.post("/api/interfaces/spi/transfer", json={"data": "03 00 00 00" + " 00" * 13}).json()["miso"]
    assert bytes.fromhex(head.replace(" ", ""))[4:].startswith(b"ChipWhisper")
    # write enable, page program at 0x1000, read back
    client.post("/api/interfaces/spi/transfer", json={"data": "06"})
    assert client.post("/api/interfaces/spi/transfer", json={"data": "05 00"}).json()["miso"].endswith("02")
    client.post("/api/interfaces/spi/transfer", json={"data": "02 00 10 00 de ad be ef"})
    assert client.post("/api/interfaces/spi/transfer", json={"data": "03 00 10 00 00 00 00 00"}).json()["miso"].endswith("de ad be ef")
    client.post("/api/interfaces/spi/transfer", json={"data": "06"})
    client.post("/api/interfaces/spi/transfer", json={"data": "20 00 10 00"})
    assert client.post("/api/interfaces/spi/transfer", json={"data": "03 00 10 00 00 00"}).json()["miso"].endswith("ff ff")
    # chip select held across two calls
    client.post("/api/interfaces/spi/transfer", json={"data": "9f", "stop": False})
    assert client.post("/api/interfaces/spi/transfer", json={"data": "00 00 00", "start": False}).json()["miso"] == "ef 40 18"
    assert client.post("/api/interfaces/spi/toggle_sck", json={"cycles": 16}).json()["toggled"] == 16
    assert client.post("/api/interfaces/spi/toggle_sck", json={"cycles": 300}).status_code == 400
    assert client.post("/api/interfaces/spi/enable", json={"cs": "nrst"}).status_code == 400
    assert client.post("/api/interfaces/spi/transfer", json={"data": "zz"}).status_code == 400
    assert not client.post("/api/interfaces/spi/disable").json()["enabled"]
    use(client, "nano")
    refused(client.post("/api/interfaces/spi/enable", json={}), "no SPI pins")


# ----- GPIO and USERIO -------------------------------------------------------------------------------------------
def test_gpio(client):
    use(client, "husky")
    g = client.get("/api/interfaces/gpio").json()
    assert g["readable"] and {"tio1", "nrst", "miso"} <= set(g["pins"])
    g = client.put("/api/interfaces/gpio", json={"pin": "tio3", "state": "low"}).json()
    assert g["pins"]["tio3"]["mode"] == "gpio_low" and g["pins"]["tio3"]["level"] == 0
    g = client.put("/api/interfaces/gpio", json={"pin": "pdic", "state": "high"}).json()
    assert g["pins"]["pdic"]["mode"] == "high" and g["pins"]["pdic"]["level"] == 1
    client.put("/api/interfaces/gpio", json={"pin": "tio3", "state": "high_z"})
    client.put("/api/interfaces/gpio", json={"pin": "pdic", "state": "high_z"})
    assert client.post("/api/interfaces/gpio/pulse", json={"pin": "nrst", "ms": 5}).json()["ms"] == 5
    assert client.get("/api/interfaces/gpio").json()["pins"]["nrst"]["mode"] == "high_z"
    assert client.put("/api/interfaces/gpio", json={"pin": "tio3", "state": "sideways"}).status_code == 400
    refused(client.put("/api/interfaces/gpio", json={"pin": "miso", "state": "high"}), "cannot be driven")
    refused(client.post("/api/interfaces/gpio/pulse", json={"pin": "tio1"}), "pulses")
    use(client, "lite")
    g = client.get("/api/interfaces/gpio").json()
    assert "miso" not in g["pins"] and g["pins"]["pdic"]["level"] is None  # the Lite reads TIO1-4 only
    use(client, "nano")
    g = client.get("/api/interfaces/gpio").json()
    assert not g["readable"] and "cannot read" in g["read_reason"] and all(p["level"] is None for p in g["pins"].values())
    refused(client.put("/api/interfaces/gpio", json={"pin": "tio1", "state": "high"}), "cannot be driven")
    assert client.put("/api/interfaces/gpio", json={"pin": "nrst", "state": "low"}).status_code == 200
    client.put("/api/interfaces/gpio", json={"pin": "nrst", "state": "high_z"})


def test_userio(client):
    use(client, "huskyplus")
    u = client.get("/api/interfaces/userio").json()
    assert u["width"] == 9 and u["pins"][-1] == "CK" and u["mode"] == "normal"
    u = client.put("/api/interfaces/userio", json={"direction": 0b101, "drive": 0b100}).json()
    assert u["direction"] == 5 and u["status"] & 0b101 == 0b100
    assert client.put("/api/interfaces/userio", json={"direction": 1024}).status_code == 400
    refused(client.put("/api/interfaces/userio", json={"mode": "target_debug_jtag"}), "OpenOCD")
    client.put("/api/interfaces/userio", json={"direction": 0, "drive": 0})
    use(client, "pro")
    refused(client.get("/api/interfaces/userio"), "Husky only")


# ----- triggers ------------------------------------------------------------------------------------------------------
def test_triggers(client):
    use(client, "husky")
    r = client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio1", "tio2"], "op": "AND", "edge": "falling_edge"}).json()
    assert r["current"]["trigger.triggers"] == "tio1 AND tio2" and r["current"]["adc.basic_mode"] == "falling_edge" and r["current"]["trigger.module"] == "basic"
    s = client.get("/api/scope/settings").json()
    assert json.dumps(s).count("tio1 AND tio2") >= 1  # the Scope tab and capture see the same trigger
    r = client.put("/api/interfaces/trigger", json={"kind": "uart_pattern", "pin": "tio1", "pattern": "'r'", "rule": 1, "baud": 115200}).json()
    assert r["applied"]["pattern"] == [ord("r")] and r["current"]["trigger.module"] == "UART"
    refused(client.put("/api/interfaces/trigger", json={"kind": "uart_pattern", "rule": 5}), "2 UART trigger rules")
    refused(client.put("/api/interfaces/trigger", json={"kind": "uart_decode"}), "Pro")
    assert client.put("/api/interfaces/trigger", json={"kind": "edge_counter", "pin": "tio4", "edges": 3}).json()["current"]["trigger.module"] == "edge_counter"
    assert client.put("/api/interfaces/trigger", json={"kind": "adc_level", "level": 0.2}).json()["current"]["trigger.module"] == "ADC"
    assert client.put("/api/interfaces/trigger", json={"kind": "adc_level", "level": 0.9}).status_code == 400
    assert client.put("/api/interfaces/trigger", json={"kind": "sequencer", "pins": ["tio4", "tio3"], "window_end": 100}).json()["applied"]["enabled"]
    assert client.put("/api/interfaces/trigger", json={"kind": "sad", "threshold": 5}).status_code == 200
    assert client.put("/api/interfaces/trigger", json={"kind": "warp"}).status_code == 400
    use(client, "pro")
    r = client.put("/api/interfaces/trigger", json={"kind": "uart_decode", "pin": "tio1", "pattern": "72 XX", "baud": 38400}).json()
    assert r["applied"]["pattern"] == [0x72, "XX"] and r["current"]["trigger.module"] == "DECODEIO"
    refused(client.put("/api/interfaces/trigger", json={"kind": "uart_pattern"}), "Husky")
    refused(client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["userio_d0"]}), "trigger input")
    use(client, "nano")
    assert client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio4"]}).status_code == 200
    refused(client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio1"]}), "TIO4 only")
    refused(client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio4"], "edge": "falling_edge"}), "rising edge only")
    refused(client.put("/api/interfaces/trigger", json={"kind": "sad"}), "SAD")
    use(client, "husky")
    client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio4"]})
    cap = client.post("/api/capture/start", json={"count": 3, "clear": True}).json()  # captures still run with the configured trigger
    assert cap["started"]


# ----- bit-banger and 1-Wire -------------------------------------------------------------------------------------------
def test_bitbanger_and_onewire(client):
    use(client, "husky")
    r = client.post("/api/interfaces/bitbang", json={"bits": "1010 1100 0000 0000", "record": "0000000011111111"}).json()
    assert r["sent"] == "1010110000000000" and r["recorded"] == "10100101"
    ow = client.post("/api/interfaces/onewire", json={"action": "read_rom"}).json()
    assert ow["presence"] and ow["family"] == "0x28" and ow["crc_ok"] and len(ow["rom"].split()) == 8
    assert client.post("/api/interfaces/onewire", json={"action": "reset"}).json()["presence"]
    refused(client.post("/api/interfaces/bitbang", json={"bits": "1", "data_pin": "TIO1"}), "Husky Plus")
    assert client.post("/api/interfaces/bitbang", json={"bits": "1", "data_pin": "USERIO_D0", "clock_pin": "USERIO_D0"}).status_code == 400
    assert client.post("/api/interfaces/bitbang", json={"bits": "12"}).status_code == 400
    assert client.post("/api/interfaces/bitbang", json={"bits": "1", "clk_div": 3}).status_code == 400
    use(client, "huskyplus")
    assert client.post("/api/interfaces/bitbang", json={"bits": "11", "data_pin": "TIO1", "clock_pin": "TIO2"}).status_code == 200
    use(client, "lite")
    refused(client.post("/api/interfaces/onewire", json={}), "Husky only")


# ----- programmers and scope settings gating -------------------------------------------------------------------------------
def test_programmer_gating(client, tmp_path):
    fw = tmp_path / "fw.hex"
    fw.write_text(":00000001FF\n")
    use(client, "nano")
    refused(client.post("/api/target/program", json={"programmer": "AVR", "path": str(fw)}), "AVR programmer", "SPI pins")
    refused(client.post("/api/target/program", json={"programmer": "iCE40", "path": str(fw)}), "iCE40")
    assert client.post("/api/target/program", json={"programmer": "STM32F", "path": str(fw)}).json()["simulated"]
    use(client, "lite")
    assert client.post("/api/target/program", json={"programmer": "XC7A35T", "path": str(fw)}).json()["ok"]


def test_scope_settings_gating(client):
    use(client, "husky")

    def node(nodes, path):
        for n in nodes:
            if n.get("path") == path:
                return n
            r = node(n.get("children") or [], path)
            if r:
                return r
    s = client.get("/api/scope/settings").json()
    tio4 = node(s, "io.tio4")
    assert "serial_rx" in tio4["choices"] and "serial_rx" in tio4["disabled_choices"]
    assert "DECODEIO" in node(s, "trigger.module")["disabled_choices"] and "UART" not in node(s, "trigger.module")["disabled_choices"]
    refused(client.put("/api/scope/settings", json={"path": "io.tio4", "value": "serial_rx"}), "TIO4")
    refused(client.put("/api/scope/settings", json={"path": "trigger.module", "value": "DECODEIO"}), "Pro")
    assert client.put("/api/scope/settings", json={"path": "io.tio4", "value": "high_z"}).status_code == 200
    use(client, "pro")
    s = client.get("/api/scope/settings").json()
    mods = node(s, "trigger.module")["disabled_choices"]
    assert "DECODEIO" not in mods and {"UART", "ADC", "edge_counter"} <= set(mods)
    refused(client.put("/api/scope/settings", json={"path": "trigger.triggers", "value": "tio1 OR userio_d0"}), "userio_d0")
    use(client, "nano")
    refused(client.put("/api/scope/settings", json={"path": "io.tio1", "value": "serial_tx"}), "always serial RX")


def test_setting_gates_unit():
    g = setting_gates(SimScope(sim_model="lite"))
    assert g["trigger.module"]["disabled"]["SAD"]
    with pytest.raises(Unsupported):
        check_setting(SimScope(sim_model="lite"), "io.tio4", "serial_rx")
    check_setting(SimScope(sim_model="lite"), "gain.db", 20)  # ungated paths pass
    assert setting_gates(None) == {}



def test_setting_gates_cached_per_scope(monkeypatch):
    """The settings tree and every setting write consult the gates; they are computed once per connected scope, again for another scope, and after invalidate_gates()."""
    from cwstudio import capabilities as capmod
    calls = []
    real = capmod.capabilities
    monkeypatch.setattr(capmod, "capabilities", lambda *a, **k: calls.append(1) or real(*a, **k))
    capmod.invalidate_gates()
    lite = SimScope(sim_model="lite")
    first = setting_gates(lite)
    for _ in range(5):
        assert setting_gates(lite) is first
        check_setting(lite, "gain.db", 20)
    assert len(calls) == 1
    husky = SimScope(sim_model="husky")
    assert "clock.adc_mul" not in setting_gates(husky) and len(calls) == 2
    capmod.invalidate_gates()
    setting_gates(husky)
    assert len(calls) == 3


@pytest.mark.parametrize("model", SIM_MODELS)
def test_adc_mul_husky_only(client, model):
    """clock.adc_mul exists on the Husky only: the other simulated models neither list it in the settings tree nor accept it, and the capabilities say why."""
    use(client, model)
    husky = model in ("husky", "huskyplus")
    paths = set()

    def walk(ns):
        for n in ns:
            if n.get("kind") == "group":
                walk(n.get("children") or [])
            else:
                paths.add(n["path"])
    walk(client.get("/api/scope/settings").json())
    assert ("clock.adc_mul" in paths) == husky and "clock.clkgen_freq" in paths
    assert client.get("/api/capabilities").json()["clock"]["adc_mul"]["available"] == husky
    sc = SimScope(sim_model=model)
    assert hasattr(sc.clock, "adc_mul") == husky
    r = client.put("/api/scope/settings", json={"path": "clock.adc_mul", "value": 2})
    if husky:
        assert r.status_code == 200, r.text
        client.put("/api/scope/settings", json={"path": "clock.adc_mul", "value": 4})
    else:
        refused(r, "Husky only")

# ----- OpenOCD -------------------------------------------------------------------------------------------------------------
def test_openocd_refused_on_simulator(client):
    use(client, "husky")
    refused(client.post("/api/interfaces/openocd/mpsse", json={"enable": True, "transport": "swd"}), "needs real hardware")
    refused(client.post("/api/interfaces/openocd/start", json={}), "needs real hardware")
    st = client.get("/api/interfaces/openocd").json()
    assert st["mpsse"] is None and not st["running"] and st["interface_cfg"].endswith("cw_openocd.cfg")
    assert client.post("/api/interfaces/openocd/command", json={"command": "targets"}).status_code == 400
    use(client, "nano")
    refused(client.post("/api/interfaces/openocd/mpsse", json={"enable": True}), "Nano")


def test_openocd_command_line_and_tcl_helpers():
    from cwstudio import openocd
    o = openocd.OpenOCD(type("S", (), {"toolchains": None})())
    o.mpsse = {"pid": 0xACE5, "sn": "ABC123", "transport": "swd"}
    cmd = o.command_line("target/stm32f3x.cfg", "swd", {"gdb": 3333, "telnet": 4444, "tcl": 6666}, binary="/x/openocd")
    joined = " ".join(cmd)
    assert "-f cw_openocd.cfg" in joined and "ftdi vid_pid 0x2b3e 0xace5" in joined and "adapter serial ABC123" in joined
    assert joined.index("transport select swd") < joined.index("-f target/stm32f3x.cfg") and "tcl_port 6666" in joined
    assert openocd.parse_wrapped("0 hello world\n") == {"ok": True, "output": "hello world"}
    assert openocd.parse_wrapped("0 {a} b") == {"ok": True, "output": "{a} b"}  # verbatim: the wrapper joins with format, not list
    assert openocd.parse_wrapped('1 invalid command name "foo"')["ok"] is False
    assert "capture {targets}" in openocd.wrap_command("targets") and "format" in openocd.wrap_command("targets")
    assert openocd.braces_balanced("echo \\{") and openocd.braces_balanced("program {a b}") and not openocd.braces_balanced("echo }{") and not openocd.braces_balanced("x {")
    assert openocd.tcl_word("/a b/$c [d].hex") == "{/a b/$c [d].hex}"
    with pytest.raises(ValueError):
        openocd.tcl_word("/a/b{.hex")
    cfg = open(os.path.join(openocd.RESOURCES, openocd.CW_CFG)).read()
    assert "adapter driver ftdi" in cfg and "ftdi channel 1" in cfg and "layout_signal SWDIO_OE" in cfg


def test_openocd_toolchain_entry():
    from cwstudio.toolchains import REGISTRY_PATH, load_registry
    reg = load_registry(REGISTRY_PATH)
    e = next(t for t in reg["toolchains"] if t["id"] == "openocd")
    assert e["probe"] == "openocd" and e["bin"] == "bin" and e["source"] == "xPack" and e["compiler"] not in ("gcc", "clang")
    for host in ("linux-x64", "win32-x64", "darwin-arm64"):
        d = e["downloads"][host]
        assert d["url"].startswith("https://github.com/xpack-dev-tools/openocd-xpack/releases/download/v" + e["version"] + "/")
        assert len(d["sha256"]) == 64 and int(d["sha256"], 16) >= 0 and d["size"] > 1_000_000
        assert d["url"].endswith(".zip" if host.startswith("win") else ".tar.gz")
    with open(REGISTRY_PATH) as f:
        raw = json.load(f)
    assert raw["revision"] >= 2


def test_openocd_tcl_against_real_binary(tmp_path):
    """Starts the system or managed OpenOCD with the dummy adapter (no hardware) and talks to it over TCL; skipped when no OpenOCD is installed."""
    import shutil
    import socket
    import subprocess
    import time
    from cwstudio import openocd
    exe = shutil.which("openocd")
    if not exe:
        pytest.skip("no openocd on PATH")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = subprocess.Popen([exe, "-c", f"tcl_port {port}", "-c", "telnet_port disabled", "-c", "gdb_port disabled", "-c", "adapter driver dummy", "-c", "transport select jtag", "-c", "noinit"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                reply = openocd.tcl_query(port, openocd.wrap_command("transport select"), timeout=2)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.skip("openocd did not open its TCL port")
        assert openocd.parse_wrapped(reply) == {"ok": True, "output": "jtag"}
        bad = openocd.parse_wrapped(openocd.tcl_query(port, openocd.wrap_command("no_such_cmd")))
        assert not bad["ok"] and "no_such_cmd" in bad["output"]
    finally:
        p.terminate()
        p.wait(5)


def test_mcp_interface_tools_refuse_politely():
    """The MCP tools exist with schemas, and an unsupported feature comes back as a polite refusal, not a tool error."""
    from cwstudio.mcp_server import StudioError, build_server

    class FakeClient:
        base = "http://fake"

        def post(self, path, body=None, timeout=None):
            if path == "/api/interfaces/spi/enable":
                raise StudioError("POST /api/interfaces/spi/enable failed (400): Unsupported: SPI: the CW-Nano has no SPI pins")
            raise StudioError(f"POST {path} failed (400): RuntimeError: boom")

        get = put = post

    m = build_server(FakeClient())
    names = {t["name"] for t in m._tools_list({})["tools"]}
    assert {"hardware_capabilities", "uart_configure", "spi_transfer", "gpio_read", "gpio_set", "trigger_configure", "bitbang", "onewire", "openocd_start", "openocd_stop", "openocd_command", "openocd_program"} <= names
    ok = m._tools_call({"name": "spi_enable", "arguments": {}})
    assert not ok["isError"] and "no SPI pins" in ok["content"][0]["text"] and '"supported": false' in ok["content"][0]["text"]
    bad = m._tools_call({"name": "openocd_stop", "arguments": {}})
    assert bad["isError"]


# ----- the research capability matrix, exactly ----------------------------------------------------------------
TIO = ["tio1", "tio2", "tio3", "tio4"]
USERIO_TRIG = [f"userio_d{i}" for i in range(8)]
MATRIX = {
    #            rx pins           tx pins  readable pins                                                         basic trigger pins
    "nano": dict(rx=["tio1"], tx=["tio2"], readable=[], drive=["tio3", "pdic", "pdid", "nrst"], trig=["tio4"], edges=["rising_edge"], mods=["basic"]),
    "lite": dict(rx=TIO[:3], tx=TIO, readable=TIO, drive=TIO + ["pdic", "pdid", "nrst"], trig=TIO + ["nrst"], edges=["rising_edge", "falling_edge", "high", "low"], mods=["basic"]),
    "pro": dict(rx=TIO[:3], tx=TIO, readable=TIO, drive=TIO + ["pdic", "pdid", "nrst"], trig=TIO + ["nrst", "sma"], edges=["rising_edge", "falling_edge", "high", "low"], mods=["basic", "SAD", "DECODEIO"]),
    "husky": dict(rx=TIO[:3], tx=TIO, readable=TIO + ["nrst", "pdic", "pdid", "miso", "mosi", "sck"], drive=TIO + ["pdic", "pdid", "nrst"], trig=TIO + ["nrst", "sma"] + USERIO_TRIG, edges=["rising_edge", "falling_edge", "high", "low"], mods=["basic", "SAD", "UART", "trace", "ADC", "edge_counter", "bitbanger"]),
}
MATRIX["huskyplus"] = MATRIX["husky"]


@pytest.mark.parametrize("model", SIM_MODELS)
def test_capability_matrix_exact(model):
    """Every entry of build/research/protocols.md section 1 for each simulated model (the simulator stands in for the develop library, so the bit-banger is present on both Huskies)."""
    c = capabilities(SimScope(sim_model=model))
    x = MATRIX[model]
    nano, husky, hp, pro = model == "nano", model in ("husky", "huskyplus"), model == "huskyplus", model == "pro"
    u = c["uart"]
    assert (u["rx_pins"], u["tx_pins"], u["remap"]) == (x["rx"], x["tx"], not nano)
    assert u["parity"] == ["none", "odd", "even", "mark", "space"] and u["stop_bits"] == [1, 1.5, 2] and u["data_bits"] == [8]
    assert "serial_rx" not in u["pins"]["tio4"] if not nano else u["pins"] == {"tio1": ["serial_rx"], "tio2": ["serial_tx"]}
    assert c["simpleserial"]["versions"] == ["1.0", "1.1", "2.1"] and c["simpleserial"]["cdc"]["available"]
    assert c["spi"]["available"] == (not nano)
    for k in ("jtag", "swd"):
        assert c[k]["model_supports"] == (not nano) and not c[k]["available"]
        assert ("USERIO 20-pin" in c[k]["headers"]) == husky
    assert c["trace"]["available"] == husky
    g = c["gpio"]
    assert g["readable"] == x["readable"] and list(g["drive"]) == x["drive"] and g["read"]["available"] == (not nano) and g["pulse"] == ["nrst", "pdic"]
    if nano:
        assert g["drive"]["tio3"] == ["high_z", "gpio_low", "gpio_high"]
    assert c["userio"]["available"] == husky and (c["userio"]["pins"][-1] == "CK" if husky else True)
    bb_pins = [f"USERIO_D{i}" for i in range(8)] + ["USERIO_CK"] + (["TIO1", "TIO2", "TIO3", "TIO4", "target_pwr", "nrst"] if hp else [])
    for k in ("bitbanger", "onewire"):
        assert c[k]["available"] == husky and c[k]["pins"] == bb_pins
    t = c["triggers"]
    assert t["basic"]["available"] and t["basic"]["pins"] == x["trig"] and t["basic"]["edges"] == x["edges"]
    expect = {"combinations": not nano, "sad": pro or husky, "uart_decode": pro, "uart_pattern": husky, "edge_counter": husky, "adc_level": husky, "sequencer": husky, "trace": husky, "bitbanger": husky, "outputs": pro or husky}
    assert {k: t[k]["available"] for k in expect} == expect
    if husky:
        assert t["uart_pattern"]["rules"] == (8 if hp else 2) and t["uart_pattern"]["data_bits"] == [5, 6, 7, 8, 9] and t["outputs"]["pins"] == ["trig_mcx", "aux_mcx"]
    if pro:
        assert t["uart_decode"]["max_bytes"] == 8 and t["outputs"]["pins"] == ["aux"]
    p = c["programmers"]
    assert {k: v["available"] for k, v in p.items()} == {"STM32F": True, "XMEGA": not nano, "AVR": not nano, "SAM4S": not nano, "NEORV32": not nano, "iCE40": not nano, "XC7A35T": not nano, "OpenOCD": False}
    la = c["logic_analyzer"]
    assert la["native"]["available"] == husky and la["adc"]["available"] and la["sim"]["available"]
    if husky:
        assert la["native"]["depth"] == (65535 if hp else 16376) and set(la["native"]["groups"]) == {"CW 20-pin", "USERIO 20-pin", "glitch"}
    gates = setting_gates(SimScope(sim_model=model))
    mods = gates["trigger.module"]
    assert [m for m in mods["choices"] if m not in mods["disabled"]] == x["mods"]
    trig = gates["trigger.triggers"]
    assert [p for p in trig["choices"] if p not in trig["disabled"]] == [p for p in x["trig"]]
    assert "serial_rx" in gates["io.tio4"]["disabled"]
    if nano:
        assert [k for k in gates["io.tio1"]["choices"] if capmod_key(k) not in gates["io.tio1"]["disabled"]] == ["serial_rx"]
        assert set(gates["adc.basic_mode"]["disabled"]) == {"falling_edge", "low", "high"}
    else:
        assert "adc.basic_mode" not in gates and "serial_tx" not in gates["io.tio4"]["disabled"]


def capmod_key(v):
    from cwstudio.capabilities import _key
    return _key(v)


def test_interface_state_follows_the_scope(client):
    """Switching the simulated model (or reconnecting) drops the SPI master, the applied trigger and the SimpleSerial version of the previous connection."""
    use(client, "husky")
    assert client.post("/api/interfaces/spi/enable", json={"speed": 1e6}).json()["enabled"]
    client.put("/api/interfaces/trigger", json={"kind": "uart_pattern", "pattern": "'r'"})
    client.post("/api/interfaces/simpleserial/connect", json={"version": "1.0"})
    st = client.get("/api/interfaces").json()
    assert st["spi"]["enabled"] and st["trigger"]["kind"] == "uart_pattern" and st["simpleserial"]["version"] == "1.0"
    use(client, "lite")
    st = client.get("/api/interfaces").json()
    assert not st["spi"]["enabled"] and st["trigger"] == {} and st["simpleserial"]["version"] is None
    assert client.post("/api/interfaces/spi/transfer", json={"data": "9f 00 00 00"}).status_code == 400  # enable it again first
    assert client.get("/api/interfaces/trigger").json()["current"]["trigger.module"] == "basic"


def test_http_errors_are_400_with_reason(client):
    use(client, "husky")
    bad = [
        ("post", "/api/interfaces/spi/toggle_sck", {"cycles": "many"}),
        ("post", "/api/interfaces/gpio/pulse", {"ms": "long"}),
        ("post", "/api/interfaces/openocd/command", {"command": "targets", "timeout": "x"}),
        ("post", "/api/interfaces/openocd/mpsse", {"enable": "perhaps"}),
        ("post", "/api/interfaces/spi/transfer", {"data": "9f", "start": "maybe"}),
        ("put", "/api/interfaces/trigger", ["basic"]),
        ("put", "/api/interfaces/uart", {"baud": "fast"}),
        ("put", "/api/interfaces/gpio", {"pin": "tio9", "state": "high"}),
        ("put", "/api/interfaces/trigger", {"kind": "basic", "pins": ["tio1"], "op": "XOR"}),
        ("put", "/api/interfaces/trigger", {"kind": "uart_pattern", "pattern": "zz"}),
        ("post", "/api/interfaces/bitbang", {"bits": ""}),
        ("post", "/api/interfaces/onewire", {"action": "search"}),
        ("post", "/api/interfaces/openocd/program", {"path": "/nonexistent/fw.hex"}),
        ("post", "/api/interfaces/openocd/start", {"ports": "3333"}),
    ]
    for method, path, body in bad:
        r = getattr(client, method)(path, json=body)
        assert r.status_code == 400, (path, body, r.status_code, r.text)
        assert ": " in r.json()["detail"], r.text
    r = client.post("/api/interfaces/spi/toggle_sck", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400  # an unreadable body counts as {} (SPI not enabled), never a 500


def test_simpleserial_and_terminal_details(client):
    use(client, "pro")
    assert client.post("/api/interfaces/simpleserial/connect", json={"version": "1.1"}).json()["kind"] == "sim"
    assert client.get("/api/target/settings").json()  # the simulated target reports its protocol version
    r = client.post("/api/interfaces/simpleserial/send", json={"cmd": "p", "data": "00 11 22 33 44 55 66 77 88 99 aa bb cc dd ee ff", "read_len": 16}).json()
    assert r["ack"] and len(bytes.fromhex(r["response"])) == 16
    assert client.post("/api/interfaces/simpleserial/send", json={"cmd": "pp"}).status_code == 400
    assert client.post("/api/interfaces/simpleserial/send", json={"cmd": "p", "data": "0"}).status_code == 400
    assert client.post("/api/interfaces/simpleserial/connect", json={"version": "cdc"}).json()["version"] == "cdc"
    # hex mode writes the bytes as they are, with no line ending; text gets the chosen one
    t0 = time.time()
    assert client.post("/api/target/serial/write", json={"data": "76 0d", "hex": True, "eol": "lf"}).json()["written"] == 2
    assert client.post("/api/target/serial/write", json={"data": "v", "eol": "cr"}).json()["written"] == 2
    recs = client.get(f"/api/target/serial?since={t0 - 1}").json()
    tx = [r for r in recs if r["dir"] == "tx"]
    assert [r["hex"] for r in tx[-2:]] == ["760d", "760d"]
    deadline = time.time() + 3
    while time.time() < deadline and not any(r["dir"] == "rx" for r in client.get(f"/api/target/serial?since={t0 - 1}").json()):
        time.sleep(0.05)
    assert any(r["dir"] == "rx" for r in client.get(f"/api/target/serial?since={t0 - 1}").json())  # the simulated target answers 'v'


def test_spi_flash_more_commands(client):
    use(client, "huskyplus")
    client.post("/api/interfaces/spi/enable", json={"speed": 20e6, "cs": "tio4"})
    x = lambda d, **k: client.post("/api/interfaces/spi/transfer", json={"data": d, **k}).json()["miso"]
    assert x("90 00 00 00 00 00").endswith("ef 17") and x("ab 00 00 00 00").endswith("17")
    assert x("4b 00 00 00 00" + " 00" * 8).endswith("d2 6a 3c 2b 1f 0e 4c 57")
    assert bytes.fromhex(x("0b 00 00 00 00" + " 00" * 4).replace(" ", ""))[5:] == b"Chip"  # fast read has a dummy byte
    x("02 00 20 00 aa")  # page program without write enable is ignored
    assert x("03 00 20 00 00").endswith("ff")
    x("06"); x("c7")  # chip erase
    assert x("05 00").endswith("01")  # busy right after the erase
    time.sleep(0.06)
    assert x("05 00").endswith("00") and x("03 00 00 00 00 00").endswith("ff ff")
    x("06"); x("02 00 30 fe 11 22 33 44")  # a page program wraps inside its 256-byte page
    assert x("03 00 30 fe 00 00").endswith("11 22") and x("03 00 30 00 00 00").endswith("33 44")
    assert client.post("/api/interfaces/spi/enable", json={"speed": 30e6}).status_code == 400
    client.post("/api/interfaces/spi/disable")


def test_bitbanger_huskyplus_pins_and_gpio_userio_readback(client):
    use(client, "huskyplus")
    r = client.post("/api/interfaces/bitbang", json={"bits": "1111 0000", "record": "0000 1111", "data_pin": "nrst", "clock_pin": "disabled"}).json()
    assert r["recorded"] == "0101" and r["data_pin"] == "nrst"
    assert client.post("/api/interfaces/bitbang", json={"bits": "1", "data_pin": "TIO1", "clock_pin": "nrst"}).status_code == 400  # nRST cannot clock
    u = client.put("/api/interfaces/userio", json={"direction": 0x1FF, "drive": 0x155, "mode": "normal"}).json()
    assert u["status"] == 0x155  # driven pins read back what is driven
    client.put("/api/interfaces/userio", json={"direction": 0, "drive": 0})


@pytest.mark.parametrize("model", ["husky", "pro", "nano"])
def test_trigger_applied_to_scope_and_captures(client, model):
    """The trigger the Interfaces tab applies is what the Scope settings tree and capture see; a capture runs with it."""
    use(client, model)
    body = {"kind": "basic", "pins": ["tio4"], "edge": "rising_edge"} if model == "nano" else {"kind": "basic", "pins": ["tio3", "tio4"], "op": "OR", "edge": "falling_edge"}
    r = client.put("/api/interfaces/trigger", json=body).json()
    flat = {}

    def walk(ns):
        for n in ns:
            if n.get("kind") == "group":
                walk(n.get("children") or [])
            else:
                flat[n["path"]] = n.get("value")
    walk(client.get("/api/scope/settings").json())
    assert flat["trigger.triggers"] == r["current"]["trigger.triggers"] and flat["adc.basic_mode"] == r["current"]["adc.basic_mode"]
    if model != "nano":
        assert flat["trigger.triggers"] == "tio3 OR tio4" and flat["adc.basic_mode"] == "falling_edge"
    client.post("/api/capture/start", json={"count": 2, "clear": True})
    deadline = time.time() + 20
    while time.time() < deadline and client.get("/api/status").json()["job"]["running"]:
        time.sleep(0.05)
    job = client.get("/api/status").json()["job"]
    assert job["done"] == 2 and not job["error"]


def test_simulate_as_switch_mid_session(client):
    """Changing "Simulate as" while a target is connected and a capture runs ends that capture cleanly and leaves a working session on the new model."""
    use(client, "husky")
    assert client.post("/api/interfaces/spi/enable", json={}).status_code == 200
    assert client.post("/api/capture/start", json={"count": 0, "clear": True}).json()["started"]  # count 0: until stopped
    deadline = time.time() + 10
    while time.time() < deadline and client.get("/api/status").json()["traces"]["count"] < 3:
        time.sleep(0.05)
    r = client.post("/api/scope/connect", json={"kind": "sim", "sim_model": "nano"})
    assert r.status_code == 200 and r.json()["sim_model"] == "nano"
    deadline = time.time() + 10
    while time.time() < deadline and client.get("/api/status").json()["job"]["running"]:
        time.sleep(0.05)
    st = client.get("/api/status").json()
    assert not st["job"]["running"] and st["scope"]["sim_model"] == "nano" and not st["target"]["connected"]
    caps = client.get("/api/capabilities").json()
    assert caps["model"] == "nano" and not caps["spi"]["available"]
    assert not client.get("/api/interfaces").json()["spi"]["enabled"]
    refused(client.post("/api/interfaces/spi/transfer", json={"data": "9f"}), "no SPI pins")
    assert client.post("/api/target/connect", json={"kind": "sim"}).status_code == 200
    client.post("/api/capture/start", json={"count": 2, "clear": True})
    deadline = time.time() + 20
    while time.time() < deadline and client.get("/api/status").json()["job"]["running"]:
        time.sleep(0.05)
    job = client.get("/api/status").json()["job"]
    assert job["done"] == 2 and not job["error"] and client.get("/api/status").json()["traces"]["count"] == 2


def test_mcp_interface_tools_against_the_simulator(client):
    """The MCP interface tools end to end against a running Studio (simulated Husky, then a Nano refusing politely)."""
    from cwstudio.mcp_server import StudioError, build_server

    class Bridge:
        base = "http://testclient"

        def _do(self, method, path, body=None, params=None):
            r = client.request(method, path, json=body, params=params)
            if r.status_code >= 400:
                detail = r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
                raise StudioError(f"{method} {path} failed ({r.status_code}): {detail}")
            return r.json() if r.content else None

        def get(self, _path, **params):
            return self._do("GET", _path, params=params or None)

        def post(self, path, body=None, timeout=None):
            return self._do("POST", path, body if body is not None else {})

        def put(self, path, body):
            return self._do("PUT", path, body)

    m = build_server(Bridge())

    def call(name, **args):
        res = m._tools_call({"name": name, "arguments": args})
        assert not res["isError"], res
        return json.loads(res["content"][0]["text"])
    use(client, "husky")
    assert call("hardware_capabilities")["model"] == "husky"
    assert call("uart_configure", baud=57600, parity="odd", stop_bits=2, rx="tio1", tx="tio2")["parity"] == "odd"
    call("uart_configure", baud=38400, parity="none", stop_bits=1)
    call("spi_enable", speed=2e6, cs="pdid")
    assert call("spi_transfer", data="9f 00 00 00")["miso"] == "ff ef 40 18"
    call("spi_disable")
    assert call("gpio_set", pin="tio3", state="low")["pins"]["tio3"]["level"] == 0
    call("gpio_set", pin="tio3", state="high_z")
    assert call("gpio_pulse", pin="nrst", ms=2)["ms"] == 2
    assert call("userio_set", direction=1, drive=1)["direction"] == 1
    call("userio_set", direction=0, drive=0)
    assert call("trigger_configure", kind="uart_pattern", pin="tio1", pattern="'r'", rule=1)["applied"]["rule"] == 1
    assert call("trigger_configure", kind="basic", pins=["tio4"])["current"]["trigger.module"] == "basic"
    assert call("bitbang", bits="1010", record="0011")["recorded"] == "10"
    assert call("onewire", action="read_rom")["crc_ok"]
    assert call("simpleserial_connect", version="2.1")["version"] == "2.1"
    st = call("openocd_status", list_targets=False)
    assert st["running"] is False and "interface_cfg" in st
    assert call("openocd_mpsse", enable=True)["supported"] is False  # the simulator cannot drive OpenOCD
    assert call("interfaces_status")["capabilities"]["model"] == "husky"
    use(client, "nano")
    no = call("spi_enable")
    assert no == {"ok": False, "supported": False, "reason": no["reason"]} and "SPI pins" in no["reason"]
    assert call("trigger_configure", kind="basic", pins=["tio1"])["supported"] is False
    assert call("bitbang", bits="1")["supported"] is False and call("onewire")["supported"] is False
    assert call("userio_set", direction=1)["supported"] is False


# ----- a scope left in MPSSE mode, unsupported scope types, capabilities on the hardware thread ----------------------------
class FakeNAEUSB:
    def __init__(self, mpsse):
        self.mpsse = mpsse

    def is_MPSSE_enabled(self):
        return self.mpsse


class FakeScope:
    """A real-hardware stand-in: no sim_model, a ChipWhisperer type string and a NAEUSB that answers MPSSE_ENABLED."""

    def __init__(self, cwtype="cwhusky", mpsse=False, sn="SN0001"):
        self.cwtype, self.sn, self.calls, self._nae = cwtype, sn, [], FakeNAEUSB(mpsse)

    def _getCWType(self):
        return self.cwtype

    def _getNAEUSB(self):
        return self._nae

    def check_feature(self, name):
        raise RuntimeError("no firmware feature list in the fake")

    def enable_MPSSE(self, enable=True, **kw):
        self.calls.append(("enable_MPSSE", enable))

    def dis(self):
        self.calls.append(("dis",))


@pytest.fixture
def fresh(monkeypatch):
    """A Studio of its own whose USB bus holds what the test says (nothing by default)."""
    from cwstudio import openocd
    bus = []
    monkeypatch.setattr(openocd, "usb_scopes", lambda: list(bus))
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session, c.bus = session, bus
        yield c


def test_mpsse_usb_configuration_rule():
    from cwstudio.openocd import is_mpsse_config
    assert is_mpsse_config([0xFF, 0xFF])  # udc_desc_*_mpsse: the vendor interface plus the MPSSE vendor interface
    assert not is_mpsse_config([0xFF, 0x02, 0x0A])  # normal mode: vendor plus one CDC port
    assert not is_mpsse_config([0xFF, 0x02, 0x0A, 0x02, 0x0A])
    assert not is_mpsse_config([0xFF])  # firmware without CDC
    assert not is_mpsse_config([])


def test_mpsse_found_on_the_usb_bus_after_a_restart(fresh, monkeypatch):
    from cwstudio import hardware, openocd
    fresh.bus.append({"model": "huskyplus", "pid": 0xACE6, "sn": "HP42", "classes": [0xFF, 0xFF]})
    fresh.session.interfaces.openocd._scan_at = 0
    st = fresh.get("/api/interfaces/openocd").json()
    m = st["mpsse"]
    assert m and m["detected"] is True and m["model"] == "huskyplus" and m["sn"] == "HP42" and m["kind"] == "huskyplus" and m["connected"] is False
    assert "Restore normal mode" in m["warning"]
    assert fresh.get("/api/interfaces").json()["mpsse"]["detected"] is True
    # OpenOCD can use the found scope: the command line names its product ID and serial number
    cmd = fresh.session.interfaces.openocd.command_line("target/stm32f3x.cfg", "jtag", openocd.DEFAULT_PORTS, binary="openocd")
    assert "ftdi vid_pid 0x2b3e 0xace6" in cmd and "adapter serial HP42" in cmd
    # Restore normal mode connects to it by kind and serial number and turns MPSSE off
    made = []

    def connect(kind="auto", sn=None, **kw):
        made.append((kind, sn))
        s = FakeScope("cwhuskyplus", mpsse=True, sn=sn)
        made.append(s)
        return s
    monkeypatch.setattr(hardware, "connect_scope", connect)
    fresh.bus.clear()
    r = fresh.post("/api/interfaces/openocd/mpsse", json={"enable": False, "reconnect": False})
    assert r.status_code == 200, r.text
    assert made[0] == ("huskyplus", "HP42") and made[1].calls == [("enable_MPSSE", False), ("dis",)]
    fresh.session.interfaces.openocd._scan_at = 0
    assert fresh.get("/api/interfaces/openocd").json()["mpsse"] is None


def test_mpsse_normal_scopes_are_left_alone(fresh):
    fresh.bus.append({"model": "husky", "pid": 0xACE5, "sn": "H1", "classes": [0xFF, 0x02, 0x0A]})
    fresh.session.interfaces.openocd._scan_at = 0
    assert fresh.get("/api/interfaces/openocd").json()["mpsse"] is None


def test_mpsse_found_on_a_connected_scope(fresh, monkeypatch):
    from cwstudio import hardware
    s = fresh.session
    scope = FakeScope("cwhusky", mpsse=True, sn="H7")
    s.scope, s.scope_kind = scope, "husky"
    st = fresh.get("/api/interfaces").json()
    m = st["mpsse"]
    assert m and m["detected"] and m["connected"] is True and m["model"] == "husky" and m["sn"] == "H7" and m["kind"] == "husky"
    assert st["capabilities"]["model"] == "husky"  # the scope stays connected until OpenOCD or Restore needs it
    made = []
    monkeypatch.setattr(hardware, "connect_scope", lambda kind="auto", sn=None, **kw: made.append((kind, sn)) or FakeScope(sn=sn))
    r = fresh.post("/api/interfaces/openocd/mpsse", json={"enable": False, "reconnect": False})
    assert r.status_code == 200, r.text
    assert s.scope is None and ("dis",) in scope.calls  # Studio let go of it before reconnecting to turn MPSSE off
    assert made == [("husky", "H7")]
    assert fresh.get("/api/interfaces").json()["mpsse"] is None


def test_mpsse_connected_scope_in_normal_mode_is_asked_once(fresh):
    s = fresh.session
    scope = FakeScope("cwlite", mpsse=False)
    asked = []
    scope._nae.is_MPSSE_enabled = lambda: asked.append(1) or False
    s.scope = scope
    for _ in range(3):
        assert fresh.get("/api/interfaces").json()["mpsse"] is None
    assert asked == [1]


def test_unsupported_scope_type_says_so(fresh):
    s = fresh.session
    s.scope = FakeScope("cw305")
    c = fresh.get("/api/capabilities").json()
    want = "this scope type (cw305) is not supported by the Interfaces tab"
    assert c["connected"] is True and c["model"] is None and c["reason"] == want
    assert c["uart"]["reason"] == want and c["jtag"]["reason"] == want
    assert fresh.get("/api/interfaces").json()["capabilities"]["spi"]["reason"] == want
    d = refused(fresh.post("/api/interfaces/spi/enable", json={}), want)
    assert "connect a scope first" not in d
    s.scope = None
    assert fresh.get("/api/capabilities").json()["uart"]["reason"] == "connect a scope first"


def test_capabilities_endpoint_runs_on_the_hardware_thread(fresh, monkeypatch):
    import threading
    from cwstudio import capabilities as capmod
    real, seen = capmod.capabilities, []

    def spy(scope, target=None):
        seen.append(threading.current_thread())
        return real(scope, target)
    monkeypatch.setattr(capmod, "capabilities", spy)
    assert fresh.post("/api/scope/connect", json={"kind": "sim", "sim_model": "husky"}).status_code == 200
    assert fresh.get("/api/capabilities").json()["model"] == "husky"
    hw = fresh.session.worker.call(threading.current_thread)
    assert seen and seen[-1] is hw
