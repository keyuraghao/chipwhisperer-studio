"""The simulator's advanced triggers (UART pattern, Pro UART decode, edge counter, ADC level, SAD, sequencer) are evaluated on what the simulated target does, and the default trigger keeps its exact traces."""
import logging
import tempfile

import numpy as np
import pytest
from starlette.testclient import TestClient

from cwstudio import simtrigger
from cwstudio.app import create_app
from cwstudio.session import Session
from cwstudio.simulator import SimScope, SimTarget

KEY = bytes(range(16))
PT = bytes.fromhex("00112233445566778899aabbccddeeff")


def rig(model="husky", seed=1):
    scope = SimScope(seed=seed, sim_model=model)
    scope.default_setup()
    scope.adc.timeout = 0.05
    target = SimTarget(scope)
    target.con(scope)
    target.set_key(KEY)
    return scope, target


def capture(scope, target, pt=PT):
    scope.arm()
    target.simpleserial_write("p", pt)
    timed_out = scope.capture()
    target.simpleserial_read("r", 16)
    return timed_out


def setup_trigger(scope, **cfg):
    scope._sim_trigger = dict(cfg)
    mod = simtrigger.MODULE_OF.get(cfg["kind"])
    if mod:
        scope.trigger.module = mod


def bit_samples(scope, target):
    return float(scope.clock.adc_freq) / target.baud


def test_default_capture_unchanged():
    """The basic trigger takes the original path: same traces as a scope that never saw the advanced triggers."""
    a, ta = rig(seed=7)
    b, tb = rig(seed=7)
    for _ in range(3):
        assert not capture(a, ta)
        ref = b._synth(PT, KEY)
        np.testing.assert_array_equal(a.get_last_trace(), ref)
    assert not hasattr(a, "_trig_fired")


def test_uart_pattern_fires_on_the_response():
    scope, target = rig()
    setup_trigger(scope, kind="uart_pattern", pin="tio1", baud=38400, pattern=[ord("r")], data_bits=8, parity="none", stop_bits=1, rule=0)
    assert not capture(scope, target)  # the default UART trigger (TIO1, 'r') no longer times out
    leak_start, spacing = scope._leak_pos()
    high = leak_start + 16 * spacing
    spb = bit_samples(scope, target)
    fired = scope._trig_fired
    # the response frame starts when the trigger falls; 'r' is its second byte (after the COBS header) and the trigger fires at the end of its stop bit
    assert fired == pytest.approx(high + 20 * spb, abs=2)
    assert scope.get_last_trace().shape == (scope.adc.samples,)


def test_uart_pattern_on_the_command_line_and_mismatches():
    scope, target = rig()
    spb = bit_samples(scope, target)
    frame = target._frame("p", PT)
    setup_trigger(scope, kind="uart_pattern", pin="tio2", baud=38400, pattern=list(frame[1:3]), data_bits=8, parity="none", stop_bits=1)
    assert not capture(scope, target)
    # the command ends one bit before the trigger rises; its bytes 1..2 end (len - 3) frames + 1 bit before that
    assert scope._trig_fired == pytest.approx(-(len(frame) - 3) * 10 * spb - spb, abs=2)
    setup_trigger(scope, kind="uart_pattern", pin="tio1", baud=115200, pattern=[ord("r")], data_bits=8, parity="none", stop_bits=1)
    assert capture(scope, target)  # wrong baud: nothing decodes as 'r', the capture times out like on the hardware
    setup_trigger(scope, kind="uart_pattern", pin="tio1", baud=38400, pattern=[0x55, 0xAA, 0x55], data_bits=8, parity="none", stop_bits=1)
    assert capture(scope, target)  # a pattern the target never sends


def test_uart_decode_pro_with_wildcards():
    scope, target = rig("pro")
    setup_trigger(scope, kind="uart_decode", pin="tio1", baud=38400, pattern=[ord("r"), "XX"])
    assert not capture(scope, target)
    assert scope._trig_fired > 0


def test_edge_counter_counts_both_edges():
    scope, target = rig()
    leak_start, spacing = scope._leak_pos()
    high = leak_start + 16 * spacing
    setup_trigger(scope, kind="edge_counter", pin="tio4", edges=1, edge="rising_edge")
    assert not capture(scope, target)
    assert scope._trig_fired == 0
    setup_trigger(scope, kind="edge_counter", pin="tio4", edges=2, edge="rising_edge")
    assert not capture(scope, target)
    assert scope._trig_fired == high  # the falling edge of the trigger pin
    setup_trigger(scope, kind="edge_counter", pin="tio4", edges=3, edge="rising_edge")
    assert capture(scope, target)  # one command gives two edges on TIO4
    setup_trigger(scope, kind="edge_counter", pin="tio2", edges=5, edge="rising_edge")
    assert not capture(scope, target)
    assert scope._trig_fired < 0  # the fifth edge of the command on the target's RX line, before the trigger
    setup_trigger(scope, kind="edge_counter", pin="tio3", edges=1, edge="rising_edge")
    assert capture(scope, target)  # nothing drives TIO3


def test_adc_level_fires_where_the_trace_crosses():
    scope, target = rig()
    setup_trigger(scope, kind="adc_level", level=-0.3)
    assert not capture(scope, target)
    t = scope._trig_fired
    leak_start, spacing = scope._leak_pos()
    assert leak_start - 30 <= t <= leak_start + 16 * spacing  # on an S-box bump, not in the idle signal
    wave = scope.get_last_trace()
    assert wave[0] < -0.3  # the recorded trace starts at the crossing (offset 0)
    setup_trigger(scope, kind="adc_level", level=-0.12)
    assert not capture(scope, target)
    assert scope._trig_fired < 0  # the idle clock ripple and noise already reach this level while the command is sent
    setup_trigger(scope, kind="adc_level", level=0.45)
    assert capture(scope, target)  # the signal never gets there


def test_sad_matches_the_reference():
    scope, target = rig()
    assert not capture(scope, target)
    start = scope.leak_start - 16
    ref = scope.get_last_trace()[start:start + 32].astype(float)
    scope._sim_sad_ref = ref
    setup_trigger(scope, kind="sad", threshold=400, start=start)
    assert not capture(scope, target)
    assert scope._trig_fired == pytest.approx(start + 32, abs=3)  # fires when the last reference sample is in
    setup_trigger(scope, kind="sad", threshold=1, start=start)
    assert capture(scope, target)  # noise alone keeps the score above a threshold of 1


def test_sad_without_reference_falls_back(caplog):
    scope, target = rig()
    scope._sim_sad_ref = None
    setup_trigger(scope, kind="sad", threshold=10, start=0)
    with caplog.at_level(logging.WARNING, logger="cwstudio.sim"):
        assert not capture(scope, target)
    assert scope._trig_fired == 0 and "no reference" in caplog.text


def test_sequencer_chains_steps():
    scope, target = rig()
    leak_start, spacing = scope._leak_pos()
    high = leak_start + 16 * spacing
    spb = bit_samples(scope, target)
    setup_trigger(scope, kind="sequencer", enabled=True, pins=["tio4", "tio1"], window_start=0, window_end=0)
    scope.adc.basic_mode = "falling_edge"
    assert not capture(scope, target)
    assert scope._trig_fired == pytest.approx(high, abs=1)  # TIO4 falls, then the response's first start bit
    scope.adc.basic_mode = "rising_edge"
    assert not capture(scope, target)
    assert high < scope._trig_fired < high + 12 * spb
    setup_trigger(scope, kind="sequencer", enabled=True, pins=["tio4", "tio1"], window_start=0, window_end=100)
    assert capture(scope, target)  # the response comes later than 100 samples after the rising edge
    setup_trigger(scope, kind="sequencer", enabled=True, pins=["tio4", "tio3"], window_start=0, window_end=0)
    assert capture(scope, target)


def test_unmodelled_module_falls_back_with_a_log(caplog):
    scope, target = rig()
    scope.trigger.module = "trace"
    with caplog.at_level(logging.WARNING, logger="cwstudio.sim"):
        assert not capture(scope, target)
    assert scope._trig_fired == 0 and "does not model" in caplog.text
    scope.trigger.module = "ADC"  # set from the Scope tab without a level
    scope._sim_trigger = None
    assert not capture(scope, target)


def test_emulated_firmware_triggers():
    """With real firmware in the emulator the lines carry its own frames and trigger window."""
    try:
        from tests import fwbuild
    except ImportError:
        import fwbuild
    try:
        elf = fwbuild.build_elf("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    except RuntimeError as e:
        pytest.skip(f"firmware build not available: {str(e).splitlines()[0]}")
    scope, target = rig()
    assert scope.load_firmware(elf)["emulated"]
    assert not capture(scope, target)
    setup_trigger(scope, kind="edge_counter", pin="tio4", edges=2, edge="rising_edge")
    assert not capture(scope, target)
    assert scope._trig_fired == pytest.approx(scope.adc.trig_count, abs=scope._spc())  # the falling edge ends the firmware's trigger window
    setup_trigger(scope, kind="uart_pattern", pin="tio1", baud=38400, pattern=[ord("r")], data_bits=8, parity="none", stop_bits=1)
    assert not capture(scope, target)
    assert scope._trig_fired > scope.adc.trig_count


def test_uart_decoder_line_level():
    ln = simtrigger.Line(1)
    end = simtrigger.send(ln, 0.0, b"\x72\x00\xff", 10.0)
    assert end == 300.0
    got = simtrigger.uart_decode(ln, 10.0)
    assert [b for b, _ in got] == [0x72, 0x00, 0xFF] and got[0][1] == 100.0
    assert [b for b, _ in simtrigger.uart_decode(ln, 10.0, parity="even")][0] is None  # wrong parity setting: errors
    assert simtrigger.match(got, [0x00, "XX"]) == 300.0


@pytest.fixture(scope="module")
def client():
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def test_api_uart_trigger_captures(client):
    """Through the API: the default UART pattern trigger (TIO1, 'r') captures instead of timing out, and SAD gets its reference from the newest trace."""
    assert client.post("/api/scope/connect", json={"kind": "sim", "sim_model": "husky"}).status_code == 200
    assert client.post("/api/target/connect", json={"kind": "sim"}).status_code == 200
    r = client.put("/api/interfaces/trigger", json={"kind": "uart_pattern", "pin": "tio1", "pattern": "'r'"})
    assert r.status_code == 200, r.text
    st = client.post("/api/capture/start", json={"count": 2, "clear": True}).json()
    assert st.get("ok", True), st
    import time
    for _ in range(200):
        s = client.get("/api/status").json()
        if not s.get("capture", {}).get("running") and s["traces"]["count"] >= 2:
            break
        time.sleep(0.05)
    assert client.get("/api/status").json()["traces"]["count"] == 2
    # SAD in the simulator takes its reference from the newest trace
    r = client.put("/api/interfaces/trigger", json={"kind": "sad", "threshold": 2000, "start": 40})
    assert r.status_code == 200, r.text
    assert len(client.session.scope._sim_sad_ref) == 32
    client.put("/api/interfaces/trigger", json={"kind": "basic", "pins": ["tio4"]})
