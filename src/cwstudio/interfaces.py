"""Hardware protocols and interfaces: UART, SimpleSerial, SPI master, GPIO and Husky USERIO, triggers, the Husky bit-banger and 1-Wire, and JTAG/SWD through OpenOCD.

Every action is checked against :func:`cwstudio.capabilities.capabilities` first, so a model that lacks a feature gets :class:`~cwstudio.capabilities.Unsupported` with the reason (HTTP 400), never a half-applied setting. On the simulator each interface talks to a simulated peripheral from :mod:`cwstudio.sim_interfaces`. All hardware access runs on Studio's hardware worker thread.

The protocol code here only configures and drives the ChipWhisperer; decoding captured logic data belongs to a separate decoder library.
"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional

from cwstudio.capabilities import Unsupported, capabilities, require
from cwstudio.openocd import OpenOCD
from cwstudio.web import HTTPException, Request

log = logging.getLogger("cwstudio.interfaces")

EOLS = {"none": b"", "lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}
SS_VERSIONS = {"1.0": "SimpleSerial", "1.1": "SimpleSerial", "2.1": "SimpleSerial2", "cdc": "SimpleSerial2_CDC"}
TRIGGER_KINDS = ("basic", "uart_decode", "uart_pattern", "edge_counter", "adc_level", "sequencer", "sad")


def parse_hex(data: str) -> bytes:
    """Bytes from hex text: spaces, commas, colons and 0x prefixes are allowed."""
    s = re.sub(r"0x", "", data or "", flags=re.I)
    s = re.sub(r"[\s,:;_-]", "", s)
    if len(s) % 2:
        raise ValueError("hex data needs an even number of digits")
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise ValueError(f"not hex: {data!r}") from None


def parse_bits(bits: str) -> List[int]:
    s = re.sub(r"[\s_,]", "", bits or "")
    if not s or set(s) - {"0", "1"}:
        raise ValueError("bits must be a string of 0 and 1")
    return [int(c) for c in s]


def parse_pattern(pattern: Any, allow_dontcare: bool = False) -> List[Any]:
    """A trigger byte pattern from a list, text in quotes ('r'), or hex bytes ('72 XX 0a'); XX is don't care when allowed."""
    if isinstance(pattern, (list, tuple)):
        return list(pattern)
    p = str(pattern or "").strip()
    if not p:
        raise ValueError("empty pattern")
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
        return [ord(c) for c in p[1:-1].encode().decode("unicode_escape")]
    out: List[Any] = []
    for tok in re.split(r"[\s,]+", p):
        if not tok:
            continue
        if tok.upper() == "XX":
            if not allow_dontcare:
                raise ValueError("this trigger has no don't-care bytes")
            out.append("XX")
        else:
            out.append(int(tok, 16))
    return out


class Interfaces:
    """Interface actions for one Studio session."""

    def __init__(self, session):
        self.s = session
        self.ss_version: Optional[str] = None
        self.uart_cfg: Dict[str, Any] = {"baud": None, "parity": "none", "stop_bits": 1}
        self.trigger_state: Dict[str, Any] = {}
        self.spi = None
        self.spi_state: Dict[str, Any] = {"enabled": False, "speed": None, "cs": None}
        self.bb_cfg: Dict[str, Any] = {"data_pin": "USERIO_D0", "clock_pin": "USERIO_CK", "clk_div": None}
        self.gpio_modes: Dict[str, str] = {}  # what Studio last drove on each pin, for scopes whose getters return constants (CW-Nano)
        self._bound_scope = None
        self._bound_target = None
        self.openocd = OpenOCD(session, publish=lambda kind, payload: session.bus.publish(kind, payload))

    # --- helpers -------------------------------------------------------------
    def _sync(self) -> None:
        """Forget per-scope and per-target state when the scope or target object changed (reconnect, another model in "Simulate as", MPSSE): the SPI master, the last trigger, UART settings and the SimpleSerial version belong to the old connection."""
        scope, target = self.s.scope, self.s.target
        if scope is not self._bound_scope:
            self._bound_scope = scope
            self.spi = None
            self.spi_state = {"enabled": False, "speed": None, "cs": None}
            self.trigger_state = {}
            self.uart_cfg = {"baud": None, "parity": "none", "stop_bits": 1}
            self.gpio_modes = {}
        if target is not self._bound_target:
            self._bound_target = target
            self.ss_version = None

    def _scope(self):
        self._sync()
        if self.s.scope is None:
            if self.openocd.mpsse:
                raise Unsupported("the scope is in MPSSE (JTAG/SWD) mode; restore normal mode first")
            raise Unsupported("connect a scope first")
        return self.s.scope

    def _sim(self) -> bool:
        return bool(getattr(self.s.scope, "sim_model", None))

    def _devs(self):
        from cwstudio import sim_interfaces
        return sim_interfaces.attach(self.s.scope)

    def _call(self, fn, *args, timeout: float = 30, **kwargs):
        return self.s.worker.call(fn, *args, timeout=timeout, **kwargs)

    def caps(self) -> Dict[str, Any]:
        if self.s.scope is None:
            return capabilities(None)
        return self._call(capabilities, self.s.scope, self.s.target)

    def _gate(self, *keys: str, what: str = "") -> Dict[str, Any]:
        self._scope()
        c = self.caps()
        if keys:
            node: Any = c
            for k in keys:
                node = node.get(k) if isinstance(node, dict) else None
            require(node, what or " ".join(keys))
        return c

    def status(self) -> Dict[str, Any]:
        """Everything the Interfaces tab shows in one call."""
        self._sync()
        try:
            self.openocd.detect()  # a scope left in MPSSE mode (Studio restarted, another tool) gets Restore normal mode too
        except Exception as e:  # noqa: BLE001
            log.debug("MPSSE detection failed: %s", e)
        out: Dict[str, Any] = {"capabilities": self.caps(), "mpsse": self.openocd.mpsse, "simpleserial": {"version": self.ss_version, "target": self.s.target_kind},
                               "spi": dict(self.spi_state), "trigger": self.trigger_state, "openocd": {k: v for k, v in self.openocd.status().items() if k != "log"}}
        if self.s.scope is not None:
            try:
                out["uart"] = self.uart_get()
            except Exception as e:  # noqa: BLE001
                out["uart"] = {"error": str(e)}
        return out

    # --- UART -------------------------------------------------------------------
    def uart_get(self) -> Dict[str, Any]:
        scope = self._scope()

        def _do():
            pins = {}
            for p in ("tio1", "tio2", "tio3", "tio4"):
                try:
                    v = getattr(scope.io, p)
                    pins[p] = v if isinstance(v, (str, type(None))) else str(v)
                except Exception:  # noqa: BLE001
                    pins[p] = None
            cfg = dict(self.uart_cfg)
            t = self.s.target
            for attr, key in (("baud", "baud"), ("parity", "parity"), ("stop_bits", "stop_bits")):
                try:
                    if t is not None and hasattr(t, attr):
                        cfg[key] = getattr(t, attr)
                except Exception:  # noqa: BLE001
                    pass
            rx = next((p for p, v in pins.items() if v == "serial_rx"), None)
            tx = next((p for p, v in pins.items() if v == "serial_tx"), None)
            if getattr(scope, "_getCWType", lambda: "")() == "cwnano":
                rx, tx = "tio1", "tio2"
            return {**cfg, "data_bits": 8, "rx": rx, "tx": tx, "pins": pins, "target": self.s.target_kind}
        return self._call(_do)

    def uart_configure(self, baud: Optional[int] = None, parity: Optional[str] = None, stop_bits: Optional[float] = None, rx: Optional[str] = None, tx: Optional[str] = None, data_bits: int = 8) -> Dict[str, Any]:
        c = self._gate("uart", what="UART")
        u = c["uart"]
        if data_bits not in (None, 8):
            raise Unsupported("ChipWhisperer's target UART always uses 8 data bits")
        if parity is not None and parity not in u["parity"]:
            raise ValueError(f"parity must be one of {u['parity']}")
        if stop_bits is not None:
            stop_bits = float(stop_bits)
            if stop_bits not in [float(x) for x in u["stop_bits"]]:
                raise ValueError("stop bits must be 1, 1.5 or 2")
            stop_bits = int(stop_bits) if stop_bits.is_integer() else stop_bits
        if baud is not None:
            baud = int(baud)
            lo, hi = u["baud"]
            if not lo <= baud <= hi:
                raise ValueError(f"baud must be {lo} to {hi}")
        if rx is not None and rx not in u["rx_pins"]:
            raise Unsupported(f"{rx} cannot be the serial RX pin on this ChipWhisperer" + (" (the CW-Nano receives on TIO1 only)" if not u["remap"] else " (TIO4 can only transmit)"))
        if tx is not None and tx not in u["tx_pins"]:
            raise Unsupported(f"{tx} cannot be the serial TX pin on this ChipWhisperer" + (" (the CW-Nano transmits on TIO2 only)" if not u["remap"] else ""))
        if rx and tx and rx == tx:
            raise ValueError("RX and TX must be different pins")
        scope = self.s.scope

        def _do():
            if u["remap"] and (rx or tx):
                cur = {p: getattr(scope.io, p, None) for p in u["all_pins"]}
                new_rx = rx or next((p for p, v in cur.items() if v == "serial_rx"), None)
                new_tx = tx or next((p for p, v in cur.items() if v == "serial_tx"), None)
                for p, v in cur.items():
                    if v in ("serial_rx", "serial_tx") and p not in (new_rx, new_tx):
                        setattr(scope.io, p, "high_z")
                if new_rx:
                    setattr(scope.io, new_rx, "serial_rx")
                if new_tx:
                    setattr(scope.io, new_tx, "serial_tx")
            t = self.s.target
            applied_to_target = False
            if t is not None:
                for attr, val in (("parity", parity), ("stop_bits", stop_bits), ("baud", baud)):
                    if val is not None and hasattr(t, attr):
                        try:
                            setattr(t, attr, val)
                            applied_to_target = True
                        except AttributeError:
                            pass
            if not applied_to_target and not self._sim() and hasattr(scope, "_get_usart"):
                cfg = {**self.uart_cfg, **{k: v for k, v in (("baud", baud), ("parity", parity), ("stop_bits", stop_bits)) if v is not None}}
                scope._get_usart().init(baud=int(cfg["baud"] or 38400), stopbits=cfg["stop_bits"], parity=cfg["parity"])
            for k, v in (("baud", baud), ("parity", parity), ("stop_bits", stop_bits)):
                if v is not None:
                    self.uart_cfg[k] = v
        self._call(_do)
        log.info("UART configured: %s baud, parity %s, %s stop bits, RX %s, TX %s", baud or self.uart_cfg.get("baud"), parity or self.uart_cfg.get("parity"), stop_bits or self.uart_cfg.get("stop_bits"), rx or "-", tx or "-")
        res = self.uart_get()
        self.s.bus.publish("setting", {"target": "scope", "path": "io", "value": res["pins"]})
        return res

    # --- SimpleSerial -------------------------------------------------------------
    def simpleserial_connect(self, version: str = "2.1") -> Dict[str, Any]:
        if version not in SS_VERSIONS:
            raise ValueError(f"version must be one of {list(SS_VERSIONS)}")
        c = self._gate("simpleserial", what="SimpleSerial")
        if version == "cdc":
            require(c["simpleserial"]["cdc"], "SimpleSerial over USB-CDC")
        kind = "sim" if self._sim() else SS_VERSIONS[version]
        info = self.s.connect_target(kind)
        if self._sim() and self.s.target is not None:
            self.s.target.protver = "2.1" if version == "cdc" else version
        self._sync()
        self.ss_version = version
        return {**info, "version": version, "kind": kind}

    def simpleserial_send(self, cmd: str = "p", data: str = "", read_cmd: str = "r", read_len: Optional[int] = None, timeout: int = 500) -> Dict[str, Any]:
        """Send one SimpleSerial command and read the reply; v1.0 targets get no ack wait."""
        self._sync()
        if self.s.target is None:
            raise Unsupported("connect a SimpleSerial target first")
        if not cmd or len(cmd) != 1:
            raise ValueError("the command is one character")
        payload = parse_hex(data)
        ack = self.ss_version != "1.0"

        def _do():
            t = self.s.target
            t.simpleserial_write(cmd, bytearray(payload))
            self.s._record_serial("tx", f"{cmd}{payload.hex()}".encode())
            n = read_len if read_len is not None else getattr(t, "output_len", 16)
            resp = None
            try:
                try:
                    resp = t.simpleserial_read(read_cmd, n, timeout=timeout, ack=ack)
                except TypeError:
                    resp = t.simpleserial_read(read_cmd, n, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                log.warning("simpleserial read: %s", e)
            if resp is not None:
                self.s._record_serial("rx", f"{read_cmd}{bytes(resp).hex()}".encode())
            return {"response": bytes(resp).hex() if resp is not None else None, "ack": ack, "version": self.ss_version}
        return self._call(_do, timeout=10 + timeout / 1000)

    # --- SPI ------------------------------------------------------------------------
    def spi_enable(self, speed: float = 1e6, cs: str = "pdid") -> Dict[str, Any]:
        c = self._gate("spi", what="SPI")
        s = c["spi"]
        speed = float(speed)
        if not s["speed"][0] <= speed <= s["speed"][1]:
            raise ValueError(f"speed must be {s['speed'][0]:g} to {s['speed'][1]:g} Hz")
        if cs not in s["pins"]["cs"]:
            raise ValueError(f"chip select must be one of {s['pins']['cs']}")
        scope = self.s.scope

        def _do():
            if self._sim():
                d = self._devs()
                d["spi_enabled"], d["spi_speed"] = True, speed
            else:
                from chipwhisperer.hardware.naeusb.spi import SPI
                if self.spi is not None:
                    try:
                        self.spi.disable()
                    except Exception:  # noqa: BLE001
                        pass
                self.spi = SPI(scope._getNAEUSB(), cs_line=(scope.io, cs), nrst_default="high")
                self.spi.enable(speed)
        self._call(_do)
        self.spi_state = {"enabled": True, "speed": speed, "cs": cs}
        log.info("SPI master enabled at %.0f Hz, chip select on %s (nRST is held high while SPI is on)", speed, cs.upper())
        return dict(self.spi_state)

    def spi_disable(self) -> Dict[str, Any]:
        self._scope()

        def _do():
            if self._sim():
                self._devs()["spi_enabled"] = False
            elif self.spi is not None:
                self.spi.disable()
            self.spi = None
        self._call(_do)
        self.spi_state = {"enabled": False, "speed": None, "cs": self.spi_state.get("cs")}
        return dict(self.spi_state)

    def _spi_ready(self):
        self._gate("spi", what="SPI")
        if not self.spi_state.get("enabled"):
            raise RuntimeError("enable the SPI master first")

    def spi_transfer(self, data: str, start: bool = True, stop: bool = True, writeonly: bool = False) -> Dict[str, Any]:
        """Clock bytes out on MOSI with chip select framing and return what came back on MISO."""
        self._spi_ready()
        out = parse_hex(data)
        if not out:
            raise ValueError("nothing to send")

        def _do():
            if self._sim():
                return self._devs()["flash"].transfer(out, start=start, stop=stop)
            r = self.spi.transfer(list(out), start=start, stop=stop, writeonly=writeonly)
            return list(r) if r is not None else []
        miso = self._call(_do)
        res = {"mosi": out.hex(" "), "miso": bytes(miso).hex(" ") if miso else "", "count": len(out)}
        self.s.bus.publish("spi", dict(res))
        return res

    def spi_toggle_sck(self, cycles: int = 8) -> Dict[str, Any]:
        self._spi_ready()
        cycles = int(cycles)
        if not 1 <= cycles <= 255:
            raise ValueError("cycles must be 1 to 255")
        if not self._sim():
            self._call(self.spi.toggle_sck, cycles)
        return {"toggled": cycles}

    # --- GPIO ------------------------------------------------------------------------
    def gpio_read(self) -> Dict[str, Any]:
        c = self._gate("gpio", what="GPIO")
        g = c["gpio"]
        scope = self.s.scope

        def _do():
            pins: Dict[str, Any] = {}
            levels: Dict[str, Optional[int]] = {}
            if self._sim():
                sp = self._devs()["pins"]
                for p in g["readable"]:
                    levels[p] = sp.level(p)
            elif g["readable"]:
                try:
                    for p, v in zip(("tio1", "tio2", "tio3", "tio4"), scope.io.tio_states):
                        levels[p] = int(v)
                except Exception as e:  # noqa: BLE001
                    log.debug("tio_states: %s", e)
                for p in ("nrst", "pdic", "pdid", "miso", "mosi", "sck"):
                    if p in g["readable"]:
                        try:
                            levels[p] = int(bool(getattr(scope.io, f"{p}_state")))
                        except Exception:  # noqa: BLE001
                            levels[p] = None
            const_getters = c["model"] == "nano" and not self._sim()  # the CW-Nano's pin getters return fixed values, so show what Studio last drove
            for p in g["pins"]:
                try:
                    mode = self.gpio_modes.get(p) if const_getters else getattr(scope.io, p)
                except Exception:  # noqa: BLE001
                    mode = None
                if mode is True:
                    mode = "high"
                elif mode is False:
                    mode = "low"
                pins[p] = {"mode": mode if isinstance(mode, (str, type(None))) else str(mode), "level": levels.get(p), "modes": g["drive"][p]}
            for p in g["readable"]:
                if p not in pins:
                    pins[p] = {"mode": None, "level": levels.get(p), "modes": []}
            return {"pins": pins, "readable": bool(g["readable"]), "read_reason": g["read"]["reason"], "t": time.time()}
        return self._call(_do)

    def gpio_set(self, pin: str, state: str) -> Dict[str, Any]:
        c = self._gate("gpio", what="GPIO")
        drive = c["gpio"]["drive"]
        if pin not in drive:
            raise Unsupported(f"{pin} cannot be driven on this ChipWhisperer (drivable: {', '.join(drive)})")
        aliases = {"high": ("gpio_high", "high"), "low": ("gpio_low", "low"), "high_z": ("high_z", "high_z"), "gpio_high": ("gpio_high", "high"), "gpio_low": ("gpio_low", "low")}
        if state not in aliases:
            raise ValueError("state must be high, low or high_z")
        value = aliases[state][0] if pin.startswith("tio") else aliases[state][1]
        if value not in drive[pin]:
            raise Unsupported(f"{pin} cannot be set to {state} on this ChipWhisperer")
        scope = self.s.scope
        self._call(setattr, scope.io, pin, value)
        self.gpio_modes[pin] = value
        self.s.bus.publish("setting", {"target": "scope", "path": f"io.{pin}", "value": value})
        return self.gpio_read()

    def gpio_pulse(self, pin: str = "nrst", ms: float = 50.0) -> Dict[str, Any]:
        """Pull a pin low for ``ms`` milliseconds, then release it (the usual target reset on nRST)."""
        c = self._gate("gpio", what="GPIO")
        if pin not in c["gpio"]["pulse"]:
            raise Unsupported(f"pulses are offered on {', '.join(c['gpio']['pulse'])} only")
        ms = float(ms)
        if not 1 <= ms <= 5000:
            raise ValueError("pulse length must be 1 to 5000 ms")
        scope = self.s.scope

        def _do():
            setattr(scope.io, pin, "low")
            time.sleep(ms / 1000.0)
            setattr(scope.io, pin, "high_z")
        self._call(_do, timeout=15)
        self.gpio_modes[pin] = "high_z"
        log.info("Pulsed %s low for %g ms", pin.upper(), ms)
        return {"pin": pin, "ms": ms}

    # --- Husky USERIO ------------------------------------------------------------------
    def userio_get(self) -> Dict[str, Any]:
        c = self._gate("userio", what="USERIO")
        width = len(c["userio"]["pins"])
        scope = self.s.scope

        def _do():
            if self._sim():
                sp = self._devs()["pins"]
                return {"mode": sp.userio_mode, "direction": sp.userio_direction, "drive": sp.userio_drive, "status": sp.userio_status()}
            u = scope.userio
            return {"mode": u.mode, "direction": int(u.direction), "drive": int(u.drive_data), "status": int(u.status)}
        r = self._call(_do)
        r.update({"pins": c["userio"]["pins"], "width": width, "modes": c["userio"]["modes"]})
        return r

    def userio_set(self, direction: Optional[int] = None, drive: Optional[int] = None, mode: Optional[str] = None) -> Dict[str, Any]:
        c = self._gate("userio", what="USERIO")
        width = len(c["userio"]["pins"])
        mx = (1 << width) - 1
        for name, v in (("direction", direction), ("drive", drive)):
            if v is not None and not 0 <= int(v) <= mx:
                raise ValueError(f"{name} must be 0 to {mx} ({width} pins)")
        if mode is not None and mode not in ("normal", "fpga_debug"):
            raise Unsupported("set the USERIO mode to normal here; JTAG/SWD routing is done by the OpenOCD section and trace by NewAE's trace notebooks")
        scope = self.s.scope

        def _do():
            if self._sim():
                sp = self._devs()["pins"]
                if mode is not None:
                    sp.userio_mode = mode
                if direction is not None:
                    sp.userio_direction = int(direction)
                if drive is not None:
                    sp.userio_drive = int(drive)
                return
            u = scope.userio
            if mode is not None and u.mode != mode:  # writing the mode also resets trace routing and fails when TraceWhisperer did not start, so only write a change
                u.mode = mode
            if (direction is not None or drive is not None) and u.mode != "normal":
                raise Unsupported("USERIO pins can be driven only in normal mode")
            if drive is not None:
                u.drive_data = int(drive)
            if direction is not None:
                u.direction = int(direction)
        self._call(_do)
        return self.userio_get()

    # --- triggers ------------------------------------------------------------------------
    def trigger_get(self) -> Dict[str, Any]:
        scope = self._scope()

        def _do():
            out: Dict[str, Any] = {}
            for path in ("trigger.module", "trigger.triggers", "adc.basic_mode"):
                obj, _, attr = path.partition(".")
                try:
                    v = getattr(getattr(scope, obj), attr)
                    out[path] = v if isinstance(v, (str, int, float, type(None))) else str(v)
                except Exception:  # noqa: BLE001
                    out[path] = None
            return out
        return {"current": self._call(_do), "applied": self.trigger_state}

    def trigger_configure(self, kind: str = "basic", **p) -> Dict[str, Any]:
        """Configure a trigger. kinds: basic, uart_decode (Pro), uart_pattern (Husky), edge_counter, adc_level, sequencer (Husky), sad (Pro, Husky)."""
        if kind not in TRIGGER_KINDS:
            raise ValueError(f"kind must be one of {TRIGGER_KINDS}")
        c = self._gate(what="triggers")
        t = c["triggers"]
        m = c["model"]
        cap_key = {"basic": "basic", "uart_decode": "uart_decode", "uart_pattern": "uart_pattern", "edge_counter": "edge_counter", "adc_level": "adc_level", "sequencer": "sequencer", "sad": "sad"}[kind]
        require(t[cap_key], kind.replace("_", " "))
        basic = t["basic"]
        pins_ok = basic["pins"]

        def pins_of(v) -> List[str]:
            ps = [v] if isinstance(v, str) else list(v or [])
            ps = [x.strip().lower() for x in ps if x and x.strip()]
            for x in ps:
                if x not in pins_ok:
                    raise Unsupported(f"{x} cannot be a trigger input on this ChipWhisperer" + (" (the CW-Nano triggers on TIO4 only)" if m == "nano" else ""))
            return ps

        def check_pattern(pattern: List[Any], max_value: int = 255) -> List[Any]:
            if not 1 <= len(pattern) <= 8:
                raise ValueError("the pattern is 1 to 8 bytes")
            for b in pattern:
                if b == "XX" and kind == "uart_decode":
                    continue
                if isinstance(b, str) and len(b) == 1:
                    b = ord(b)
                if not isinstance(b, int) or isinstance(b, bool) or not 0 <= b <= max_value:
                    raise ValueError(f"pattern values must be 0 to {max_value} (0x{max_value:x})" + (" or XX" if kind == "uart_decode" else ""))
            return [ord(b) if isinstance(b, str) and len(b) == 1 else b for b in pattern]

        cfg: Dict[str, Any] = {"kind": kind}
        sets: List[tuple] = []  # (path, value) applied in order
        scope = self.s.scope
        husky = m in ("husky", "huskyplus")
        if husky and kind != "sequencer":
            # with the sequencer on, the Husky library treats trigger.triggers and trigger.module as per-step lists, so leave sequencing first
            sets.append(("trigger.sequencer_enabled", False))
        if husky and kind != "uart_pattern" and (self.trigger_state or {}).get("kind") == "uart_pattern":
            sets.append(("UARTTrigger.enabled", False))  # release the TraceWhisperer hardware the UART trigger borrowed
        if kind == "basic":
            pins = pins_of(p["pins"] if p.get("pins") is not None else "tio4")
            op = str(p.get("op") or "OR").upper()
            if op not in ("OR", "AND", "NAND"):
                raise ValueError("op must be OR, AND or NAND")
            if not pins:
                raise ValueError("choose at least one trigger pin")
            if len(pins) > 1:
                require(t["combinations"], "combining trigger pins")
                if len(set(pins)) != len(pins):
                    raise ValueError("each trigger pin can appear once")
            edge = p.get("edge") or "rising_edge"
            if edge not in basic["edges"]:
                raise Unsupported(f"{edge}: " + ("the CW-Nano triggers on a rising edge only" if m == "nano" else "unknown trigger mode"))
            cfg.update(pins=pins, op=op, edge=edge)
            if m != "nano":
                if m != "lite":
                    sets.append(("trigger.module", "basic"))
                sets.append(("trigger.triggers", f" {op} ".join(pins)))
                sets.append(("adc.basic_mode", edge))
        elif kind == "uart_decode":
            pin = pins_of(p.get("pin") or "tio1")[0]
            baud = float(p.get("baud") or 38400)
            lo, hi = t["uart_decode"]["baud"]
            if not lo < baud <= hi:
                raise ValueError(f"baud must be up to {hi:g}")
            pattern = check_pattern(parse_pattern(p.get("pattern") or "'r'", allow_dontcare=True))
            cfg.update(pin=pin, baud=baud, pattern=pattern)
            sets += [("trigger.triggers", pin), ("adc.basic_mode", "rising_edge"), ("trigger.module", "DECODEIO"), ("decode_IO.decode_type", "USART"), ("decode_IO.rx_baud", baud), ("decode_IO.trigger_pattern", pattern)]
        elif kind == "uart_pattern":
            u = t["uart_pattern"]
            pin = pins_of(p.get("pin") or "tio1")[0]
            rule = int(p.get("rule", 0))
            if not 0 <= rule < u["rules"]:
                raise Unsupported(f"this ChipWhisperer has {u['rules']} UART trigger rules (0 to {u['rules'] - 1})")
            data_bits = int(p.get("data_bits", 8))
            stop_bits = int(p.get("stop_bits", 1))
            parity = p.get("parity") or "none"
            if data_bits not in u["data_bits"] or stop_bits not in u["stop_bits"] or parity not in u["parity"]:
                raise ValueError("data bits 5 to 9, stop bits 1 or 2, parity none, odd or even")
            pattern = check_pattern(parse_pattern(p.get("pattern") or "'r'"), (1 << data_bits) - 1)
            baud = int(p.get("baud") or 38400)
            if not 1 <= baud <= 20_000_000:
                raise ValueError("baud must be 1 to 20000000")
            cfg.update(pin=pin, rule=rule, baud=baud, data_bits=data_bits, stop_bits=stop_bits, parity=parity, pattern=pattern)
            sets += [("trigger.triggers", pin), ("trigger.module", "UART"), ("UARTTrigger.enabled", True), ("UARTTrigger.baud", baud), ("UARTTrigger.data_bits", data_bits), ("UARTTrigger.stop_bits", stop_bits), ("UARTTrigger.parity", parity),
                     ("UARTTrigger.set_pattern_match()", (rule, pattern)), ("UARTTrigger.trigger_source", rule)]
        elif kind == "edge_counter":
            pin = pins_of(p.get("pin") or "tio4")[0]
            edges = int(p.get("edges", 1))
            if not 1 <= edges <= 2 ** 16:
                raise ValueError("edges must be 1 to 65536")
            edge = p.get("edge") or "rising_edge"
            if edge not in basic["edges"]:
                raise ValueError(f"edge must be one of {basic['edges']}")
            cfg.update(pin=pin, edges=edges, edge=edge)
            sets += [("trigger.triggers", pin), ("trigger.module", "edge_counter"), ("trigger.edges", edges), ("adc.basic_mode", edge)]
        elif kind == "adc_level":
            level = float(p.get("level", 0.1))
            if not -0.5 <= level <= 0.5:
                raise ValueError("level must be -0.5 to 0.5")
            cfg.update(level=level)
            sets += [("trigger.module", "ADC"), ("trigger.level", level)]
        elif kind == "sequencer":
            if not p.get("enabled", True):
                cfg.update(enabled=False)
                sets += [("trigger.sequencer_enabled", False)]
            else:
                pins = pins_of(p.get("pins") or ["tio4", "tio3"])
                if len(pins) != 2:
                    raise ValueError("the sequencer here combines two trigger pins (first, then second)")
                ws, we = int(p.get("window_start", 0)), int(p.get("window_end", 0))
                for v in (ws, we):
                    if not 0 <= v < 2 ** 16:
                        raise ValueError("windows are 0 to 65535 ADC clock cycles")
                cfg.update(enabled=True, pins=pins, window_start=ws, window_end=we)
                sets += [("trigger.sequencer_enabled", True), ("trigger.num_triggers", 2), ("trigger.module", ["basic", "basic"]), ("trigger.triggers", pins), ("trigger.window_start", ws), ("trigger.window_end", we)]
        elif kind == "sad":
            threshold = int(p.get("threshold", 10))
            start = int(p.get("start", 0))
            if not 1 <= threshold <= (100_000 if m == "pro" else 2 ** 31):
                raise ValueError("threshold must be 1 to 100000 on the Pro" if m == "pro" else "threshold must be at least 1")
            if start < 0:
                raise ValueError("the reference start sample must be 0 or more")
            cfg.update(threshold=threshold, start=start)
            sets += [("trigger.module", "SAD"), ("SAD.reference()", start), ("SAD.threshold", threshold)]

        def _apply():
            sim = self._sim()
            for path, value in sets:
                if sim:
                    if path == "SAD.reference()":  # the simulated SAD trigger compares against this (none yet: it triggers on TIO4 and says so)
                        from cwstudio.simtrigger import SAD_LENGTH
                        n = SAD_LENGTH.get(m, 32)
                        try:
                            ref = self._reference_trace()[value:value + n]
                        except RuntimeError:
                            ref = None
                        if ref is not None and len(ref) < n:
                            raise ValueError(f"the SAD reference needs {n} samples from sample {value}; capture a longer trace or start earlier")
                        scope._sim_sad_ref = ref
                    if path in ("trigger.module", "trigger.triggers", "adc.basic_mode") and not isinstance(value, list):
                        obj, _, attr = path.partition(".")
                        setattr(getattr(scope, obj), attr, value)
                    continue
                if path == "UARTTrigger.set_pattern_match()":
                    scope.UARTTrigger.set_pattern_match(value[0], value[1])
                    continue
                if path == "SAD.reference()":
                    wave = self._reference_trace()
                    n = 128 if m == "pro" else int(getattr(scope.SAD, "sad_reference_length", 128))
                    ref = wave[value:value + n] if m == "pro" else wave[value:]  # the Pro takes exactly 128 samples; the Husky takes what it needs (twice its length when emode is off)
                    if len(ref) < n:
                        raise ValueError(f"the SAD reference needs {n} samples from sample {value}; capture a longer trace or start earlier")
                    scope.SAD.reference = ref
                    continue
                obj = scope
                parts = path.split(".")
                for a in parts[:-1]:
                    obj = getattr(obj, a)
                setattr(obj, parts[-1], value)
            if sim:
                scope._sim_trigger = dict(cfg)
        self._call(_apply)
        self.trigger_state = cfg
        log.info("Trigger configured: %s", ", ".join(f"{k}={v}" for k, v in cfg.items()))
        self.s.bus.publish("setting", {"target": "scope", "path": "trigger", "value": dict(cfg)})
        return {"applied": cfg, **self.trigger_get()}

    def _reference_trace(self):
        import numpy as np
        if len(self.s.store):
            return np.asarray(self.s.store.waves[-1], dtype=float)
        w = self.s.scope.get_last_trace()
        if w is None or not len(w):
            raise RuntimeError("capture a trace first: the SAD reference is cut from the newest trace")
        return np.asarray(w, dtype=float)

    # --- bit-banger and 1-Wire -----------------------------------------------------------
    def _bb_pins(self, c, data_pin: str, clock_pin: str):
        pins = c["bitbanger"]["pins"]
        if data_pin not in pins:
            raise Unsupported(f"{data_pin} cannot be the bit-banger data pin here" + (" (TIO, target_pwr and nRST need a Husky Plus)" if c["model"] == "husky" else ""))
        clock_ok = [p for p in pins if p not in ("target_pwr", "nrst")] + ["disabled"]
        if clock_pin not in clock_ok:
            raise Unsupported(f"{clock_pin} cannot be the bit-banger clock pin here")
        if data_pin == clock_pin:
            raise ValueError("data and clock must be different pins")

    def _default_clk_div(self, slot_us: float = 10.0) -> int:
        try:
            f = float(self.s.scope.clock.adc_freq)
        except Exception:  # noqa: BLE001
            f = 29.5e6
        d = int(round(slot_us * 1e-6 * f / 2.0)) * 2
        return max(2, min(65534, d))

    def bitbang(self, bits: str, record: Optional[str] = None, data_pin: str = "USERIO_D0", clock_pin: str = "USERIO_CK", clk_div: Optional[int] = None) -> Dict[str, Any]:
        """Send a bit pattern on the data pin (with a clock on the clock pin unless it is 'disabled'). ``record`` marks, per bit, which slots to release (high-z) and record; returns the recorded bits."""
        c = self._gate("bitbanger", what="bit-banger")
        self._bb_pins(c, data_pin, clock_pin)
        out = parse_bits(bits)
        rec = parse_bits(record) if record else [0] * len(out)
        if len(rec) != len(out):
            raise ValueError("record must have one 0/1 per bit")
        clk_div = int(clk_div or self._default_clk_div(1.0))
        if clk_div % 2 or not 2 <= clk_div <= 65534:
            raise ValueError("clock divider must be even, 2 to 65534")
        scope = self.s.scope

        def _do():
            if self._sim():
                bb = self._devs()["bitbanger"]
                bb.data_pin, bb.clock_pin, bb.clk_div = data_pin, clock_pin, clk_div
                return bb.send(out, rec, rec)
            bb = scope.bitbanger
            if len(out) > bb.max_length:
                raise ValueError(f"at most {bb.max_length} bits per pattern")
            if sum(rec) > bb.max_record:
                raise ValueError(f"at most {bb.max_record} recorded bits per pattern")
            bb.clock_pin = "disabled"
            bb.data_pin = data_pin
            bb.clock_pin = clock_pin
            bb.clk_div = clk_div
            bb.pattern_data, bb.pattern_hiz, bb.pattern_en, bb.record_en, bb.trig_bits = out, rec, [0] * len(out), rec, [0] * len(out)
            bb.num_bits = len(out)
            bb.trigger_en = False
            bb.go()
            bb.wait_for_done(timeout=2)
            n = sum(rec)
            if not n:
                return []
            word = bb.recorded_data(nbytes=max(1, math.ceil(n / 8)))
            return [(word >> i) & 1 for i in range(n)]
        got = self._call(_do, timeout=15)
        self.bb_cfg = {"data_pin": data_pin, "clock_pin": clock_pin, "clk_div": clk_div}
        return {"sent": "".join(map(str, out)), "recorded": "".join(map(str, got)), "count": len(out), **self.bb_cfg}

    def onewire(self, action: str = "read_rom", data_pin: str = "USERIO_D0", clk_div: Optional[int] = None) -> Dict[str, Any]:
        """1-Wire on the bit-banger: ``reset`` (reset pulse and presence detect) or ``read_rom`` (command 0x33: family code, serial and CRC)."""
        c = self._gate("onewire", what="1-Wire")
        if action not in ("reset", "read_rom"):
            raise ValueError("action must be reset or read_rom")
        self._bb_pins(c, data_pin, "disabled")
        clk_div = int(clk_div or self._default_clk_div(10.0))
        if clk_div % 2 or not 2 <= clk_div <= 65534:
            raise ValueError("clock divider must be even, 2 to 65534")
        scope = self.s.scope

        def _do():
            if self._sim():
                bb = self._devs()["bitbanger"]
                present = bb.onewire_reset()
                rom = bb.onewire_read_rom(0x33) if action == "read_rom" and present else None
                return present, rom
            bb = scope.bitbanger
            ow = bb.onewire
            bb.clock_pin = "disabled"
            bb.data_pin = data_pin
            bb.clk_div = clk_div
            ow.set_defaults()
            try:
                ow.send_rst_pd(trigger_en=False)
                present = True
            except AssertionError:
                present = False
            rom = None
            if action == "read_rom" and present:
                bb.sendpacket(ow._get_read_rom(0x33), trigger_en=False)
                rom = int(bb.recorded_data()).to_bytes(8, "little")
            return present, rom
        present, rom = self._call(_do, timeout=15)
        out: Dict[str, Any] = {"action": action, "presence": bool(present), "data_pin": data_pin, "clk_div": clk_div}
        if rom is not None:
            from cwstudio.sim_interfaces import crc8_maxim
            out.update(rom=rom.hex(" "), family=f"0x{rom[0]:02x}", serial=rom[1:7][::-1].hex(), crc=f"0x{rom[7]:02x}", crc_ok=crc8_maxim(rom[:7]) == rom[7])
        return out


def register_routes(app, session) -> None:
    """Add the /api/interfaces/* routes (called from ``create_app``)."""
    import asyncio

    ifc = getattr(session, "interfaces", None)
    if ifc is None:
        ifc = Interfaces(session)
        session.interfaces = ifc

    async def run(fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("interfaces API error", exc_info=True)
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")

    async def body(req: Request) -> Dict[str, Any]:
        try:
            p = await req.json()
        except Exception:  # noqa: BLE001
            return {}
        if p is None:
            return {}
        if not isinstance(p, dict):
            raise HTTPException(status_code=400, detail="ValueError: the request body must be a JSON object")
        return p

    def flag(v: Any, default: bool) -> bool:
        """A JSON boolean; also accepts the strings true/false, 1/0, yes/no, on/off."""
        if v is None:
            return default
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("true", "1", "yes", "on"):
                return True
            if t in ("false", "0", "no", "off", ""):
                return False
            raise ValueError(f"expected true or false, got {v!r}")
        return bool(v)

    def pick(p: Dict[str, Any], *keys: str) -> Dict[str, Any]:
        return {k: p[k] for k in keys if k in p and p[k] is not None}

    @app.get("/api/interfaces")
    async def interfaces_status():
        """Capabilities plus the state of every interface (UART pins, SimpleSerial version, SPI, trigger, OpenOCD and MPSSE)."""
        return await run(ifc.status)

    @app.get("/api/interfaces/uart")
    async def uart_get():
        return await run(ifc.uart_get)

    @app.put("/api/interfaces/uart")
    async def uart_put(req: Request):
        """Body: baud, parity (none/odd/even/mark/space), stop_bits (1/1.5/2), rx and tx pins (tio1-tio4, only modes valid for the model), data_bits (always 8)."""
        p = await body(req)
        return await run(ifc.uart_configure, **pick(p, "baud", "parity", "stop_bits", "rx", "tx", "data_bits"))

    @app.post("/api/interfaces/simpleserial/connect")
    async def ss_connect(req: Request):
        """Body: version (1.0, 1.1, 2.1 or cdc). Connects the matching SimpleSerial target."""
        p = await body(req)
        return await run(ifc.simpleserial_connect, str(p.get("version", "2.1")))

    @app.post("/api/interfaces/simpleserial/send")
    async def ss_send(req: Request):
        """Body: cmd, data (hex), read_cmd, read_len, timeout (ms). v1.0 does not wait for an ack."""
        p = await body(req)
        return await run(ifc.simpleserial_send, **pick(p, "cmd", "data", "read_cmd", "read_len", "timeout"))

    @app.post("/api/interfaces/spi/enable")
    async def spi_enable(req: Request):
        """Body: speed (Hz), cs (pdid, pdic, tio3, tio4)."""
        p = await body(req)
        return await run(ifc.spi_enable, **pick(p, "speed", "cs"))

    @app.post("/api/interfaces/spi/disable")
    async def spi_disable():
        return await run(ifc.spi_disable)

    @app.post("/api/interfaces/spi/transfer")
    async def spi_transfer(req: Request):
        """Body: data (hex bytes for MOSI), start, stop (chip select framing), writeonly. Returns the MISO bytes."""
        p = await body(req)
        return await run(lambda: ifc.spi_transfer(str(p.get("data", "")), flag(p.get("start"), True), flag(p.get("stop"), True), flag(p.get("writeonly"), False)))

    @app.post("/api/interfaces/spi/toggle_sck")
    async def spi_toggle(req: Request):
        p = await body(req)
        return await run(lambda: ifc.spi_toggle_sck(int(p.get("cycles", 8))))

    @app.get("/api/interfaces/gpio")
    async def gpio_get():
        return await run(ifc.gpio_read)

    @app.put("/api/interfaces/gpio")
    async def gpio_put(req: Request):
        """Body: pin, state (high, low, high_z)."""
        p = await body(req)
        return await run(ifc.gpio_set, str(p.get("pin", "")), str(p.get("state", "")))

    @app.post("/api/interfaces/gpio/pulse")
    async def gpio_pulse(req: Request):
        """Body: pin (nrst or pdic), ms."""
        p = await body(req)
        return await run(lambda: ifc.gpio_pulse(str(p.get("pin", "nrst")), float(p.get("ms", 50))))

    @app.get("/api/interfaces/userio")
    async def userio_get():
        return await run(ifc.userio_get)

    @app.put("/api/interfaces/userio")
    async def userio_put(req: Request):
        """Body: direction (bit mask, 1 = driven by the Husky), drive (bit mask), mode (normal)."""
        p = await body(req)
        return await run(ifc.userio_set, **pick(p, "direction", "drive", "mode"))

    @app.get("/api/interfaces/trigger")
    async def trigger_get():
        return await run(ifc.trigger_get)

    @app.put("/api/interfaces/trigger")
    async def trigger_put(req: Request):
        """Body: kind (basic, uart_decode, uart_pattern, edge_counter, adc_level, sequencer, sad) plus its parameters."""
        p = await body(req)
        kind = str(p.pop("kind", "basic"))
        return await run(ifc.trigger_configure, kind, **p)

    @app.post("/api/interfaces/bitbang")
    async def bitbang(req: Request):
        """Body: bits ('0110...'), record (same length, 1 = release and record that slot), data_pin, clock_pin, clk_div."""
        p = await body(req)
        return await run(ifc.bitbang, str(p.get("bits", "")), p.get("record"), **pick(p, "data_pin", "clock_pin", "clk_div"))

    @app.post("/api/interfaces/onewire")
    async def onewire(req: Request):
        """Body: action (reset, read_rom), data_pin, clk_div."""
        p = await body(req)
        return await run(ifc.onewire, str(p.get("action", "read_rom")), **pick(p, "data_pin", "clk_div"))

    oc = ifc.openocd

    @app.get("/api/interfaces/openocd")
    async def openocd_status():
        """OpenOCD binary, scripts folder, server state, ports, MPSSE state (including a scope found already in MPSSE mode, with detected true) and the newest log lines."""
        def _do():
            try:
                oc.detect()
            except Exception as e:  # noqa: BLE001
                log.debug("MPSSE detection failed: %s", e)
            return oc.status()
        return await run(_do)

    @app.get("/api/interfaces/openocd/targets")
    async def openocd_targets():
        return await run(oc.target_configs)

    @app.get("/api/interfaces/openocd/log")
    async def openocd_log(since: int = 0):
        return oc.log_since(since)

    @app.post("/api/interfaces/openocd/mpsse")
    async def openocd_mpsse(req: Request):
        """Body: enable (bool), transport (jtag, swd), header (target, userio). Enabling releases the scope; disabling restores normal mode and reconnects."""
        p = await body(req)
        try:
            enable = flag(p.get("enable"), True)
            reconnect = flag(p.get("reconnect"), True)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"ValueError: {e}")
        if enable:
            return await run(oc.mpsse_enable, str(p.get("transport", "jtag")), str(p.get("header", "target")))
        return await run(oc.mpsse_disable, reconnect)

    @app.post("/api/interfaces/openocd/start")
    async def openocd_start(req: Request):
        """Body: target_cfg (e.g. target/stm32f3x.cfg), transport, ports {gdb, telnet, tcl}, extra (list of -c commands)."""
        p = await body(req)
        return await run(oc.start, p.get("target_cfg"), p.get("transport"), p.get("ports"), p.get("extra"))

    @app.post("/api/interfaces/openocd/stop")
    async def openocd_stop():
        return await run(oc.stop)

    @app.post("/api/interfaces/openocd/command")
    async def openocd_command(req: Request):
        """Body: command. Runs on OpenOCD's TCL port; returns ok and the output."""
        p = await body(req)
        return await run(lambda: oc.command(str(p.get("command", "")), float(p.get("timeout", 30))))

    @app.post("/api/interfaces/openocd/program")
    async def openocd_program(req: Request):
        """Body: path (.hex/.elf/.bin on the Studio machine), verify, reset, address (for .bin)."""
        p = await body(req)
        return await run(lambda: oc.program(str(p.get("path", "")), flag(p.get("verify"), True), flag(p.get("reset"), True), p.get("address")))
