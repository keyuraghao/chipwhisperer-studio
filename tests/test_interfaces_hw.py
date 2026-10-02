"""Real-hardware code paths of the Interfaces tab without hardware.

* Every chipwhisperer library attribute and method that :mod:`cwstudio.interfaces`, :mod:`cwstudio.openocd` and :mod:`cwstudio.hardware` use on real scopes is checked by reading the library source (develop and the PyPI 6.0.0 copy the bundles ship, when present), so a rename in either version fails here.
* Fake scopes record the exact sequence of settings Studio writes (trigger sequencer, UART trigger, SAD reference, USERIO, CW-Nano pins, SPI master, bit-banger and 1-Wire, MPSSE enable/disable and reconnect).
* OpenOCD runs for real (system ``openocd`` or ``$CWSTUDIO_OPENOCD``) with Studio's interface config and ``noinit``, so the command line, the TCL channel, start/stop/log and quoting are exercised without a probe.
"""
import ast
import os
import shutil
import socket
import subprocess
import sys
import time

import numpy as np
import pytest

from cwstudio import capabilities as capmod
from cwstudio import openocd as ocdmod
from cwstudio.capabilities import Unsupported
from cwstudio.interfaces import Interfaces

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIB_ROOTS = {
    "develop": os.path.join(os.path.dirname(ROOT), "chipwhisperer", "software", "chipwhisperer"),
    "6.0.0": os.path.join(ROOT, "build", "cwstudio-venv", "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages", "chipwhisperer"),
}
LIB_ROOTS = {k: v for k, v in LIB_ROOTS.items() if os.path.isdir(v)}

# (file, class, member, kind): kind "set" = property with a setter, "get" = property, "def" = method
EXTRA = "capture/scopes/cwhardware/ChipWhispererExtra.py"
API = [
    (EXTRA, "TriggerSettings", "triggers", "set"),
    (EXTRA, "ProTrigger", "module", "set"),
    (EXTRA, "HuskyTrigger", "module", "set"),
    (EXTRA, "HuskyTrigger", "sequencer_enabled", "set"),
    (EXTRA, "HuskyTrigger", "num_triggers", "set"),
    (EXTRA, "HuskyTrigger", "window_start", "set"),
    (EXTRA, "HuskyTrigger", "window_end", "set"),
    (EXTRA, "HuskyTrigger", "level", "set"),
    (EXTRA, "HuskyTrigger", "edges", "set"),
    *[(EXTRA, "GPIOSettings", p, "set") for p in ("tio1", "tio2", "tio3", "tio4", "pdic", "pdid", "nrst")],
    *[(EXTRA, "GPIOSettings", p, "get") for p in ("tio_states", "pdic_state", "pdid_state", "nrst_state", "miso_state", "mosi_state", "sck_state")],
    ("capture/scopes/_OpenADCInterface.py", "TriggerSettings", "basic_mode", "set"),
    ("capture/scopes/cwhardware/ChipWhispererDecodeTrigger.py", "ChipWhispererDecodeTrigger", "decode_type", "set"),
    ("capture/scopes/cwhardware/ChipWhispererDecodeTrigger.py", "ChipWhispererDecodeTrigger", "rx_baud", "set"),
    ("capture/scopes/cwhardware/ChipWhispererDecodeTrigger.py", "ChipWhispererDecodeTrigger", "trigger_pattern", "set"),
    ("capture/scopes/cwhardware/ChipWhispererSAD.py", "ChipWhispererSAD", "reference", "set"),
    ("capture/scopes/cwhardware/ChipWhispererSAD.py", "ChipWhispererSAD", "threshold", "set"),
    ("capture/scopes/cwhardware/ChipWhispererSAD.py", "HuskySAD", "reference", "set"),
    ("capture/scopes/cwhardware/ChipWhispererSAD.py", "HuskySAD", "threshold", "set"),
    ("capture/scopes/cwhardware/ChipWhispererSAD.py", "HuskySAD", "sad_reference_length", "get"),
    ("capture/trace/TraceWhisperer.py", "UARTTrigger", "enabled", "set"),
    ("capture/trace/TraceWhisperer.py", "UARTTrigger", "baud", "set"),
    ("capture/trace/TraceWhisperer.py", "UARTTrigger", "trigger_source", "set"),
    ("capture/trace/TraceWhisperer.py", "UARTTrigger", "set_pattern_match", "def"),
    ("capture/trace/TraceWhisperer.py", "TraceWhisperer", "data_bits", "set"),
    ("capture/trace/TraceWhisperer.py", "TraceWhisperer", "stop_bits", "set"),
    ("capture/trace/TraceWhisperer.py", "TraceWhisperer", "parity", "set"),
    ("capture/scopes/cwhardware/ChipWhispererHuskyMisc.py", "USERIOSettings", "mode", "set"),
    ("capture/scopes/cwhardware/ChipWhispererHuskyMisc.py", "USERIOSettings", "direction", "set"),
    ("capture/scopes/cwhardware/ChipWhispererHuskyMisc.py", "USERIOSettings", "drive_data", "set"),
    ("capture/scopes/cwhardware/ChipWhispererHuskyMisc.py", "USERIOSettings", "status", "get"),
    ("hardware/naeusb/spi.py", "SPI", "enable", "def"),
    ("hardware/naeusb/spi.py", "SPI", "disable", "def"),
    ("hardware/naeusb/spi.py", "SPI", "transfer", "def"),
    ("hardware/naeusb/spi.py", "SPI", "toggle_sck", "def"),
    ("hardware/naeusb/serial.py", "USART", "init", "def"),
    ("hardware/naeusb/programmer_targetfpga.py", "LatticeICE40", "program", "def"),
    ("hardware/naeusb/programmer_targetfpga.py", "XilinxGeneric", "program", "def"),
    ("capture/scopes/OpenADC.py", "OpenADC", "enable_MPSSE", "def"),
    ("capture/scopes/OpenADC.py", "OpenADC", "_getNAEUSB", "def"),
    ("capture/scopes/OpenADC.py", "OpenADC", "_get_usart", "def"),
    ("capture/scopes/OpenADC.py", "OpenADC", "dis", "def"),
    ("capture/scopes/cwnano.py", "CWNano", "_get_usart", "def"),
    *[("capture/scopes/cwnano.py", "GPIOSettings", p, "set") for p in ("tio1", "tio2", "tio3", "pdic", "pdid", "nrst")],
    *[("capture/targets/SimpleSerial.py", "SimpleSerial", p, "set") for p in ("baud", "parity", "stop_bits")],
    ("capture/targets/SimpleSerial.py", "SimpleSerial", "simpleserial_read", "def"),
    ("capture/targets/SimpleSerial.py", "SimpleSerial", "simpleserial_write", "def"),
    *[("capture/targets/SimpleSerial2.py", "SimpleSerial2", p, "set") for p in ("baud", "parity", "stop_bits")],
    ("capture/targets/SimpleSerial2.py", "SimpleSerial2", "simpleserial_read", "def"),
]
# The bit-banger and 1-Wire helper exist in develop only (capabilities offers them when scope.bitbanger exists).
BB = "capture/scopes/cwhardware/ChipWhispererHuskyBitBanger.py"
API_DEVELOP = [
    *[(BB, "BitBanger", p, "set") for p in ("data_pin", "clock_pin", "clk_div", "pattern_data", "pattern_hiz", "pattern_en", "record_en", "trig_bits", "num_bits", "trigger_en")],
    *[(BB, "BitBanger", p, "get") for p in ("max_length", "max_record")],
    *[(BB, "BitBanger", p, "def") for p in ("go", "wait_for_done", "recorded_data", "sendpacket")],
    *[(BB, "OneWireHelper", p, "def") for p in ("set_defaults", "send_rst_pd", "_get_read_rom")],
]


def _members(tree: ast.Module, cls: str, seen=None):
    """{name: set of kinds} for a class and its bases defined in the same file."""
    seen = seen or set()
    out = {}
    node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls), None)
    if node is None or cls in seen:
        return out
    seen.add(cls)
    for b in node.bases:
        name = b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)
        if name:
            for k, v in _members(tree, name, seen).items():
                out.setdefault(k, set()).update(v)
    for f in node.body:
        if not isinstance(f, ast.FunctionDef):
            continue
        kinds = out.setdefault(f.name, set())
        kinds.add("def")
        for d in f.decorator_list:
            if isinstance(d, ast.Name) and d.id == "property":
                kinds.add("get")
            if isinstance(d, ast.Attribute) and d.attr == "setter":
                kinds.add("set")
    return out


def _params(tree: ast.Module, cls: str, fn: str):
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    f = next(x for x in node.body if isinstance(x, ast.FunctionDef) and x.name == fn)
    return [a.arg for a in f.args.args + f.args.kwonlyargs]


@pytest.mark.skipif(not LIB_ROOTS, reason="no chipwhisperer library source next to the repository")
@pytest.mark.parametrize("version", sorted(LIB_ROOTS))
def test_library_api_used_by_studio_exists(version):
    root = LIB_ROOTS[version]
    trees = {}
    missing = []
    for path, cls, member, kind in API + (API_DEVELOP if version == "develop" else []):
        if path not in trees:
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as f:
                trees[path] = ast.parse(f.read())
        if kind not in _members(trees[path], cls).get(member, set()):
            missing.append(f"{path}:{cls}.{member} ({kind})")
    assert not missing, f"chipwhisperer {version} lacks: {missing}"
    # signatures Studio relies on
    assert {"enable", "husky_userio"} <= set(_params(trees["capture/scopes/OpenADC.py"], "OpenADC", "enable_MPSSE"))
    assert {"usb", "nrst_default", "cs_line"} <= set(_params(trees["hardware/naeusb/spi.py"], "SPI", "__init__"))
    assert {"data", "start", "stop", "writeonly"} <= set(_params(trees["hardware/naeusb/spi.py"], "SPI", "transfer"))
    assert {"baud", "stopbits", "parity"} <= set(_params(trees["hardware/naeusb/serial.py"], "USART", "init"))
    assert "ack" in _params(trees["capture/targets/SimpleSerial.py"], "SimpleSerial", "simpleserial_read")
    assert "ack" in _params(trees["capture/targets/SimpleSerial2.py"], "SimpleSerial2", "simpleserial_read")
    assert "sck_speed" in _params(trees["hardware/naeusb/programmer_targetfpga.py"], "LatticeICE40", "program")
    assert {"index", "pattern"} <= set(_params(trees["capture/trace/TraceWhisperer.py"], "UARTTrigger", "set_pattern_match"))
    if version == "develop":
        with open(os.path.join(root, BB), encoding="utf-8") as f:
            bb = ast.parse(f.read())
        assert "nbytes" in _params(bb, "BitBanger", "recorded_data") and "trigger_en" in _params(bb, "BitBanger", "sendpacket") and "timeout" in _params(bb, "BitBanger", "wait_for_done")


# ----- fake scopes ----------------------------------------------------------------------------------------
class Node:
    """An attribute bag that records every assignment as (path, value) in a shared log."""

    def __init__(self, log, name, **attrs):
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_name", name)
        for k, v in attrs.items():
            object.__setattr__(self, k, v)

    def __setattr__(self, k, v):
        self._log.append((f"{self._name}.{k}", v))
        object.__setattr__(self, k, v)


class FakeUARTTrigger(Node):
    def set_pattern_match(self, index, pattern, mask=None, enable_rule=True):
        self._log.append(("UARTTrigger.set_pattern_match()", (index, list(pattern))))


class FakeBitBanger(Node):
    """Behaves like develop's BitBanger: recorded bits come back LSB first as one word; released slots read 0xA5's bits; the 1-Wire bus answers with a ROM."""
    ROM = bytes([0x28, 0xFF, 0x4C, 0x1A, 0x62, 0x16, 0x03])

    def __init__(self, log, plus=False):
        super().__init__(log, "bitbanger", max_length=512, max_record=64, data_pin="USERIO_D0", clock_pin="USERIO_CK", clk_div=2, pattern_data=[], pattern_hiz=[], pattern_en=[], record_en=[], trig_bits=[], num_bits=0, trigger_en=False, matched=True, present=True)
        bb = self
        from cwstudio.sim_interfaces import crc8_maxim

        class OW:
            def set_defaults(self):
                log.append(("onewire.set_defaults()", None))

            def send_rst_pd(self, trigger_en=True):
                log.append(("onewire.send_rst_pd()", trigger_en))
                assert bb.present, "no presence detect"

            @staticmethod
            def _get_read_rom(cmd):
                return ("read_rom", cmd)
        object.__setattr__(self, "onewire", OW())
        object.__setattr__(self, "_word", 0)
        object.__setattr__(self, "_rom", int.from_bytes(self.ROM + bytes([crc8_maxim(self.ROM)]), "little"))

    def go(self):
        rec = [i for i, r in enumerate(self.record_en) if r]
        bits = [((0xA5 >> (7 - i % 8)) & 1) if self.pattern_hiz[i] else self.pattern_data[i] for i in rec]
        object.__setattr__(self, "_word", sum(b << n for n, b in enumerate(bits)))
        self._log.append(("bitbanger.go()", len(self.pattern_data)))

    def wait_for_done(self, timeout=1):
        pass

    def sendpacket(self, packet, trigger_en=True, timeout=1):
        self._log.append(("bitbanger.sendpacket()", packet))
        object.__setattr__(self, "_word", self._rom)

    def recorded_data(self, nbytes=8, return_word=True):
        return self._word & ((1 << (8 * nbytes)) - 1)


class FakeScope:
    """Just enough of an OpenADC (Lite, Pro, Husky, Husky Plus) or CW-Nano scope for Interfaces, with every write logged."""

    def __init__(self, model="husky", bitbanger=True, features=None):
        self.log = []
        self.model = model
        self.features = features or {}
        self.sn = "50203120374a3850323037303931303a"
        cw = {"husky": "cwhusky", "huskyplus": "cwhuskyplus", "pro": "cw1200", "lite": "cwlite", "nano": "cwnano"}[model]
        self._cwtype = cw
        nano = model == "nano"
        if nano:  # the CW-Nano's getters return constants (cwnano.py GPIOSettings)
            self.io = Node(self.log, "io", tio1=None, tio2=None, tio3=None, tio4="high_z", pdic=False, pdid=True, nrst=True)
        else:
            self.io = Node(self.log, "io", tio1="serial_rx", tio2="serial_tx", tio3="high_z", tio4="high_z", pdic="high_z", pdid="high_z", nrst="high_z", tio_states=(1, 0, 1, 0), pdic_state=True, pdid_state=False, nrst_state=True, miso_state=False, mosi_state=True, sck_state=False)
        self.trigger = Node(self.log, "trigger", module="basic", triggers="tio4", sequencer_enabled=False)
        self.adc = Node(self.log, "adc", basic_mode="rising_edge")
        self.clock = Node(self.log, "clock", adc_freq=29.538e6)
        self.LA = None
        self.trace = Node(self.log, "trace", present=True) if model in ("husky", "huskyplus") else None
        if model in ("husky", "huskyplus"):
            self.UARTTrigger = FakeUARTTrigger(self.log, "UARTTrigger", enabled=False)
            self.SAD = Node(self.log, "SAD", sad_reference_length=192 if model == "huskyplus" else 128, threshold=0)
            self.userio = Node(self.log, "userio", mode="normal", direction=0, drive_data=0, status=0x0F3)
            if bitbanger:
                self.bitbanger = FakeBitBanger(self.log, plus=model == "huskyplus")
        if model == "pro":
            self.SAD = Node(self.log, "SAD", threshold=0)
            self.decode_IO = Node(self.log, "decode_IO", decode_type="USART", rx_baud=0, trigger_pattern=[])
        self.usart_inits = []
        self.mpsse_calls = []
        self.disconnected = False

    def _getCWType(self):
        return self._cwtype

    def check_feature(self, name, raise_exception=False):
        return self.features.get(name, True)

    def get_last_trace(self):
        return np.linspace(-0.4, 0.4, 1000)

    def _get_usart(self):
        scope = self

        class U:
            def init(self, baud=115200, stopbits=1, parity="none"):
                scope.usart_inits.append((baud, stopbits, parity))
        return U()

    def _getNAEUSB(self):
        return "naeusb"

    def enable_MPSSE(self, enable=True, husky_userio=None, scope_default_setup=True):
        self.mpsse_calls.append((enable, husky_userio))

    def dis(self):
        self.disconnected = True


class FakeWorker:
    def call(self, fn, *args, timeout=None, **kwargs):
        return fn(*args, **kwargs)


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, kind, payload, **kw):
        self.events.append((kind, payload))


class FakeStore:
    def __init__(self, waves=None):
        self.waves = waves or []

    def __len__(self):
        return len(self.waves)


class FakeSession:
    def __init__(self, scope, target=None):
        self.scope = scope
        self.target = target
        self.worker = FakeWorker()
        self.bus = FakeBus()
        self.store = FakeStore()
        self.scope_kind = scope._cwtype if scope is not None else None
        self.target_kind = None
        self.toolchains = None
        self.connects = []
        self.serial = []

    def connect_target(self, kind):
        self.target_kind = kind
        return {"connected": True, "type": kind}

    def connect_scope(self, kind="auto", sn=None, *a, **k):
        self.connects.append((kind, sn))
        self.scope = FakeScope("husky")
        return {"connected": True, "sn": sn}

    def _record_serial(self, d, data):
        self.serial.append((d, data))

    def _push_status(self):
        pass


def _sets(scope, prefix=""):
    return [(p, v) for p, v in scope.log if p.startswith(prefix)]


def test_fake_husky_trigger_sequences():
    sc = FakeScope("husky")
    ifc = Interfaces(FakeSession(sc))
    ifc.trigger_configure("sequencer", pins=["tio4", "tio3"], window_start=5, window_end=100)
    assert sc.log == [("trigger.sequencer_enabled", True), ("trigger.num_triggers", 2), ("trigger.module", ["basic", "basic"]), ("trigger.triggers", ["tio4", "tio3"]), ("trigger.window_start", 5), ("trigger.window_end", 100)]
    sc.log.clear()
    ifc.trigger_configure("basic", pins=["tio1", "tio2"], op="nand", edge="high")
    # the sequencer is turned off first, otherwise the library would treat the trigger string as a per-step list
    assert sc.log == [("trigger.sequencer_enabled", False), ("trigger.module", "basic"), ("trigger.triggers", "tio1 NAND tio2"), ("adc.basic_mode", "high")]
    sc.log.clear()
    ifc.trigger_configure("uart_pattern", pin="tio1", pattern="72 0a", rule=1, baud=115200, data_bits=8, parity="even", stop_bits=2)
    assert sc.log == [("trigger.sequencer_enabled", False), ("trigger.triggers", "tio1"), ("trigger.module", "UART"), ("UARTTrigger.enabled", True), ("UARTTrigger.baud", 115200), ("UARTTrigger.data_bits", 8),
                      ("UARTTrigger.stop_bits", 2), ("UARTTrigger.parity", "even"), ("UARTTrigger.set_pattern_match()", (1, [0x72, 0x0A])), ("UARTTrigger.trigger_source", 1)]
    sc.log.clear()
    ifc.trigger_configure("edge_counter", pin="tio4", edges=7, edge="falling_edge")
    assert sc.log[:2] == [("trigger.sequencer_enabled", False), ("UARTTrigger.enabled", False)]  # leaving the UART trigger releases the TraceWhisperer hardware
    assert ("trigger.edges", 7) in sc.log and ("trigger.module", "edge_counter") in sc.log
    with pytest.raises(ValueError):
        ifc.trigger_configure("edge_counter", edge="sideways")
    with pytest.raises(Unsupported):
        ifc.trigger_configure("uart_pattern", rule=2)  # the Husky has rules 0 and 1
    with pytest.raises(ValueError):
        ifc.trigger_configure("uart_pattern", pattern="1ff")  # 9-bit value with 8 data bits
    ifc.trigger_configure("uart_pattern", pattern="1ff", data_bits=9)
    with pytest.raises(ValueError):
        ifc.trigger_configure("uart_pattern", pattern="01 02 03 04 05 06 07 08 09")
    with pytest.raises(ValueError):
        ifc.trigger_configure("uart_pattern", pattern="72 XX")  # no don't-care bytes on the Husky


def test_fake_sad_reference_per_model():
    sc = FakeScope("husky")
    s = FakeSession(sc)
    s.store = FakeStore([np.linspace(-0.3, 0.3, 600).astype(np.float32)])
    Interfaces(s).trigger_configure("sad", start=100, threshold=20)
    ref = dict(sc.log)["SAD.reference"]
    assert isinstance(ref, np.ndarray) and ref.dtype == np.float64 and len(ref) == 500  # the whole tail: the Husky takes twice its length when emode is off
    assert [p for p, _ in sc.log][-3:] == ["trigger.module", "SAD.reference", "SAD.threshold"]
    pro = FakeScope("pro")
    s = FakeSession(pro)
    s.store = FakeStore([np.linspace(-0.3, 0.3, 600)])
    Interfaces(s).trigger_configure("sad", start=10, threshold=5)
    assert len(dict(pro.log)["SAD.reference"]) == 128
    with pytest.raises(ValueError):
        Interfaces(s).trigger_configure("sad", start=500)  # fewer than 128 samples left
    with pytest.raises(ValueError):
        Interfaces(s).trigger_configure("sad", threshold=0)
    with pytest.raises(ValueError):
        Interfaces(s).trigger_configure("sad", threshold=200000)


def test_fake_pro_decode_io():
    sc = FakeScope("pro")
    ifc = Interfaces(FakeSession(sc))
    ifc.trigger_configure("uart_decode", pin="tio2", baud=9600, pattern="'r'")
    assert sc.log[-1] == ("decode_IO.trigger_pattern", [0x72])
    ifc.trigger_configure("uart_decode", pin="tio2", baud=9600, pattern="72 XX 0a")
    assert sc.log[-6:] == [("trigger.triggers", "tio2"), ("adc.basic_mode", "rising_edge"), ("trigger.module", "DECODEIO"), ("decode_IO.decode_type", "USART"), ("decode_IO.rx_baud", 9600.0), ("decode_IO.trigger_pattern", [0x72, "XX", 0x0A])]
    assert not any(p == "trigger.sequencer_enabled" for p, _ in sc.log)  # the Pro has no sequencer
    with pytest.raises(ValueError):
        ifc.trigger_configure("uart_decode", pattern="100")
    with pytest.raises(ValueError):
        ifc.trigger_configure("uart_decode", pattern="01 02 03 04 05 06 07 08 09")


def test_fake_lite_basic_trigger_leaves_module_alone():
    sc = FakeScope("lite")
    Interfaces(FakeSession(sc)).trigger_configure("basic", pins=["tio3"], edge="low")
    assert sc.log == [("trigger.triggers", "tio3"), ("adc.basic_mode", "low")]  # the Lite's trigger.module has no setter


def test_fake_userio_mode_written_only_on_change():
    sc = FakeScope("husky")
    ifc = Interfaces(FakeSession(sc))
    u = ifc.userio_set(direction=0b11, drive=0b01, mode="normal")
    assert ("userio.mode", "normal") not in sc.log and ("userio.direction", 3) in sc.log and ("userio.drive_data", 1) in sc.log
    assert u["width"] == 8 and u["status"] == 0x0F3  # 6.0.0 and develop without USERIO clocks: D0-D7
    ifc.userio_set(mode="fpga_debug")
    assert sc.log[-1] == ("userio.mode", "fpga_debug")
    with pytest.raises(Unsupported):
        ifc.userio_set(direction=1)  # pins are driven in normal mode only
    with pytest.raises(Unsupported):
        ifc.userio_set(mode="target_debug_swd")


def test_fake_gpio_husky_and_nano():
    sc = FakeScope("husky")
    g = Interfaces(FakeSession(sc)).gpio_read()
    assert [g["pins"][p]["level"] for p in ("tio1", "tio2", "tio3", "tio4")] == [1, 0, 1, 0]
    assert g["pins"]["pdic"]["level"] == 1 and g["pins"]["mosi"]["level"] == 1 and g["pins"]["sck"]["level"] == 0
    lite = FakeScope("lite")
    g = Interfaces(FakeSession(lite)).gpio_read()
    assert g["pins"]["pdic"]["level"] is None and "miso" not in g["pins"]
    nano = FakeScope("nano")
    ifc = Interfaces(FakeSession(nano))
    g = ifc.gpio_read()
    assert not g["readable"] and g["pins"]["nrst"]["mode"] is None and g["pins"]["pdic"]["mode"] is None  # not the getters' constants
    g = ifc.gpio_set("nrst", "low")
    assert nano.log[-1] == ("io.nrst", "low") and g["pins"]["nrst"]["mode"] == "low" and g["pins"]["nrst"]["level"] is None
    g = ifc.gpio_set("tio3", "high")
    assert nano.log[-1] == ("io.tio3", "gpio_high") and g["pins"]["tio3"]["mode"] == "gpio_high"
    ifc.gpio_pulse("nrst", 1)
    assert nano.log[-2:] == [("io.nrst", "low"), ("io.nrst", "high_z")] and ifc.gpio_read()["pins"]["nrst"]["mode"] == "high_z"
    with pytest.raises(Unsupported):
        ifc.gpio_set("tio1", "high")


def test_fake_uart_configure_paths():
    class T:  # a SimpleSerial target whose serial reader supports parity and stop bits
        baud, parity, stop_bits = 38400, "none", 1
    sc = FakeScope("husky")
    s = FakeSession(sc, T())
    ifc = Interfaces(s)
    r = ifc.uart_configure(baud=115200, parity="odd", stop_bits=1.5, rx="tio2", tx="tio1")
    assert (s.target.baud, s.target.parity, s.target.stop_bits) == (115200, "odd", 1.5) and not sc.usart_inits
    assert (r["rx"], r["tx"]) == ("tio2", "tio1") and ("io.tio2", "serial_rx") in sc.log and ("io.tio1", "serial_tx") in sc.log
    s.target = None  # no target: the scope USART is configured directly
    ifc.uart_configure(baud=57600, stop_bits=2)
    assert sc.usart_inits[-1] == (57600, 2, "odd")
    nano = FakeScope("nano")
    ifc = Interfaces(FakeSession(nano))
    r = ifc.uart_configure(baud=9600)
    assert (r["rx"], r["tx"]) == ("tio1", "tio2") and not any(p.startswith("io.") for p, _ in nano.log)


def test_fake_spi_master(monkeypatch):
    import chipwhisperer.hardware.naeusb.spi as spimod
    made = []

    class FakeSPI:
        def __init__(self, usb, timeout=200, nrst_default="high", cs_line=None):
            made.append(self)
            self.args = (usb, nrst_default, cs_line)
            self.calls = []

        def enable(self, speed=400e3, set_cs_high=True):
            self.calls.append(("enable", speed))

        def disable(self, set_cs_highz=True):
            self.calls.append(("disable",))

        def transfer(self, data, start=True, stop=True, writeonly=False):
            self.calls.append(("transfer", list(data), start, stop, writeonly))
            return [0xFF] + [0xEF, 0x40, 0x18][: len(data) - 1]

        def toggle_sck(self, n, mosistate=False):
            self.calls.append(("toggle", n))
    monkeypatch.setattr(spimod, "SPI", FakeSPI)
    sc = FakeScope("lite")
    ifc = Interfaces(FakeSession(sc))
    ifc.spi_enable(speed=4e6, cs="tio3")
    assert made[0].args == ("naeusb", "high", (sc.io, "tio3")) and made[0].calls == [("enable", 4e6)]
    r = ifc.spi_transfer("9f 00 00 00", stop=False)
    assert r["miso"] == "ff ef 40 18" and made[0].calls[-1] == ("transfer", [0x9F, 0, 0, 0], True, False, False)
    ifc.spi_toggle_sck(12)
    assert made[0].calls[-1] == ("toggle", 12)
    ifc.spi_enable(speed=1e6, cs="pdid")  # re-enabling releases the old master first
    assert made[0].calls[-1] == ("disable",) and len(made) == 2
    ifc.spi_disable()
    assert made[1].calls[-1] == ("disable",) and ifc.spi is None
    with pytest.raises(Unsupported):
        Interfaces(FakeSession(FakeScope("lite", features={"TARGET_SPI": False}))).spi_enable()


def test_fake_bitbanger_and_onewire():
    sc = FakeScope("husky")
    ifc = Interfaces(FakeSession(sc))
    r = ifc.bitbang("1010 1100 0000 0000", record="0000 0000 1111 1111", data_pin="USERIO_D2", clock_pin="USERIO_D3", clk_div=30)
    assert r["recorded"] == "10100101"
    paths = [p for p, _ in sc.log]
    assert paths.index("bitbanger.clock_pin") < paths.index("bitbanger.data_pin")  # clock released before data is moved
    assert ("bitbanger.num_bits", 16) in sc.log and ("bitbanger.trigger_en", False) in sc.log and paths[-1] == "bitbanger.go()"
    with pytest.raises(ValueError):
        ifc.bitbang("1" * 80, record="1" * 80)  # more than max_record (64) recorded bits
    with pytest.raises(Unsupported):
        ifc.bitbang("1", data_pin="TIO1")  # Husky Plus only
    ow = ifc.onewire("read_rom", data_pin="USERIO_D1")
    assert ow["presence"] and ow["family"] == "0x28" and ow["crc_ok"] and ow["serial"] == "0316621a4cff"
    assert ("onewire.send_rst_pd()", False) in sc.log and ("bitbanger.clock_pin", "disabled") in sc.log
    sc.bitbanger.present = False
    assert ifc.onewire("read_rom")["presence"] is False
    old = FakeScope("husky", bitbanger=False)  # chipwhisperer 6.0.0 has no scope.bitbanger
    with pytest.raises(Unsupported, match="newer than 6.0.0"):
        Interfaces(FakeSession(old)).bitbang("1")
    hp = FakeScope("huskyplus")
    assert Interfaces(FakeSession(hp)).bitbang("11", data_pin="TIO1", clock_pin="TIO2")["sent"] == "11"


def test_fake_husky_swd_needs_pin_control():
    c = capmod.capabilities(FakeScope("husky", features={"HUSKY_PIN_CONTROL": False}))
    assert c["jtag"]["available"] and not c["swd"]["available"] and "1.4" in c["swd"]["reason"]
    c = capmod.capabilities(FakeScope("husky"))
    assert c["swd"]["available"] and "USERIO 20-pin" in c["swd"]["headers"]
    c = capmod.capabilities(FakeScope("lite", features={"HUSKY_PIN_CONTROL": False}))
    assert c["swd"]["available"]  # the pin control feature is a Husky thing
    c = capmod.capabilities(FakeScope("pro", features={"MPSSE": False}))
    assert not c["jtag"]["available"] and "firmware is too old" in c["jtag"]["reason"]


@pytest.mark.parametrize("model,header", [("husky", "userio"), ("lite", "target")])
def test_fake_mpsse_enable_disable_reconnects(monkeypatch, model, header):
    from cwstudio import hardware
    sc = FakeScope(model)
    s = FakeSession(sc)
    s.scope_kind = model
    ifc = Interfaces(s)
    ifc.status()  # binds the interface state to this scope
    ifc.spi_state = {"enabled": True, "speed": 1e6, "cs": "pdid"}
    info = ifc.openocd.mpsse_enable("swd" if model == "husky" else "jtag", header)
    assert sc.mpsse_calls == [(True, "swd" if header == "userio" else None)] and sc.disconnected
    assert s.scope is None and info["pid"] == ocdmod.PIDS[model] and info["sn"] == sc.sn and info["kind"] == model
    with pytest.raises(Unsupported, match="MPSSE"):
        ifc.gpio_read()
    st = ifc.status()
    assert st["mpsse"]["enabled"] and not st["spi"]["enabled"]  # the SPI master belonged to the released scope
    assert ifc.openocd.mpsse_enable("jtag") is info  # already on: no second switch
    off = FakeScope(model)
    calls = []

    def fake_connect(kind="auto", sn=None, **kw):
        calls.append((kind, sn))
        return off
    monkeypatch.setattr(hardware, "connect_scope", fake_connect)
    monkeypatch.setattr(ocdmod.time, "sleep", lambda t: None)
    res = ifc.openocd.mpsse_disable()
    assert calls == [(model, sc.sn)] and off.mpsse_calls == [(False, None)] and off.disconnected
    assert s.connects == [(model, sc.sn)] and res["scope"]["sn"] == sc.sn and ifc.openocd.mpsse is None
    assert ifc.gpio_read()["pins"]  # the reconnected scope works again


def test_fake_mpsse_reconnect_retries(monkeypatch):
    from cwstudio import hardware
    sc = FakeScope("pro")
    s = FakeSession(sc)
    ifc = Interfaces(s)
    ifc.openocd.mpsse_enable("jtag", "target")
    monkeypatch.setattr(hardware, "connect_scope", lambda kind="auto", sn=None, **kw: FakeScope("pro"))
    monkeypatch.setattr(ocdmod.time, "sleep", lambda t: None)
    tries = []

    def flaky(kind, sn=None):
        tries.append(1)
        if len(tries) < 3:
            raise OSError("device still enumerating")
        return {"connected": True}
    s.connect_scope = flaky
    assert ifc.openocd.mpsse_disable()["scope"]["connected"] and len(tries) == 3
    ifc.openocd.mpsse = {"kind": "pro", "sn": "x"}
    s.connect_scope = lambda kind, sn=None: (_ for _ in ()).throw(OSError("gone"))
    with pytest.raises(RuntimeError, match="reconnecting failed"):
        ifc.openocd.mpsse_disable()


# ----- OpenOCD for real -------------------------------------------------------------------------------------
def _openocd_binary():
    exe = os.environ.get("CWSTUDIO_OPENOCD") or shutil.which("openocd")
    if not exe:
        pytest.skip("no OpenOCD (set CWSTUDIO_OPENOCD or put openocd on PATH)")
    return exe


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scripts_for(exe):
    return ocdmod.OpenOCD(type("S", (), {"toolchains": None})()).scripts_dir(exe)


def test_openocd_server_lifecycle_with_cw_config(tmp_path):
    """Studio's own start/command/stop with the ChipWhisperer interface config; 'noinit' keeps the server up without a probe."""
    exe = _openocd_binary()
    if not _scripts_for(exe):
        pytest.skip("OpenOCD scripts folder not found")

    class S:
        toolchains = None
        scope = None
    events = []
    o = ocdmod.OpenOCD(S(), publish=lambda k, p: events.append((k, p)))
    o.binary = lambda: exe
    o.mpsse = {"pid": 0xACE6, "sn": "50203120374a3850", "transport": "swd", "kind": "huskyplus"}
    ports = {"gdb": _free_port(), "telnet": _free_port(), "tcl": _free_port()}
    with pytest.raises(ValueError):
        o.start("target/stm32f3x.cfg", "swd", {**ports, "tcl": ports["gdb"]})
    with pytest.raises(ValueError):
        o.start("target/stm32f3x.cfg", "swd", ports, extra="noinit")
    with pytest.raises(ValueError):
        o.start("target/stm32f3x.cfg", "uart", ports)
    st = o.start("target/stm32f3x.cfg", None, ports, ["noinit"])
    try:
        assert st["running"] and st["transport"] == "swd"
        for _ in range(100):
            try:
                ocdmod.tcl_query(ports["tcl"], "version", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        assert o.command("transport select") == {"command": "transport select", "ok": True, "output": "swd"}
        assert o.command("adapter name")["output"] == "ftdi"
        assert "stm32f3x.cpu" in o.command("targets")["output"]
        echo = o.command("echo {a {b} c}")
        assert echo["ok"] and echo["output"] == "a {b} c"
        assert o.command("echo \\{")["output"] == "{"  # an unbalanced brace in the output comes back verbatim
        bad = o.command("no_such_cmd")
        assert not bad["ok"] and "no_such_cmd" in bad["output"]
        with pytest.raises(ValueError):
            o.command("echo {")
        lines = [r["line"] for r in o.log_since(0)["lines"]]
        assert lines[0].startswith("$ ") and "ftdi vid_pid 0x2b3e 0xace6" in lines[0] and "> targets" in lines
        assert any("Error" in l or "no_such_cmd" in l for l in lines)
        assert any(k == "openocd" and p.get("kind") == "log" for k, p in events)
        with pytest.raises(RuntimeError, match="already running"):
            o.start("target/stm32f3x.cfg", None, ports, ["noinit"])
    finally:
        st = o.stop()
    assert not st["running"] and st["exit_code"] is not None
    for _ in range(50):
        if any(k == "openocd" and p.get("kind") == "state" and p.get("running") is False for k, p in events):
            break
        time.sleep(0.1)
    assert any(k == "openocd" and p.get("kind") == "state" and p.get("running") is False for k, p in events)
    with pytest.raises(RuntimeError, match="not running"):
        o.command("targets")


def test_openocd_program_quoting(tmp_path):
    """program{} with spaces, $ and [ in the path reaches OpenOCD as one word (checked with the dummy adapter), and the address must be a number."""
    exe = _openocd_binary()
    d = tmp_path / "dir with space $x [y]"
    d.mkdir()
    fw = d / "fw image.hex"
    fw.write_text(":00000001FF\n")
    sent = []

    class S:
        toolchains = None
    o = ocdmod.OpenOCD(S())
    o.command = lambda c, timeout=30: sent.append(c) or {"ok": True, "output": ""}
    o.program(str(fw), verify=True, reset=False)
    assert sent[-1] == "program {" + str(fw) + "} verify"
    o.program(str(fw), verify=False, reset=True, address="0x08000000")
    assert sent[-1].endswith("} reset 0x08000000")
    with pytest.raises(ValueError):
        o.program(str(fw), address="0x0; shutdown")
    with pytest.raises(FileNotFoundError):
        o.program(str(tmp_path / "x.bin"))
    (tmp_path / "x.bin").write_bytes(b"\0")
    with pytest.raises(ValueError, match="address"):
        o.program(str(tmp_path / "x.bin"))
    port = _free_port()
    p = subprocess.Popen([exe, "-c", f"tcl_port {port}", "-c", "telnet_port disabled", "-c", "gdb_port disabled", "-c", "adapter driver dummy", "-c", "transport select jtag"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                ocdmod.tcl_query(port, "version", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        r = ocdmod.parse_wrapped(ocdmod.tcl_query(port, ocdmod.wrap_command(sent[0]), timeout=10))
        # no target behind the dummy adapter, so programming fails at the reset step: the path parsed as one argument
        assert not r["ok"] and "Unable to reset target" in r["output"], r
    finally:
        p.terminate()
        p.wait(5)
