"""Simulated ChipWhisperer scope and SimpleSerial AES target.

The simulator implements the subset of the `chipwhisperer` API that Studio uses (`arm/capture/get_last_trace`, `_dict_repr`, settings properties, `simpleserial_write/read`, `set_key`, ...).  It produces traces that leak the Hamming weight of the first-round S-box output, so a CPA attack recovers the key from a few dozen traces, and it models a glitch-vulnerable loop so the glitch sweep UI can be exercised.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import numpy as np

from cwstudio import simtrigger
from cwstudio.aes import HW, SBOX, encrypt_block

log = logging.getLogger("cwstudio.sim")


class _Settings:
    """Base class: subclasses declare FIELDS = [(name, default, doc), ...]."""

    FIELDS: List[tuple] = []

    def __init__(self):
        for name, default, _doc in self.FIELDS:
            object.__setattr__(self, name, default)

    _hidden: frozenset = frozenset()  # fields this model's hardware does not have: kept for the simulator's own use, left out of the settings tree

    def _dict_repr(self):
        rtn = OrderedDict()
        for name, _default, _doc in self.FIELDS:
            if name not in self._hidden:
                rtn[name] = getattr(self, name)
        return rtn

    def __repr__(self):
        return f"{type(self).__name__}({dict(self._dict_repr())})"


def _make_props(cls):
    """Turn FIELDS into real properties so settings introspection sees docs/setters."""
    for name, default, doc in cls.FIELDS:
        priv = "_" + name

        def getter(self, priv=priv):
            return getattr(self, priv)

        def setter(self, v, priv=priv, name=name):
            validate = getattr(self, "_validate_" + name, None)
            if validate:
                v = validate(v)
            setattr(self, priv, v)

        setattr(cls, name, property(getter, setter, doc=doc))
    orig_init = cls.__init__

    def __init__(self, *a, **k):
        for name, default, _doc in self.FIELDS:
            setattr(self, "_" + name, default)
        orig_init(self, *a, **k)

    cls.__init__ = __init__
    return cls


@_make_props
class SimGain(_Settings):
    FIELDS = [
        ("mode", "high", "Gain stage: 'low' or 'high'."),
        ("gain", 30, "Raw gain setting 0..78."),
        ("db", 25.0, "Gain in dB (derived)."),
    ]

    def __init__(self):
        pass

    def _validate_gain(self, v):
        v = int(v)
        if not 0 <= v <= 78:
            raise ValueError("gain must be 0..78")
        self._db = round(v * 0.6 + (5 if self._mode == "high" else -10), 1)
        return v


@_make_props
class SimADC(_Settings):
    FIELDS = [
        ("state", False, "Whether the ADC is currently armed (read only)."),
        ("basic_mode", "rising_edge", "Trigger mode: rising_edge, falling_edge, low, high."),
        ("timeout", 2.0, "Capture timeout in seconds."),
        ("offset", 0, "Number of samples to skip after the trigger."),
        ("presamples", 0, "Samples to record before the trigger."),
        ("samples", 5000, "Number of ADC samples to record in a single capture (the limit depends on the model: 24400 on a Lite, 96000 on a Pro, 137209 on a Husky at 12 bits, 100000 on a Nano)."),
        ("decimate", 1, "Keep one sample out of every `decimate`."),
        ("trig_count", 0, "Number of ADC clock cycles the trigger was active (read only)."),
        # Husky
        ("bits_per_sample", 12, "ADC resolution: 12, or 8 for more samples and faster streaming (Husky)."),
        ("max_samples", 137209, "Most samples one capture can hold at the current bits_per_sample (read only, Husky)."),
        ("max_presamples", 26605, "Most presamples at the current bits_per_sample (read only, Husky)."),
        ("segments", 1, "Captures per arm, each `samples` long (Husky): the fast way to collect many traces."),
        ("segment_cycles", 0, "ADC cycles between segments when segment_cycle_counter_en is set (Husky)."),
        ("segment_cycle_counter_en", False, "Start a new segment every segment_cycles instead of on each trigger (Husky)."),
        ("stream", False, "Stream samples to the host while capturing: no sample limit, about 10 MS/s (Pro and Husky)."),
        ("errors", "", "Sticky capture errors since the last arm (Husky: clip, gain, segment, presample, FIFO over/underflow, trigger too soon); write anything to clear."),
        # Nano
        ("clk_freq", 7.5e6, "ADC clock in Hz: 240 MHz divided by 1, 2, 4, 8, 16, 32 or 64 (Nano)."),
        ("clk_src", "int", "ADC clock source: int (the Nano's own divider) or ext (the HS1 input) (Nano)."),
    ]
    _MODEL_FIELDS = {
        "nano": {"state", "samples", "clk_freq", "clk_src"},
        "lite": {"state", "basic_mode", "timeout", "offset", "presamples", "samples", "decimate", "trig_count"},
        "pro": {"state", "basic_mode", "timeout", "offset", "presamples", "samples", "decimate", "trig_count", "stream"},
        "husky": {"state", "basic_mode", "timeout", "offset", "presamples", "samples", "decimate", "trig_count", "bits_per_sample", "max_samples", "max_presamples", "segments", "segment_cycles", "segment_cycle_counter_en", "stream", "errors"},
    }

    def __init__(self, model: str = "husky"):
        self.model = model
        shown = self._MODEL_FIELDS.get("husky" if model in ("husky", "huskyplus") else model, self._MODEL_FIELDS["husky"])
        self._hidden = frozenset(name for name, _d, _doc in self.FIELDS if name not in shown)
        self._refresh_limits()

    def _profile(self):
        from cwstudio.profiles import MODEL_PROFILES
        return MODEL_PROFILES.get(self.model) or MODEL_PROFILES["husky"]

    def _refresh_limits(self):
        adc = self._profile()["adc"]
        bits = int(self._bits_per_sample) if self.model in ("husky", "huskyplus") else adc["bits"]
        self._max_samples = adc["max_samples"].get(bits) or adc["max_samples"][adc["bits"]]
        self._max_presamples = adc.get("max_presamples", {}).get(bits, self._max_samples)

    def _validate_samples(self, v):
        v = int(v)
        limit = self._max_samples
        streaming = self._profile()["adc"]["stream"] and self._stream
        if v < 1 or (v > limit and not streaming) or v > 100_000_000:
            raise ValueError(f"samples must be 1..{limit}" + (" (or turn on adc.stream)" if self._profile()["adc"]["stream"] else ""))
        return v

    def _validate_presamples(self, v):
        v = int(v)
        if v < 0 or v > self._max_presamples:
            raise ValueError(f"presamples must be 0..{self._max_presamples}")
        if self.model in ("husky", "huskyplus") and v == 1:
            raise ValueError("presamples cannot be 1 on the Husky")
        return v

    def _validate_bits_per_sample(self, v):
        v = int(v)
        if v not in (8, 12):
            raise ValueError("bits_per_sample must be 8 or 12")
        self._bits_per_sample = v
        self._refresh_limits()
        if self._samples > self._max_samples and not self._stream:
            self._samples = self._max_samples
        return v

    def _validate_segments(self, v):
        v = int(v)
        if not 1 <= v <= 65535:
            raise ValueError("segments must be 1..65535")
        return v

    def _validate_errors(self, v):
        return ""  # any write clears, like the hardware

    def _validate_clk_freq(self, v):
        from cwstudio.profiles import NANO_BASE_HZ, NANO_DIVISORS
        if v is None:
            return None
        want = float(v)
        return NANO_BASE_HZ / min(NANO_DIVISORS, key=lambda d: abs(NANO_BASE_HZ / d - want))

    def _validate_clk_src(self, v):
        if v not in ("int", "ext"):
            raise ValueError("clk_src must be int or ext")
        return v

    def _validate_basic_mode(self, v):
        if v not in ("rising_edge", "falling_edge", "low", "high"):
            raise ValueError("invalid trigger mode")
        return v


# ADC clock sources of the CW-Lite and Pro: (clock the ADC follows, multiplier). The Husky sets the multiplier with adc_mul instead.
ADC_SOURCES = {"clkgen_x4": ("clkgen", 4), "clkgen_x1": ("clkgen", 1), "extclk_x4": ("extclk", 4), "extclk_x1": ("extclk", 1), "extclk_dir": ("extclk", 1)}


@_make_props
class SimClock(_Settings):
    FIELDS = [
        ("adc_src", "clkgen_x4", "ADC clock source (CW-Lite, Pro): clkgen_x4, clkgen_x1, extclk_x4, extclk_x1 or extclk_dir. Sets adc_mul to the multiplier."),
        ("adc_mul", 4, "ADC clock multiplier (Husky only): the ADC samples at adc_mul times the target clock (1..16)."),
        ("adc_freq", 29538459, "ADC sampling frequency (derived)."),
        ("adc_locked", True, "ADC DCM locked (read only)."),
        ("clkgen_src", "system", "CLKGEN source."),
        ("clkgen_freq", 7384615, "Target clock frequency in Hz."),
        ("clkgen_locked", True, "CLKGEN locked (read only)."),
        ("freq_ctr", 7384615, "Frequency counter reading (read only)."),
        ("extclk_monitor_enabled", False, "Flag a change of the external clock frequency (Husky)."),
        ("extclk_error", False, "The external clock changed since the PLL was set; write False to clear (Husky)."),
    ]
    _HUSKY_ONLY = ("adc_mul", "extclk_monitor_enabled", "extclk_error")

    def __init__(self, husky: bool = True):
        self._husky = husky  # clock.adc_mul exists on the Husky only; as a CW-Lite, Pro or Nano the simulator has no such attribute, like the hardware

    def _dict_repr(self):
        return OrderedDict((name, getattr(self, name)) for name, _default, _doc in self.FIELDS if self._husky or name not in self._HUSKY_ONLY)

    def _derive(self, clkgen=None, mul=None):
        """The ADC rate follows the target clock (CLKGEN; on EXTCLK the simulated target is clocked from CLKGEN through HS2 as usual, so the same frequency) times the multiplier, as on the hardware."""
        f = float(self._clkgen_freq if clkgen is None else clkgen)
        self._adc_freq = int(f * (self._adc_mul if mul is None else mul))
        self._freq_ctr = int(f)

    def _validate_clkgen_freq(self, v):
        v = float(v)
        self._derive(clkgen=v)
        return int(v)

    def _validate_adc_src(self, v):
        if v not in ADC_SOURCES:
            raise ValueError(f"adc_src must be one of {', '.join(ADC_SOURCES)}")
        self._adc_mul = ADC_SOURCES[v][1]
        self._derive()
        return v

    def _validate_adc_mul(self, v):
        v = int(v)
        if not 1 <= v <= 16:
            raise ValueError("adc_mul must be 1..16")
        if v in (1, 4) and self._adc_src != "extclk_dir":  # keep adc_src describing the same clock where it can
            self._adc_src = f"{ADC_SOURCES[self._adc_src][0]}_x{v}"
        self._derive(mul=v)
        return v


def _husky_only(prop):
    """Wrap a SimClock property so that it exists on the Husky models only (AttributeError with the reason otherwise, as on a CW-Lite or Pro)."""
    def check(self):
        if not self._husky:
            from cwstudio.capabilities import ADC_MUL_REASON
            raise AttributeError(ADC_MUL_REASON)

    def fget(self):
        check(self)
        return prop.fget(self)

    def fset(self, v):
        check(self)
        prop.fset(self, v)
    return property(fget, fset, doc=prop.__doc__)


SimClock.adc_mul = _husky_only(SimClock.adc_mul)


@_make_props
class SimTrigger(_Settings):
    FIELDS = [
        ("triggers", "tio4", "Trigger pin(s), e.g. 'tio4' or 'tio1 or tio2'."),
        ("module", "basic", "Trigger module."),
    ]

    def __init__(self):
        pass


@_make_props
class SimIO(_Settings):
    FIELDS = [
        ("tio1", "serial_rx", "Target IO1 function."),
        ("tio2", "serial_tx", "Target IO2 function."),
        ("tio3", "high_z", "Target IO3 function."),
        ("tio4", "high_z", "Target IO4 function."),
        ("pdid", "high_z", "PDID pin state."),
        ("pdic", "high_z", "PDIC pin state."),
        ("nrst", "high_z", "nRST pin state."),
        ("hs2", "clkgen", "HS2 output: clkgen, glitch or disabled."),
        ("target_pwr", True, "Target power switch."),
        ("glitch_hp", False, "High-power crowbar MOSFET."),
        ("glitch_lp", False, "Low-power crowbar MOSFET."),
        ("clkout", 7.5e6, "Target clock output in Hz: 240 MHz divided by 1, 2, 4, 8, 16, 32 or 64, or None for high-Z (Nano)."),
    ]

    def __init__(self, model: str = "husky"):
        self.model = model
        self._hidden = frozenset({"hs2", "target_pwr", "glitch_hp", "glitch_lp"} if model == "nano" else {"clkout"})

    def _validate_clkout(self, v):
        from cwstudio.profiles import NANO_BASE_HZ, NANO_DIVISORS
        if v is None:
            return None
        want = float(v)
        return NANO_BASE_HZ / min(NANO_DIVISORS, key=lambda d: abs(NANO_BASE_HZ / d - want))


@_make_props
class SimGlitch(_Settings):
    FIELDS = [
        ("enabled", False, "Power the glitch MMCMs (Husky)."),
        ("mmcm_locked", True, "Glitch MMCMs locked (read only, Husky)."),
        ("clk_src", "clkgen", "Glitch clock source: target, clkgen (Lite, Pro) or pll (Husky)."),
        ("phase_shift_steps", 4592, "Phase steps per glitch clock period: 56 x VCO / clkgen (read only, Husky)."),
        ("width", 0.0, "Glitch pulse width: percent of a clock period (-49.8..49.8) on a Lite or Pro, phase steps (0..phase_shift_steps/2) on a Husky."),
        ("width_fine", 0, "Fine width adjust, -255..255 DCM steps (Lite, Pro)."),
        ("offset", 0.0, "Glitch offset: percent of a clock period on a Lite or Pro, phase steps on a Husky."),
        ("offset_fine", 0, "Fine offset adjust, -255..255 DCM steps (Lite, Pro)."),
        ("trigger_src", "manual", "continuous, manual, ext_single, ext_continuous."),
        ("arm_timing", "after_scope", "no_glitch, before_scope, after_scope."),
        ("ext_offset", 0, "Clock cycles between trigger and glitch (CPU cycles of 8.3 ns on a Nano)."),
        ("repeat", 1, "Number of consecutive glitch cycles (on a Nano the pulse length in 8.3 ns cycles, 0 = off)."),
        ("num_glitches", 1, "Glitches per trigger, 1..32, each with its own ext_offset and repeat (Husky)."),
        ("actual_num_glitches", 0, "Glitches fired since the counter was reset (read only, Husky)."),
        ("output", "clock_xor", "clock_xor, clock_or, glitch_only, clock_only, enable_only."),
    ]
    _MODEL_FIELDS = {
        "nano": {"ext_offset", "repeat"},
        "lite": {"clk_src", "width", "width_fine", "offset", "offset_fine", "trigger_src", "arm_timing", "ext_offset", "repeat", "output"},
        "husky": {"enabled", "mmcm_locked", "clk_src", "phase_shift_steps", "width", "offset", "trigger_src", "arm_timing", "ext_offset", "repeat", "num_glitches", "actual_num_glitches", "output"},
    }

    def __init__(self, model: str = "husky"):
        self.model = model
        key = "husky" if model in ("husky", "huskyplus") else ("nano" if model == "nano" else "lite")
        shown = self._MODEL_FIELDS[key]
        self._hidden = frozenset(name for name, _d, _doc in self.FIELDS if name not in shown)
        if key == "husky":
            self._width, self._offset = 0, 0
        if key == "nano":
            self._repeat = 0  # off until a sweep sets it, like default_setup on the hardware

    def _husky(self) -> bool:
        return self.model in ("husky", "huskyplus")

    def _validate_width(self, v):
        if self._husky():
            v = int(v)
            if not 0 <= v <= self._phase_shift_steps // 2:
                raise ValueError(f"width must be 0..{self._phase_shift_steps // 2} phase steps")
            return v
        v = float(v)
        if not -49.8 <= v <= 49.8:
            raise ValueError("width must be -49.8..49.8 percent")
        return v

    def _validate_offset(self, v):
        if self._husky():
            v = int(v)
            half = self._phase_shift_steps // 2
            if not -half <= v <= half:
                raise ValueError(f"offset must be -{half}..{half} phase steps")
            return v
        v = float(v)
        if not -49.8 <= v <= 49.8:
            raise ValueError("offset must be -49.8..49.8 percent")
        return v

    def _validate_repeat(self, v):
        v = int(v)
        if self.model == "nano":
            if v < 0:
                raise ValueError("repeat must be 0 or more")
            return v
        if not 1 <= v <= 8192:
            raise ValueError("repeat must be 1..8192")
        return v

    def _validate_num_glitches(self, v):
        v = int(v)
        if not 1 <= v <= 32:
            raise ValueError("num_glitches must be 1..32")
        return v

    def width_percent(self) -> float:
        """The pulse width as a percent of a target clock period whatever the model's own unit, for the simulated target's outcome model."""
        if self.model == "nano":
            return min(49.8, abs(int(self._repeat)) * 6.25)  # 8.3 ns cycles against a 133 ns period at 7.5 MHz
        if self._husky():
            return abs(int(self._width)) / max(1, int(self._phase_shift_steps)) * 100.0
        return abs(float(self._width))

    def reset_glitch_counter(self):
        self._actual_num_glitches = 0


@_make_props
class SimXADC(_Settings):
    """The Husky's FPGA monitor: temperature, rails and sticky alarms. The simulator sits at 48 C; a test can set temp above temp_trigger to raise the alarm."""
    FIELDS = [
        ("temp", 48.2, "FPGA temperature in C."),
        ("max_temp", 51.0, "Highest temperature seen since power-up."),
        ("temp_trigger", 85.0, "User alarm threshold in C (clocks shut down above it)."),
        ("vccint", 1.0, "Core rail in V."),
        ("vccaux", 1.8, "Auxiliary rail in V."),
        ("vccbram", 1.0, "Block RAM rail in V."),
        ("status", "", "Sticky alarms; write 0 to clear."),
    ]

    def __init__(self):
        pass

    def _validate_temp(self, v):
        v = float(v)
        self._max_temp = max(float(self._max_temp), v)
        if v > float(self._temp_trigger):
            self._status = "user temperature alarm"
        return v

    def _validate_status(self, v):
        return ""


class SimErrors:
    """scope.errors: the SAM3U's serial overflow flags (every model) with the library's clear()."""

    def __init__(self, scope):
        self._scope = scope
        self.sam_errors = ""

    def clear(self):
        self.sam_errors = ""
        sc = self._scope
        if getattr(sc, "XADC", None) is not None:
            sc.XADC.status = 0
        if hasattr(sc.adc, "_errors"):
            sc.adc._errors = ""
        if hasattr(sc.clock, "_extclk_error"):
            sc.clock._extclk_error = False

    def _dict_repr(self):
        return OrderedDict(sam_errors=self.sam_errors)


class SimScope:
    """A scope that stands in for any ChipWhisperer model (sim_model, Husky by default) driving `SimTarget`."""

    _name = "ChipWhisperer-Simulator"

    def __init__(self, seed: Optional[int] = None, sim_model: str = "husky"):
        self.sim_model = sim_model  # which ChipWhisperer the simulator stands in for (capabilities.SIM_MODELS); decides which protocols and features Studio offers
        self.sn = "SIM000001"
        self.fw_version = {"major": 0, "minor": 1, "debug": 0}
        self.gain = SimGain()
        self.adc = SimADC(sim_model)
        self.clock = SimClock(husky=sim_model in ("husky", "huskyplus"))
        self.trigger = SimTrigger()
        self.io = SimIO(sim_model)
        self.glitch = SimGlitch(sim_model)
        if sim_model == "nano":
            self.clock._adc_mul = 1  # the Nano samples once per target clock at its default 7.5 MHz / 7.5 MHz
        self.XADC = SimXADC() if sim_model in ("husky", "huskyplus") else None
        self.errors = SimErrors(self)
        self.connectStatus = True
        self._armed = False
        self._last_trace = np.zeros(0, np.float32)
        self._rng = np.random.default_rng(seed)
        self.target: Optional["SimTarget"] = None
        self.noise = 0.02
        self.leak_amplitude = 0.04
        self.leak_start = 60           # sample index of first S-box leak
        self.leak_spacing = 45         # samples between successive S-box bytes
        self.capture_delay = 0.0       # extra latency per capture (seconds) to mimic USB
        self._bg_cache: Optional[np.ndarray] = None
        self._geo_cache: Optional[tuple] = None
        self._measure_hook = None      # set by the logic analyser's analog-to-logic source: a callable(n) giving what the measure input sees instead of the AES leakage
        self.firmware = None           # codemap.sim.FirmwareSim when an ELF was programmed: the target runs it in the emulator and traces come from its execution
        self.LA = None
        if sim_model in ("husky", "huskyplus"):
            from cwstudio.logic.sources import SimLA
            self.LA = SimLA(self, plus=sim_model == "huskyplus")  # the Husky logic analyser (scope.LA) on the demo traffic

    # --- API used by Studio ------------------------------------------------
    def _dict_repr(self):
        rtn = OrderedDict()
        rtn["sn"] = self.sn
        rtn["fw_version"] = self.fw_version
        nano = self.sim_model == "nano"
        if not nano:
            rtn["gain"] = self.gain._dict_repr()  # the Nano front end has a fixed gain
        rtn["adc"] = self.adc._dict_repr()
        if not nano:
            rtn["clock"] = self.clock._dict_repr()  # the Nano's clocks are adc.clk_freq and io.clkout
            rtn["trigger"] = self.trigger._dict_repr()  # the Nano triggers on TIO4 rising edge, full stop
        rtn["io"] = self.io._dict_repr()
        rtn["glitch"] = self.glitch._dict_repr()
        if self.XADC is not None:
            rtn["XADC"] = self.XADC._dict_repr()
        rtn["errors"] = self.errors._dict_repr()
        return rtn

    def __repr__(self):
        return f"SimScope({dict(self._dict_repr())})"

    def _getCWType(self):
        return "cwsim"

    def get_name(self):
        from cwstudio.capabilities import LABELS
        label = LABELS.get(self.sim_model, "").replace("ChipWhisperer-", "")
        return f"{self._name} ({label})" if label else self._name

    def default_setup(self, verbose=True):
        self.adc.samples = 5000
        self.adc.offset = 0
        self.adc.basic_mode = "rising_edge"
        self.trigger.triggers = "tio4"
        self.io.tio1 = "serial_rx"
        self.io.tio2 = "serial_tx"
        if self.sim_model == "nano":
            self.adc.clk_freq = 7.5e6
            self.io.clkout = 7.5e6
            self.glitch.repeat = 0
            return
        self.gain.gain = 30
        self.clock.clkgen_freq = 7.37e6
        if self.sim_model in ("husky", "huskyplus"):
            self.clock.adc_mul = 4
            self.adc.segments = 1
            self.glitch.enabled = False
        else:
            self.clock.adc_src = "clkgen_x4"
        self.io.hs2 = "clkgen"

    def cglitch_setup(self, default_setup=True):
        """Clock glitching: the glitch clock on HS2, crowbar off (like the library's scope.cglitch_setup)."""
        if self.sim_model == "nano":
            raise AttributeError("the CW-Nano has no clock glitch")
        if default_setup:
            self.default_setup()
        self.glitch.clk_src = "pll" if self.sim_model in ("husky", "huskyplus") else "clkgen"
        self.glitch.output = "clock_xor"
        self.glitch.trigger_src = "ext_single"
        self.io.hs2 = "glitch"
        self.io.glitch_hp = False
        self.io.glitch_lp = False
        if self.sim_model in ("husky", "huskyplus"):
            self.glitch.enabled = True

    def vglitch_setup(self, glitcht="lp", default_setup=True):
        """Voltage glitching: glitch_only output, the chosen crowbar MOSFET on (like the library's scope.vglitch_setup)."""
        if default_setup:
            self.default_setup()
        if self.sim_model == "nano":
            self.glitch.repeat = 1
            return
        self.glitch.output = "glitch_only"
        self.glitch.trigger_src = "ext_single"
        self.io.hs2 = "clkgen"
        self.io.glitch_hp = glitcht in ("hp", "both")
        self.io.glitch_lp = glitcht in ("lp", "both")
        if self.sim_model in ("husky", "huskyplus"):
            self.glitch.enabled = True

    def glitch_disable(self):
        """Glitch output off and the crowbar MOSFETs released (like the library's scope.glitch_disable)."""
        if self.sim_model == "nano":
            self.glitch.repeat = 0
            return
        self.glitch.trigger_src = "manual"
        self.io.glitch_hp = False
        self.io.glitch_lp = False
        self.io.hs2 = "clkgen"
        if self.sim_model in ("husky", "huskyplus"):
            self.glitch.enabled = False

    def con(self, **kwargs):
        self.connectStatus = True
        return True

    def dis(self):
        self.connectStatus = False
        return True

    def arm(self):
        if not self.connectStatus:
            raise OSError("scope not connected")
        self._armed = True
        self.adc._state = True
        self._segment_pts = []  # (plaintext, key) of every command seen while armed: a segmented capture (adc.segments > 1) records one segment per command
        if self.target is not None:
            self.target._traffic = []  # the advanced triggers see what happens on the lines from now on

    def capture(self, poll_done: bool = False) -> bool:
        """Return True on timeout (like the real API)."""
        if not self._armed:
            return True
        if self._measure_hook is not None:
            # a digital line on the measure input triggers the capture itself
            self._last_trace = np.asarray(self._measure_hook(int(self.adc.samples)), dtype=np.float32)
            self._armed = False
            self.adc._state = False
            return False
        deadline = time.time() + float(self.adc.timeout)
        adv = simtrigger.configured(self)
        if adv is not None:
            return self._capture_advanced(adv, deadline)
        if not self.trigger_driven():
            # the trigger watches pins the simulated target never toggles: like real hardware, wait out the timeout; the target's run is not captured
            time.sleep(max(0.0, deadline - time.time()))
            if self.target is not None:
                self.target._triggered = False
                self.target._last_run = None
            self._armed = False
            self.adc._state = False
            return True
        while self.target is None or not self.target._triggered:
            if time.time() > deadline:
                self._armed = False
                self.adc._state = False
                return True
            time.sleep(0.0005)
        if self.capture_delay:
            time.sleep(self.capture_delay)
        fw_run = getattr(self.target, "_last_run", None)
        segments = self._segments()
        if segments > 1 and fw_run is None:
            pts = list(getattr(self, "_segment_pts", []))[:segments]
            if len(pts) < segments:  # fewer triggers than segments: the hardware waits for the rest and times out
                self.adc._errors = (self.adc._errors + ", " if self.adc._errors else "") + f"segmenting error: {len(pts)} of {segments} segments triggered before the timeout"
                return self._timeout(deadline)
            self._last_trace = np.concatenate([self._synth(pt, key) for pt, key in pts]).astype(np.float32)
            leak_start, leak_spacing = self._leak_pos()
            trig = int(leak_start + 16 * leak_spacing)
        elif fw_run is not None:
            from cwstudio.codemap.sim import synth
            self._last_trace, trig = synth(self, *fw_run)
            self.target._last_run = None
        else:
            self._last_trace = self._synth(self.target._last_pt, self.target._key)
            leak_start, leak_spacing = self._leak_pos()
            trig = int(leak_start + 16 * leak_spacing)
        self.target._triggered = False
        self._armed = False
        self.adc._state = False
        self.adc._trig_count = trig
        self._after_capture()
        return False

    def _segments(self) -> int:
        """Segments per arm on the simulated Husky (1 on every other model)."""
        if self.sim_model not in ("husky", "huskyplus"):
            return 1
        try:
            return max(1, int(self.adc.segments))
        except (AttributeError, TypeError, ValueError):
            return 1

    def get_last_trace_segmented(self) -> np.ndarray:
        """The last capture as one row per segment (the library's scope.get_last_trace_segmented)."""
        n = max(1, int(self.adc.samples))
        w = self._last_trace
        rows = max(1, len(w) // n)
        return w[: rows * n].reshape(rows, n)

    def _after_capture(self):
        """What the Husky reports after a capture: the clip error when the front end saturates (a gain above 70 in the simulator) and the count of glitches fired."""
        if self.sim_model not in ("husky", "huskyplus"):
            return
        if int(self.gain.gain) >= 70 and "clip" not in self.adc._errors:
            self.adc._errors = (self.adc._errors + ", " if self.adc._errors else "") + "clip error: the ADC saturated (lower gain.db)"
        if self.target is not None and self.target._glitch_active():
            self.glitch._actual_num_glitches = min(65535, self.glitch._actual_num_glitches + int(self.glitch.num_glitches))

    def _timeout(self, deadline: float) -> bool:
        time.sleep(max(0.0, deadline - time.time()))
        if self.target is not None:
            self.target._triggered = False
            self.target._last_run = None
        self._armed = False
        self.adc._state = False
        return True

    def _capture_advanced(self, cfg: Dict[str, Any], deadline: float) -> bool:
        """A capture with a trigger other than the trigger pins (simtrigger): wait for the target's command, find when the trigger fires on what the target did, and record the trace from there; a trigger that never fires times out like the hardware."""
        while self.target is None or not self.target._triggered:
            if time.time() > deadline:
                self._armed = False
                self.adc._state = False
                return True
            time.sleep(0.0005)
        if self.capture_delay:
            time.sleep(self.capture_delay)
        fw_run = getattr(self.target, "_last_run", None)
        if fw_run is not None:
            hi, lo = fw_run[0].trigger_window()
            trig = int(((lo if lo is not None else fw_run[0].total_cycles) - (hi or 0)) * self._spc())
        else:
            leak_start, leak_spacing = self._leak_pos()
            trig = int(leak_start + 16 * leak_spacing)
        if cfg["kind"] == "fallback":
            self._trig_note(f"Simulator: {cfg['why']}; the capture triggers on the target's trigger pin (TIO4) instead")
            fire, win = 0.0, None
        elif cfg["kind"] == "sad" and getattr(self, "_sim_sad_ref", None) is None:
            self._trig_note("Simulator: the SAD trigger has no reference (no trace had been captured when it was configured); the capture triggers on the target's trigger pin (TIO4) instead")
            fire, win = 0.0, None
        else:
            fire, win = simtrigger.fire_time(self, cfg)
        if fire is None:
            self._trig_note(f"Simulator: the {cfg['kind'].replace('_', ' ')} trigger ({simtrigger.describe(cfg)}) never fired on what the simulated target did; the capture times out")
            return self._timeout(deadline)
        t0 = simtrigger.trace_start(self, fire)
        n = int(self.adc.samples)
        wave = win.trace(t0, n) if win is not None else None
        if wave is None:
            wave = simtrigger.render(self, t0, n)
        self._last_trace = np.asarray(wave, dtype=np.float32)
        self.target._last_run = None
        self.target._triggered = False
        self._armed = False
        self.adc._state = False
        self.adc._trig_count = trig
        self._after_capture()
        self._trig_fired = float(fire)  # where the trigger fired, in ADC samples from the target's trigger rise (tests and the log)
        return False

    def _trig_note(self, msg: str) -> None:
        """Log a trigger notice once per configuration (not on every capture)."""
        key = (msg, repr(getattr(self, "_sim_trigger", None)))
        if getattr(self, "_trig_noted", None) != key:
            self._trig_noted = key
            log.warning("%s", msg)

    # pins the simulated target drives: TIO4 is its trigger output; the UART lines (TIO1, TIO2) idle high and never give an edge on their own
    _TRIGGER_PIN = "tio4"
    _IDLE_HIGH = ("tio1", "tio2")

    def trigger_driven(self) -> bool:
        """Whether the configured trigger (trigger.triggers, e.g. 'tio4', 'tio1 OR tio4', 'tio4 AND tio1') ever fires when the simulated target raises its trigger pin. Only TIO4 is driven by the target, so a trigger on other pins times out as it would on real hardware."""
        expr = str(self.trigger.triggers or self._TRIGGER_PIN).strip().lower()
        pins = [p for p in re.split(r"\s+(?:or|and|nand)\s+|\s*[|&]\s*", expr) if p]
        if self._TRIGGER_PIN not in pins:
            return False
        if re.search(r"\sand\s|&", expr) and not re.search(r"\snand\s", expr):
            return all(p == self._TRIGGER_PIN or p in self._IDLE_HIGH for p in pins)  # AND: every other input must be high when TIO4 rises
        return True

    def load_firmware(self, path: str) -> Dict[str, Any]:
        """Program the simulated target: an ELF (or a .hex with its .elf next to it) runs in the emulator from now on; anything else keeps the built-in AES model."""
        from cwstudio.codemap.sim import load
        return load(self, path)

    ADC_BITS = {"nano": 8, "lite": 10, "pro": 10, "husky": 12, "huskyplus": 12}  # chipwhisperer: the Nano's ADC is 8-bit, the Lite's and Pro's 10-bit, the Husky's 12-bit

    def get_last_trace(self, as_int: bool = False) -> np.ndarray:
        """The last trace as floats in -0.5 to 0.5 or, with ``as_int``, as the raw unsigned ADC codes of the model (float = code / 2**bits - 0.5, like the hardware)."""
        if as_int:
            bits = self.ADC_BITS.get(self.sim_model, 10)
            if self.sim_model in ("husky", "huskyplus"):
                bits = int(self.adc.bits_per_sample)
            full = 1 << bits
            return np.clip(np.round((self._last_trace.astype(np.float64) + 0.5) * full), 0, full - 1).astype(np.int16)
        return self._last_trace

    # --- leakage model ------------------------------------------------------
    def _spc(self) -> int:
        """ADC samples per target clock cycle (4 with the default clkgen_x4 / adc_mul 4)."""
        if self.sim_model == "nano":
            try:
                return max(1, int(round(float(self.adc.clk_freq or 0) / float(self.io.clkout or 1))))
            except (TypeError, ValueError, ZeroDivisionError):
                return 1
        try:
            return int(self.clock._adc_mul)  # also set by adc_src on the models without adc_mul
        except (AttributeError, TypeError, ValueError):
            return 4

    def _leak_pos(self):
        """Sample index of the first S-box leak and the spacing between bytes at the current ADC clock. They are set for 4 samples per target clock; another multiplier stretches or squeezes the trace like on the hardware (the default is used exactly as set)."""
        spc = self._spc()
        if spc == 4:
            return self.leak_start, self.leak_spacing
        return int(round(self.leak_start * spc / 4)), max(1, int(round(self.leak_spacing * spc / 4)))

    def _leak_width(self) -> int:
        """Sigma (samples) of a byte's S-box dip: 6 at 4 samples per clock, scaled with the ADC clock like the positions (the dip lasts a fixed number of target clocks)."""
        return max(1, int(round(6 * self._spc() / 4)))

    def _round_shape(self):
        """Half-window and sigma (samples) of the per-round activity bumps: 40 and 15 at 4 samples per clock."""
        f = self._spc() / 4
        return max(2, int(round(40 * f))), max(1.0, 15.0 * f)

    def _background(self, n: int) -> np.ndarray:
        """Clock ripple + slow envelope for `n` samples (key independent, cached)."""
        spc = self._spc()
        bg = self._bg_cache
        if bg is None or bg.shape[0] != n or getattr(self, "_bg_spc", 4) != spc:
            t = np.arange(n, dtype=np.float32)
            clk = 0.03 * np.sin(2 * np.pi * t / float(spc))  # one ripple period per target clock cycle
            env = 0.02 * np.sin(2 * np.pi * t / (700.0 * spc / 4))
            bg = clk + env
            self._bg_cache = bg
            self._bg_spc = spc
        return bg

    def _leak_geometry(self, n: int, offset: int):
        """Cached sample indices and Gaussian shapes of the per-byte and per-round bumps.

        Returns None when the per-byte windows overlap (custom leak_spacing), in which case `_synth` applies them one by one so every sample sees the same sequence of float32 roundings.
        """
        width = self._leak_width()
        leak_start, leak_spacing = self._leak_pos()
        geo_key = (n, offset, leak_start, leak_spacing, width)
        cached = self._geo_cache
        if cached is not None and cached[0] == geo_key:
            return cached[1]
        geo = None
        if leak_spacing >= 6 * width:
            b_idx, b_win, b_shape = [], [], []
            for b in range(16):
                center = leak_start + b * leak_spacing - offset
                if 0 <= center < n:
                    idx = np.arange(max(0, center - 3 * width), min(n, center + 3 * width))
                    b_idx.append(idx)
                    b_win.append(np.full(idx.shape[0], b, np.intp))
                    b_shape.append(np.exp(-0.5 * ((idx - center) / width) ** 2))
            r_idx, r_shape = [], []
            r_start = leak_start + 16 * leak_spacing - offset
            r_half, r_sigma = self._round_shape()
            for r in range(9):
                c = r_start + r * 16 * leak_spacing
                if 0 <= c < n:
                    idx = np.arange(max(0, c - r_half), min(n, c + r_half))
                    r_idx.append(idx)
                    r_shape.append(0.12 * np.exp(-0.5 * ((idx - c) / r_sigma) ** 2))
            cat = lambda parts, dt: np.concatenate(parts) if parts else np.zeros(0, dt)  # noqa: E731
            geo = (cat(b_idx, np.intp), cat(b_win, np.intp), cat(b_shape, np.float64),
                   cat(r_idx, np.intp), cat(r_shape, np.float64))
        self._geo_cache = (geo_key, geo)
        return geo

    def _synth(self, pt: bytes, key: bytes, n: Optional[int] = None, offset: Optional[int] = None) -> np.ndarray:
        """The AES trace: ``n`` samples (adc.samples) from ``offset`` (adc.offset) samples after the trigger rise; the simulated triggers other than the trigger pin pass their own."""
        n = int(self.adc.samples) if n is None else int(n)
        gain_scale = (0.6 + self.gain.gain / 78.0) * (1.4 if self.gain.mode == "high" else 0.7)
        # Background: clock ripple + slow envelope + noise
        wave = self._background(n) + self._rng.normal(0, self.noise, n).astype(np.float32)
        offset = int(self.adc.offset) if offset is None else int(offset)
        has_key = bool(pt and key and len(pt) >= 16 and len(key) >= 16)
        geo = self._leak_geometry(n, offset)
        if geo is not None:
            b_idx, b_win, b_shape, r_idx, r_shape = geo
            # Round 1 S-box leakage: one bump per byte, amplitude ~ HW(sbox(pt^k)); windows are disjoint so one scatter is exact
            if has_key and b_idx.shape[0]:
                x = np.frombuffer(bytes(pt[:16]), np.uint8) ^ np.frombuffer(bytes(key[:16]), np.uint8)
                amp = self.leak_amplitude * (HW[SBOX[x]].astype(np.int64) - 4) + 0.15
                wave[b_idx] -= amp[b_win] * b_shape
            # Rounds 2..10 as generic activity (no key dependence)
            if r_idx.shape[0]:
                wave[r_idx] -= r_shape
        else:
            leak_start, leak_spacing = self._leak_pos()
            width = self._leak_width()
            r_half, r_sigma = self._round_shape()
            if has_key:
                for b in range(16):
                    center = leak_start + b * leak_spacing - offset
                    if 0 <= center < n:
                        hw = int(HW[SBOX[pt[b] ^ key[b]]])
                        lo, hi = max(0, center - 3 * width), min(n, center + 3 * width)
                        idx = np.arange(lo, hi)
                        bump = np.exp(-0.5 * ((idx - center) / width) ** 2)
                        wave[lo:hi] -= (self.leak_amplitude * (hw - 4) + 0.15) * bump
            r_start = leak_start + 16 * leak_spacing - offset
            for r in range(9):
                c = r_start + r * 16 * leak_spacing
                if 0 <= c < n:
                    lo, hi = max(0, c - r_half), min(n, c + r_half)
                    idx = np.arange(lo, hi)
                    wave[lo:hi] -= 0.12 * np.exp(-0.5 * ((idx - c) / r_sigma) ** 2)
        wave *= gain_scale
        return np.clip(wave, -0.5, 0.5, out=wave)  # float32 already: clip in place instead of a copy


class SimTarget:
    """A SimpleSerial-2 style AES target with a glitchable loop counter (the simpleserial-glitch g command)."""

    def __init__(self, scope: Optional[SimScope] = None):
        self.scope = scope
        self._key = bytes(range(16))
        self._last_pt = b""
        self._last_ct = b""
        self._triggered = False
        self._trigger_count = 0  # how often the trigger pin went high (the simulated logic analyser watches it)
        self._rx = bytearray()   # data the *host* can read (target -> host)
        self._lock = threading.Lock()
        self.output_len = 16
        self.baud = 38400
        self.parity = "none"
        self.stop_bits = 1
        self.simpleserial_last_read = ""
        self.simpleserial_last_sent = ""
        self.protver = "2.1"
        self.connectStatus = True
        self._pending_response: Optional[bytes] = None
        self._last_run = None  # (Run, per-cycle power) of the last emulated command that raised the trigger
        self._traffic: List[tuple] = []  # since the scope was armed: ("rx", bytes the target received), ("tx", bytes it sent), ("op", trigger timing) for the simulated advanced triggers
        self._raw_in = False
        self.glitch_window = (20, 60)   # ext_offset window where glitches succeed
        self.glitch_width_ok = (5.0, 40.0)

    def _dict_repr(self):
        rtn = OrderedDict()
        rtn["output_len"] = self.output_len
        rtn["baud"] = self.baud
        rtn["parity"] = self.parity
        rtn["stop_bits"] = self.stop_bits
        rtn["simpleserial_last_read"] = self.simpleserial_last_read
        rtn["simpleserial_last_sent"] = self.simpleserial_last_sent
        rtn["protver"] = self.protver
        return rtn

    def __repr__(self):
        return f"SimTarget({dict(self._dict_repr())})"

    def con(self, scope=None, **kwargs):
        self.scope = scope or self.scope
        if self.scope is not None:
            self.scope.target = self
        self.connectStatus = True

    def dis(self):
        self.connectStatus = False
        if self.scope is not None and self.scope.target is self:
            self.scope.target = None

    def close(self):
        self.dis()

    def flush(self):
        with self._lock:
            self._rx.clear()

    def is_done(self):
        return True

    # --- SimpleSerial ------------------------------------------------------
    def set_key(self, key, ack=True, timeout=250, always_send=False):
        self._key = bytes(key)
        self.simpleserial_last_sent = "k" + self._key.hex()

    def _glitch_active(self) -> bool:
        g = self.scope.glitch if self.scope else None
        if g is None:
            return False
        if g.model == "nano":
            return int(g.repeat) > 0  # the Nano fires on the TIO4 edge whenever repeat is nonzero
        if not (g.enabled or g.repeat > 0):
            return False
        return g.trigger_src in ("ext_single", "ext_continuous") and g.repeat > 0

    def _glitch_outcome(self) -> str:
        """'normal', 'success' or 'reset' for the current glitch settings."""
        g = self.scope.glitch
        lo, hi = self.glitch_window
        w = g.width_percent()
        if w < 1.0:
            return "normal"
        if w > self.glitch_width_ok[1] + 5 or (g.model != "nano" and int(g.repeat) > 20):
            return "reset"
        in_window = lo <= int(g.ext_offset) <= hi
        if in_window and self.glitch_width_ok[0] <= w <= self.glitch_width_ok[1]:
            r = self.scope._rng.random()
            return "success" if r < 0.75 else ("reset" if r < 0.9 else "normal")
        if w > self.glitch_width_ok[1] - 5:
            return "reset" if self.scope._rng.random() < 0.3 else "normal"
        return "normal"

    def _firmware(self):
        return getattr(self.scope, "firmware", None) if self.scope is not None else None

    def _record(self, kind: str, value) -> None:
        """Keep what crossed the target's pins since the scope was armed (simtrigger lays it out in time)."""
        if kind in ("rx", "tx") and not value:
            return
        tr = self._traffic
        tr.append((kind, bytes(value) if kind in ("rx", "tx") else value))
        if len(tr) > simtrigger.TRAFFIC_MAX:
            del tr[: len(tr) - simtrigger.TRAFFIC_MAX]

    def _frame(self, cmd: str, data: bytes) -> bytes:
        """The bytes a SimpleSerial host sends (or the target answers) for ``cmd`` with the target's protocol version."""
        from cwstudio.codemap.emu import SimpleSerial
        try:
            return SimpleSerial(str(self.protver) if str(self.protver) in ("2.1", "1.1", "1.0") else "2.1").frame(cmd, data)
        except Exception:  # noqa: BLE001
            return (cmd + bytes(data).hex() + "\n").encode()

    @staticmethod
    def _op(run) -> tuple:
        hi, lo = run.trigger_window()
        return ("op", (hi, lo, run.total_cycles))

    def _fw_glitch(self) -> str:
        """Outcome of the armed glitch on the emulated firmware: 'normal', 'skip' (the core misses instructions where the glitch lands) or 'reset' (too strong: the target crashes). Where it lands (ext_offset) is up to the firmware itself."""
        g = self.scope.glitch
        w = g.width_percent()
        lo, hi = self.glitch_width_ok
        if w < 1.0:
            return "normal"
        if w > hi + 5 or (g.model != "nano" and int(g.repeat) > 20):
            return "reset"
        r = self.scope._rng.random()
        if lo <= w <= hi:
            return "skip" if r < 0.75 else ("reset" if r < 0.85 else "normal")
        if w > hi - 5:
            return "reset" if r < 0.3 else "normal"
        return "normal"

    def _fw_write(self, fw, cmd: str, data: bytes) -> None:
        """A command for the emulated firmware: run it and keep its response (and, for commands that raise the trigger, the run the scope turns into a trace). An armed glitch makes the emulated core skip the instructions at ext_offset cycles after the trigger, or crash it."""
        if not self._raw_in:
            self._record("rx", fw.fw.ss.frame(cmd, data))
        if cmd == "k":
            self._key = bytes(data)
            return
        key = self._key if cmd == "p" else None
        outcome = self._fw_glitch() if self._glitch_active() else "normal"
        try:
            run, resp, P = fw.command(cmd, data, key)
        except Exception as e:  # noqa: BLE001
            log.warning("emulated firmware: %s", e)
            self._pending_response = None
            return
        if outcome == "skip" and run.trig:
            g = self.scope.glitch
            try:
                run, resp, P = fw.glitched(cmd, data, key, int(g.ext_offset), 1 + (max(1, int(g.repeat)) - 1) // 2)
            except Exception as e:  # noqa: BLE001  the glitched firmware faulted or hung: the target crashed
                log.debug("glitched firmware crashed: %s", e)
                outcome = "reset"
        if outcome == "reset" and run.trig:  # the trigger fired, then the target crashed: no response, and it starts over
            self._record(*self._op(run))
            self._last_run = (run, P)
            self._triggered = True
            self._trigger_count += 1
            self._pending_response = None
            fw._state = None
            return
        if cmd == "p":
            self._last_pt = data
            self._last_ct = resp or b""
        if run.trig:
            self._record(*self._op(run))
            self._last_run = (run, P)
            self._triggered = True
            self._trigger_count += 1
        self._record("tx", run.output)
        self._pending_response = resp
        with self._lock:
            self._rx += run.output  # what the firmware really sent (binary frames for SimpleSerial 2.1)

    def simpleserial_write(self, cmd, num, end="\n", var_len=False):
        data = bytes(num)
        self.simpleserial_last_sent = f"{cmd}{data.hex()}"
        fw = self._firmware()
        if fw is not None:
            self._fw_write(fw, cmd, data)
            return
        if not self._raw_in:
            self._record("rx", self._frame(cmd, data))
        if cmd in ("p", "g"):
            self._record("op", None)  # the built-in model raises the trigger pin as soon as the command is in
        if cmd == "p":
            self._last_pt = data
            self._last_ct = encrypt_block(self._key, data) if len(data) == 16 else b""
            self._triggered = True
            self._trigger_count += 1
            if self.scope is not None and getattr(self.scope, "_armed", False):
                getattr(self.scope, "_segment_pts", []).append((data, self._key))
            resp = self._last_ct
            if self._glitch_active() and self._glitch_outcome() == "success":
                resp = bytes(b ^ 0xFF for b in resp)
            self._pending_response = resp
        elif cmd == "g":
            # simpleserial-glitch style: loop counter, expect 0x09C4 (2500) normally
            self._triggered = True
            self._trigger_count += 1
            outcome = self._glitch_outcome() if self._glitch_active() else "normal"
            if outcome == "reset":
                self._pending_response = None
            elif outcome == "success":
                self._pending_response = (2500 - int(self.scope._rng.integers(1, 40))).to_bytes(4, "little")
            else:
                self._pending_response = (2500).to_bytes(4, "little")
        elif cmd == "k":
            self.set_key(data)
        elif cmd == "x":
            pass  # reset request: nothing to answer
        else:
            self._pending_response = b""
        if self._pending_response is not None and cmd in ("p", "g") and not self._raw_in:
            self._record("tx", self._frame("r", self._pending_response))
        # Also mirror into the raw rx buffer as a SimpleSerial-formatted line
        if self._pending_response is not None and cmd in ("p", "g"):
            with self._lock:
                self._rx += (f"r{self._pending_response.hex()}\n").encode()

    def simpleserial_read(self, cmd, pay_len, end="\n", timeout=250, ack=True):
        if self._pending_response is None:
            return None
        resp = self._pending_response
        self._pending_response = None
        with self._lock:
            self._rx.clear()
        self.simpleserial_last_read = f"{cmd}{resp.hex()}"
        return bytearray(resp[:pay_len]) if pay_len else bytearray(resp)

    def simpleserial_read_witherrors(self, cmd, pay_len, end="\n", timeout=250, glitch_timeout=8000, ack=True):
        if self._pending_response is None:
            return {"valid": False, "payload": None, "full_response": "", "rv": None}
        resp = self._pending_response
        self._pending_response = None
        with self._lock:
            self._rx.clear()
        self.simpleserial_last_read = f"{cmd}{resp.hex()}"
        return {"valid": True, "payload": bytearray(resp), "full_response": f"{cmd}{resp.hex()}\n", "rv": 0}

    # --- raw serial -------------------------------------------------------
    def write(self, data, timeout=0):
        if isinstance(data, str):
            data = data.encode()
        line = bytes(data)
        self._record("rx", line)
        fw = self._firmware()
        if fw is not None:  # raw serial traffic goes straight to the emulated firmware
            try:
                out = fw.raw(line)
            except Exception as e:  # noqa: BLE001
                log.warning("emulated firmware: %s", e)
                out = b""
            with self._lock:
                self._rx += out
            self._record("tx", out)
            return
        with self._lock:
            before = len(self._rx)
        self._raw_in = True
        try:
            self._write_line(line)
        finally:
            self._raw_in = False
        with self._lock:
            sent = bytes(self._rx[before:]) if len(self._rx) >= before else b""
        self._record("tx", sent)

    def _write_line(self, line: bytes) -> None:
        text = line.decode(errors="replace").strip()
        if text.startswith("p") and len(text) == 33:
            self.simpleserial_write("p", bytes.fromhex(text[1:]))
        elif text.startswith("k") and len(text) == 33:
            self.simpleserial_write("k", bytes.fromhex(text[1:]))
        elif text == "g":
            self.simpleserial_write("g", b"")
        elif text.startswith("v"):
            with self._lock:
                self._rx += b"z01\n"
        else:
            with self._lock:
                self._rx += b"z00\n"

    def read(self, num_char=0, timeout=250):
        with self._lock:
            if num_char <= 0:
                num_char = len(self._rx)
            out = bytes(self._rx[:num_char])
            del self._rx[:num_char]
        return out.decode("latin-1")  # like the chipwhisperer serial readers: one character per byte

    def in_waiting(self):
        with self._lock:
            return len(self._rx)

    def in_waiting_tx(self):
        return 0
