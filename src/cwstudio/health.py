"""What the connected scope says about itself between captures: temperature and rails (Husky XADC), PLL and DCM lock, external clock drift, sticky capture errors, SAM3U serial overflows, glitches fired, the last trigger length.

The monitors a model has come from its profile (profiles.py). Everything here runs on the hardware thread and never raises: a monitor that cannot be read is simply absent.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from cwstudio.profiles import MODEL_PROFILES


def _get(obj, *path):
    for p in path:
        if obj is None:
            return None
        try:
            obj = getattr(obj, p)
        except Exception:  # noqa: BLE001
            return None
    return obj


def _text(v) -> str:
    if v in (None, 0, False, "", "0", "False"):
        return ""
    return str(v).strip()


def read_health(scope, model: Optional[str]) -> Dict[str, Any]:
    """The monitors of ``model`` read from ``scope`` now, plus ``problems``: the human sentences the UI shows (empty when all is well)."""
    out: Dict[str, Any] = {"ts": time.time(), "model": model, "problems": []}
    if scope is None or not model:
        return out
    monitors = (MODEL_PROFILES.get(model) or {}).get("monitors", [])
    problems: List[str] = out["problems"]
    if "xadc" in monitors:
        x = _get(scope, "XADC")
        if x is not None:
            temp, max_temp = _get(x, "temp"), _get(x, "max_temp")
            rails = {k: _get(x, k) for k in ("vccint", "vccaux", "vccbram")}
            alarms = _text(_get(x, "status"))
            out["xadc"] = {"temp": None if temp is None else round(float(temp), 1), "max_temp": None if max_temp is None else round(float(max_temp), 1), "rails": {k: (None if v is None else round(float(v), 3)) for k, v in rails.items()}, "alarms": alarms}
            if alarms:
                problems.append(f"XADC alarm: {alarms} (the glitch, logic analyser and SAD blocks are powered down until the errors are cleared)")
            elif temp is not None and float(temp) >= 80:
                problems.append(f"FPGA at {float(temp):.0f} C: close to the thermal shutdown, power down the glitch MMCMs or reduce the ADC clock")
    if "pll_lock" in monitors or "dcm_lock" in monitors:
        clk = _get(scope, "clock")
        locked = {}
        for name in ("clkgen_locked", "adc_locked"):
            v = _get(clk, name)
            if v is not None:
                locked[name] = bool(v)
        if locked:
            out["locked"] = locked
            for name, v in locked.items():
                if not v:
                    problems.append(f"{'the PLL' if 'pll_lock' in monitors else 'the DCM'} is not locked ({name}): traces are not synchronous to the target clock; reset the clocks or lower clkgen_freq")
        mm = _get(scope, "glitch", "mmcm_locked")
        if mm is not None and _get(scope, "glitch", "enabled"):
            out["glitch_mmcm_locked"] = bool(mm)
            if not mm:
                problems.append("the glitch MMCMs are not locked: the glitch output is undefined (check glitch.clk_src and the clock frequency)")
    if "extclk" in monitors:
        clk = _get(scope, "clock")
        err = _get(clk, "extclk_error")
        fc = _get(clk, "freq_ctr")
        if fc is not None:
            out["freq_ctr"] = float(fc)
        if err is not None:
            out["extclk_error"] = bool(err)
            if err:
                problems.append("the external clock frequency changed since the PLL was set (clock.extclk_error): set clock.clkgen_src = extclk again")
    if "adc_errors" in monitors:
        e = _text(_get(scope, "adc", "errors"))
        out["adc_errors"] = e
        if e:
            problems.append(f"capture error: {e}")
    if "fifo_overflow" in monitors:
        # the Lite and Pro expose no error register through the library; the status word's overflow bit surfaces as a library warning, so nothing to read here
        pass
    if "sam_errors" in monitors:
        e = _text(_get(scope, "errors", "sam_errors"))
        out["sam_errors"] = e
        if e:
            problems.append(f"USB controller: {e} (the target UART buffers overflowed; read faster or lower the baud rate)")
    if "la_errors" in monitors:
        la = _get(scope, "LA")
        if la is not None and _get(la, "enabled"):
            e = _text(_get(la, "errors"))
            if e:
                out["la_errors"] = e
                problems.append(f"logic analyser: {e}")
    if "trace_errors" in monitors:
        tr = _get(scope, "trace")
        if tr is not None and _get(tr, "enabled"):
            e = _text(_get(tr, "errors"))
            if e:
                out["trace_errors"] = e
                problems.append(f"trace: {e}")
    if "glitch_counter" in monitors:
        n = _get(scope, "glitch", "actual_num_glitches")
        if n is not None:
            out["glitches_fired"] = int(n)
    if "trig_count" in monitors:
        n = _get(scope, "adc", "trig_count")
        if n is not None:
            out["trig_count"] = int(n)
    return out


def clear_errors(scope) -> Dict[str, Any]:
    """Clear every sticky error the scope keeps: the library's scope.errors.clear() where it exists (Husky: SAM, XADC, ADC, extclk and trace errors), else the individual registers."""
    cleared = []
    clr = _get(scope, "errors", "clear")
    if callable(clr):
        try:
            clr()
            cleared.append("errors")
        except Exception:  # noqa: BLE001
            pass
    for path, value in (("adc.errors", 0), ("XADC.status", 0), ("clock.extclk_error", False), ("LA.errors", 0), ("trace.errors", 0)):
        group, name = path.split(".")
        obj = _get(scope, group)
        if obj is None or not hasattr(obj, name):
            continue
        try:
            setattr(obj, name, value)
            cleared.append(path)
        except Exception:  # noqa: BLE001
            pass
    rst = _get(scope, "glitch", "reset_glitch_counter")
    if callable(rst):
        try:
            rst()
            cleared.append("glitch counter")
        except Exception:  # noqa: BLE001
            pass
    return {"cleared": cleared}
