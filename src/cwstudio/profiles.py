"""What each ChipWhisperer model is made of and what it is good at: the per-model profile behind the capture, glitch and health cards and behind the simulator's shape.

The facts come from the NewAE sources (the Nano SAM4S firmware, the Lite and Pro Spartan-6 OpenADC designs, the Husky Artix-7 design and the chipwhisperer 6.0 host library). Numbers that the hardware reports itself (the Husky's sample limits, the glitch phase steps) are read live when a scope is connected and override the table.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

NANO_DIVISORS = [1, 2, 4, 8, 16, 32, 64]
NANO_BASE_HZ = 240e6

# glitch parameter rows: path, label, unit, min, max, int, note. The Glitch tab builds its parameter picker from these.
_GLITCH_NANO = [
    {"path": "glitch.ext_offset", "label": "offset", "unit": "cycles of 8.3 ns", "min": 0, "max": 4294967295, "int": True, "note": "delay from the TIO4 edge in 120 MHz CPU cycles; about 200 ns of interrupt jitter on top"},
    {"path": "glitch.repeat", "label": "width", "unit": "cycles (0 = off)", "min": 0, "max": 4294967295, "int": True, "note": "pulse length: 1 to 7 are exact, above 7 each count adds about 16 ns"},
]
_GLITCH_DCM = [
    {"path": "glitch.width", "label": "width", "unit": "% of a clock period", "min": -49.8, "max": 49.8, "int": False, "note": "256 steps per period; every coarse step reprograms the DCM through ICAP (slow sweeps)"},
    {"path": "glitch.offset", "label": "offset", "unit": "% of a clock period", "min": -49.8, "max": 49.8, "int": False, "note": "same DCM mechanism as the width"},
    {"path": "glitch.width_fine", "label": "width fine", "unit": "DCM steps", "min": -255, "max": 255, "int": True, "note": "about 5 ns across the whole range"},
    {"path": "glitch.offset_fine", "label": "offset fine", "unit": "DCM steps", "min": -255, "max": 255, "int": True, "note": ""},
    {"path": "glitch.ext_offset", "label": "ext offset", "unit": "glitch clock cycles", "min": 0, "max": 4294967295, "int": True, "note": "cycles between the trigger and the glitch"},
    {"path": "glitch.repeat", "label": "repeat", "unit": "cycles", "min": 1, "max": 8192, "int": True, "note": "consecutive glitched cycles"},
]
_GLITCH_HUSKY = [
    {"path": "glitch.width", "label": "width", "unit": "phase steps", "min": 0, "max": None, "int": True, "note": "0 to phase_shift_steps / 2; one step is a period / phase_shift_steps (about 2.4 ns per 100 steps at 7.37 MHz)"},
    {"path": "glitch.offset", "label": "offset", "unit": "phase steps", "min": None, "max": None, "int": True, "note": "signed, same step size as the width"},
    {"path": "glitch.ext_offset", "label": "ext offset", "unit": "glitch clock cycles", "min": 0, "max": 4294967295, "int": True, "note": "per glitch when num_glitches > 1"},
    {"path": "glitch.repeat", "label": "repeat", "unit": "cycles", "min": 1, "max": 8192, "int": True, "note": "per glitch when num_glitches > 1"},
    {"path": "glitch.num_glitches", "label": "glitches", "unit": "per trigger", "min": 1, "max": 32, "int": True, "note": "each with its own ext_offset and repeat"},
]

MODEL_PROFILES: Dict[str, Dict[str, Any]] = {
    "nano": {
        "label": "ChipWhisperer-Nano", "brain": "one SAM4S microcontroller, no FPGA: capture, clocks, trigger and glitch are firmware",
        "adc": {"bits": 8, "bits_choices": [8], "max_samples": {8: 100000}, "presamples": False, "offset": False, "decimate": False, "stream": False, "segments": False, "gain": False,
                "max_rate_hz": 15e6, "note": "one sample per ADC clock, fixed gain, AC coupled"},
        "clock": {"kind": "fixed_divider", "base_hz": NANO_BASE_HZ, "divisors": NANO_DIVISORS, "target_hz": [3.75e6, 60e6], "adc_hz": [3.75e6, 15e6], "ratio": "power of two", "phase": False, "ext_clock": "ADC clock input only (adc.clk_src = ext)"},
        "trigger": {"kind": "fixed", "pins": ["tio4"], "edges": ["rising_edge"], "modules": ["basic"], "sequencer_slots": 0},
        "glitch": {"kind": "crowbar_firmware", "params": _GLITCH_NANO, "outputs": ["lp"], "clock_glitch": False, "readback": None, "jitter_ns": 200, "step_ns": 8.33},
        "monitors": ["sam_errors"],
        "firmware": {"bundled": "0.65.0", "upstream": "0.66.0"},
        "best_for": ["teaching CPA, SPA and timing attacks on the built-in STM32F030", "fast capture loops: one request to arm, one bulk read", "quick crowbar glitch sweeps (two writes per point)", "STM32 and CW308 targets up to 15 MS/s"],
        "avoid": ["captures that need presamples or a trigger offset", "precision or clock glitching", "targets above 15 MHz or low-amplitude leakage (fixed gain)", "trigger logic beyond one rising edge", "XMEGA and AVR targets"],
    },
    "lite": {
        "label": "ChipWhisperer-Lite", "brain": "Spartan-6 LX9 OpenADC design plus a SAM3U USB controller",
        "adc": {"bits": 10, "bits_choices": [10], "max_samples": {10: 24400}, "presamples": True, "offset": True, "decimate": True, "decimate_max": 8192, "stream": False, "segments": False, "gain": True, "gain_db": [-6.5, 56],
                "max_rate_hz": 105e6, "note": "block RAM FIFO of 24400 samples; fill modes need a newer bitstream"},
        "clock": {"kind": "dcm", "clkgen_hz": [3.2e6, 200e6], "mul": [2, 256], "div": [1, 256], "adc_mul": [1, 4], "phase": [-255, 255], "ext_clock": "HS1 input, clkgen_src = extclk"},
        "trigger": {"kind": "basic", "pins": ["tio1", "tio2", "tio3", "tio4", "nrst"], "edges": ["rising_edge", "falling_edge", "high", "low"], "modules": ["basic"], "sequencer_slots": 0},
        "glitch": {"kind": "dcm_percent", "params": _GLITCH_DCM, "outputs": ["hs2", "lp", "hp"], "clock_glitch": True, "readback": None, "note": "two phase-shifted DCM clocks ANDed; coarse steps reprogram the DCM through ICAP"},
        "monitors": ["dcm_lock", "fifo_overflow", "trig_count", "sam_errors"],
        "firmware": {"bundled": "0.65.0", "upstream": None},
        "best_for": ["courses and SimpleSerial CPA on XMEGA and STM32 targets", "clock and crowbar glitching of UFO targets", "a cheap JTAG and SWD probe", "the same ADC, clocking and glitch engine as the Pro"],
        "avoid": ["captures longer than 24400 samples (no streaming)", "triggering without a trigger pin (no SAD or UART trigger)", "large glitch sweeps on width or offset (each coarse step is slow)"],
    },
    "pro": {
        "label": "ChipWhisperer-Pro", "brain": "Spartan-6 LX45 OpenADC design plus a SAM3U USB controller, in an enclosure",
        "adc": {"bits": 10, "bits_choices": [10], "max_samples": {10: 96000}, "presamples": True, "offset": True, "decimate": True, "decimate_max": 8192, "stream": True, "stream_rate_hz": 10e6, "segments": False, "gain": True, "gain_db": [-6.5, 56],
                "max_rate_hz": 105e6, "note": "96000-sample FIFO; streaming has no sample limit but stops on a FIFO overflow above about 10 MS/s"},
        "clock": {"kind": "dcm", "clkgen_hz": [3.2e6, 200e6], "mul": [2, 256], "div": [1, 256], "adc_mul": [1, 4], "phase": [-255, 255], "ext_clock": "HS1 input, clkgen_src = extclk"},
        "trigger": {"kind": "basic", "pins": ["tio1", "tio2", "tio3", "tio4", "nrst", "sma"], "edges": ["rising_edge", "falling_edge", "high", "low"], "modules": ["basic", "SAD", "DECODEIO"], "sequencer_slots": 0, "sad": {"samples": 128, "bits": 10, "threshold_max": 100000}, "outputs": ["aux"]},
        "glitch": {"kind": "dcm_percent", "params": _GLITCH_DCM, "outputs": ["hs2", "lp", "hp", "aux"], "clock_glitch": True, "readback": "cycle_counter", "note": "same engine as the Lite plus a glitch cycle counter"},
        "monitors": ["dcm_lock", "fifo_overflow", "trig_count", "glitch_counter", "sam_errors"],
        "firmware": {"bundled": "1.62.0", "upstream": None},
        "best_for": ["long or streamed captures (RSA, boot loaders)", "triggering without a GPIO: SAD on the trace shape or a UART pattern (DecodeIO)", "exporting the trigger, glitch or clock on the AUX SMA", "the same workflows as the Lite with four times the samples"],
        "avoid": ["expecting better resolution or less jitter than a Lite (same analog path and DCMs)", "streaming above about 10 MS/s (the capture dies on a FIFO overflow)"],
    },
    "husky": {
        "label": "ChipWhisperer-Husky", "brain": "Artix-7 35T design plus a SAM3U USB controller; CDCI6214 PLL clocking",
        "adc": {"bits": 12, "bits_choices": [12, 8], "max_samples": {12: 137209, 8: 204796}, "presamples": True, "max_presamples": {12: 26605, 8: 38890}, "offset": True, "decimate": True, "decimate_max": 8192, "decimate_with_presamples": False, "stream": True, "stream_rate_hz": 10e6,
                "segments": True, "max_segments": 65535, "segment_total_bytes": 165888, "gain": True, "gain_db": [-6.5, 56], "max_rate_hz": 200e6, "errors": True, "note": "8-bit mode stores 204796 samples and streams 1.5x faster; segments give thousands of traces per second"},
        "clock": {"kind": "pll", "clkgen_hz": [5e6, 200e6], "adc_mul": [1, 32], "phase": "percent of the ADC period", "ext_clock": "HS1 or AUX MCX, with a frequency change monitor", "vco_hz": [600e6, 1200e6]},
        "trigger": {"kind": "modules", "pins": ["tio1", "tio2", "tio3", "tio4", "nrst", "sma"] + [f"userio_d{i}" for i in range(8)], "edges": ["rising_edge", "falling_edge", "high", "low"], "modules": ["basic", "SAD", "UART", "trace", "ADC", "edge_counter", "bitbanger"], "sequencer_slots": 2,
                    "sad": {"samples": 512, "bits": 8, "threshold_max": 127, "interval": True, "mask": True}, "uart_rules": 2, "trace_rules": 2, "outputs": ["trig_mcx", "aux_mcx"]},
        "glitch": {"kind": "mmcm_steps", "params": _GLITCH_HUSKY, "outputs": ["hs2", "lp", "hp", "trig_mcx"], "clock_glitch": True, "readback": "counter", "max_glitches": 32, "steps_per_vco_period": 56, "note": "width and offset in MMCM phase steps; phase_shift_steps = 56 x VCO / clkgen"},
        "monitors": ["adc_errors", "xadc", "extclk", "pll_lock", "trig_count", "glitch_counter", "la_errors", "trace_errors", "sam_errors"],
        "firmware": {"bundled": "1.7.0", "upstream": "1.8.0"},
        "best_for": ["segmented capture: thousands of traces per second", "12-bit traces with a flexible PLL and ADC phase control", "triggers without a GPIO: SAD with a mask, UART rules, trace from the firmware's own program counter", "multi-glitch sequences with a fired counter", "logic analyser, thermal and error reporting while attacking"],
        "avoid": ["presamples together with decimate (not supported)", "long crowbar sweeps without watching the XADC alarms (they power the glitch down)", "custom bitstreams: the FPGA is over 80 percent full"],
    },
}
MODEL_PROFILES["huskyplus"] = copy.deepcopy(MODEL_PROFILES["husky"])
MODEL_PROFILES["huskyplus"].update({"label": "ChipWhisperer-Husky Plus", "brain": "Artix-7 100T design plus a SAM3U USB controller; CDCI6214 PLL clocking", "firmware": {"bundled": "1.2.0", "upstream": "1.3.0"}})
MODEL_PROFILES["huskyplus"]["adc"].update({"max_samples": {12: 346246, 8: 518344}, "max_presamples": {12: 51163, 8: 75745}, "segment_total_bytes": 442368, "max_rate_hz": 250e6})
MODEL_PROFILES["huskyplus"]["clock"]["clkgen_hz"] = [5e6, 250e6]
MODEL_PROFILES["huskyplus"]["trigger"].update({"sequencer_slots": 4, "uart_rules": 8, "trace_rules": 8})
MODEL_PROFILES["huskyplus"]["best_for"] = MODEL_PROFILES["husky"]["best_for"] + ["2.5x the sample memory, four sequencer slots, eight UART and trace rules"]


def profile(model: Optional[str]) -> Optional[Dict[str, Any]]:
    """A copy of the static profile of ``model`` (None for a model Studio does not know)."""
    p = MODEL_PROFILES.get(model or "")
    return copy.deepcopy(p) if p else None


def profile_for(scope, model: Optional[str]) -> Optional[Dict[str, Any]]:
    """The profile with the values the connected hardware reports itself: the Husky's sample and presample limits for the current bit depth and the glitch phase steps. Hardware thread only when ``scope`` is real."""
    p = profile(model)
    if p is None or scope is None:
        return p
    adc = getattr(scope, "adc", None)
    live: Dict[str, Any] = {}
    if adc is not None:
        for name, key in (("bits_per_sample", "bits"), ("max_samples", "max_samples_now"), ("max_presamples", "max_presamples_now"), ("max_segments", "max_segments_now")):
            try:
                v = getattr(adc, name)
            except Exception:  # noqa: BLE001
                continue
            if v is not None:
                live[key] = int(v)
    if live:
        p["adc"]["live"] = live
    g = getattr(scope, "glitch", None)
    try:
        steps = int(getattr(g, "phase_shift_steps")) if g is not None else None
    except Exception:  # noqa: BLE001
        steps = None
    if steps:
        p["glitch"]["phase_shift_steps"] = steps
        for row in p["glitch"]["params"]:
            if row["path"] == "glitch.width":
                row["max"] = steps // 2
            elif row["path"] == "glitch.offset":
                row["min"], row["max"] = -steps // 2, steps // 2
    return p


def sample_limit(scope, model: Optional[str]) -> Optional[int]:
    """The most ADC samples one non-streaming capture can hold on this scope, or None when unknown or when streaming is on and the model streams. Hardware thread only when ``scope`` is real."""
    p = MODEL_PROFILES.get(model or "")
    adc = getattr(scope, "adc", None)
    if p is None or adc is None:
        return None
    try:
        if p["adc"]["stream"] and (getattr(adc, "stream", False) or getattr(adc, "stream_mode", False)):
            return None
    except Exception:  # noqa: BLE001
        pass
    try:
        live = getattr(adc, "max_samples", None)
        if live:
            return int(live)
    except Exception:  # noqa: BLE001
        pass
    try:
        bits = int(getattr(adc, "bits_per_sample", p["adc"]["bits"]))
    except Exception:  # noqa: BLE001
        bits = p["adc"]["bits"]
    return p["adc"]["max_samples"].get(bits) or p["adc"]["max_samples"].get(p["adc"]["bits"])


def glitch_params(model: Optional[str]) -> List[Dict[str, Any]]:
    p = MODEL_PROFILES.get(model or "")
    return copy.deepcopy(p["glitch"]["params"]) if p else []


# settings paths the model does not have although the generic tree, the simulator or another model would offer them; gate_settings drops them and check_setting refuses writes
MISSING_PATHS: Dict[str, Dict[str, str]] = {
    "nano": {
        "adc.presamples": "the CW-Nano captures from the trigger edge: no presamples", "adc.offset": "the CW-Nano has no trigger offset", "adc.decimate": "the CW-Nano samples once per ADC clock: no decimation",
        "adc.basic_mode": "the CW-Nano triggers on a rising edge of TIO4 only", "adc.timeout": "the CW-Nano capture timeout is fixed at 2 s", "adc.trig_count": "the CW-Nano does not count the trigger length",
        "adc.stream": "the CW-Nano does not stream", "adc.stream_mode": "the CW-Nano does not stream", "adc.bits_per_sample": "the CW-Nano ADC is 8-bit", "adc.segments": "segmented capture is on the Husky only",
        "gain.mode": "the CW-Nano front end has a fixed gain", "gain.gain": "the CW-Nano front end has a fixed gain", "gain.db": "the CW-Nano front end has a fixed gain",
        "clock.clkgen_src": "the CW-Nano has no CLKGEN: io.clkout picks one of seven divisions of 240 MHz", "clock.clkgen_freq": "the CW-Nano has no CLKGEN: set io.clkout", "clock.adc_src": "the CW-Nano has no ADC clock multiplier: set adc.clk_freq",
        "clock.adc_mul": "the CW-Nano has no ADC clock multiplier: set adc.clk_freq", "clock.freq_ctr": "the CW-Nano has no frequency counter", "clock.adc_locked": "the CW-Nano has no PLL lock to report", "clock.clkgen_locked": "the CW-Nano has no PLL lock to report", "clock.adc_freq": "the CW-Nano reports the ADC clock as adc.clk_freq",
        "io.hs2": "the CW-Nano has no HS2 clock output", "io.target_pwr": "the CW-Nano cannot switch the target power", "io.glitch_hp": "the CW-Nano has one crowbar path, always on", "io.glitch_lp": "the CW-Nano has one crowbar path, always on",
        "glitch.width": "the CW-Nano glitch has only ext_offset and repeat (its width is repeat, in 8.3 ns cycles)", "glitch.offset": "the CW-Nano glitch has only ext_offset and repeat", "glitch.width_fine": "the CW-Nano glitch has only ext_offset and repeat", "glitch.offset_fine": "the CW-Nano glitch has only ext_offset and repeat",
        "glitch.clk_src": "the CW-Nano has no clock glitch", "glitch.trigger_src": "the CW-Nano glitch fires on the TIO4 edge after every arm", "glitch.output": "the CW-Nano has one crowbar output", "glitch.arm_timing": "the CW-Nano glitch is armed with the capture", "glitch.enabled": "the CW-Nano glitch is enabled by a nonzero repeat", "glitch.num_glitches": "multi-glitch is on the Husky only",
    },
    "lite": {"adc.stream": "streaming needs a ChipWhisperer-Pro or Husky", "adc.stream_mode": "streaming needs a ChipWhisperer-Pro or Husky", "adc.bits_per_sample": "the CW-Lite ADC is 10-bit", "adc.segments": "segmented capture is on the Husky only", "adc.segment_cycles": "segmented capture is on the Husky only", "adc.segment_cycle_counter_en": "segmented capture is on the Husky only", "glitch.num_glitches": "multi-glitch is on the Husky only", "glitch.enabled": "the CW-Lite glitch module is always powered"},
    "pro": {"adc.bits_per_sample": "the CW-Pro ADC is 10-bit", "adc.segments": "segmented capture is on the Husky only", "adc.segment_cycles": "segmented capture is on the Husky only", "adc.segment_cycle_counter_en": "segmented capture is on the Husky only", "glitch.num_glitches": "multi-glitch is on the Husky only", "glitch.enabled": "the CW-Pro glitch module is always powered"},
    "husky": {"adc.fifo_fill_mode": "fill modes are a CW-Lite and Pro setting; the Husky uses adc.segments", "clock.adc_src": "the Husky sets the ADC clock with clock.adc_mul", "glitch.width_fine": "the Husky sets the width in phase steps (glitch.width); there is no fine register", "glitch.offset_fine": "the Husky sets the offset in phase steps (glitch.offset); there is no fine register"},
}
MISSING_PATHS["huskyplus"] = dict(MISSING_PATHS["husky"])
