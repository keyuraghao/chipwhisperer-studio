"""Recipes: the best way to reach a goal on the connected ChipWhisperer, chosen per model from the profile.

A recipe is a goal (fast capture, long capture, a trigger without a GPIO, a first voltage or clock glitch, cleanest traces) with, for the connected model, the settings and actions that get there and the notes that matter. The Quick Info card offers them as buttons; the MCP tools list and apply them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from cwstudio.profiles import MODEL_PROFILES

GOALS = ["fast_capture", "long_capture", "trigger_without_gpio", "voltage_glitch", "clock_glitch", "clean_traces"]
TITLES = {"fast_capture": "Capture as many traces per second as possible", "long_capture": "Capture a long operation", "trigger_without_gpio": "Trigger without a GPIO from the target", "voltage_glitch": "Start a voltage glitch campaign", "clock_glitch": "Start a clock glitch campaign", "clean_traces": "Cleanest traces for CPA"}


def recipes_for(model: Optional[str]) -> List[Dict[str, Any]]:
    """Every goal with what this model does for it: ``settings`` (scope settings to write, in order), ``actions`` (scope actions), ``capture`` (capture parameters to use), ``notes`` and ``available`` (False with a reason when the model has no good way)."""
    p = MODEL_PROFILES.get(model or "")
    if p is None:
        return [{"id": g, "title": TITLES[g], "available": False, "reason": "connect a scope first", "settings": [], "actions": [], "capture": {}, "notes": []} for g in GOALS]
    husky = model in ("husky", "huskyplus")
    out: List[Dict[str, Any]] = []

    def add(goal, available=True, reason=None, settings=(), actions=(), capture=None, notes=()):
        out.append({"id": goal, "title": TITLES[goal], "available": available, "reason": reason, "settings": [list(s) for s in settings], "actions": list(actions), "capture": dict(capture or {}), "notes": list(notes)})

    if husky:
        add("fast_capture", settings=[("adc.segments", 20), ("adc.decimate", 1)], capture={"display_rate": 5}, notes=["20 traces per arm: one command per segment, one download; use a trace count that is a multiple of 20", "watch adc.errors for a segmenting error if the target is slow to answer", "the Capture tab shows the segment count and the live limit"])
        add("long_capture", settings=[("adc.bits_per_sample", 8), ("adc.stream", True), ("adc.presamples", 0), ("adc.samples", 2_000_000)], notes=["streaming has no sample limit at about 10 MS/s; lower clock.adc_mul or raise adc.decimate when the slow FIFO overflows", "8-bit mode streams 1.5x faster; switch back to 12 bits for CPA on short windows"])
        add("trigger_without_gpio", notes=["SAD: capture one trace with a GPIO or manual trigger, then cut the reference from the operation's own shape (Interfaces, Triggers, SAD, Preview on stored traces)", "UART pattern: trigger on the byte sequence the target sends or receives (Interfaces, Triggers, UART pattern rules)", "trace: a program-counter match from the firmware's ELF on a Cortex-M target (NewAE trace notebooks for now)", "sequencer: chain a UART byte with a SAD match when one alone fires too often"])
        add("voltage_glitch", actions=["vglitch_lp"], settings=[("glitch.repeat", 1), ("glitch.num_glitches", 1)], notes=["sweep glitch.width 100 to 2000 phase steps and glitch.ext_offset over the operation; the Glitch tab shows one step in nanoseconds", "the result class not fired means the glitch counter did not move: check glitch.trigger_src and arm timing", "the XADC alarm powers the glitch down after a long crowbar sweep; clear it from the health card"])
        add("clock_glitch", actions=["cglitch_setup"], notes=["the glitched clock leaves on HS2; a target on its own crystal or PLL ignores it (use the crowbar then)", "sweep glitch.width and glitch.offset in phase steps, glitch.ext_offset in cycles"])
        add("clean_traces", settings=[("adc.bits_per_sample", 12), ("adc.decimate", 1), ("clock.adc_mul", 4)], notes=["raise gain.db until the health card reports a clip error, then back off one step", "align the ADC to the target clock: clock.adc_phase, or run the ADC from the target's clock with clock.clkgen_src = extclk", "keep the glitch MMCMs and the logic analyser powered down while capturing"])
    elif model == "pro":
        add("fast_capture", capture={"display_rate": 5}, notes=["the Pro captures one trace per arm; lower the display rate and keep adc.samples short", "an FPGA target with a batch mode (CW305) drives itself and removes the USB round trip"])
        add("long_capture", settings=[("adc.stream", True), ("adc.presamples", 0), ("adc.samples", 2_000_000)], notes=["streaming has no sample limit but the capture dies on a FIFO overflow above about 10 MS/s: raise adc.decimate or lower the ADC clock"])
        add("trigger_without_gpio", notes=["SAD: 128 samples of the operation's own shape, threshold on the sum of differences (Interfaces, Triggers, SAD, Preview on stored traces)", "UART: the I/O decode trigger fires on a byte pattern on the serial line"])
        add("voltage_glitch", actions=["vglitch_lp"], settings=[("glitch.repeat", 1)], notes=["sweep glitch.width and glitch.offset in percent of a period plus glitch.ext_offset; every coarse step reprograms the DCM, so keep the width range narrow"])
        add("clock_glitch", actions=["cglitch_setup"], notes=["the glitched clock leaves on HS2 and, if wanted, on the AUX SMA (trigger outputs)"])
        add("clean_traces", settings=[("adc.decimate", 1), ("clock.adc_src", "clkgen_x4")], notes=["raise gain.db until the traces clip, then back off", "clock.adc_phase shifts the sampling point in 5 ns across its range"])
    elif model == "lite":
        add("fast_capture", capture={"display_rate": 5}, notes=["one trace per arm over full-speed USB: keep adc.samples at what the attack needs and lower the display rate"])
        add("long_capture", settings=[("adc.decimate", 4)], notes=["24400 samples per capture and no streaming: decimate to cover more time, or use adc.offset to capture a later window of the operation in a second run"])
        add("trigger_without_gpio", available=False, reason="the Lite has basic pin triggers only: add a trigger pin to the firmware, or use an ADC level with an external comparator", notes=["a Pro or Husky adds SAD and UART triggers"])
        add("voltage_glitch", actions=["vglitch_lp"], settings=[("glitch.repeat", 1)], notes=["sweep glitch.width and glitch.offset in percent of a period plus glitch.ext_offset; every coarse step reprograms the DCM, so keep the width range narrow"])
        add("clock_glitch", actions=["cglitch_setup"], notes=["the glitched clock leaves on HS2; a target on its own crystal ignores it"])
        add("clean_traces", settings=[("adc.decimate", 1), ("clock.adc_src", "clkgen_x4")], notes=["raise gain.db until the traces clip, then back off"])
    else:  # nano
        add("fast_capture", capture={"display_rate": 5}, notes=["the Nano is already the fastest loop: one request to arm, one bulk read; keep adc.samples short"])
        add("long_capture", settings=[("adc.samples", 100000)], notes=["100000 samples at one sample per ADC clock; lower adc.clk_freq to cover more time (no decimation or streaming)"])
        add("trigger_without_gpio", available=False, reason="the Nano triggers on a rising edge of TIO4 only")
        add("voltage_glitch", actions=["vglitch_lp"], notes=["sweep glitch.ext_offset in 8.3 ns cycles and glitch.repeat 1 to 7 for the pulse length; about 200 ns of trigger jitter, so repeat each point"])
        add("clock_glitch", available=False, reason="the Nano has no clock glitch (crowbar only)")
        add("clean_traces", settings=[("adc.clk_freq", 7.5e6)], notes=["fixed gain and 8 bits: keep the shunt measurement clean and average more traces", "match adc.clk_freq to a power-of-two multiple of io.clkout"])
    return out


def apply(session, goal: str) -> Dict[str, Any]:
    """Apply a recipe's actions and settings on the connected scope; returns what was done and what the user still has to do."""
    from cwstudio.capabilities import model_of
    model = model_of(session.scope)
    rec = next((r for r in recipes_for(model) if r["id"] == goal), None)
    if rec is None:
        raise ValueError(f"unknown recipe {goal}; one of {', '.join(GOALS)}")
    if not rec["available"]:
        raise ValueError(rec["reason"])
    done: List[str] = []
    for action in rec["actions"]:
        session.scope_action(action)
        done.append(f"action {action}")
    for path, value in rec["settings"]:
        v = session.set_scope_setting(path, value)
        done.append(f"{path} = {v}")
    return {"recipe": goal, "title": rec["title"], "applied": done, "capture": rec["capture"], "notes": rec["notes"]}
