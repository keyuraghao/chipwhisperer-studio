"""Where logic captures come from, and the long job that runs a capture on Studio's hardware thread.

* ``native``: the Husky / Husky Plus built-in logic analyser (``scope.LA``): one capture group of 9 signals, sampling clock from target/usb/pll times the oversampling factor and divided by downsample, a hardware trigger (pin edges, glitch, the ADC capture trigger or manual), no pre-trigger. Optionally the ADC trace captured on the same trigger comes along, on the same time base.
* ``adc``: any ChipWhisperer: a digital line wired to the measure input, captured as one trace (or N traces in a row) with the current scope settings and thresholded with a level and hysteresis.
* ``sigrok``: an external analyser through ``sigrok-cli``.
* ``sim``: the simulator's demo traffic on 18 channels (needs the simulator connected).

Each source is a generator: it does a little hardware work, yields while waiting for a trigger or a process, and returns a :class:`~cwstudio.logic.model.LogicCapture`. :class:`LogicJob` steps the generator from the hardware worker, so Stop works and other short hardware calls still get through.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Callable, Dict, Generator, List, Optional

import numpy as np

from cwstudio.capabilities import HUSKY, LA_GROUPS, Unsupported, model_of
from cwstudio.logic import synth
from cwstudio.logic.model import AnalogChannel, LogicCapture, auto_level, schmitt
from cwstudio.worker import LongJob

log = logging.getLogger("cwstudio.logic")

LA_TRIGGERS = ["capture", "manual", "glitch", "glitch_source", "glitch_trigger", "trigger_glitch", "HS1", "trigger signal 0", "trigger signal 1"] + \
    [f"{e}_tio{n}" for n in range(1, 5) for e in ("rising", "falling")] + [f"{e}_userio_d{n}" for n in range(8) for e in ("rising", "falling")]
LA_TRIGGER_HELP = {
    "capture": "the ADC capture trigger (scope.trigger.triggers); Studio fires the target with a SimpleSerial command",
    "manual": "trigger_now() right after arming: shows whatever is on the pins",
    "glitch": "glitch enable", "glitch_source": "manual glitch trigger, source clock domain", "glitch_trigger": "glitch trigger, MMCM1 domain", "trigger_glitch": "the glitch module's trigger input",
    "HS1": "the HS1 input clock", "trigger signal 0": "trigger signal 0", "trigger signal 1": "trigger signal 1",
}
CLK_SOURCES = ["usb", "target", "pll"]
# capture defaults shared by the Logic tab, the HTTP API and the MCP tools: downsample 96 turns the 96 MHz USB clock into 1 MHz (16 ms at the Husky's full depth, enough for a few UART bytes); 20 analog segments show a burst of traffic
DEFAULTS = {"native": {"downsample": 96, "timeout": 5.0}, "adc": {"segments": 20}}
SIM_LA_MAP = {
    "CW 20-pin": ["UART TX", "UART RX", "1-Wire", "TRIG", "HS1", "HS1", None, "TRIG", "ADC"],
    "USERIO 20-pin": ["SPI CS", "SPI SCK", "SPI MOSI", "SPI MISO", "I2C SCL", "I2C SDA", "CAN", "SWDIO", "SWCLK"],
}


class Stopped(Exception):
    pass


# ----- helpers ------------------------------------------------------------------------------------------
def adc_rate(scope) -> float:
    """The ADC sample rate after decimation, from whatever the scope model exposes."""
    sr = None
    for path in (("clock", "adc_freq"), ("adc", "clk_freq"), ("clock", "adc_rate")):
        try:
            v = getattr(getattr(scope, path[0]), path[1])
            if v and float(v) > 0:
                sr = float(v)
                break
        except Exception:  # noqa: BLE001
            continue
    if sr is None:
        try:
            sr = float(scope.clock.clkgen_freq) * 4
        except Exception:  # noqa: BLE001
            sr = 29.538e6
    try:
        dec = int(getattr(scope.adc, "decimate", 1) or 1)
    except Exception:  # noqa: BLE001
        dec = 1
    return sr / max(dec, 1)


def _num(v, default, cast=float):
    try:
        return default if v is None or v == "" else cast(v)
    except (TypeError, ValueError):
        return default


def fire_target(target, how: str, rng) -> Optional[bytes]:
    """Make the target do something that raises its trigger: a SimpleSerial 'p' with a random plaintext."""
    if how != "simpleserial" or target is None:
        return None
    pt = bytes(rng.integers(0, 256, 16, dtype=np.uint8))
    try:
        if hasattr(target, "flush"):
            target.flush()
        target.simpleserial_write("p", pt)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"could not send the SimpleSerial command to fire the trigger: {e}") from None
    return pt


def _read_response(target, pt) -> Optional[bytes]:
    if pt is None or target is None:
        return None
    try:
        r = target.simpleserial_read("r", 16, timeout=500)
        return bytes(r) if r is not None else None
    except Exception:  # noqa: BLE001
        return None


def _wait(seconds: float, ctx) -> Generator[None, None, None]:
    end = time.time() + seconds
    while time.time() < end:
        if ctx.stop_requested:
            raise Stopped()
        time.sleep(min(0.005, max(0.0, end - time.time())))
        yield


# ----- simulated Husky logic analyser ---------------------------------------------------------------------
class SimLA:
    """The documented ``scope.LA`` API of the Husky, driven by the simulator's demo traffic."""

    _name = "Husky Logic Analyzer Setting (simulated)"

    def __init__(self, scope, plus: bool = False):
        self._scope = scope
        self._is_husky_plus = plus
        self._enabled = False
        self._clk_source = "usb"
        self._osf = 1.0
        self._downsample = 1
        self._group = "CW 20-pin"
        self._depth = self.max_capture_depth
        self._trigger = "capture"
        self._armed = False
        self._manual = False
        self._data: Optional[np.ndarray] = None
        self._count0 = 0
        self.last_window: Optional[Dict[str, Any]] = None

    # --- settings ---
    @property
    def present(self) -> bool:
        return True

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, v):
        self._enabled = bool(v)

    @property
    def clkgen_enabled(self) -> bool:
        return True

    @property
    def max_capture_depth(self) -> int:
        return 65535 if self._is_husky_plus else 16376

    @property
    def capture_depth(self) -> int:
        return self._depth

    @capture_depth.setter
    def capture_depth(self, depth):
        depth = int(depth)
        if depth > self.max_capture_depth:
            raise ValueError("Maximum capture depth is %s" % self.max_capture_depth)
        if depth % 2:
            depth -= 1
        self._depth = max(2, depth)

    @property
    def clk_source(self) -> str:
        return self._clk_source

    @clk_source.setter
    def clk_source(self, v):
        if v not in CLK_SOURCES:
            raise ValueError("Must be one of 'target', 'usb', or 'pll'")
        self._clk_source = v

    @property
    def source_clock_frequency(self) -> float:
        if self._clk_source == "usb":
            return 96e6
        return float(self._scope.clock.clkgen_freq)

    @property
    def oversampling_factor(self) -> float:
        return self._osf

    @oversampling_factor.setter
    def oversampling_factor(self, f):
        f = float(f)
        if f <= 0:
            raise ValueError("oversampling factor must be positive")
        self._osf = f

    @property
    def sampling_clock_frequency(self) -> float:
        return self.source_clock_frequency * self._osf

    @property
    def locked(self) -> bool:
        return 5e6 <= self.sampling_clock_frequency <= 300e6

    @property
    def downsample(self) -> int:
        return self._downsample

    @downsample.setter
    def downsample(self, factor):
        factor = int(factor)
        if factor < 1 or factor > 2 ** 16:
            raise ValueError("Error: downsample value out of range.")
        self._downsample = factor

    @property
    def capture_group(self) -> str:
        return self._group

    @capture_group.setter
    def capture_group(self, g):
        if g not in LA_GROUPS:
            raise ValueError("Invalid capture group")
        self._group = g

    @property
    def trigger_source(self) -> str:
        return self._trigger

    @trigger_source.setter
    def trigger_source(self, v):
        if v not in LA_TRIGGERS or v == "manual":
            raise ValueError("Must be one of 'glitch', 'capture', 'glitch_source', 'HS1', '[rising|falling]_userio_d[0-7]', or [rising|falling]_tio[0-3]")
        self._trigger = v

    @property
    def errors(self):
        return None

    @errors.setter
    def errors(self, v):
        pass

    # --- capture ---
    def _target_count(self) -> int:
        t = getattr(self._scope, "target", None)
        return int(getattr(t, "_trigger_count", 0)) if t is not None else 0

    def arm(self):
        if not self.locked:
            raise Exception("LA clock is not locked! Review your settings. If everything looks good, you may need to re-specify scope.LA.oversampling_factor.")
        self._armed, self._manual, self._data = True, False, None
        self._count0 = self._target_count()

    def trigger_now(self):
        self._manual = True

    def fifo_empty(self) -> bool:
        if self._data is not None:
            return False
        if not self._armed:
            return True
        trig = self._trigger
        edge = trig.startswith(("rising_", "falling_")) or trig == "HS1"
        if not (self._manual or edge or self._target_count() > self._count0):
            return True
        self._render()
        return False

    def _render(self):
        scope = self._scope
        t = getattr(scope, "target", None)
        pt = getattr(t, "_last_pt", b"") if t is not None else b""
        key = getattr(t, "_key", None)
        ct = None
        if pt and key and len(pt) == 16:
            from cwstudio.aes import encrypt_block
            ct = encrypt_block(bytes(key), bytes(pt))
        baud = float(getattr(t, "baud", 38400) or 38400) if t is not None else 38400.0
        sig, _ = synth.demo_signals(baud=baud, pt=bytes(pt) if pt and len(pt) == 16 else None, ct=ct, target_clock_hz=float(scope.clock.clkgen_freq), adc_clock_hz=float(scope.clock.adc_freq))
        sr = self.sampling_clock_frequency / self._downsample
        n = self._depth
        t0 = 0.0
        trig = self._trigger
        if self._manual:
            t0 = -2e-3
        elif trig.startswith(("rising_", "falling_")) or trig == "HS1":
            if trig == "HS1":
                name, rising = "HS1", True
            else:
                rising = trig.startswith("rising_")
                pin = trig.split("_", 1)[1]
                if pin.startswith("tio"):
                    name = SIM_LA_MAP["CW 20-pin"][int(pin[3:]) - 1]
                else:
                    name = SIM_LA_MAP["USERIO 20-pin"][int(pin[-1])]
            s = sig.get(name)
            t0 = None
            if isinstance(s, synth.Line):
                times, vals = s.events()
                hit = np.flatnonzero(vals == (1 if rising else 0))
                t0 = float(times[hit[0]]) if hit.shape[0] else None
            elif isinstance(s, synth.Clock):
                t0 = s.phase + (0 if rising else s.duty / s.freq)
            if t0 is None:
                self._armed = True
                return
        names = self._group_signals(sig)
        rendered = synth.render({f"s{i}": v for i, v in enumerate(names) if v is not None}, sr, t0, n)
        bits = np.zeros((9, n), np.uint8)
        for i in range(9):
            r = rendered.get(f"s{i}")
            if r is None:
                continue
            v0, e = r
            arr = np.zeros(n + 1, np.int8)
            arr[e] = 1
            bits[i] = ((np.cumsum(arr[:n]) & 1) ^ v0).astype(np.uint8)
        self._data = bits
        self._armed = False
        self.last_window = {"t0": t0, "samplerate": sr, "samples": n}

    def _group_signals(self, sig) -> List[Any]:
        if self._group in SIM_LA_MAP:
            return [sig.get(name) if name else None for name in SIM_LA_MAP[self._group]]
        # glitch group: clocks and pulses around the glitch
        g = self._scope.glitch
        f = float(self._scope.clock.clkgen_freq)
        t_gl = (int(g.ext_offset) + 1) / f
        width = max(1, int(g.repeat)) / f
        def pulse(t, w):
            ln = synth.Line(0)
            ln.set(t, 1).set(t + w, 0)
            return ln
        clk = synth.Clock(f)
        return [pulse(t_gl + float(g.offset) / 100 / f, width * max(0.02, abs(float(g.width)) / 100)), clk, synth.Clock(f, phase=float(g.offset) / 100 / f), synth.Clock(f, phase=(float(g.offset) + float(g.width)) / 100 / f),
                pulse(t_gl, width), sig.get("TRIG"), pulse(t_gl - 1 / f, width + 2 / f), synth.Line(0), pulse(t_gl, 1 / f)]

    def read_capture_data(self, check_empty=False):
        assert self.fifo_empty() is False, "FIFO is empty"
        bits = self._data
        self._data = None
        n = bits.shape[1] // 2 * 2
        # each entry holds 2 samples of each signal: signal s in byte s // 4, first sample at bit 2*(s%4)+1, second at bit 2*(s%4)
        entries = np.zeros((n // 2, 3), np.uint8)
        for s in range(9):
            byte, b1 = s // 4, 2 * (s % 4) + 1
            entries[:, byte] |= (bits[s, 0:n:2] << b1) | (bits[s, 1:n:2] << (b1 - 1))
        return [list(map(int, e)) for e in entries]

    @staticmethod
    def extract(raw, index):
        """Same bit layout as the chipwhisperer library's LASettings.extract."""
        if not 0 <= index <= 8:
            raise ValueError
        byte_index, bit1 = index // 4, 2 * (index % 4) + 1
        arr = np.asarray(raw, np.uint8).reshape(-1, 3)[:, byte_index]
        out = np.empty(arr.shape[0] * 2, np.int64)
        out[0::2] = (arr >> bit1) & 1
        out[1::2] = (arr >> (bit1 - 1)) & 1
        return out


class SimMeasure:
    """What the simulated scope's measure input sees during an analog-to-logic capture: one demo signal, as an analog waveform, continuing across segments."""

    def __init__(self, scope, signal: str, seed: Optional[int] = None):
        if signal not in synth.DEMO_CHANNELS:
            raise ValueError(f"the simulator has no signal {signal!r}; choose one of {', '.join(synth.DEMO_CHANNELS)}")
        t = getattr(scope, "target", None)
        baud = float(getattr(t, "baud", 38400) or 38400) if t is not None else 38400.0
        self.signals, _ = synth.demo_signals(baud=baud)
        self.signal = signal
        self.scope = scope
        s = self.signals[signal]
        first = float(s.events()[0][0]) if isinstance(s, synth.Line) and s.times else 0.0
        self.t = first - 20e-6
        self.t_start = self.t
        self.rng = np.random.default_rng(seed)

    def __call__(self, n: int) -> np.ndarray:
        sr = adc_rate(self.scope)
        r = synth.render({"x": self.signals[self.signal]}, sr, self.t, n)["x"]
        arr = np.zeros(n + 1, np.int8)
        arr[r[1]] = 1
        bits = (np.cumsum(arr[:n]) & 1) ^ r[0]
        self.t += n / sr
        return synth.to_analog(bits, self.rng, noise=0.015, rise_samples=max(0.5, sr / 20e6))


# ----- capture sources -------------------------------------------------------------------------------------
def native_capture(job: "LogicJob") -> Generator[None, None, LogicCapture]:
    scope, target, s = job.scope, job.target, job.settings
    la = getattr(scope, "LA", None)
    try:
        present = la is not None and bool(la.present)
    except Exception:  # noqa: BLE001
        present = False
    if not present:
        raise Unsupported("the Husky logic analyser component is not available")
    sim = bool(getattr(scope, "sim_model", None))
    group = s.get("group") or "CW 20-pin"
    if group not in LA_GROUPS:
        raise ValueError(f"capture group must be one of {', '.join(LA_GROUPS)}")
    trig = s.get("trigger") or "capture"
    if trig not in LA_TRIGGERS:
        raise ValueError(f"unknown LA trigger {trig!r}")
    job.phase = "configuring"
    if not la.enabled:
        la.enabled = True
    clk = s.get("clk_source") or "usb"
    if clk not in CLK_SOURCES:
        raise ValueError("clock source must be usb, target or pll")
    clk_changed = la.clk_source != clk
    if clk_changed:
        la.clk_source = clk
        if not sim:
            yield from _wait(0.25, job)  # chipwhisperer 6.0.0 does not wait for the new source frequency itself (later versions sleep 0.25 s in the setter)
    osf = _num(s.get("oversampling"), 1.0)
    if osf <= 0:
        raise ValueError("the oversampling factor must be positive")
    # the MMCM settings depend on the source frequency, so a new clock source needs the factor written again; the getter rounds down to a whole number, so a fractional factor is always written
    if clk_changed or abs(float(la.oversampling_factor) - osf) > 1e-9:
        la.oversampling_factor = osf
        if not sim:
            end = time.time() + 1.0
            while not la.locked and time.time() < end:
                yield from _wait(0.02, job)
    ds = int(_num(s.get("downsample"), DEFAULTS["native"]["downsample"], int))
    if int(la.downsample) != ds:
        la.downsample = ds
    la.capture_group = group
    maxd = int(getattr(la, "max_capture_depth", 65535 if model_of(scope) == "huskyplus" else 16376))
    depth = max(2, min(int(_num(s.get("depth"), maxd, int)), maxd))
    la.capture_depth = depth
    if trig != "manual":
        la.trigger_source = trig
    if group == "USERIO 20-pin" and not sim and s.get("userio_inputs", True):
        try:
            scope.userio.mode = "normal"
            scope.userio.direction = 0
        except Exception as e:  # noqa: BLE001
            job.warnings.append(f"could not make the USERIO pins inputs: {e}")
    if not la.locked:
        raise RuntimeError(f"the logic analyser clock is not locked at {la.sampling_clock_frequency / 1e6:.3f} MHz; change the clock source or oversampling factor")
    try:
        la.errors = 0
    except Exception:  # noqa: BLE001
        pass
    with_analog = bool(s.get("with_analog")) and trig == "capture"
    # The "capture" trigger is the ADC capture trigger, which only reaches the logic analyser while the
    # ADC is armed, so arm it whenever that trigger is selected (keep the analog trace only if asked).
    arm_adc = trig == "capture"
    fire = s.get("fire") or ("simpleserial" if trig in ("capture", "trigger_glitch", "glitch", "glitch_source", "glitch_trigger") and target is not None else "none")
    job.phase = "armed"
    if arm_adc:
        scope.arm()
    la.arm()
    pt = None
    if trig == "manual":
        la.trigger_now()
    else:
        pt = fire_target(target, fire, job.rng)
    timed_out = None
    if arm_adc:
        timed_out = scope.capture()
    timeout = _num(s.get("timeout"), DEFAULTS["native"]["timeout"])
    deadline = time.time() + timeout
    job.phase = "waiting for trigger"
    while la.fifo_empty():
        if job.stop_requested:
            raise Stopped()
        if time.time() > deadline:
            raise RuntimeError(f"no logic analyser trigger within {timeout:g} s (trigger {trig}); try the manual trigger to check the setup")
        time.sleep(0.002)
        yield
    sr = float(la.sampling_clock_frequency) / int(la.downsample)
    # the FIFO fills at the sample rate after the trigger: reading before the whole depth is in underflows it, so wait the full capture time (slow rates with a big downsample take seconds; Stop still works)
    wait = depth / sr * 1.02 + 0.002
    yield from _wait(min(wait, 0.5) if sim else wait, job)
    job.phase = "reading"
    raw = la.read_capture_data()
    try:
        errs = la.errors
    except Exception:  # noqa: BLE001
        errs = None
    names = LA_GROUPS[group]
    bits = {}
    for i, name in enumerate(names):
        b = np.asarray(la.extract(raw, i), dtype=np.uint8)
        bits[name] = b
    n = min(b.shape[0] for b in bits.values())
    bits = {k: v[:n] for k, v in bits.items()}
    _read_response(target, pt)
    cap = LogicCapture.from_bits(bits, sr, 0, source="native", name=f"Husky LA {group}")
    if sr > 250e6:
        job.warnings.append(f"{sr / 1e6:.1f} MHz is above the 250 MHz the Husky logic analyser is specified for")
    if errs:
        job.warnings.append(f"FIFO errors: {errs}")
    if with_analog:
        if timed_out:
            job.warnings.append("the ADC timed out, so there is no analog trace")
        else:
            wave = np.asarray(scope.get_last_trace(), dtype=np.float32).copy()
            asr = adc_rate(scope)
            pre = int(_num(getattr(scope.adc, "presamples", 0), 0, int))
            off = int(_num(getattr(scope.adc, "offset", 0), 0, int))
            cap.analog.append(AnalogChannel("ADC trace", wave, asr, t0=(off - pre) / asr))
    cap.meta.update({"group": group, "clk_source": clk, "oversampling": float(la.oversampling_factor), "downsample": int(la.downsample), "sampling_clock": float(la.sampling_clock_frequency),
                     "depth": n, "trigger_source": trig, "fire": fire, "errors": errs, "pretrigger": False, "plaintext": pt.hex() if pt else None})
    return cap


def adc_capture(job: "LogicJob") -> Generator[None, None, LogicCapture]:
    scope, target, s = job.scope, job.target, job.settings
    sim = bool(getattr(scope, "sim_model", None))
    segments = max(1, min(int(_num(s.get("segments"), DEFAULTS["adc"]["segments"], int)), 2000))
    old_samples = None
    hook = None
    if s.get("samples"):
        old_samples = int(scope.adc.samples)
        scope.adc.samples = int(s["samples"])
    if sim:
        hook = SimMeasure(scope, s.get("sim_signal") or "UART TX", seed=s.get("seed"))
        scope._measure_hook = hook
    fire = s.get("fire") or "none"
    traces = []
    try:
        for k in range(segments):
            if job.stop_requested:
                raise Stopped()
            job.phase = f"segment {k + 1}/{segments}"
            scope.arm()
            pt = fire_target(target, fire, job.rng)
            timed_out = scope.capture()
            if timed_out:
                if not traces:
                    raise RuntimeError("the scope timed out waiting for its trigger; check scope.trigger.triggers and the wiring, or fire the target with a SimpleSerial command")
                job.warnings.append(f"segment {k + 1} timed out; kept {len(traces)} segments")
                break
            traces.append(np.asarray(scope.get_last_trace(), dtype=np.float32).copy())
            _read_response(target, pt)
            yield
    finally:
        if old_samples is not None:
            try:
                scope.adc.samples = old_samples
            except Exception:  # noqa: BLE001
                pass
        if hook is not None:
            scope._measure_hook = None
    data = np.concatenate(traces)
    sr = adc_rate(scope)
    lvl = s.get("level", "auto")
    level, hyst = auto_level(data)
    if lvl not in (None, "", "auto"):
        level = float(lvl)
    # "auto" hysteresis is 10 % of the signal swing whether the threshold is automatic or a number
    if s.get("hysteresis") not in (None, "", "auto"):
        hyst = float(s["hysteresis"])
    bits = schmitt(data, level, hyst)
    if s.get("invert"):
        bits = 1 - bits
    pre = int(_num(getattr(scope.adc, "presamples", 0), 0, int))
    off = int(_num(getattr(scope.adc, "offset", 0), 0, int))
    trigger = pre - off
    name = s.get("name") or ((s.get("sim_signal") or "UART TX") if sim else "ADC logic")
    cap = LogicCapture.from_bits({name: bits.astype(np.uint8)}, sr, trigger, source="adc", name=f"Analog input ({name})")
    cap.analog.append(AnalogChannel("ADC input", data, sr, t0=-trigger / sr, threshold=[level, hyst]))
    seg = traces[0].shape[0]
    cap.meta.update({"segments": len(traces), "segment_samples": seg, "segment_bounds": [k * seg for k in range(1, len(traces))], "level": level, "hysteresis": hyst, "fire": fire,
                     "note": "segments are separate triggers placed end to end; time between them is not captured" if not sim and len(traces) > 1 else None})
    return cap


def sigrok_capture(job: "LogicJob") -> Generator[None, None, LogicCapture]:
    from cwstudio.logic import formats, sigrok
    s = job.settings
    st = sigrok.status()
    if not st["available"]:
        raise Unsupported(st["reason"])
    out = os.path.join(job.out_dir, f"sigrok-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.sr")
    os.makedirs(job.out_dir, exist_ok=True)
    chans = s.get("channels")
    if isinstance(chans, str):
        chans = [c.strip() for c in chans.split(",") if c.strip()]
    args = sigrok.capture_args(s.get("device") or "", out, _num(s.get("samplerate"), None), chans or None, _num(s.get("samples"), None, int), _num(s.get("time_ms"), None), s.get("triggers") or None, s.get("config") or None)
    job.phase = "capturing (sigrok-cli)"
    proc = sigrok.start_capture(args)
    timeout = _num(s.get("timeout"), 60.0)
    deadline = time.time() + timeout
    try:
        while proc.poll() is None:
            if job.stop_requested:
                raise Stopped()
            if time.time() > deadline:
                raise RuntimeError(f"sigrok-cli did not finish within {timeout:g} s (waiting for a trigger?)")
            time.sleep(0.01)
            yield
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
    err = (proc.stderr.read() if proc.stderr else "") or ""
    if proc.returncode != 0:
        lines = err.strip().splitlines()
        raise RuntimeError(f"sigrok-cli failed: {lines[-1] if lines else 'exit code %d' % proc.returncode}")
    if not os.path.exists(out):
        raise RuntimeError("sigrok-cli wrote no capture file")
    cap = formats.read_sr(out)
    cap.source = "sigrok"
    cap.name = f"sigrok {s.get('device')}"
    cap.meta.update({"device": s.get("device"), "file": out, "args": args})
    return cap


def sim_capture(job: "LogicJob") -> Generator[None, None, LogicCapture]:
    scope, target, s = job.scope, job.target, job.settings
    if not getattr(scope, "sim_model", None):
        raise Unsupported("the simulated logic source needs the simulator connected")
    sr = _num(s.get("samplerate"), 4e6)
    if sr <= 0 or sr > 1e9:
        raise ValueError("sample rate must be between 1 Hz and 1 GHz")
    if s.get("samples"):
        n = int(s["samples"])
    else:
        n = int(round(_num(s.get("duration_ms"), 25.0) * 1e-3 * sr))
    if not 16 <= n <= 100_000_000:
        raise ValueError("between 16 samples and 100 million samples per capture")
    pre = min(max(_num(s.get("pretrigger"), 40.0), 0.0), 100.0) / 100.0
    t_start = -pre * n / sr
    names = s.get("channels") or list(synth.DEMO_CHANNELS)
    bad = [x for x in names if x not in synth.DEMO_CHANNELS]
    if bad:
        raise ValueError(f"unknown simulator channel(s) {', '.join(bad)}; choose from {', '.join(synth.DEMO_CHANNELS)}")
    pt = fire_target(target, s.get("fire") or "none", job.rng)
    ct = _read_response(target, pt)
    baud = float(getattr(target, "baud", 38400) or 38400) if target is not None else 38400.0
    sig, desc = synth.demo_signals(baud=_num(s.get("baud"), baud), pt=pt, ct=ct, clock_hz=_num(s.get("clock_hz"), 1e6), ss_version=str(s.get("ss_version") or "1.1"))
    job.phase = "generating"
    yield
    edges = synth.render({k: sig[k] for k in names}, sr, t_start, n, jitter=_num(s.get("jitter_ns"), 0.0) * 1e-9, glitch_rate=_num(s.get("glitches_per_ms"), 0.0) * 1e3, seed=s.get("seed"))
    cap = LogicCapture.from_edges(edges, n, sr, int(round(-t_start * sr)), source="sim", name="Simulator demo traffic")
    cap.meta.update({"baud": desc["baud"], "plaintext": desc["pt"], "ciphertext": desc["ct"], "pretrigger": pre * 100, "jitter_ns": _num(s.get("jitter_ns"), 0.0), "glitches_per_ms": _num(s.get("glitches_per_ms"), 0.0)})
    return cap


SOURCES: Dict[str, Callable[["LogicJob"], Generator[None, None, LogicCapture]]] = {"native": native_capture, "adc": adc_capture, "sigrok": sigrok_capture, "sim": sim_capture}


class LogicJob(LongJob):
    """One logic capture on the hardware thread."""

    name = "logic"

    def __init__(self, source: str, settings: Dict[str, Any], scope, target, out_dir: str, on_done: Callable[["LogicJob"], None], publish: Callable[[str, Dict[str, Any]], None]):
        super().__init__()
        if source not in SOURCES:
            raise ValueError(f"unknown logic source {source!r}; choose one of {', '.join(SOURCES)}")
        self.source = source
        self.settings = dict(settings or {})
        self.scope, self.target = scope, target
        self.out_dir = out_dir
        self.on_done = on_done
        self.publish = publish
        self.phase = "starting"
        self.warnings: List[str] = []
        self.capture: Optional[LogicCapture] = None
        self.stopped = False
        self.rng = np.random.default_rng(self.settings.get("seed"))
        self._gen = None

    def start(self):
        self.publish("la", {"kind": "job", "state": "running", "source": self.source, "phase": self.phase})
        self._gen = SOURCES[self.source](self)

    def step(self) -> bool:
        try:
            next(self._gen)
            return False
        except StopIteration as e:
            self.capture = e.value
            return True
        except Stopped:
            self.stopped = True
            return True

    def request_stop(self):
        super().request_stop()

    def finish(self):
        if self._gen is not None:
            try:
                self._gen.close()
            except Exception:  # noqa: BLE001
                pass
        if self.stop_requested and self.capture is None:
            self.stopped = True
        self.on_done(self)

    def progress(self) -> Dict[str, Any]:
        return {"source": self.source, "phase": self.phase}
