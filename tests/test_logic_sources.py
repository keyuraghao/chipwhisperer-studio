"""Logic analyser capture sources, API, MCP tools and performance.

* The Husky path against a fake ``scope.LA`` that implements the documented API (packing samples the way the FPGA FIFO does, and decoding them with the chipwhisperer library's own ``LASettings.extract`` when it is installed).
* The simulated Husky LA and the analog-to-logic path against the simulator, for every model.
* The sigrok path against a fake ``sigrok-cli`` that writes a real .sr zip, and graceful refusal when sigrok-cli is missing.
* Capability gating per simulated model, the ``/api/la/*`` routes, the MCP tools, and the visible-range query on 10 channels of 10 million samples.
"""
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import zipfile

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio.aes import encrypt_block
from cwstudio.app import create_app
from cwstudio.capabilities import SIM_MODELS, Unsupported, capabilities
from cwstudio.logic import decoders as D
from cwstudio.logic import sigrok, synth
from cwstudio.logic.model import Channel, LogicCapture
from cwstudio.logic.sources import LogicJob, SimLA
from cwstudio.session import Session
from cwstudio.simulator import SimScope

try:
    from chipwhisperer.capture.scopes.cwhardware.ChipWhispererHuskyMisc import LASettings
    LIB_EXTRACT = LASettings.extract
except Exception:  # noqa: BLE001
    LIB_EXTRACT = None


@pytest.fixture(scope="module")
def client():
    os.environ.pop("CWSTUDIO_SIGROK_CLI", None)
    sigrok._cache.clear()
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def use(client, model):
    assert client.post("/api/scope/connect", json={"kind": "sim", "sim_model": model}).status_code == 200
    assert client.post("/api/target/connect", json={"kind": "sim"}).status_code == 200


def refused(r, *words):
    assert r.status_code == 400, r.text
    d = r.json()["detail"]
    assert d.startswith("Unsupported:"), d
    for w in words:
        assert w in d, d
    return d


# ----- fake Husky ----------------------------------------------------------------------------------------
def pack(bits):
    """Samples of 9 signals as FIFO entries: 3 bytes holding 2 samples per signal, signal s in byte s // 4, the first sample at bit 2*(s%4)+1 and the second at bit 2*(s%4) (ChipWhispererHuskyMisc.LASettings.extract)."""
    n = bits.shape[1] // 2 * 2
    out = []
    for k in range(0, n, 2):
        e = [0, 0, 0]
        for s in range(9):
            hi = 2 * (s % 4) + 1
            e[s // 4] |= (int(bits[s, k]) << hi) | (int(bits[s, k + 1]) << (hi - 1))
        out.append(e)
    return out


class FakeLA:
    """The documented scope.LA API with call recording; the capture data is a known pattern."""

    def __init__(self, plus=False):
        self.calls = []
        self.present = True
        self._enabled = False
        self.clk_source = "usb"
        self.oversampling_factor = 1
        self.downsample = 1
        self.capture_group = "CW 20-pin"
        self._depth = 16376
        self._trig = "capture"
        self.locked = True
        self.max_capture_depth = 65535 if plus else 16376
        self.errors_value = None
        self.fifo_polls = 3
        self.never = False
        self.data = None

    def __setattr__(self, k, v):
        if k in ("clk_source", "oversampling_factor", "downsample", "capture_group") and "calls" in self.__dict__:
            self.calls.append(("set", k, v))
        object.__setattr__(self, k, v)

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, v):
        self.calls.append(("set", "enabled", v))
        self._enabled = v

    @property
    def capture_depth(self):
        return self._depth

    @capture_depth.setter
    def capture_depth(self, d):
        if d > self.max_capture_depth:
            raise ValueError("Maximum capture depth")
        self._depth = d - (d % 2)

    @property
    def trigger_source(self):
        return self._trig

    @trigger_source.setter
    def trigger_source(self, v):
        self.calls.append(("set", "trigger_source", v))
        self._trig = v

    @property
    def source_clock_frequency(self):
        return 96e6 if self.clk_source == "usb" else 7.37e6

    @property
    def sampling_clock_frequency(self):
        return self.source_clock_frequency * self.oversampling_factor

    @property
    def errors(self):
        return self.errors_value

    @errors.setter
    def errors(self, v):
        self.calls.append(("clear_errors",))

    def arm(self):
        if not self.locked:
            raise Exception("LA clock is not locked!")
        self.calls.append(("arm",))
        n = self._depth
        k = np.arange(n)
        self.data = np.stack([((k >> s) & 1) for s in range(8)] + [(k % 7 == 0).astype(int)]).astype(np.uint8)
        self._polls = self.fifo_polls

    def trigger_now(self):
        self.calls.append(("trigger_now",))

    def fifo_empty(self):
        if self.never:
            return True
        self._polls -= 1
        return self._polls > 0

    def read_capture_data(self):
        self.calls.append(("read",))
        return pack(self.data)

    @staticmethod
    def extract(raw, index):
        if LIB_EXTRACT is not None:
            return LIB_EXTRACT(raw, index)
        return SimLA.extract(raw, index)


class FakeADC:
    offset = 100
    presamples = 20
    samples = 1000
    decimate = 1


class FakeScope:
    def __init__(self, plus=False):
        self.LA = FakeLA(plus)
        self.adc = FakeADC()
        self.clock = type("C", (), {"adc_freq": 29.538e6, "clkgen_freq": 7.37e6})()
        self.userio = type("U", (), {"mode": "trace", "direction": 0xFF})()
        self.armed = 0

    def _getCWType(self):
        return "cwhuskyplus" if self.LA.max_capture_depth == 65535 else "cwhusky"

    def arm(self):
        self.armed += 1

    def capture(self):
        return False

    def get_last_trace(self):
        return np.linspace(-0.4, 0.4, self.adc.samples).astype(np.float32)


def run(source, settings, scope, target=None, tmp=None):
    job = LogicJob(source, settings, scope, target, tmp or tempfile.mkdtemp(), on_done=lambda j: None, publish=lambda k, p: None)
    job.start()
    for _ in range(200000):
        if job.step():
            break
    job.finish()
    return job


def test_husky_la_against_fake_scope():
    sc = FakeScope()
    job = run("native", {"group": "USERIO 20-pin", "clk_source": "pll", "oversampling": 4, "downsample": 2, "depth": 1001, "trigger": "rising_userio_d3"}, sc)
    cap = job.capture
    assert [c.name for c in cap.channels] == ["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "CK"]
    assert cap.samplerate == pytest.approx(7.37e6 * 4 / 2) and cap.n == 1000 and cap.trigger == 0
    for s in range(9):
        assert np.array_equal(cap.channels[s].dense(0, cap.n), sc.LA.data[s]), s
    names = [c[1:] for c in sc.LA.calls if c[0] == "set"]
    assert ("enabled", True) in names and ("clk_source", "pll") in names and ("oversampling_factor", 4.0) in names and ("downsample", 2) in names
    assert ("capture_group", "USERIO 20-pin") in names and ("trigger_source", "rising_userio_d3") in names
    assert ("arm",) in sc.LA.calls and ("clear_errors",) in sc.LA.calls and ("trigger_now",) not in sc.LA.calls
    assert sc.userio.mode == "normal" and sc.userio.direction == 0  # USERIO pins made inputs
    assert cap.meta["pretrigger"] is False and cap.meta["group"] == "USERIO 20-pin"


def test_husky_la_manual_errors_analog_and_failures():
    sc = FakeScope(plus=True)
    sc.LA.errors_value = "FIFO overflow, "
    job = run("native", {"group": "CW 20-pin", "trigger": "manual", "depth": 65535}, sc)
    assert ("trigger_now",) in sc.LA.calls and not any(c[1] == "trigger_source" for c in sc.LA.calls if c[0] == "set")
    assert job.capture.n == 65534 and [c.name for c in job.capture.channels][-1] == "ADC clock"
    assert any("FIFO" in w for w in job.warnings)
    # LA trigger on the ADC capture trigger with the analog trace on the same time base
    sc2 = FakeScope()
    job = run("native", {"trigger": "capture", "with_analog": True, "fire": "none", "depth": 200}, sc2)
    an = job.capture.analog[0]
    assert sc2.armed == 1 and an.samplerate == pytest.approx(29.538e6) and an.t0 == pytest.approx((100 - 20) / 29.538e6) and an.data.shape[0] == 1000
    # unlocked clock, a trigger that never comes, a bad group
    sc3 = FakeScope()
    sc3.LA.locked = False
    with pytest.raises(RuntimeError, match="not locked"):
        run("native", {}, sc3)
    sc4 = FakeScope()
    sc4.LA.never = True
    with pytest.raises(RuntimeError, match="no logic analyser trigger"):
        run("native", {"timeout": 0.05, "depth": 100}, sc4)
    with pytest.raises(ValueError):
        run("native", {"group": "debug"}, FakeScope())
    sc5 = FakeScope()
    sc5.LA.present = False
    with pytest.raises(Unsupported):
        run("native", {}, sc5)


def test_sim_la_packing_matches_the_library():
    sc = SimScope(sim_model="husky")
    la = sc.LA
    la.capture_depth = 2000
    la.downsample = 10
    la.trigger_source = "rising_tio4"
    la.arm()
    assert not la.fifo_empty()
    want = la._data.copy()
    raw = la.read_capture_data()
    assert len(raw) == 1000 and all(len(e) == 3 for e in raw)
    ext = LIB_EXTRACT or SimLA.extract
    for s in range(9):
        assert np.array_equal(np.asarray(ext(raw, s)), want[s]), s
    assert la.fifo_empty()
    with pytest.raises(ValueError):
        la.capture_depth = 20000
    la.oversampling_factor = 10
    assert not la.locked
    with pytest.raises(Exception, match="not locked"):
        la.arm()


@pytest.mark.parametrize("model", ["husky", "huskyplus"])
def test_sim_husky_la_decodes_the_simpleserial_response(client, model):
    use(client, model)
    r = client.post("/api/la/capture", json={"source": "native", "settings": {"group": "CW 20-pin", "downsample": 96, "trigger": "capture", "fire": "simpleserial", "with_analog": model == "huskyplus"}, "wait": True})
    assert r.status_code == 200, r.text
    cap = r.json()
    assert cap["samplerate"] == 1e6 and cap["samples"] == (16376 if model == "husky" else 65534)
    pt = bytes.fromhex(cap["meta"]["plaintext"])
    ct = encrypt_block(bytes(range(16)), pt)
    dec = client.post("/api/la/decode", json={"type": "uart", "channels": {"tx": "IO1"}, "limit": 100}).json()
    got = bytes(a["value"] for a in dec["annotations"])
    assert got == synth.ss1_line("r", ct) + b"z00\n"
    assert dec["meta"]["baud"] == 38400
    assert (len(cap["analog"]) == 1) == (model == "huskyplus")
    io4 = next(c for c in cap["channels"] if c["name"] == "IO4")
    assert io4["init"] == 1 and io4["edges"] == 1  # the trigger is high at t = 0 and falls after the encryption


@pytest.mark.parametrize("model", SIM_MODELS)
def test_analog_to_logic_on_every_model(client, model):
    use(client, model)
    r = client.post("/api/la/capture", json={"source": "adc", "settings": {"segments": 64, "sim_signal": "UART TX", "seed": 3}, "wait": True})
    assert r.status_code == 200, r.text
    cap = r.json()
    assert cap["samplerate"] == pytest.approx(29.48e6, rel=0.01) and cap["samples"] == 64 * 5000
    assert cap["analog"][0]["threshold"] and cap["meta"]["segments"] == 64
    dec = client.post("/api/la/decode", json={"type": "uart", "channels": {"tx": 0}, "limit": 100}).json()
    sig, desc = synth.demo_signals()
    got = bytes(a["value"] for a in dec["annotations"])
    assert len(got) >= 30 and desc["uart_resp"].startswith(got), got
    assert dec["meta"]["baud"] == 38400
    # the scope's own sample count is restored afterwards
    assert client.session.scope.adc.samples == 5000


def test_sim_source_and_gating_per_model(client):
    for model in SIM_MODELS:
        use(client, model)
        la = client.get("/api/capabilities").json()["logic_analyzer"]
        husky = model in ("husky", "huskyplus")
        assert la["native"]["available"] == husky and la["adc"]["available"] and la["sim"]["available"] and la["files"]["available"]
        assert not la["external"]["available"] and "sigrok-cli is not installed" in la["external"]["reason"]
        if husky:
            assert la["native"]["depth"] == (65535 if model == "huskyplus" else 16376)
        else:
            refused(client.post("/api/la/capture", json={"source": "native", "wait": True}), "Husky")
        src = {s["id"]: s for s in client.get("/api/la/sources").json()["sources"]}
        assert src["native"]["available"] == husky
    refused(client.post("/api/la/capture", json={"source": "sigrok", "wait": True}), "sigrok-cli is not installed")
    assert client.post("/api/la/capture", json={"source": "files"}).status_code == 400
    caps = capabilities(None)["logic_analyzer"]
    assert caps["native"]["reason"] == "connect a scope first" and caps["files"]["available"]
    r = client.post("/api/la/capture", json={"source": "sim", "settings": {"samplerate": 8e6, "duration_ms": 30, "pretrigger": 40, "jitter_ns": 20, "glitches_per_ms": 0.0, "seed": 1}, "wait": True}).json()
    assert r["samples"] == 240000 and r["trigger"] == 96000 and len(r["channels"]) == 18


# ----- the API on a capture ------------------------------------------------------------------------------
def test_api_view_channels_decoders_search_measure_files(client, tmp_path):
    use(client, "husky")
    cap = client.post("/api/la/capture", json={"source": "sim", "settings": {"samplerate": 4e6, "duration_ms": 25}, "wait": True}).json()
    n = cap["samples"]
    v = client.post("/api/la/view", json={"a": 0, "b": n, "px": 1000}).json()
    assert len(v["channels"]) == 18 and any("b" in c for c in v["channels"]) and any("edges" in c for c in v["channels"])
    # decoders: guessed channels, results in the view and in the table, CSV export
    for kind in ("uart", "spi", "i2c", "onewire", "jtag", "swd", "can", "simpleserial"):
        d = client.post("/api/la/decoders", json={"type": kind}).json()
        assert d["error"] is None and d["count"] > 0, (kind, d)
    v = client.post("/api/la/view", json={"a": 0, "b": n, "px": 1000}).json()
    assert len(v["decoders"]) == 8 and all("rows" in d for d in v["decoders"])
    t = client.get("/api/la/annotations", params={"q": "EF 40 18"}).json()
    assert t["total"] == 1 and "MISO FF EF 40 18" in t["items"][0]["text"]
    t = client.get("/api/la/annotations", params={"q": "nack"}).json()
    assert t["total"] >= 2
    csv = client.get("/api/la/annotations.csv").text.splitlines()
    assert csv[0].startswith("start_s,end_s") and len(csv) > 100
    # search: edges, patterns and decoded values
    trig = cap["trigger"]
    e = client.post("/api/la/search", json={"kind": "edge", "channel": "TRIG", "edge": "rising", "from": 0}).json()
    assert e["index"] == trig and e["t"] == 0
    p = client.post("/api/la/search", json={"kind": "pattern", "pattern": "1X1", "channels": ["UART TX", "UART RX", "TRIG"], "from": 0}).json()
    assert p["index"] == trig
    p = client.post("/api/la/search", json={"kind": "pattern", "pattern": "0", "channels": ["SPI CS"], "edge_channel": "SPI SCK", "edge": "rising", "from": 0}).json()
    assert p["found"] and p["index"] > trig
    d = client.post("/api/la/search", json={"kind": "decoded", "text": "0x18DAF110", "from": 0}).json()
    assert d["found"] and "18DAF110" in d["item"]["text"]
    # measurements: the demo clock is 1 MHz, 50 % duty
    m = client.post("/api/la/measure", json={"channel": "CLK", "t0": 0, "t1": 1e-3}).json()
    assert m["frequency"] == pytest.approx(1e6, rel=1e-3) and m["duty"] == pytest.approx(0.5, abs=0.01) and m["edges"] in (1999, 2000, 2001)
    # channels: rename, colour, hide, reorder, bus
    r = client.put("/api/la/channels", json={"channels": [{"index": "CLK", "name": "Clock", "color": "#123456", "hidden": True}], "order": ["TRIG"], "buses": [{"name": "spi", "channels": ["SPI CS", "SPI SCK"], "format": "bin"}]}).json()
    clk = next(c for c in r["channels"] if c["name"] == "Clock")
    assert clk["hidden"] and clk["color"] == "#123456" and r["order"][0] == 2 and r["buses"][0]["channels"] == [4, 5]
    v = client.post("/api/la/view", json={"a": 0, "b": n, "px": 500}).json()
    assert all(c["i"] != clk["index"] for c in v["channels"]) and v["buses"][0]["runs"]
    assert client.put("/api/la/channels", json={"channels": [{"index": 0, "color": "red"}]}).status_code == 400
    # decoders follow renamed channels by name or index
    assert all(d.get("error") is None for d in client.post("/api/la/view", json={"a": 0, "b": n, "px": 500}).json()["decoders"])
    # export and re-import every format, from the API and as downloads
    for fmt in ("vcd", "csv", "sr"):
        out = client.post("/api/la/export", json={"format": fmt, "path": str(tmp_path / f"cap.{fmt}")}).json()
        assert os.path.getsize(out["path"]) > 100
        back = client.post("/api/la/import", json={"path": out["path"]}).json()
        assert back["samples"] == n and back["samplerate"] == 4e6 and back["trigger"] == trig
        assert client.get(f"/api/la/download/{fmt}").status_code == 200
    up = client.post("/api/la/import/upload", files={"file": ("x.vcd", open(tmp_path / "cap.vcd", "rb").read())}).json()
    assert up["samples"] == n
    assert client.post("/api/la/import", json={"path": str(tmp_path / "missing.vcd")}).status_code == 400
    lst = client.get("/api/la/captures").json()
    assert len(lst["captures"]) <= 8 and lst["current"] == up["id"]
    # stored power traces show up as an analog row on the same time base
    assert client.post("/api/capture/start", json={"count": 1, "clear": True}).status_code == 200
    for _ in range(100):
        if client.get("/api/traces").json()["count"]:
            break
        time.sleep(0.05)
    an = client.post("/api/la/analog/from_trace", json={"index": -1}).json()["analog"]
    assert an and an[-1]["samples"] == 5000
    assert client.post("/api/la/analog/to_waveform", json={"index": len(an) - 1}).json()["count"] == 2
    for d in client.get("/api/la/decoders").json()["decoders"]:
        client.delete(f"/api/la/decoders/{d['id']}")


# ----- sigrok -----------------------------------------------------------------------------------------------
FAKE_SIGROK = r'''#!{python}
"""A stand-in for sigrok-cli: version, scan, decoder list, captures into a real .sr zip, and protocol decoder output."""
import sys, zipfile, json, os
args = sys.argv[1:]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
if "--version" in args:
    print("sigrok-cli 0.7.2\n\nLibraries and features:\n- libsigrok 0.5.2/5:1:1 (rt: 0.5.2/5:1:1).")
elif "--scan" in args:
    print("The following devices were found:")
    print("demo - Demo device with 13 channels: D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4")
    print("fx2lafw:conn=1.2 - Saleae Logic with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7")
elif "-L" in args:
    print("Supported hardware drivers:\n  demo                 Demo driver and pattern generator\n\nSupported protocol decoders:\n  uart                 Universal Asynchronous Receiver/Transmitter\n  spi                  Serial Peripheral Interface\n\nSupported output formats:\n  srzip   srzip")
elif "-P" in args:
    path = args[args.index("-i") + 1]
    with zipfile.ZipFile(path) as z:
        assert "metadata" in z.namelist()
    print("10-90 uart-1: rx-data: 41")
    print("90-170 uart-1: rx-data: 42")
    print("not an annotation line")
elif "-d" in args:
    if args[args.index("-d") + 1] == "broken":
        print("sr: no device found", file=sys.stderr)
        sys.exit(1)
    out = args[args.index("-o") + 1]
    n = int(args[args.index("--samples") + 1]) if "--samples" in args else 100
    rate = 1000000
    if "--config" in args:
        for kv in args[args.index("--config") + 1].split(":"):
            if kv.startswith("samplerate="):
                rate = int(kv.split("=")[1])
    chans = args[args.index("-C") + 1].split(",") if "-C" in args else ["D0", "D1", "D2"]
    data = bytes(((i // 4) & 1) | ((1 if i % 10 == 0 else 0) << 1) | (1 << 2) for i in range(n))
    meta = "[global]\nsigrok version=0.5.2\n\n[device 1]\ndriver=demo\ncapturefile=logic-1\nunitsize=1\ntotal probes=%d\nsamplerate=%d kHz\n" % (len(chans), rate // 1000)
    meta += "".join("probe%d=%s\n" % (i + 1, c) for i, c in enumerate(chans))
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("version", "2")
        z.writestr("metadata", meta)
        z.writestr("logic-1-1", data)
'''


@pytest.fixture
def fake_sigrok(tmp_path, monkeypatch):
    exe = tmp_path / "sigrok-cli"
    exe.write_text(FAKE_SIGROK.replace("{python}", sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CWSTUDIO_SIGROK_CLI", str(exe))
    sigrok._cache.clear()
    yield tmp_path
    sigrok._cache.clear()


def _calls(d):
    return [json.loads(x) for x in (d / "calls.jsonl").read_text().splitlines()]


def test_sigrok_missing_shows_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv("CWSTUDIO_SIGROK_CLI", str(tmp_path / "nope"))
    monkeypatch.setenv("PATH", str(tmp_path))
    sigrok._cache.clear()
    st = sigrok.status()
    assert not st["available"] and "not installed" in st["reason"] and any("sigrok-cli" in s for s in st["install"]["steps"]) and "sigrok.org" in st["install"]["url"]
    with pytest.raises(RuntimeError, match="not installed"):
        sigrok.scan()


def test_sigrok_capture_scan_and_decoders(client, fake_sigrok):
    st = client.get("/api/la/sigrok", params={"refresh": True}).json()
    assert st["available"] and st["version"] == "0.7.2"
    use(client, "lite")
    assert client.get("/api/capabilities").json()["logic_analyzer"]["external"]["available"]
    devs = client.get("/api/la/sigrok/scan").json()["devices"]
    assert [d["id"] for d in devs] == ["demo", "fx2lafw:conn=1.2"] and devs[1]["channels"][:2] == ["D0", "D1"] and devs[1]["description"].startswith("Saleae")
    assert [d["id"] for d in client.get("/api/la/sigrok/decoders").json()["decoders"]] == ["uart", "spi"]
    r = client.post("/api/la/capture", json={"source": "sigrok", "settings": {"device": "demo", "samplerate": 2000000, "channels": ["D0", "D1", "D2"], "samples": 1000, "triggers": "D0=r"}, "wait": True})
    assert r.status_code == 200, r.text
    cap = r.json()
    assert cap["source"] == "sigrok" and cap["samples"] == 1000 and cap["samplerate"] == 2e6 and [c["name"] for c in cap["channels"]] == ["D0", "D1", "D2"]
    assert cap["channels"][0]["edges"] == 249 and cap["channels"][2]["edges"] == 0
    args = _calls(fake_sigrok)[-1]
    assert args[:2] == ["-d", "demo"] and "--config" in args and "samplerate=2000000" in args and args[args.index("-C") + 1] == "D0,D1,D2"
    assert args[args.index("--samples") + 1] == "1000" and args[args.index("--triggers") + 1] == "D0=r" and "--wait-trigger" in args and args[-2] == "-o"
    assert client.post("/api/la/capture", json={"source": "sigrok", "settings": {"device": "demo", "triggers": "D0 rising"}, "wait": True}).status_code == 400
    bad = client.post("/api/la/capture", json={"source": "sigrok", "settings": {"device": "broken"}, "wait": True})
    assert bad.status_code == 400 and "no device found" in bad.json()["detail"]
    # sigrok's protocol decoders as an extra on top of the built-in ones
    d = client.post("/api/la/decoders", json={"type": "sigrok", "options": {"spec": "uart:rx=D0:baudrate=9600"}}).json()
    assert d["error"] is None and d["count"] == 2
    one = client.post("/api/la/decode", json={"type": "sigrok", "options": {"spec": "uart:rx=D0:baudrate=9600"}}).json()
    assert [(a["s"], a["e"], a["row"], a["text"]) for a in one["annotations"]] == [(10, 90, "uart-1 rx-data", "41"), (90, 170, "uart-1 rx-data", "42")]
    assert "-P" in _calls(fake_sigrok)[-1]
    assert client.post("/api/la/decoders", json={"type": "sigrok", "options": {"spec": "uart rx D0"}}).status_code == 400
    client.delete(f"/api/la/decoders/{d['id']}")


# ----- MCP --------------------------------------------------------------------------------------------------
class TestClientAdapter:
    """StudioClient over the in-process TestClient, so the MCP tools run against the real routes."""

    __test__ = False
    base = "http://testclient"

    def __init__(self, c):
        self.c = c

    def _r(self, r, method, path):
        from cwstudio.mcp_server import StudioError
        if r.status_code >= 400:
            raise StudioError(f"{method} {path} failed ({r.status_code}): {r.json().get('detail')}")
        return r.json()

    def get(self, _path, **params):
        return self._r(self.c.get(_path, params={k: v for k, v in params.items() if v is not None}), "GET", _path)

    def post(self, path, body=None, timeout=None):
        return self._r(self.c.post(path, json=body or {}), "POST", path)

    def put(self, path, body):
        return self._r(self.c.put(path, json=body), "PUT", path)


def test_mcp_logic_tools(client, tmp_path):
    from cwstudio.mcp_server import build_server
    m = build_server(TestClientAdapter(client))

    def call(name, **args):
        r = m._tools_call({"name": name, "arguments": args})
        assert not r["isError"], r
        return json.loads(r["content"][0]["text"])
    names = {t["name"] for t in m._tools_list({})["tools"]}
    assert {"la_sources", "la_capture", "la_import", "la_export", "la_decode", "la_measure", "la_channels", "la_search", "la_status"} <= names
    use(client, "nano")
    src = {s["id"]: s for s in call("la_sources")["sources"]}
    assert not src["native"]["available"] and src["adc"]["available"]
    no = call("la_capture", source="native")
    assert no["supported"] is False and "Husky" in no["reason"]
    no = call("la_capture", source="sigrok", device="demo")
    assert no["supported"] is False and "sigrok-cli" in no["reason"]
    cap = call("la_capture", source="sim", samplerate=4e6, duration_ms=25)
    assert cap["samples"] == 100000
    dec = call("la_decode", decoder="spi")
    assert any("MISO FF EF 40 18" in a["text"] for a in dec["annotations"])
    dec = call("la_decode", decoder="i2c", channels={"scl": "I2C SCL", "sda": "I2C SDA"}, add_to_view=True)
    assert dec["count"] > 10 and call("la_status")["decoders"]
    meas = call("la_measure", channel="CLK", t0=0, t1=0.001)
    assert round(meas["frequency"]) == 1000000
    ch = call("la_channels", rename={"CLK": "clock"}, hide=["CAN"])
    assert any(c["name"] == "clock" for c in ch["channels"]) and next(c for c in ch["channels"] if c["name"] == "CAN")["hidden"]
    hit = call("la_search", kind="edge", channel="TRIG", edge="falling", from_sample=0)
    assert hit["t"] == pytest.approx(0.25e-3, abs=1e-6)
    out = call("la_export", format="sr", path=str(tmp_path / "mcp.sr"))
    back = call("la_import", path=out["path"])
    assert back["samples"] == 100000
