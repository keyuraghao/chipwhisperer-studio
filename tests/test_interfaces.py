"""Protocols and interfaces against the simulator standing in for each ChipWhisperer model: capabilities, gating (unsupported actions refused with 400 and the reason), UART, SPI with the simulated flash, GPIO, USERIO, triggers, bit-banger and 1-Wire, programmer gating, scope settings gating, OpenOCD helpers and the OpenOCD toolchain entry."""
import json
import os
import tempfile

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
    assert openocd.parse_wrapped("0 {hello world}") == {"ok": True, "output": "hello world"}
    assert openocd.parse_wrapped('1 {invalid command name "foo"}')["ok"] is False
    assert "capture {targets}" in openocd.wrap_command("targets")
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
