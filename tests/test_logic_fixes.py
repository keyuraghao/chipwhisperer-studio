"""Regression tests for logic analyser fixes: decoder option validation and per-decoder errors in the view, buses kept by channel name across captures, SPI cpol/cpha options, UART 1.5 stop bits on back-to-back frames, automatic hysteresis with a numeric threshold, defaults shared by the tab, the API and MCP, the MCP capture timeout, and the ``channel`` key of PUT /api/la/channels."""
import json
import os
import tempfile

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio.app import create_app
from cwstudio.logic import decoders as D
from cwstudio.logic import sigrok
from cwstudio.logic import synth as S
from cwstudio.logic.model import LogicCapture
from cwstudio.logic.sources import DEFAULTS
from cwstudio.session import Session


@pytest.fixture(scope="module")
def client():
    os.environ.pop("CWSTUDIO_SIGROK_CLI", None)
    sigrok._cache.clear()
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        assert c.post("/api/scope/connect", json={"kind": "sim", "sim_model": "husky"}).status_code == 200
        assert c.post("/api/target/connect", json={"kind": "sim"}).status_code == 200
        yield c


def _sim(client, **settings):
    r = client.post("/api/la/capture", json={"source": "sim", "settings": {"samplerate": 4e6, "duration_ms": 5, "seed": 1, **settings}, "wait": True})
    assert r.status_code == 200, r.text
    return r.json()


def _capture(lines, sr, n):
    return LogicCapture.from_edges(S.render(lines, sr, 0.0, n), n, sr, 0)


# ----- decoders --------------------------------------------------------------------------------------------
def test_uart_one_and_a_half_stop_bits_back_to_back():
    data = list(range(0, 256, 7))
    tx = S.Line(1)
    S.uart(tx, 10e-6, data, 115200, stop_bits=1.5, gap=0)
    n = int((10e-6 + len(data) * 10.5 / 115200 + 50e-6) * 4e6)
    r = D.decode(_capture({"TX": tx}, 4e6, n), "uart", {"tx": "TX"}, {"baud": 115200, "stop_bits": 1.5})
    assert r.values("tx") == data and r.meta["tx_errors"] == 0


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_cpol_cpha_options_are_honoured(mode):
    cpol, cpha = mode >> 1, mode & 1
    data = [0x12, 0xA5, 0xFF, 0x3C]
    lines = {k: S.Line(v) for k, v in (("CS", 1), ("SCK", cpol), ("MOSI", 0))}
    S.spi(lines["CS"], lines["SCK"], lines["MOSI"], None, 10e-6, data, [0] * len(data), 1e6, cpol=cpol, cpha=cpha)
    cap = _capture(lines, 20e6, int(60e-6 * 20e6))
    r = D.decode(cap, "spi", {"cs": "CS", "sck": "SCK", "mosi": "MOSI"}, {"cpol": cpol, "cpha": cpha})
    assert r.values("mosi") == data and r.meta["mode"] == mode
    # cpol/cpha win over a mode sent alongside them (the UI always fills mode in)
    r = D.decode(cap, "spi", {"cs": "CS", "sck": "SCK", "mosi": "MOSI"}, {"mode": 0, "cpol": cpol, "cpha": cpha})
    assert r.values("mosi") == data


def test_validate_options():
    assert D.validate_options("spi", {"word_size": "16", "mode": "3", "cpol": 1}) == {"word_size": "16", "mode": "3", "cpol": 1}
    D.validate_options("uart", {"baud": "auto", "stop_bits": "1.5", "data_bits": 9, "inverted": "false", "glitch_ns": 0})
    for kind, opts, word in (("spi", {"word_size": 0}, "word size"), ("spi", {"word_size": 33}, "word size"), ("spi", {"mode": 4}, "mode"), ("spi", {"cpha": 2}, "cpha"),
                             ("uart", {"baud": "fast"}, "baud"), ("uart", {"baud": -9600}, "baud"), ("uart", {"stop_bits": 3}, "stop bits"), ("uart", {"glitch_ns": -1}, "glitch filter"), ("can", {"sample_point": 99}, "sample point")):
        with pytest.raises(ValueError, match=word):
            D.validate_options(kind, opts)


def test_bad_decoder_options_are_refused_and_never_break_the_view(client):
    cap = _sim(client)
    la = client.session.logic
    before = len(client.get("/api/la").json()["decoders"])
    r = client.post("/api/la/decoders", json={"type": "spi", "options": {"word_size": 0}})
    assert r.status_code == 400 and "word size" in r.json()["detail"]
    assert len(client.get("/api/la").json()["decoders"]) == before
    ok = client.post("/api/la/decoders", json={"type": "uart"}).json()
    assert client.put(f"/api/la/decoders/{ok['id']}", json={"options": {"word_size": 0, "data_bits": 4}}).status_code == 400
    assert client.post("/api/la/decode", json={"type": "spi", "options": {"word_size": 0}}).status_code == 400
    # a decoder that still fails at decode time (stored before validation, or an unexpected exception) reports its own error and the view still answers
    la.decoders["dbad"] = {"id": "dbad", "type": "spi", "name": "broken SPI", "channels": {}, "options": {"word_size": 0}, "enabled": True, "rev": 0}
    try:
        v = client.post("/api/la/view", json={"a": 0, "b": cap["samples"], "px": 500})
        assert v.status_code == 200, v.text
        decs = {d["id"]: d for d in v.json()["decoders"]}
        assert decs["dbad"]["error"] and decs["dbad"]["rows"] == [] and decs["dbad"]["name"] == "broken SPI"
        assert decs[ok["id"]].get("error") is None and decs[ok["id"]]["rows"]
    finally:
        la.decoders.pop("dbad", None)
        client.delete(f"/api/la/decoders/{ok['id']}")


# ----- buses and channels ----------------------------------------------------------------------------------
def test_buses_are_kept_by_name_and_disabled_when_channels_are_missing(client):
    full = _sim(client)
    names = [c["name"] for c in full["channels"]]
    r = client.put("/api/la/channels", json={"buses": [{"name": "spi", "channels": [names.index("SPI CS"), "SPI SCK"], "format": "bin"}, {"name": "uart", "channels": ["UART TX", "UART RX"]}]}).json()
    assert [b["channels"] for b in r["buses"]] == [["SPI CS", "SPI SCK"], ["UART TX", "UART RX"]] and not any(b["disabled"] for b in r["buses"])
    # a capture with fewer channels: the SPI bus is disabled with a notice, the UART bus maps onto the new indices, the view still works
    small = _sim(client, channels=["UART RX", "UART TX", "SPI CS"])
    st = client.get("/api/la").json()["buses"]
    assert st[0]["disabled"] and st[0]["missing"] == ["SPI SCK"] and "SPI SCK" in st[0]["notice"]
    assert not st[1]["disabled"] and st[1]["index"] == [1, 0]
    v = client.post("/api/la/view", json={"a": 0, "b": small["samples"], "px": 400})
    assert v.status_code == 200, v.text
    v = v.json()
    assert len(v["buses"]) == 2 and v["buses"][0]["disabled"] and v["buses"][1]["runs"] and len(v["notices"]) == 1
    # back to every channel: the bus is active again
    _sim(client)
    assert not any(b["disabled"] for b in client.get("/api/la").json()["buses"])
    # renaming a member carries over to the bus
    r = client.put("/api/la/channels", json={"channels": [{"channel": "UART TX", "name": "TXD"}]}).json()
    assert r["buses"][1]["channels"] == ["TXD", "UART RX"] and not r["buses"][1]["disabled"]
    client.put("/api/la/channels", json={"channels": [{"channel": "TXD", "name": "UART TX"}], "buses": []})


def test_channels_put_takes_index_or_name_under_channel(client):
    cap = _sim(client)
    i = next(c["index"] for c in cap["channels"] if c["name"] == "CLK")
    r = client.put("/api/la/channels", json={"channels": [{"channel": i, "hidden": True}, {"channel": "TRIG", "color": "#abcdef"}]}).json()
    by = {c["name"]: c for c in r["channels"]}
    assert by["CLK"]["hidden"] and by["TRIG"]["color"] == "#abcdef"
    r = client.put("/api/la/channels", json={"channels": [{"channel": str(i), "hidden": False}, {"index": "TRIG", "color": "#123456"}]}).json()
    by = {c["name"]: c for c in r["channels"]}
    assert not by["CLK"]["hidden"] and by["TRIG"]["color"] == "#123456"
    bad = client.put("/api/la/channels", json={"channels": [{"name": "x"}]})
    assert bad.status_code == 400 and "channel" in bad.json()["detail"]


# ----- capture settings --------------------------------------------------------------------------------------
def test_numeric_threshold_keeps_auto_hysteresis(client):
    base = {"segments": 2, "sim_signal": "UART TX", "seed": 5}
    auto = client.post("/api/la/capture", json={"source": "adc", "settings": base, "wait": True}).json()
    lvl, hyst = auto["meta"]["level"], auto["meta"]["hysteresis"]
    assert hyst > 0
    num = client.post("/api/la/capture", json={"source": "adc", "settings": {**base, "level": str(lvl), "hysteresis": "auto"}, "wait": True}).json()
    assert num["meta"]["level"] == pytest.approx(lvl) and num["meta"]["hysteresis"] == pytest.approx(hyst)
    fixed = client.post("/api/la/capture", json={"source": "adc", "settings": {**base, "level": str(lvl), "hysteresis": 0.01}, "wait": True}).json()
    assert fixed["meta"]["hysteresis"] == pytest.approx(0.01)


def test_defaults_are_the_same_in_the_tab_api_and_mcp(client):
    src = client.get("/api/la/sources").json()
    assert src["defaults"] == DEFAULTS and DEFAULTS["native"]["downsample"] == 96 and DEFAULTS["adc"]["segments"] == 20
    adc = client.post("/api/la/capture", json={"source": "adc", "settings": {"sim_signal": "UART TX", "seed": 2}, "wait": True}).json()
    assert adc["meta"]["segments"] == 20
    nat = client.post("/api/la/capture", json={"source": "native", "settings": {"trigger": "manual", "depth": 200}, "wait": True}).json()
    assert nat["meta"]["downsample"] == 96
    js = open(os.path.join(os.path.dirname(D.__file__), "..", "static", "js", "logic.js"), encoding="utf-8").read()
    assert "D.native.downsample" in js and "D.adc.segments" in js


def test_mcp_la_capture_timeout_reaches_the_trigger_timeout():
    from cwstudio.mcp_server import build_server
    sent = []

    class Rec:
        base = "http://rec"

        def post(self, path, body=None, timeout=None):
            sent.append((path, body, timeout))
            return {"ok": True}

        def get(self, path, **params):
            return {}

        def put(self, path, body):
            return {}
    m = build_server(Rec())
    r = m._tools_call({"name": "la_capture", "arguments": {"source": "native", "trigger": "rising_tio1", "timeout": 12.5}})
    assert not r["isError"], r
    path, body, http_timeout = sent[-1]
    assert path == "/api/la/capture" and body["settings"]["timeout"] == 12.5 and body["timeout"] > 12.5 and http_timeout > body["timeout"]
    m._tools_call({"name": "la_capture", "arguments": {"source": "sim"}})
    assert "timeout" not in sent[-1][1]["settings"]
    out = json.dumps(m._tools_list({}))
    assert "trigger" in next(t for t in m._tools_list({})["tools"] if t["name"] == "la_capture")["description"] and out
