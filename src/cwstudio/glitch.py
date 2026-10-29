"""Glitch parameter sweep implemented as an interleaved long job.

The sweep iterates over a cartesian product of numeric parameter ranges, each bound to a scope settings path (e.g. ``glitch.width``).  For every point it optionally resets the target, arms the scope, sends a SimpleSerial command and classifies the response as ``normal``, ``success`` (valid but unexpected), ``garbled`` (a reply that is not a valid frame), ``reset`` (no reply) or, on a Husky, ``not_fired`` (the glitch counter did not move, so the point measured nothing). A checker (``checkers.py``) can refine that from what the reply means.
"""
from __future__ import annotations

import csv
import itertools
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

from cwstudio import checkers as cwcheckers
from cwstudio import settings as cwsettings
from cwstudio.events import EventBus, trace_event
from cwstudio.worker import LongJob

log = logging.getLogger("cwstudio.glitch")


def _frange(start: float, stop: float, step: float) -> List[float]:
    if step == 0:
        return [start]
    vals = []
    v = start
    n = 0
    sign = 1 if stop >= start else -1
    step = abs(step) * sign
    while (sign > 0 and v <= stop + 1e-9) or (sign < 0 and v >= stop - 1e-9):
        vals.append(round(v, 6))
        n += 1
        v = start + n * step
        if n > 1_000_000:
            break
    return vals


class GlitchJob(LongJob):
    name = "glitch"

    def __init__(self, scope, target, bus: EventBus, params: Dict[str, Any], checker: Optional[cwcheckers.Checker] = None):
        super().__init__()
        self.scope = scope
        self.target = target
        self.bus = bus
        self.p = params
        self.checker = checker  # refines the default classification from what the reply means (see checkers.py)
        self.checker_name = params.get("checker") or "expected"
        self.params = params.get("parameters", [])
        if not self.params:
            raise ValueError("no sweep parameters")
        self.order = params.get("order", "nested")
        self.repeats = max(1, int(params.get("repeats", 1)))
        self.cmd = params.get("command", "g")
        self.data = bytes.fromhex(params.get("data", "") or "")
        self.expected = params.get("expected")  # hex or None
        self.expected_b = bytes.fromhex(self.expected) if self.expected else None
        self.out_len = int(params.get("output_len", 4))
        self.reset_mode = params.get("reset", "nrst")      # none | nrst | pdic
        self.reset_delay = float(params.get("reset_delay", 0.05))
        self.reset_on = params.get("reset_on", "reset")    # never | reset | always
        self.glitch_timeout = int(params.get("glitch_timeout", 1000))
        self.arm_scope = bool(params.get("arm_scope", True))
        # stream each captured trace to the waveform view so the Scope tab animates during a sweep
        self.display = bool(params.get("display", True)) and self.arm_scope
        self.points: List[tuple] = []
        self.i = 0
        self.rep = 0
        self.results: List[Dict[str, Any]] = []
        self.counts = {"normal": 0, "success": 0, "reset": 0, "garbled": 0, "not_fired": 0}
        self._last_progress = 0.0
        from cwstudio.capabilities import model_of
        from cwstudio.profiles import MODEL_PROFILES
        readback = ((MODEL_PROFILES.get(model_of(scope) or "") or {}).get("glitch") or {}).get("readback")
        self._counter = readback == "counter" and hasattr(getattr(scope, "glitch", None), "actual_num_glitches")  # the Husky counts the glitches it emitted

    # --- lifecycle ---------------------------------------------------------
    def start(self):
        if self.target is None:
            raise RuntimeError("no target connected")
        axes = []
        for prm in self.params:
            if "values" in prm and prm["values"]:
                vals = list(prm["values"])
            else:
                vals = _frange(float(prm["start"]), float(prm["stop"]), float(prm.get("step", 1)))
            if prm.get("int"):
                vals = [int(round(v)) for v in vals]
            axes.append(vals)
        self.points = list(itertools.product(*axes))
        if self.order == "random":
            random.shuffle(self.points)
        if not self.points:
            raise ValueError("sweep is empty")
        try:
            self.target.flush()
        except Exception:  # noqa: BLE001
            pass
        self.bus.publish("glitch", {"state": "running", **self.progress()})

    def step(self) -> bool:
        if self.i >= len(self.points):
            return True
        point = self.points[self.i]
        # Apply parameters when starting a new point
        if self.rep == 0:
            for prm, val in zip(self.params, point):
                cwsettings.set_value(self.scope, prm["path"], val)
        outcome, resp, fired, detail = self._one_shot()
        rec = {"i": self.i, "rep": self.rep, "result": outcome, "response": resp,
               "values": list(point), "t": round(time.time() - (self.started_at or 0), 3)}
        if fired is not None:
            rec["fired"] = fired
        if detail:
            rec["detail"] = detail
        self.results.append(rec)
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        self.bus.publish("glitch_result", rec, droppable=False)
        self.rep += 1
        if self.rep >= self.repeats:
            self.rep = 0
            self.i += 1
        now = time.time()
        if now - self._last_progress >= 0.25:
            self._last_progress = now
            self.bus.publish("glitch", {"state": "running", **self.progress()}, droppable=True)
        return self.i >= len(self.points)

    def finish(self):
        state = "error" if self.error else ("stopped" if self.stop_requested and self.i < len(self.points) else "done")
        self.bus.publish("glitch", {"state": state, "error": self.error, **self.progress()})

    def progress(self) -> Dict[str, Any]:
        return {"done": self.i, "total": len(self.points), "point": self.i, "points": len(self.points),
                "repeats": self.repeats,
                "counts": dict(self.counts), "parameters": [p["path"] for p in self.params], "checker": self.checker_name,
                "command": self.cmd, "data": self.data.hex(), "expected": self.expected}

    # --- one glitch attempt -----------------------------------------------
    def _reset_target(self):
        io = getattr(self.scope, "io", None)
        if io is None or self.reset_mode == "none":
            return
        pin = self.reset_mode
        try:
            setattr(io, pin, "low")
            time.sleep(self.reset_delay)
            setattr(io, pin, "high_z")
            time.sleep(self.reset_delay)
        except Exception as e:  # noqa: BLE001
            log.debug("reset failed: %s", e)
        try:
            self.target.flush()
        except Exception:  # noqa: BLE001
            pass

    def _glitches_fired(self) -> Optional[int]:
        if not self._counter:
            return None
        try:
            return int(self.scope.glitch.actual_num_glitches)
        except Exception:  # noqa: BLE001
            return None

    def _one_shot(self):
        if self.reset_on == "always":
            self._reset_target()
        timed_out = False
        fired0 = self._glitches_fired()
        if self.arm_scope:
            self.scope.arm()
        self.target.simpleserial_write(self.cmd, bytearray(self.data))
        if self.arm_scope:
            try:
                timed_out = bool(self.scope.capture())
            except Exception as e:  # noqa: BLE001
                log.debug("capture during glitch raised: %s", e)
                timed_out = True
        val = self.target.simpleserial_read_witherrors("r", self.out_len, glitch_timeout=self.glitch_timeout,
                                                       timeout=50)
        resp_hex = None
        if val and val.get("valid") and val.get("payload") is not None:
            payload = bytes(val["payload"])
            resp_hex = payload.hex()
            if self.expected_b is not None and payload != self.expected_b:
                outcome = "success"
            elif self.expected_b is None and timed_out:
                outcome = "reset"
            else:
                outcome = "normal"
        else:
            resp_hex = (val or {}).get("full_response") if isinstance(val, dict) else None
            outcome = "garbled" if resp_hex else "reset"  # something came back but not a valid frame, or nothing at all
        fired = None
        if fired0 is not None:
            now = self._glitches_fired()
            fired = (now - fired0) if now is not None and now >= fired0 else None
            if fired == 0 and outcome == "normal":
                outcome = "not_fired"  # the target answered normally because no glitch was emitted (trigger source, arm timing or enable), not because it survived one
        detail = None
        if self.checker is not None:
            payload = bytes(val["payload"]) if val and val.get("valid") and val.get("payload") is not None else None
            ctx = {"resp": payload, "hex": payload.hex() if payload is not None else None, "valid": payload is not None, "timed_out": timed_out,
                   "expected": self.expected_b, "value": int.from_bytes(payload, "little") if payload else None,
                   "expected_value": int.from_bytes(self.expected_b, "little") if self.expected_b else None,
                   "fired": fired, "outcome": outcome, "n": len(self.results)}
            outcome, detail = cwcheckers.apply(self.checker, ctx)
        if self.display:
            try:
                wave = self.scope.get_last_trace()
                if wave is not None and len(wave):
                    self.bus.publish_event(trace_event("trace", wave, {"source": "glitch", "result": outcome}, droppable=True))
            except Exception:  # noqa: BLE001  -- a display hiccup must never stop the sweep
                pass
        if outcome in ("reset", "garbled") and self.reset_on in ("reset", "always"):
            self._reset_target()
        return outcome, resp_hex, fired, detail

    def export_csv(self, path: str) -> str:
        path = os.path.expanduser(path)
        if not path.endswith(".csv"):
            path += ".csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["i", "rep"] + [p["path"] for p in self.params] + ["result", "response", "fired", "detail", "t"])
            for r in self.results:
                w.writerow([r["i"], r["rep"]] + r["values"] + [r["result"], r["response"], r.get("fired", ""), r.get("detail", ""), r["t"]])
        return path
