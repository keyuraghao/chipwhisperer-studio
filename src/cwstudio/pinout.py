"""Pin diagrams of the ChipWhisperer scopes: the 20-pin target header, the Husky USERIO header and the coax ports, per model.

The tables follow NewAE's hardware documentation (rtfm.newae.com, pages ChipWhisperer-Lite, ChipWhisperer-Pro, ChipWhisperer-Nano and ChipWhisperer-Husky) and the chipwhisperer library's bit-banger docstrings for the Husky Plus. The Quick Info card in the Connect tab, /api/pinout and the MCP tool ``scope_pinout`` show the diagram of the scope Studio has open (the active device), not of the first device on the USB bus.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from cwstudio.capabilities import LABELS, MODELS

# Pin roles, used by the UI to colour the diagram: power, ground, io (target IO), spi (programming/SPI), clock, reset, nc (not connected)
_P, _G, _IO, _SPI, _CLK, _RST, _NC = "power", "ground", "io", "spi", "clock", "reset", "nc"


def _pin(n: int, name: str, role: str, function: str, direction: str = "I/O", alias: Optional[str] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {"pin": n, "name": name, "role": role, "dir": direction, "function": function}
    if alias:
        d["alias"] = alias  # the name used in scope.io (tio1, pdic, nrst, hs2 ...)
    return d


def _target_header(model: str) -> List[Dict[str, Any]]:
    """The 20-pin target header (two rows of ten, odd pins on one row, even on the other). The electrical pinout is the same on every model; what differs is which pins the scope drives."""
    nano, lite, pro, husky, plus = (model == m for m in ("nano", "lite", "pro", "husky", "huskyplus"))
    vusb = "5 V from the power supply" if pro else f"Not connected on the {LABELS[model]}"
    vusb_role = _P if pro else _NC
    if nano:
        pin8 = _pin(8, "VTarget", _P, "Input: set to the target's IO voltage (1.5 V to 5 V)", "I")
    elif lite:
        pin8 = _pin(8, "VTarget", _P, "Input: reference for the FPGA IO voltage (advanced use; 3.3 V normally)", "I")
    else:
        pin8 = _pin(8, "N/C", _NC, f"Not connected on the {LABELS[model]}", "")
    spi_fn = {"MISO": "SPI input (MISO), for SPI and AVR programming", "MOSI": "SPI output (MOSI), for SPI and AVR programming", "SCK": "SPI clock (SCK), for SPI and AVR programming"}
    if nano:
        spi_fn = {k: "Unused on the ChipWhisperer-Nano" for k in spi_fn}
    pdic = "Puts an STM32 target into its bootloader" if nano else "PDI clock (XMEGA programming), SPI chip select, or STM32 bootloader"
    pdid = "User output IO" if nano else "PDI data (XMEGA programming); TMS/SWDIO for JTAG/SWD through OpenOCD"
    tio = {
        1: "UART RX or user output" if nano else "Target IO 1, usually UART TX or RX" + ("; bit-banger data or clock on the Husky Plus" if plus else ""),
        2: "UART TX or user output" if nano else "Target IO 2, usually UART RX or TX" + ("; bit-banger data or clock on the Husky Plus" if plus else ""),
        3: "User output" if nano else "Target IO 3, usually bidirectional IO for smartcards" + ("; bit-banger data or clock on the Husky Plus" if plus else ""),
        4: "Trigger input" if nano else "Target IO 4, usually the trigger input" + ("; bit-banger data or clock on the Husky Plus" if plus else ""),
    }
    hs2 = "High-speed clock output" if nano else "High-speed output, normally the clock or glitch out"
    return [
        _pin(1, "+VUSB (5V)", vusb_role, vusb, "O" if pro else ""),
        _pin(2, "GND", _G, "System ground", "O"),
        _pin(3, "+3.3V", _P, "3.3 V to the target, switchable (scope.io.target_pwr), 200 mA", "O", "target_pwr"),
        _pin(4, "FPGA-HS1", _CLK, "High-speed input, normally the target clock in", "I/O", "hs1"),
        _pin(5, "PROG-RESET", _RST, "Target reset (nRST)", "I/O", "nrst"),
        _pin(6, "FPGA-HS2", _CLK, hs2, "I/O", "hs2"),
        _pin(7, "PROG-MISO", _NC if nano else _SPI, spi_fn["MISO"], "" if nano else "I/O", None if nano else "miso"),
        pin8,
        _pin(9, "PROG-MOSI", _NC if nano else _SPI, spi_fn["MOSI"], "" if nano else "I/O", None if nano else "mosi"),
        _pin(10, "FPGA-TARG1", _IO, tio[1], "I/O", "tio1"),
        _pin(11, "PROG-SCK", _NC if nano else _SPI, spi_fn["SCK"], "" if nano else "I/O", None if nano else "sck"),
        _pin(12, "FPGA-TARG2", _IO, tio[2], "I/O", "tio2"),
        _pin(13, "PROG-PDIC", _SPI, pdic, "I/O", "pdic"),
        _pin(14, "FPGA-TARG3", _IO, tio[3], "I/O", "tio3"),
        _pin(15, "PROG-PDID", _SPI, pdid, "I/O", "pdid"),
        _pin(16, "FPGA-TARG4", _IO, tio[4], "I/O", "tio4"),
        _pin(17, "GND", _G, "Ground", "O"),
        _pin(18, "+3.3V", _P, "3.3 V to the target", "O"),
        _pin(19, "GND", _G, "Ground", "O"),
        _pin(20, "+VUSB (5V)", vusb_role, vusb, "O" if pro else ""),
    ]


def _userio_header() -> List[Dict[str, Any]]:
    """The Husky's USERIO header: data lines on the odd pins, ground on the even ones. The data pins are GPIO by default and can carry Arm trace, FPGA debug, JTAG or SWD, or act as trigger inputs."""
    pins = [_pin(1, "+3.3V", _P, "3.3 V", "O"), _pin(2, "+3.3V", _P, "3.3 V", "O"), _pin(3, "CK", _CLK, "Clock (trace clock in, or GPIO)", "I/O", "userio_ck"), _pin(4, "GND", _G, "Ground", "O")]
    for n, d in zip(range(5, 20, 2), range(7, -1, -1)):
        pins.append(_pin(n, f"D{d}", _IO, f"Data {d}: GPIO, trace, debug or trigger input" + (" (SWO/TDO for Arm trace)" if d == 2 else ""), "I/O", f"userio_d{d}"))
        pins.append(_pin(n + 1, "GND", _G, "Ground", "O"))
    return pins


def _ports(model: str) -> List[Dict[str, Any]]:
    if model == "nano":
        return [{"name": "MEASURE", "kind": "3-pin header (or SMA once the target end is snapped off)", "function": "Input to the low-noise amplifier and ADC: power measurement"},
                {"name": "GLITCH", "kind": "3-pin header (or SMA once the target end is snapped off)", "function": "Voltage glitch (crowbar) output; scope.glitch.repeat > 0 enables it"},
                {"name": "USB", "kind": "Micro-USB", "function": "Host connection and power"}]
    if model == "lite":
        return [{"name": "MEASURE", "kind": "SMA", "function": "Input to the low-noise amplifier and ADC: power measurement"},
                {"name": "GLITCH", "kind": "SMA", "function": "Voltage glitch (crowbar) output through the high- and low-power MOSFETs (scope.io.glitch_hp / glitch_lp)"},
                {"name": "USB", "kind": "Micro-USB", "function": "Host connection and power"}]
    if model == "pro":
        return [{"name": "MEASURE", "kind": "SMA", "function": "Input to the low-noise amplifier and ADC: power measurement"},
                {"name": "GLITCH", "kind": "SMA", "function": "Voltage glitch (crowbar) output through the glitch MOSFETs (scope.io.glitch_hp / glitch_lp)"},
                {"name": "AUX", "kind": "SMA", "function": "Trigger input (scope.trigger.triggers 'aux'), trigger output, glitch or clock output (scope.io.aux_io_mcx)"},
                {"name": "TRIG IN / TRIG OUT", "kind": "SMA", "function": "External trigger input and trigger output"},
                {"name": "USB", "kind": "USB-B", "function": "Host connection; 5 V jack preferred for power"}]
    return [{"name": "MEASURE POS", "kind": "SMA", "function": "Input to the low-noise amplifier and ADC: power measurement"},
            {"name": "GLITCH (CROWBAR)", "kind": "SMA", "function": "Voltage glitch output through the high- and low-power MOSFETs (scope.io.glitch_hp / glitch_lp)"},
            {"name": "TRIG / GLITCH OUT", "kind": "SMB", "function": "Trigger or glitch signal out (scope.io.glitch_trig_mcx)"},
            {"name": "AUX", "kind": "SMB", "function": "Trigger or clock input, or clock output (scope.io.aux_io_mcx)"},
            {"name": "USB", "kind": "USB-C", "function": "Host connection and power"}]


def pinout_for_model(model: Optional[str]) -> Dict[str, Any]:
    """The pin diagram of one scope model: ``connectors`` (each with ``pins``: pin, name, role, dir, function, alias) and ``ports`` (coax and USB). Unknown models get ``{"model": None}``."""
    if model not in LABELS:
        return {"model": None, "label": None, "connectors": [], "ports": [], "notes": []}
    connectors = [{"name": "20-pin target header", "rows": 2, "pins": _target_header(model),
                   "note": "Odd pins on one row, even on the other; pin 1 is marked on the board." + (" Not fitted by default on the Nano: solder it on for an external target." if model == "nano" else "")}]
    notes: List[str] = []
    if model in ("husky", "huskyplus"):
        connectors.append({"name": "USERIO 20-pin header", "rows": 2, "pins": _userio_header(), "note": "Front header: D0 to D7 and CK; modes GPIO, Arm trace, FPGA debug, JTAG or SWD (scope.userio.mode)."})
    if model == "nano":
        notes.append("The Nano has no SPI lines on the header, so SPI, JTAG and SWD through the scope are unavailable; TIO1 and TIO2 are fixed as UART RX and TX.")
    elif model == "huskyplus":
        notes.append("Husky Plus: the bit-banger can use TIO1 to TIO4, target_pwr and nRST on the target header as well as the USERIO pins.")
    if model in ("lite", "pro", "husky", "huskyplus"):
        notes.append("JTAG/SWD through OpenOCD on the target header: SCK = TCK/SWCLK, PDID = TMS/SWDIO, MISO = TDO, MOSI = TDI, PDIC = TRST.")
    return {"model": model, "label": LABELS[model], "connectors": connectors, "ports": _ports(model), "notes": notes}


def pinout(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The pin diagram of the scope Studio has open, from ``info`` (Session.scope_info: the cache filled on the hardware thread, so this never touches USB): ``connected`` False with a message when none, else the model's diagram plus the device's name, serial number and USB location."""
    if not info or not info.get("connected"):
        return {"connected": False, "message": "No ChipWhisperer connected", "model": None, "label": None, "connectors": [], "ports": [], "notes": []}
    model = info.get("sim_model") or MODELS.get(str(info.get("type") or ""))
    out = pinout_for_model(model)
    out["connected"] = True
    out["device"] = {"name": info.get("name"), "sn": info.get("sn"), "simulated": bool(info.get("simulated")), "hw_loc": info.get("hw_loc")}
    if model is None:
        out["message"] = f"No pin diagram for this scope type ({info.get('name') or info.get('type') or 'unknown'})"
    return out
