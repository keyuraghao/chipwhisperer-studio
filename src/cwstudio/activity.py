"""What analysis Studio is doing right now, for the header and the API.

Long jobs on the hardware (capture, glitch sweep) have their own status. Analyses run elsewhere: the CPA in its thread, the code map build in a request's executor, the t-test and the DFA in a request. ``Activity`` keeps one list of what is in progress, with a phase and a progress figure each, and the last thing that finished, and publishes every change as an ``analysis`` event so every window shows the same state.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional


class Activity:
    def __init__(self, publish: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self._lock = threading.Lock()
        self._publish = publish
        self._active: Dict[int, Dict[str, Any]] = {}
        self._next = 1
        self.last: Optional[Dict[str, Any]] = None
        self._last_pub = 0.0

    # --- lifecycle -------------------------------------------------------------------------------
    def begin(self, name: str, phase: str = "running", detail: Optional[str] = None) -> int:
        with self._lock:
            i = self._next
            self._next += 1
            self._active[i] = {"id": i, "name": name, "phase": phase, "detail": detail, "progress": None, "started": time.time()}
        self._emit(force=True)
        return i

    def update(self, i: int, phase: Optional[str] = None, detail: Optional[str] = None, progress: Optional[float] = None, **extra) -> None:
        with self._lock:
            a = self._active.get(i)
            if a is None:
                return
            if phase is not None:
                a["phase"] = phase
            if detail is not None:
                a["detail"] = detail
            if progress is not None:
                a["progress"] = max(0.0, min(1.0, float(progress)))
            a.update(extra)
        self._emit(force=phase is not None)

    def end(self, i: int, error: Optional[str] = None, detail: Optional[str] = None) -> None:
        with self._lock:
            a = self._active.pop(i, None)
            if a is None:
                return
            now = time.time()
            self.last = {"name": a["name"], "state": "error" if error else "done", "error": error, "detail": detail if detail is not None else a.get("detail"),
                         "seconds": round(now - a["started"], 2), "finished": now}
        self._emit(force=True)

    @contextmanager
    def track(self, name: str, phase: str = "running", detail: Optional[str] = None) -> Iterator[Callable[..., None]]:
        """``with activity.track("CPA") as update:`` marks the block as in progress and done (or failed with the exception's text) when it ends; ``update(phase=..., detail=..., progress=...)`` reports along the way."""
        i = self.begin(name, phase, detail)
        try:
            yield lambda **kw: self.update(i, **kw)
        except BaseException as e:
            self.end(i, error=f"{type(e).__name__}: {e}")
            raise
        else:
            self.end(i)

    # --- state -------------------------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            active: List[Dict[str, Any]] = [{**a, "elapsed": round(now - a["started"], 1)} for a in self._active.values()]
            return {"active": active, "busy": bool(active), "last": dict(self.last) if self.last else None}

    def _emit(self, force: bool = False) -> None:
        if self._publish is None:
            return
        now = time.time()
        if not force and now - self._last_pub < 0.25:  # progress ticks: a few a second at most
            return
        self._last_pub = now
        try:
            self._publish("analysis", self.status())
        except Exception:  # noqa: BLE001  a closed bus must never break the analysis itself
            pass
