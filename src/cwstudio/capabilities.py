"""What the connected ChipWhisperer can do: protocols, triggers, programmers, debug and logic-analyser sources.

Every entry is ``{"available": bool, "reason": str or None, ...details}``. The UI offers only what is available and shows the reason for the rest. Detection uses the scope model, the firmware feature list (``scope.check_feature``) and the attributes the installed ``chipwhisperer`` library creates, so the answer follows both the hardware and the library version (for example the Husky bit-banger exists only in newer libraries).

The facts behind each rule (with file references into the chipwhisperer library) are in docs/wiki/Protocols-and-Interfaces.md.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

MODELS = {
    "cwnano": "nano", "cwlite": "lite", "cw1200": "pro", "cwhusky": "husky", "cwhuskyplus": "huskyplus",
}
LABELS = {"nano": "ChipWhisperer-Nano", "lite": "ChipWhisperer-Lite", "pro": "ChipWhisperer-Pro", "husky": "ChipWhisperer-Husky", "huskyplus": "ChipWhisperer-Husky Plus"}
SIM_MODELS = ("husky", "huskyplus", "pro", "lite", "nano")
HUSKY = ("husky", "huskyplus")
OPENADC = ("lite", "pro", "husky", "huskyplus")

UART_PIN_MODES = {
    "nano": {"tio1": ["serial_rx"], "tio2": ["serial_tx"]},
    "openadc": {"tio1": ["serial_rx", "serial_tx", "high_z", "gpio_low", "gpio_high"], "tio2": ["serial_tx", "serial_rx", "high_z", "gpio_low", "gpio_high"], "tio3": ["high_z", "serial_rx", "serial_tx", "gpio_low", "gpio_high"], "tio4": ["high_z", "serial_tx", "gpio_low", "gpio_high"]},
}
GPIO_DRIVE = {
    "nano": {"tio3": ["high_z", "gpio_low", "gpio_high"], "pdic": ["high_z", "low", "high"], "pdid": ["high_z", "low", "high"], "nrst": ["high_z", "low", "high"]},
    "openadc": {"tio1": ["high_z", "gpio_low", "gpio_high"], "tio2": ["high_z", "gpio_low", "gpio_high"], "tio3": ["high_z", "gpio_low", "gpio_high"], "tio4": ["high_z", "gpio_low", "gpio_high"], "pdic": ["high_z", "low", "high"], "pdid": ["high_z", "low", "high"], "nrst": ["high_z", "low", "high"]},
}
TRIGGER_MODULES = {"lite": ["basic"], "nano": ["basic"], "pro": ["basic", "SAD", "DECODEIO"], "husky": ["basic", "SAD", "UART", "trace", "ADC", "edge_counter", "bitbanger"]}
LA_GROUPS = {
    "CW 20-pin": ["IO1", "IO2", "IO3", "IO4", "HS1", "HS2", "AUX MCX", "TRIG MCX", "ADC clock"],
    "USERIO 20-pin": ["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "CK"],
    "glitch": ["glitch out", "source clock", "MMCM1", "MMCM2", "glitch go", "capture trigger", "glitch enable", "manual trigger", "MMCM1 trigger"],
}


class Unsupported(RuntimeError):
    """An action the connected hardware (or the simulator's model) cannot do; the message is the reason shown to the user. The HTTP API answers it with status 400."""


def cap(available: bool, reason: Optional[str] = None, **details) -> Dict[str, Any]:
    return {"available": bool(available), "reason": None if available else (reason or "not supported by this hardware"), **details}


def require(entry: Dict[str, Any], what: str = "") -> None:
    """Raise :class:`Unsupported` with the entry's reason when a capability entry is not available."""
    if not entry or not entry.get("available"):
        reason = (entry or {}).get("reason") or "not supported by this hardware"
        raise Unsupported(f"{what}: {reason}" if what else reason)


def model_of(scope) -> Optional[str]:
    if scope is None:
        return None
    sim = getattr(scope, "sim_model", None)
    if sim:
        return sim
    try:
        return MODELS.get(scope._getCWType())
    except Exception:  # noqa: BLE001
        return None


def _feature(scope, name: str) -> Optional[bool]:
    """True/False from the firmware feature list, None if the scope cannot say (simulator, old library)."""
    if getattr(scope, "sim_model", None):
        return True
    try:
        return bool(scope.check_feature(name))
    except Exception:  # noqa: BLE001
        return None


def _has(scope, attr: str) -> bool:
    try:
        return getattr(scope, attr, None) is not None
    except Exception:  # noqa: BLE001
        return False


def capabilities(scope, target=None) -> Dict[str, Any]:
    m = model_of(scope)
    if m is None:
        none = cap(False, "connect a scope first")
        return {"connected": scope is not None, "model": None, "label": None, "uart": none, "simpleserial": none, "spi": none, "jtag": none, "swd": none, "trace": none, "gpio": none, "userio": none, "bitbanger": none, "onewire": none,
                "triggers": {}, "programmers": {}, "logic_analyzer": logic_sources(None)}
    husky, nano, openadc = m in HUSKY, m == "nano", m in OPENADC
    spi_fw = _feature(scope, "TARGET_SPI")
    mpsse_fw = _feature(scope, "MPSSE")
    pin_ctl = _feature(scope, "HUSKY_PIN_CONTROL")
    la = getattr(scope, "LA", None)
    try:
        la_present = bool(la is not None and la.present)
    except Exception:  # noqa: BLE001
        la_present = False
    trace = getattr(scope, "trace", None)
    try:
        trace_present = bool(trace is not None and getattr(trace, "present", False))
    except Exception:  # noqa: BLE001
        trace_present = False

    def fw(flag, what):
        return None if flag in (True, None) else f"the scope firmware is too old for {what}; update it from the Connect tab"

    out: Dict[str, Any] = {"connected": True, "model": m, "label": LABELS.get(m, m), "simulated": bool(getattr(scope, "sim_model", None))}
    upins = UART_PIN_MODES["nano" if nano else "openadc"]
    out["uart"] = cap(True, pins=upins, rx_pins=[k for k, v in upins.items() if "serial_rx" in v], tx_pins=[k for k, v in upins.items() if "serial_tx" in v], all_pins=["tio1", "tio2", "tio3", "tio4"], parity=["none", "odd", "even", "mark", "space"], stop_bits=[1, 1.5, 2], data_bits=[8], baud=[500, 2000000], remap=not nano)
    out["simpleserial"] = cap(True, versions=["1.0", "1.1", "2.1"], cdc=cap(_feature(scope, "CDC") is not False, "this firmware has no USB-CDC serial port"))
    out["spi"] = cap(openadc and spi_fw is not False, "the CW-Nano has no SPI pins" if nano else fw(spi_fw, "the SPI master"),
                     pins={"sck": "SCK", "mosi": "MOSI", "miso": "MISO", "cs": ["pdid", "pdic", "tio3", "tio4"]}, speed=[1e3, 20e6])
    jtag_model = openadc and mpsse_fw is not False
    jtag_ok = jtag_model and not getattr(scope, "sim_model", None)
    jtag_reason = "the CW-Nano's 20-pin header has no TCK/TDI/TDO (SPI) lines" if nano else (fw(mpsse_fw, "JTAG/SWD (MPSSE)") or ("needs real hardware: OpenOCD cannot drive the simulator" if jtag_model else None))
    out["jtag"] = cap(jtag_ok, jtag_reason, headers=["20-pin target"] + (["USERIO 20-pin"] if husky else []), needs="OpenOCD", speed_khz=500, model_supports=jtag_model)
    swd_userio = husky and pin_ctl is not False
    out["swd"] = cap(jtag_ok, jtag_reason, headers=["20-pin target"] + (["USERIO 20-pin"] if swd_userio else []), needs="OpenOCD", speed_khz=500, model_supports=jtag_model)
    out["trace"] = cap(husky and (trace_present or out["simulated"]), "Arm trace needs a ChipWhisperer-Husky" if not husky else "the Husky trace component is not available", modes=["swo", "parallel"], notebooks="https://github.com/newaetech/chipwhisperer-jupyter/tree/main/demos/husky/trace")
    # Every model can drive its pins; only the OpenADC scopes can read them back (the CW-Nano getters return constants).
    drive = GPIO_DRIVE["nano" if nano else "openadc"]
    readable = [] if nano else ["tio1", "tio2", "tio3", "tio4"] + (["nrst", "pdic", "pdid", "miso", "mosi", "sck"] if husky else [])
    out["gpio"] = cap(True, pins=list(drive), drive=drive, readable=readable, read=cap(bool(readable), "the CW-Nano cannot read its pins back (its getters return fixed values)"), pulse=["nrst", "pdic"])
    ck = (not out["simulated"]) and _has(scope, "userio") and hasattr(type(scope.userio), "clocks")
    out["userio"] = cap(husky, "the USERIO header is on the Husky only", pins=["D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7"] + (["CK"] if (ck or out["simulated"]) else []), modes=["normal", "trace", "fpga_debug", "target_debug_jtag", "target_debug_swd"])
    bb = _has(scope, "bitbanger") or (husky and out["simulated"])
    bb_pins = [f"USERIO_D{i}" for i in range(8)] + ["USERIO_CK"] + (["TIO1", "TIO2", "TIO3", "TIO4", "target_pwr", "nrst"] if m == "huskyplus" else [])
    out["bitbanger"] = cap(bb, "the bit-banger needs a Husky and a chipwhisperer library newer than 6.0.0" if husky else "the bit-banger is on the Husky only", pins=bb_pins, clk_div=[2, 65534])
    out["onewire"] = cap(bb, out["bitbanger"]["reason"], pins=bb_pins, read_rom=0x33)

    t = {}
    t["basic"] = cap(True, edges=["rising_edge"] if nano else ["rising_edge", "falling_edge", "high", "low"], pins=["tio4"] if nano else ["tio1", "tio2", "tio3", "tio4", "nrst"] + (["sma"] if m in ("pro",) + HUSKY else []) + ([f"userio_d{i}" for i in range(8)] if husky else []))
    t["combinations"] = cap(not nano, "the CW-Nano triggers on TIO4 only", ops=["OR", "AND", "NAND"])
    t["sad"] = cap(m == "pro" or husky, "SAD triggering needs a ChipWhisperer-Pro or Husky")
    t["uart_decode"] = cap(m == "pro", "the I/O decode trigger is a ChipWhisperer-Pro feature (the Husky has the UART trigger instead)", max_bytes=8, baud=[0, 1e6])
    t["uart_pattern"] = cap(husky and (_has(scope, "UARTTrigger") or out["simulated"]), "the UART pattern trigger needs a ChipWhisperer-Husky" if not husky else "the Husky trace component is not available", rules=8 if m == "huskyplus" else 2, data_bits=[5, 6, 7, 8, 9], parity=["none", "odd", "even"], stop_bits=[1, 2])
    t["edge_counter"] = cap(husky, "edge counting needs a ChipWhisperer-Husky")
    t["adc_level"] = cap(husky, "ADC level triggering needs a ChipWhisperer-Husky")
    t["sequencer"] = cap(husky, "the trigger sequencer needs a ChipWhisperer-Husky")
    t["trace"] = cap(out["trace"]["available"], out["trace"]["reason"])
    t["bitbanger"] = cap(bb, out["bitbanger"]["reason"])
    t["outputs"] = cap(m == "pro" or husky, "a trigger output needs a ChipWhisperer-Pro (AUX) or Husky (MCX)", pins=["aux"] if m == "pro" else ["trig_mcx", "aux_mcx"])
    out["triggers"] = t

    p = {}
    p["STM32F"] = cap(True)
    p["XMEGA"] = cap(not nano, "the CW-Nano programs STM32F targets only")
    p["AVR"] = cap(openadc, "AVR ISP needs the SPI pins, which the CW-Nano does not have")
    p["SAM4S"] = cap(openadc, "SAM-BA programming is not supported on the CW-Nano")
    p["NEORV32"] = cap(openadc and spi_fw is not False, out["spi"]["reason"])
    p["iCE40"] = cap(openadc and spi_fw is not False, out["spi"]["reason"])
    p["XC7A35T"] = cap(openadc and spi_fw is not False, out["spi"]["reason"])
    p["OpenOCD"] = cap(jtag_ok, out["jtag"]["reason"])
    out["programmers"] = p

    out["logic_analyzer"] = logic_sources(scope, m, la_present)
    return out


def logic_sources(scope, m: Optional[str] = None, la_present: bool = False) -> Dict[str, Any]:
    """Logic analyser sources: the Husky's built-in LA, a line on the analog input of any ChipWhisperer, the simulator's demo traffic, external analysers through sigrok-cli (when installed) and files from any analyser."""
    from cwstudio.logic import sigrok
    none = cap(False, "connect a scope first")
    sg = sigrok.status()
    external = cap(sg["available"], sg["reason"], tool="sigrok-cli", path=sg["path"], version=sg["version"], install=sg["install"])
    files = cap(True, formats=["vcd", "csv", "sr"], export=["vcd", "csv", "sr"])
    if m is None:
        return {"native": none, "adc": none, "sim": cap(False, "connect the simulator for its demo traffic"), "external": external, "files": files}
    husky = m in HUSKY
    simulated = bool(getattr(scope, "sim_model", None))
    depth = 65535 if m == "huskyplus" else 16376
    return {
        "native": cap(husky and (la_present or simulated), "the built-in logic analyser is on the ChipWhisperer-Husky; use the analog input or an external analyser" if not husky else "the Husky logic analyser component is not available (TraceWhisperer did not start)",
                      depth=depth, groups=LA_GROUPS, channels_per_capture=9, max_rate_hz=250e6, clk_sources=["usb", "target", "pll"], downsample=[1, 65536], pretrigger=False, with_analog=husky),
        "adc": cap(True, channels=1, note="one digital line wired to the measure input, captured as traces and thresholded", max_segments=2000),
        "sim": cap(simulated, "the simulated logic source needs the simulator connected", channels=18),
        "external": external,
        "files": files,
    }


# ----- gating of the generic scope settings tree -------------------------------------------------
_TIO_ALL = ["serial_rx", "serial_tx", "high_z", "gpio_low", "gpio_high", "gpio_disabled", None]
_MODULE_REASON = {"SAD": "SAD triggering needs a ChipWhisperer-Pro or Husky", "DECODEIO": "the I/O decode trigger is a ChipWhisperer-Pro feature", "UART": "the UART pattern trigger needs a ChipWhisperer-Husky",
                  "trace": "the trace trigger needs a ChipWhisperer-Husky", "ADC": "ADC level triggering needs a ChipWhisperer-Husky", "edge_counter": "edge counting needs a ChipWhisperer-Husky", "bitbanger": "the bit-banger trigger needs a Husky and a chipwhisperer library newer than 6.0.0"}


def setting_gates(scope, caps: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Per-model choices for scope settings whose valid values depend on the hardware: {path: {"choices": [...], "disabled": {value: reason}}}. Unsupported values stay listed but disabled, with the reason."""
    c = caps or capabilities(scope)
    m = c.get("model")
    if not m:
        return {}
    gates: Dict[str, Dict[str, Any]] = {}
    nano = m == "nano"
    for pin in ("tio1", "tio2", "tio3", "tio4"):
        if nano:
            ok = {"tio1": ["serial_rx"], "tio2": ["serial_tx"], "tio3": ["high_z", "gpio_low", "gpio_high", "gpio_disabled", None], "tio4": ["high_z", None]}[pin]
            why = {"tio1": "on the CW-Nano TIO1 is always serial RX", "tio2": "on the CW-Nano TIO2 is always serial TX", "tio3": "the CW-Nano's TIO3 is a GPIO only", "tio4": "on the CW-Nano TIO4 is the trigger input only"}[pin]
        else:
            ok = [x for x in _TIO_ALL if not (pin == "tio4" and x == "serial_rx")]
            why = "TIO4 cannot receive serial data (it can transmit, or be a GPIO or trigger input)"
        gates[f"io.{pin}"] = {"choices": list(_TIO_ALL), "disabled": {_key(x): why for x in _TIO_ALL if x not in ok}}
    mods = TRIGGER_MODULES["husky" if m in HUSKY else m]
    all_mods = ["basic", "SAD", "DECODEIO", "UART", "trace", "ADC", "edge_counter", "bitbanger"]
    dis = {x: _MODULE_REASON.get(x, "not supported by this hardware") for x in all_mods if x not in mods}
    if m in HUSKY:
        for name, key in (("UART", "uart_pattern"), ("trace", "trace"), ("bitbanger", "bitbanger")):
            entry = c["triggers"].get(key) or {}
            if not entry.get("available"):
                dis[name] = entry.get("reason") or _MODULE_REASON[name]
    gates["trigger.module"] = {"choices": all_mods, "disabled": dis}
    pins = ["tio1", "tio2", "tio3", "tio4", "nrst", "sma"] + [f"userio_d{i}" for i in range(8)]
    ok_pins = c["triggers"]["basic"]["pins"]
    gates["trigger.triggers"] = {"choices": pins, "disabled": {p: ("the CW-Nano triggers on TIO4 only" if nano else ("the SMA/AUX trigger input needs a ChipWhisperer-Pro or Husky" if p == "sma" else "USERIO pins are on the Husky only")) for p in pins if p not in ok_pins}}
    if nano:
        modes = ["rising_edge", "falling_edge", "low", "high"]
        gates["adc.basic_mode"] = {"choices": modes, "disabled": {x: "the CW-Nano triggers on a rising edge only" for x in modes if x != "rising_edge"}}
    return gates


def _key(v) -> str:
    """The JSON-side spelling of a choice (matches settings.js valueKey)."""
    if v is None:
        return "__none__"
    if isinstance(v, bool):
        return "__true__" if v else "__false__"
    return str(v)


def gate_settings(nodes, scope) -> list:
    """Apply :func:`setting_gates` to a settings tree from ``settings.describe``: replace the choices and add ``disabled_choices`` ({value key: reason})."""
    gates = setting_gates(scope)
    if not gates:
        return nodes

    def walk(ns):
        for n in ns or []:
            if n.get("kind") == "group":
                walk(n.get("children"))
            elif n.get("path") in gates and n.get("writable"):
                g = gates[n["path"]]
                n["choices"] = list(g["choices"])
                n["disabled_choices"] = dict(g["disabled"])
    walk(nodes)
    return nodes


def check_setting(scope, path: str, value) -> None:
    """Refuse a scope setting value the connected model does not support (raises :class:`Unsupported` with the reason)."""
    gates = setting_gates(scope)
    g = gates.get(path)
    if not g:
        return
    if path == "trigger.triggers" and isinstance(value, str):
        import re
        for tok in re.split(r"\s+", value.strip().lower()):
            if tok and tok not in ("or", "and", "nand") and tok in g["disabled"]:
                raise Unsupported(f"{tok}: {g['disabled'][tok]}")
        return
    k = _key(value if not isinstance(value, str) or value.lower() not in ("none", "null") else None)
    if k in g["disabled"]:
        raise Unsupported(f"{path} = {value}: {g['disabled'][k]}")
