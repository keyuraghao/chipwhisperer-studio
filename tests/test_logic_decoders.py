"""Logic analyser decoders, file formats and the capture model.

Every decoder gets a round trip: a waveform is generated from known data with :mod:`cwstudio.logic.synth` (written from the protocol timing rules, not from the decoder), sampled, decoded, and the decoded values compared with the data. Edge cases: UART parity and framing errors, every SPI mode, I2C NACK, repeated start and 10-bit addresses, CAN extended IDs with stuff bits and CRC errors, JTAG IR/DR shifts, SWD WAIT/FAULT/parity errors, 1-Wire ROM CRC and missing presence, SimpleSerial v1 and v2. VCD, CSV and .sr files round trip, and files in other tools' dialects import.
"""
import os
import zipfile

import numpy as np
import pytest

from cwstudio.logic import decoders as D
from cwstudio.logic import formats as F
from cwstudio.logic import synth as S
from cwstudio.logic.model import Channel, LogicCapture, auto_level, parse_pattern, schmitt


def capture(lines, sr, t0=0.0, dur=None, n=None, trigger=None, **kw):
    """Render synth lines into a capture."""
    if n is None:
        end = max((ln.times[-1] for ln in lines.values() if isinstance(ln, S.Line) and ln.times), default=0)
        n = int(((dur if dur is not None else end - t0) + 50e-6) * sr) + 10
    e = S.render(lines, sr, t0, n, **kw)
    return LogicCapture.from_edges(e, n, sr, int(round(-t0 * sr)) if trigger is None else trigger)


# ----- UART ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("db,parity,stop,inv,msb", [(8, "none", 1, False, False), (7, "even", 1, False, False), (5, "odd", 2, False, False), (9, "mark", 1.5, False, False), (6, "space", 1, True, False), (8, "none", 1, False, True)])
def test_uart_round_trip(db, parity, stop, inv, msb):
    rng = np.random.default_rng(db)
    data = [int(x) for x in rng.integers(0, 1 << db, 40)]
    tx = S.Line(0 if inv else 1)
    S.uart(tx, 10e-6, data, 115200, data_bits=db, parity=parity, stop_bits=stop, inverted=inv, msb_first=msb, gap=0.3)
    cap = capture({"TX": tx}, 4e6)
    r = D.decode(cap, "uart", {"tx": "TX"}, {"baud": 115200, "data_bits": db, "parity": parity, "stop_bits": stop, "inverted": inv, "bit_order": "msb" if msb else "lsb"})
    assert r.values("tx") == data
    assert r.meta["tx_errors"] == 0


def test_uart_auto_baud_jitter_and_errors():
    data = list(b"Hello, ChipWhisperer! \x00\xff\x55\xaa")
    tx = S.Line(1)
    S.uart(tx, 20e-6, data, 57600, parity="even", bad_parity=[3, 7])
    # a break (line held low) and a byte whose stop bit is low: framing errors
    t = tx.times[-1] + 200e-6
    tx.set(t, 0).set(t + 400e-6, 1)
    cap = capture({"TX": tx}, 2e6, jitter=0.6e-6, seed=1)
    r = D.decode(cap, "uart", {"tx": 0}, {"baud": "auto", "parity": "even"})
    assert r.meta["baud"] == 57600
    vals = r.values("tx", kinds=None)
    assert vals[:len(data)] == data
    kinds = r.rows["tx"]["kind"]
    texts = r.texts("tx")
    assert [i for i, k in enumerate(kinds[:len(data)]) if k == "error"] == [3, 7]
    assert "parity error" in texts[3]
    assert any("break" in t or "framing" in t for t in texts[len(data):])


def test_uart_both_lines_and_ascii_format():
    rx, tx = S.Line(1), S.Line(1)
    S.uart(rx, 20e-6, b"ping\n", 9600)
    S.uart(tx, 6e-3, b"pong\n", 9600)
    cap = capture({"RX": rx, "TX": tx}, 1e6)
    r = D.decode(cap, "uart", {"rx": "RX", "tx": "TX"}, {"format": "ascii"})
    assert bytes(r.values("rx")) == b"ping\n" and bytes(r.values("tx")) == b"pong\n"
    assert r.texts("rx")[:4] == ["p", "i", "n", "g"] and r.texts("rx")[4] == "\\n"
    with pytest.raises(D.DecodeError):
        D.decode(cap, "uart", {}, {})


# ----- SPI ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_spi_modes(mode):
    cpol, cpha = mode >> 1, mode & 1
    lines = {"CS": S.Line(1), "SCK": S.Line(cpol), "MOSI": S.Line(0), "MISO": S.Line(1)}
    tx, rx = [0x9F, 0x00, 0xA5, 0x3C], [0xFF, 0xEF, 0x40, 0x18]
    t = S.spi(lines["CS"], lines["SCK"], lines["MOSI"], lines["MISO"], 5e-6, tx, rx, 1e6, cpol, cpha)
    S.spi(lines["CS"], lines["SCK"], lines["MOSI"], lines["MISO"], t + 3e-6, [0x01, 0x80], [0x7E, 0x81], 1e6, cpol, cpha)
    cap = capture(lines, 20e6)
    r = D.decode(cap, "spi", {"cs": "CS", "sck": "SCK", "mosi": "MOSI", "miso": "MISO"}, {"mode": mode})
    assert r.values("mosi") == tx + [0x01, 0x80]
    assert r.values("miso") == rx + [0x7E, 0x81]
    assert r.meta["transfers"] == 2 and not r.warnings
    # the wrong mode is noticed
    wrong = D.decode(cap, "spi", {"cs": "CS", "sck": "SCK", "mosi": "MOSI"}, {"mode": mode ^ 2})
    assert wrong.warnings


def test_spi_lsb_first_16_bit_words_cs_active_high_and_no_cs():
    lines = {"CS": S.Line(0), "SCK": S.Line(0), "MOSI": S.Line(0)}
    S.spi(lines["CS"], lines["SCK"], lines["MOSI"], None, 2e-6, [0x1234, 0xBEEF], [], 2e6, msb_first=False, word_size=16, cs_active=1)
    cap = capture(lines, 25e6)
    r = D.decode(cap, "spi", {"cs": "CS", "sck": "SCK", "mosi": "MOSI"}, {"bit_order": "lsb", "word_size": 16, "cs_active": "high"})
    assert r.values("mosi") == [0x1234, 0xBEEF]
    r2 = D.decode(cap, "spi", {"sck": "SCK", "mosi": "MOSI"}, {"bit_order": "lsb", "word_size": 16})
    assert r2.values("mosi") == [0x1234, 0xBEEF]


# ----- I2C ----------------------------------------------------------------------------------------------
def test_i2c_write_repeated_start_read_and_nack():
    scl, sda = S.Line(1), S.Line(1)
    ops = [("start",), ("byte", 0xA0, True), ("byte", 0x12, True), ("start",), ("byte", 0xA1, True), ("byte", 0xCA, True), ("byte", 0xFE, False), ("stop",)]
    t = S.i2c(scl, sda, 10e-6, ops, 100e3)
    S.i2c(scl, sda, t + 30e-6, [("start",), ("byte", 0x46, False), ("stop",)], 100e3)
    cap = capture({"SCL": scl, "SDA": sda}, 2e6)
    r = D.decode(cap, "i2c", {"scl": "SCL", "sda": "SDA"}, {})
    texts = r.texts("i2c")
    assert texts == ["S", "Write 0x50", "ACK", "12", "ACK", "Sr", "Read 0x50", "ACK", "CA", "ACK", "FE", "NACK", "P", "S", "Write 0x23", "NACK", "P"]
    assert r.meta["nacks"] == 2 and r.meta["transactions"] == 2
    assert r.values("i2c") == [0x12, 0xCA, 0xFE]
    r8 = D.decode(cap, "i2c", {"scl": "SCL", "sda": "SDA"}, {"address_format": "8-bit"})
    assert "Write 0xA0" in r8.texts("i2c") and "Read 0xA1" in r8.texts("i2c")


def test_i2c_ten_bit_address():
    scl, sda = S.Line(1), S.Line(1)
    a = S.i2c_address(0x2A5, False, ten_bit=True)
    hi_read = S.i2c_address(0x2A5, True, ten_bit=True)[0]
    ops = [("start",), ("byte", a[0], True), ("byte", a[1], True), ("byte", 0x77, True), ("start",), ("byte", hi_read, True), ("byte", 0x99, False), ("stop",)]
    S.i2c(scl, sda, 5e-6, ops, 400e3)
    cap = capture({"SCL": scl, "SDA": sda}, 8e6)
    texts = D.decode(cap, "i2c", {"scl": 0, "sda": 1}, {}).texts("i2c")
    assert "Write 0x2A5" in texts and "Read 0x2A5" in texts and "77" in texts and "99" in texts


# ----- 1-Wire ---------------------------------------------------------------------------------------------
def test_onewire_read_rom_and_skip_rom():
    from cwstudio.sim_interfaces import crc8_maxim
    rom = bytes([0x28, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
    rom += bytes([crc8_maxim(rom)])
    ln = S.Line(1)
    t = S.onewire(ln, 10e-6, [("reset", True), ("write", b"\x33"), ("read", rom)])
    S.onewire(ln, t + 100e-6, [("reset", True), ("write", b"\xcc\x44")])
    t2 = ln.times[-1] + 1e-3
    S.onewire(ln, t2, [("reset", False)])
    cap = capture({"DQ": ln}, 2e6)
    r = D.decode(cap, "onewire", {"owr": "DQ"}, {})
    texts = r.texts("onewire")
    assert texts[:3] == ["Reset", "Presence", "Read ROM (0x33)"]
    assert r.values("onewire")[:8] == list(rom)
    assert "Skip ROM (0xCC)" in texts and "Convert T (0x44)" in texts
    assert texts[-2:] == ["Reset", "No presence"]
    assert r.texts("rom") == ["family 0x28 serial 665544332211 CRC ok"]
    assert r.meta["resets"] == 3 and r.meta["presence"] == 2


def test_onewire_rom_crc_error():
    ln = S.Line(1)
    S.onewire(ln, 10e-6, [("reset", True), ("write", b"\x33"), ("read", bytes([0x28, 1, 2, 3, 4, 5, 6, 0x00]))])
    r = D.decode(capture({"DQ": ln}, 1e6), "onewire", {"owr": 0}, {})
    assert "CRC error" in r.texts("rom")[0] and r.rows["rom"]["kind"][0] == "error"


# ----- JTAG ---------------------------------------------------------------------------------------------
def test_jtag_ir_dr():
    lines = {"TCK": S.Line(0), "TMS": S.Line(0), "TDI": S.Line(0), "TDO": S.Line(0)}
    ops = [("reset",), ("ir", 0xE, 4, 0x1), ("dr", 0, 32, 0x4BA00477), ("ir", 0xA, 4, 0x1), ("dr", 0x123456789, 35, 0x2), ("idle", 3)]
    S.jtag(lines["TCK"], lines["TMS"], lines["TDI"], lines["TDO"], 5e-6, ops, 1e6)
    cap = capture(lines, 10e6)
    r = D.decode(cap, "jtag", {"tck": "TCK", "tms": "TMS", "tdi": "TDI", "tdo": "TDO"}, {})
    irs, drs = r.values("ir", kinds=None), r.values("dr", kinds=None)
    assert [(x["tdi"], x["tdo"], x["bits"]) for x in irs] == [(0xE, 0x1, 4), (0xA, 0x1, 4)]
    assert [(x["tdi"], x["tdo"], x["bits"]) for x in drs] == [(0, 0x4BA00477, 32), (0x123456789, 0x2, 35)]
    assert "IDCODE" in r.texts("ir")[0] and "IDCODE: part 0xBA00" in r.texts("dr")[0]
    states = r.texts("state")
    assert states[0] == "Test-Logic-Reset" and "Shift-IR" in states and "Update-DR" in states and r.meta["final_state"] == "Run-Test/Idle"


# ----- SWD ----------------------------------------------------------------------------------------------
def test_swd_requests_ack_and_data():
    lines = {"SWCLK": S.Line(0), "SWDIO": S.Line(1)}
    ops = [("line_reset",), ("jtag_to_swd",), ("line_reset",), ("idle", 2), ("read", 0, 0x0, "ok", 0x2BA01477), ("idle", 2), ("write", 0, 0x8, "ok", 0x000000F0), ("idle", 1),
           ("read", 1, 0xC, "wait", 0), ("idle", 1), ("write", 1, 0x4, "fault", 0), ("idle", 1), ("read", 1, 0x0, "ok", 0xDEADBEEF), ("idle", 3)]
    S.swd(lines["SWCLK"], lines["SWDIO"], 2e-6, ops, 1e6)
    cap = capture(lines, 8e6)
    r = D.decode(cap, "swd", {"swclk": "SWCLK", "swdio": "SWDIO"}, {})
    t = r.texts("swd")
    assert t[:3] == ["Line reset", "JTAG-to-SWD", "Line reset"]
    assert ["DP read DPIDR", "OK", "0x2BA01477", "DP write SELECT", "OK", "0x000000F0", "AP read 0xC", "WAIT", "AP write 0x4", "FAULT", "AP read 0x0", "OK", "0xDEADBEEF"] == t[3:]
    assert r.meta == {"ok": 3, "wait": 1, "fault": 1, "errors": 0, "resets": 2}


def test_swd_data_parity_error():
    lines = {"SWCLK": S.Line(0), "SWDIO": S.Line(1)}
    S.swd(lines["SWCLK"], lines["SWDIO"], 2e-6, [("idle", 2), ("read", 0, 0x4, "ok", 0x12345678), ("idle", 2)], 1e6)
    # flip the parity bit: the target drives it on cycle 2 + 8 + 1 + 3 + 32 (0-based 46) right after the rising edge
    half = 0.5e-6
    t_par = 2e-6 + 46 * 2 * half + half + 0.2 * half
    k = int(np.searchsorted(lines["SWDIO"].times, t_par - 1e-9))
    lines["SWDIO"].vals[k] ^= 1
    if k + 1 < len(lines["SWDIO"].vals) and lines["SWDIO"].vals[k + 1] == lines["SWDIO"].vals[k]:
        del lines["SWDIO"].times[k + 1], lines["SWDIO"].vals[k + 1]
    r = D.decode(capture(lines, 8e6), "swd", {"swclk": 0, "swdio": 1}, {})
    assert any("parity error" in x for x in r.texts("swd")) and r.meta["errors"] == 1


# ----- CAN ----------------------------------------------------------------------------------------------
def test_can_standard_extended_stuffing_crc_ack():
    can = S.Line(1)
    frames = [(0x123, b"\xde\xad\xbe\xef", False), (0x18DAF110, b"\x00\x00\xff\xff", True), (0x7FF, b"", False), (0x000, b"\x00" * 8, False)]
    t = 20e-6
    for ident, data, ext in frames:
        t = S.can(can, t, 500e3, ident, data, extended=ext) + 10e-6
    t = S.can(can, t, 500e3, 0x321, b"\x01", bad_crc=True) + 10e-6
    t = S.can(can, t, 500e3, 0x055, b"\x02", ack=False) + 10e-6
    S.can(can, t, 500e3, 0x0AB, b"", rtr=True, dlc=2)
    bits = S.can_bits(0x000, b"\x00" * 8)
    assert len(bits) > 1 + 11 + 3 + 4 + 64 + 15 + 10 + 3  # stuff bits were added
    cap = capture({"CAN": can}, 8e6)
    r = D.decode(cap, "can", {"can": "CAN"}, {"bitrate": "auto"})
    assert r.meta["bitrate"] == 500e3
    fr = r.values("frames", kinds=None)
    assert [(f["id"], f["extended"], bytes(f["data"])) for f in fr[:4]] == [(i, e, d) for i, d, e in frames]
    assert all(f["crc_ok"] and f["ack"] for f in fr[:4])
    assert not fr[4]["crc_ok"] and fr[5]["crc_ok"] and not fr[5]["ack"]
    assert fr[6]["rtr"] and fr[6]["dlc"] == 2 and fr[6]["data"] == []
    assert r.meta["frames"] == 7 and r.meta["errors"] == 2
    assert "ext 0x18DAF110 [4] 00 00 FF FF" in r.texts("frames")


def test_can_stuff_error():
    can = S.Line(1)
    bits = S.can_bits(0x000, b"\x00")
    assert bits[:6] == [0, 0, 0, 0, 0, 1]  # SOF and four ID zeros, then the stuff bit
    bits[5] = 0  # six equal bits in a row: a stuff error
    for k, b in enumerate(bits):
        can.set(10e-6 + k * 2e-6, b)
    r = D.decode(capture({"CAN": can}, 4e6), "can", {"can": 0}, {"bitrate": 500e3})
    assert any("stuff error" in t for t in r.texts("fields"))


# ----- SimpleSerial ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["1.1", "2"])
def test_simpleserial_v1_and_v2(version):
    sig, desc = S.demo_signals(baud=230400, ss_version=version)
    cap = capture({k: sig[k] for k in ("UART TX", "UART RX")}, 4e6, t0=-3e-3, n=int(8e-3 * 4e6))
    r = D.decode(cap, "simpleserial", {"rx": "UART RX", "tx": "UART TX"}, {"version": "auto"})
    cmd = r.values("rx", kinds=None)
    resp = r.values("tx", kinds=None)
    assert cmd[0]["cmd"] == "p" and cmd[0]["payload"] == desc["pt"]
    assert resp[0]["cmd"] == "r" and resp[0]["payload"] == desc["ct"]
    assert resp[1]["status"] == 0 and r.rows["tx"]["kind"][1] == "ack"
    assert r.meta["rx_version"] == ("2" if version == "2" else "1")


def test_simpleserial_v2_crc_error():
    frame = bytearray(S.ss2_frame("p", 0, bytes(range(16))))
    frame[5] ^= 0x01
    rx = S.Line(1)
    S.uart(rx, 10e-6, bytes(frame), 115200)
    r = D.decode(capture({"RX": rx}, 2e6), "simpleserial", {"rx": 0}, {})
    assert "CRC error" in r.texts("rx")[0] and r.rows["rx"]["kind"][0] == "error"


def test_demo_traffic_decodes_everything():
    """The simulator's demo traffic exercises every decoder, so each can be demonstrated."""
    sig, desc = S.demo_signals()
    sr, t0 = 4e6, -10e-3
    n = int(25e-3 * sr)
    cap = LogicCapture.from_edges({k: v for k, v in S.render(sig, sr, t0, n).items() if k in S.DEMO_CHANNELS}, n, sr, int(-t0 * sr))
    got = {k: D.decode(cap, k, D.guess_channels(k, cap), {}) for k in D.DECODERS}
    assert bytes(got["uart"].values("tx")) == desc["uart_resp"] and bytes(got["uart"].values("rx")) == desc["uart_cmd"]
    assert got["spi"].values("miso")[1:4] == list(S.DEMO_JEDEC)
    assert "Read 0x50" in got["i2c"].texts("i2c") and "NACK" in got["i2c"].texts("i2c")
    assert "CRC ok" in got["onewire"].texts("rom")[0]
    assert got["jtag"].values("dr", kinds=None)[0]["tdo"] == S.DEMO_IDCODE
    assert f"0x{S.DEMO_DPIDR:08X}" in got["swd"].texts("swd") and got["swd"].meta["wait"] == 1 and got["swd"].meta["fault"] == 1
    assert got["can"].meta["frames"] == 2 and got["can"].meta["errors"] == 0
    assert got["simpleserial"].values("tx", kinds=None)[0]["payload"] == desc["ct"]


def test_result_view_aggregates_dense_rows():
    tx = S.Line(1)
    S.uart(tx, 5e-6, bytes(range(256)) * 4, 1e6)
    cap = capture({"TX": tx}, 16e6)
    r = D.decode(cap, "uart", {"tx": 0}, {"baud": 1e6})
    rows = r.view(0, cap.n, 400)
    items = rows[0]["items"]
    assert len(items) <= 400 // 4 + 2 and any(it[3] == "agg" for it in items)
    assert sum(it[4] if it[3] == "agg" else 1 for it in items) == 1024
    zoomed = r.view(0, cap.n / 200, 800)[0]["items"]
    assert all(it[3] != "agg" for it in zoomed) and zoomed[0][2] == "00"


# ----- model ----------------------------------------------------------------------------------------------
def test_view_matches_dense_samples_and_measure():
    rng = np.random.default_rng(5)
    n = 200000
    bits = (rng.random(n) < 0.002).cumsum() & 1
    clk = (np.arange(n) // 10) & 1
    cap = LogicCapture.from_bits({"rnd": bits.astype(np.uint8), "clk": clk.astype(np.uint8)}, 1e6, trigger=1000)
    assert np.array_equal(cap.channels[0].dense(0, n), bits) and np.array_equal(cap.channels[1].dense(123, 4567), clk[123:4567])
    v = cap.view(1000, 5000, 1000)
    c0 = v["channels"][0]
    assert "edges" in c0 and c0["v0"] == bits[1000]
    assert c0["edges"] == [int(x) for x in np.flatnonzero(np.diff(bits)) + 1 if 1000 < x <= 5000]
    vb = cap.view(0, n, 500)["channels"][1]
    assert "b" in vb and set(vb["b"]) == {"2"}
    m = cap.measure("clk", 0, 100000)
    assert m["frequency"] == pytest.approx(50e3) and m["duty"] == pytest.approx(0.5) and m["high"]["avg"] == pytest.approx(10e-6)
    assert cap.t(1000) == 0 and cap.next_edge("clk", 10, 1, "falling") == 20 and cap.next_edge("clk", 25, -1, "rising") == 10


def test_pattern_search_and_buses():
    a = np.array([0, 0, 1, 1, 0, 1, 1, 1, 0, 0], np.uint8)
    b = np.array([0, 1, 1, 0, 0, 0, 1, 1, 1, 0], np.uint8)
    c = np.array([1, 1, 0, 0, 0, 0, 0, 1, 1, 1], np.uint8)
    cap = LogicCapture.from_bits({"a": a, "b": b, "c": c}, 1.0)
    pat = parse_pattern("1X0", [0, 1, 2])
    assert pat == {0: 1, 2: 0}
    assert cap.find_pattern(pat, -1) == 2 and cap.find_pattern(pat, 2) == 5 and cap.find_pattern(pat, 9, -1) == 5
    assert cap.find_pattern({0: 1}, -1, edge={"channel": "b", "kind": "rising"}) == 6
    assert cap.bus_value([0, 1, 2], 6) == 0b110
    runs = cap.view(0, 10, 100, buses=[{"channels": ["a", "b", "c"]}])["buses"][0]["runs"]
    assert [r[2] for r in runs] == [int(f"{x}{y}{z}", 2) for x, y, z in zip(a, b, c)]
    with pytest.raises(ValueError):
        parse_pattern("10Z", [0, 1, 2])


def test_schmitt_and_auto_level():
    x = np.array([0.0, 0.3, 0.55, 0.45, 0.52, 0.48, 0.2, 0.9, 0.49, 0.51, 0.1])
    assert schmitt(x, 0.5, 0.2).tolist() == [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0]
    assert schmitt(x, 0.5, 0.0).tolist() == [0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0]
    lvl, hyst = auto_level(np.concatenate((np.full(100, -0.3), np.full(100, 0.3))))
    assert abs(lvl) < 1e-9 and hyst == pytest.approx(0.06)


# ----- file formats -----------------------------------------------------------------------------------------
def _same(a, b, names=True):
    assert a.n == b.n and a.trigger == b.trigger and a.samplerate == pytest.approx(b.samplerate, rel=1e-9)
    assert len(a.channels) == len(b.channels)
    for x, y in zip(a.channels, b.channels):
        if names:
            assert x.name.replace(" ", "_") == y.name.replace(" ", "_")
        assert x.init == y.init and np.array_equal(x.edges.astype(np.int64), y.edges.astype(np.int64)), x.name


@pytest.mark.parametrize("fmt", ["vcd", "csv", "sr"])
@pytest.mark.parametrize("sr", [1e6, 29538459.0, 96e6, 1e6 / 3])
def test_format_round_trip(tmp_path, fmt, sr):
    sig, _ = S.demo_signals()
    t0, n = -1e-3, int(4e-3 * sr)
    names = ["UART TX", "UART RX", "TRIG", "CLK", "SPI CS", "SPI SCK", "SPI MOSI", "SPI MISO", "I2C SCL"]
    cap = LogicCapture.from_edges({k: v for k, v in S.render(sig, sr, t0, n).items() if k in names}, n, sr, int(-t0 * sr))
    path = F.save(cap, str(tmp_path / "x"), fmt)
    back = F.load(path)
    _same(cap, back)
    if fmt == "csv":
        sub = F.load(F.save(cap, str(tmp_path / "sub"), fmt, channels=[2, 0]))
        assert [c.name for c in sub.channels] == ["TRIG", "UART TX"]


def test_sr_many_channels_and_chunks(tmp_path):
    rng = np.random.default_rng(1)
    n = 50000
    bits = {f"D{i}": ((rng.random(n) < 0.01).cumsum() & 1).astype(np.uint8) for i in range(13)}
    cap = LogicCapture.from_bits(bits, 24e6)
    p = str(tmp_path / "c.sr")
    F.write_sr(cap, p, chunk=7777)
    with zipfile.ZipFile(p) as z:
        meta = z.read("metadata").decode()
        assert "unitsize=2" in meta and "samplerate=24 MHz" in meta and "probe13=D12" in meta
        assert len([x for x in z.namelist() if x.startswith("logic-1-")]) == 7
    _same(cap, F.read_sr(p))


def test_import_foreign_files(tmp_path):
    # Saleae Logic 2 CSV with a negative (pre-trigger) start
    p = tmp_path / "saleae.csv"
    p.write_text("Time [s],Channel 0,Channel 1\n-0.000002000,1,0\n0.000000000,0,0\n0.000001500,0,1\n0.000004000,1,1\n0.000010000,1,1\n")
    c = F.load(str(p))
    assert [x.name for x in c.channels] == ["Channel 0", "Channel 1"] and c.samplerate == pytest.approx(2e6) and c.trigger == 4
    assert c.channels[0].edges.tolist() == [4, 12] and c.channels[1].edges.tolist() == [7]
    # generic CSV with sample indices, and a headerless dense CSV
    p2 = tmp_path / "idx.csv"
    p2.write_text("sample;a;b\n0;0;1\n5;1;1\n9;1;0\n20;1;0\n")
    c2 = F.load(str(p2), samplerate=1e3)
    assert c2.n == 21 and c2.samplerate == 1e3 and c2.channels[0].edges.tolist() == [5] and c2.channels[1].edges.tolist() == [9]
    p3 = tmp_path / "dense.csv"
    p3.write_text("0,1\n0,1\n1,0\n1,1\n")
    c3 = F.load(str(p3), samplerate=10)
    assert c3.n == 4 and c3.channels[0].edges.tolist() == [2] and c3.channels[1].edges.tolist() == [2, 3]
    # VCD from another tool: 1 ps timescale, a vector, x values, timestamps on a 10 ns grid
    p4 = tmp_path / "other.vcd"
    p4.write_text("$timescale 1ps $end\n$scope module top $end\n$var wire 1 ! clk $end\n$var reg 4 \" bus [3:0] $end\n$upscope $end\n$enddefinitions $end\n#0\n$dumpvars\n0!\nbx \"\n$end\n#10000\n1!\nb1010 \"\n#20000\n0!\n#30000\n1!\nb11 \"\n#40000\n")
    c4 = F.load(str(p4))
    assert c4.samplerate == pytest.approx(1e8) and c4.n == 5
    assert [x.name for x in c4.channels] == ["clk", "bus[3]", "bus[2]", "bus[1]", "bus[0]"]
    assert c4.channels[0].edges.tolist() == [1, 2, 3] and c4.channels[1].edges.tolist() == [1, 3] and c4.channels[4].edges.tolist() == [3]
    # a sigrok session written by hand (as PulseView does), 3 channels in one byte
    p5 = tmp_path / "pv.sr"
    samples = bytes([0b000, 0b001, 0b011, 0b111, 0b110, 0b100])
    with zipfile.ZipFile(p5, "w") as z:
        z.writestr("version", "2")
        z.writestr("metadata", "[global]\nsigrok version=0.5.1\n\n[device 1]\ncapturefile=logic-1\ntotal probes=3\nsamplerate=250 kHz\ntotal analog=0\nprobe1=SCL\nprobe2=SDA\nprobe3=INT\nunitsize=1\n")
        z.writestr("logic-1-1", samples[:4])
        z.writestr("logic-1-2", samples[4:])
    c5 = F.load(str(p5))
    assert c5.samplerate == 250e3 and c5.n == 6 and [x.name for x in c5.channels] == ["SCL", "SDA", "INT"]
    assert c5.channels[0].edges.tolist() == [1, 4] and c5.channels[2].edges.tolist() == [3]
    with pytest.raises(ValueError):
        F.load(str(tmp_path / "saleae.csv"), fmt="xyz")


def test_annotations_csv(tmp_path):
    import csv
    rows = [{"t": 0.001, "t_end": 0.002, "s": 10, "e": 20, "decoder": "UART", "label": "UART TX", "channel_name": "TX", "kind": "data", "text": "41", "value": 65}]
    p = str(tmp_path / "d.csv")
    F.annotations_csv(rows, p)
    got = list(csv.reader(open(p)))
    assert got[0][:4] == ["start_s", "end_s", "start_sample", "end_sample"] and got[1][-2:] == ["41", "65"]
