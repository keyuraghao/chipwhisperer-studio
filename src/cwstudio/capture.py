"""Capture loop implemented as an interleaved long job."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import numpy as np

from cwstudio.events import EventBus, trace_event
from cwstudio.traces import TraceStore
from cwstudio.worker import LongJob

log = logging.getLogger("cwstudio.capture")


class KeyTextGen:
    """Fixed/random key & text generator (equivalent of cw.ktp.Basic)."""

    def __init__(self, key_mode="fixed", text_mode="random", key: Optional[bytes] = None,
                 text: Optional[bytes] = None, length: int = 16, seed: Optional[int] = None):
        self.key_mode = key_mode
        self.text_mode = text_mode
        self.length = length
        self.key = key if key else bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")[:length]
        self.text = text if text else bytes.fromhex("00112233445566778899aabbccddeeff")[:length]
        self.rng = np.random.default_rng(seed)
        self.fixed_text = self.text  # what the tvla mode alternates with random texts
        self._tvla_toggle = False

    def _rand(self) -> bytes:
        return self.rng.integers(0, 256, self.length, dtype=np.uint8).tobytes()

    def next_pair(self):
        if self.key_mode == "random":
            self.key = self._rand()
        if self.text_mode == "random":
            self.text = self._rand()
        elif self.text_mode == "counter":
            v = int.from_bytes(self.text, "big") + 1
            self.text = (v % (1 << (8 * self.length))).to_bytes(self.length, "big")
        elif self.text_mode == "tvla":  # fixed versus random, interleaved, for the t-test
            self._tvla_toggle = not self._tvla_toggle
            self.text = self.fixed_text if self._tvla_toggle else self._rand()
        return self.key, self.text


class CaptureJob(LongJob):
    name = "capture"

    def __init__(self, scope, target, store: TraceStore, bus: EventBus, params: Dict[str, Any]):
        super().__init__()
        self.scope = scope
        self.target = target
        self.store = store
        self.bus = bus
        self.p = params
        self.count = int(params.get("count", 0) or 0)       # 0 = continuous
        self.store_traces = bool(params.get("store", True))
        self.mode = params.get("mode", "simpleserial")      # simpleserial | trigger_only
        self.ack = bool(params.get("ack", True))
        self.max_timeouts = int(params.get("max_timeouts", 10))
        self.display_rate = float(params.get("display_rate", 25.0))
        self.min_period = 1.0 / max(0.5, float(params.get("max_rate", 0) or 1e9))  # optional throttle
        self.gen = KeyTextGen(
            key_mode=params.get("key_mode", "fixed"),
            text_mode=params.get("text_mode", "random"),
            key=_hex(params.get("key")),
            text=_hex(params.get("text")),
            length=int(params.get("length", 16)),
            seed=params.get("seed"),
        )
        self.done_count = 0
        self.timeouts = 0
        self.consecutive_timeouts = 0
        self._last_pub = 0.0
        self._last_progress = 0.0
        self._last_step = 0.0
        self.rate = 0.0
        self._rate_t0 = time.time()
        self._rate_n0 = 0
        self.last_wave: Optional[np.ndarray] = None
        self.last_index = -1
        # per model: the Husky captures adc.segments traces per arm (split into stored traces here) and reports sticky capture errors after each capture
        from cwstudio.capabilities import model_of
        from cwstudio.profiles import MODEL_PROFILES
        prof = (MODEL_PROFILES.get(model_of(scope) or "") or {}).get("adc", {})
        self.segments = 1
        if prof.get("segments"):
            try:
                self.segments = max(1, int(getattr(scope.adc, "segments", 1)))
            except (AttributeError, TypeError, ValueError):
                self.segments = 1
        self._reads_errors = bool(prof.get("errors")) and hasattr(getattr(scope, "adc", None), "errors")
        self.hw_errors: Optional[str] = None  # the last capture error the hardware reported (Husky adc.errors)
        self.hw_error_count = 0

    def start(self):
        if self.mode == "simpleserial" and self.target is None:
            raise RuntimeError("no target connected (use trigger-only mode to capture without a target)")
        if self.gen.text_mode == "tvla" and self.store_traces:
            self.store.meta["tvla_fixed_text"] = self.gen.fixed_text.hex()  # the t-test groups the traces by this plaintext
        self.bus.publish("capture", {"state": "running", **self.progress()})
        if self.mode == "simpleserial" and hasattr(self.target, "flush"):
            try:
                self.target.flush()
            except Exception:  # noqa: BLE001
                pass

    def step(self) -> bool:
        if self.count and self.done_count >= self.count:
            return True
        if self.min_period > 0:
            wait = self._last_step + self.min_period - time.time()
            if wait > 0:
                time.sleep(min(wait, 0.05))
                return False
        self._last_step = time.time()
        try:
            if self.segments > 1:
                traces = self._capture_segmented()
            elif self.mode == "trigger_only":
                traces = self._capture_trigger_only()
            else:
                traces = self._capture_simpleserial()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            log.error("capture error: %s", self.error)
            return True
        self._read_errors()
        if traces is None:
            self.timeouts += 1
            self.consecutive_timeouts += 1
            why = f" ({self.hw_errors})" if self.hw_errors else ""
            self.bus.publish("log", {"level": "WARNING", "logger": "capture",
                                     "msg": f"capture timeout ({self.consecutive_timeouts} in a row){why}"})
            if self.consecutive_timeouts >= self.max_timeouts:
                self.error = f"{self.max_timeouts} consecutive timeouts - check trigger and target" + why
                return True
            return False
        self.consecutive_timeouts = 0
        if isinstance(traces, tuple):
            traces = [traces]
        for wave, tin, tout, key in traces:
            idx = -1
            if self.store_traces:
                idx = self.store.append(wave, tin, tout, key)
            self.done_count += 1
            self.last_wave = wave
            self.last_index = idx
            if self.count and self.done_count >= self.count:
                break
        wave, idx = self.last_wave, self.last_index
        tin, tout, key = traces[-1][1], traces[-1][2], traces[-1][3]
        now = time.time()
        if now - self._rate_t0 >= 1.0:
            self.rate = (self.done_count - self._rate_n0) / (now - self._rate_t0)
            self._rate_t0, self._rate_n0 = now, self.done_count
        if now - self._last_pub >= 1.0 / self.display_rate or (self.count and self.done_count == self.count):
            self._last_pub = now
            self.bus.publish_event(trace_event("trace", wave, {
                "index": idx, "count": self.done_count, "textin": tin, "textout": tout, "key": key,
                "generation": self.store.generation, "stored": self.store_traces,
            }))
        if now - self._last_progress >= 0.25:
            self._last_progress = now
            self.bus.publish("capture", {"state": "running", **self.progress()}, droppable=True)
        return bool(self.count and self.done_count >= self.count)

    def finish(self):
        state = "error" if self.error else ("stopped" if self.stop_requested and not
                                             (self.count and self.done_count >= self.count) else "done")
        if self.last_wave is not None:
            self.bus.publish_event(trace_event("trace", self.last_wave, {
                "index": self.last_index, "count": self.done_count, "generation": self.store.generation,
                "stored": self.store_traces,
            }, droppable=False))
        self.bus.publish("capture", {"state": state, "error": self.error, **self.progress()})

    def progress(self) -> Dict[str, Any]:
        return {
            "done": self.done_count, "total": self.count, "target": self.count, "timeouts": self.timeouts,
            "rate": round(self.rate, 1), "stored": len(self.store), "mode": self.mode, "segments": self.segments,
            "hw_errors": self.hw_errors, "hw_error_count": self.hw_error_count,
            "elapsed": round((getattr(self, "ended_at", None) or time.time()) - (self.started_at or time.time()), 1),  # frozen once the job has ended
        }

    def _read_errors(self):
        """What the Husky's capture error register says after this capture (sticky until the next arm): kept for the status and the capture banner, logged once per distinct message."""
        if not self._reads_errors:
            return
        try:
            e = self.scope.adc.errors
        except Exception:  # noqa: BLE001
            return
        text = "" if e in (None, 0, False, "0", "False", "") else str(e).strip()
        if not text:
            return
        self.hw_error_count += 1
        if text != self.hw_errors:
            self.hw_errors = text
            self.bus.publish("log", {"level": "WARNING", "logger": "capture", "msg": f"the scope reported a capture error: {text}"})

    # --- capture primitives ------------------------------------------------
    def _capture_simpleserial(self):
        import chipwhisperer as cw
        key, text = self.gen.next_pair()
        tr = cw.capture_trace(self.scope, self.target, bytearray(text), bytearray(key), ack=self.ack)
        if tr is None:
            return None
        return (np.asarray(tr.wave, np.float32), bytes(tr.textin or b""), bytes(tr.textout or b""),
                bytes(tr.key or b""))

    def _capture_trigger_only(self):
        self.scope.arm()
        timed_out = self.scope.capture()
        if timed_out:
            return None
        wave = self.scope.get_last_trace()
        if wave is None or len(wave) == 0:
            return None
        return np.asarray(wave, np.float32), b"", b"", b""

    def _capture_segmented(self):
        """One arm, adc.segments triggers (one SimpleSerial command each, or the target's own in trigger-only mode), one download, split into one trace per segment: the Husky's fast path."""
        seg = self.segments
        pairs = []
        if self.mode == "simpleserial":
            pairs = [self.gen.next_pair() for _ in range(seg)]
        self.scope.arm()
        out_len = int(getattr(self.target, "output_len", 16) or 16)
        texts_out = []
        last_key = None
        for key, text in pairs:
            if key != last_key:
                self.target.set_key(bytearray(key), ack=self.ack)
                last_key = key
            self.target.simpleserial_write("p", bytearray(text))
            try:
                r = self.target.simpleserial_read("r", out_len, ack=self.ack)
            except Exception:  # noqa: BLE001  a missed reply is a timeout for that segment, not a crash
                r = None
            texts_out.append(bytes(r) if r is not None else b"")
        timed_out = self.scope.capture()
        if timed_out:
            return None
        wave = self.scope.get_last_trace()
        if wave is None or len(wave) == 0:
            return None
        wave = np.asarray(wave, np.float32)
        n = len(wave) // seg
        if n == 0 or len(wave) != n * seg:
            raise RuntimeError(f"segmented capture returned {len(wave)} samples, not {seg} segments of equal length")
        traces = []
        for i in range(seg):
            key, text = pairs[i] if pairs else (b"", b"")
            traces.append((wave[i * n:(i + 1) * n].copy(), bytes(text), texts_out[i] if texts_out else b"", bytes(key)))
        return traces


def _hex(s: Optional[str]) -> Optional[bytes]:
    if not s:
        return None
    s = s.replace(" ", "").replace(":", "")
    return bytes.fromhex(s)
