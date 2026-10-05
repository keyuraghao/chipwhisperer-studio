"""The single hardware thread.

`chipwhisperer` scope/target objects are not thread-safe and the USB transport keeps state between calls, so every hardware access in Studio is funnelled through one `HardwareWorker` thread.

Two kinds of work are supported:

* **short jobs** - a callable submitted with `submit()`/`call()`; runs to completion and resolves a `Future`.
* **one long job** - an object with a `step()` method (see `LongJob`).  The worker calls `step()` repeatedly and services queued short jobs between steps, so the UI stays responsive during a long capture or glitch sweep.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("cwstudio.worker")


class HardwareTimeout(TimeoutError):
    """A hardware call did not finish in time and is still running on the hardware thread."""


class HardwareBusy(RuntimeError):
    """The hardware thread is occupied (or stuck) with another job, so this call was not run."""


def _job_name(fn: Callable) -> str:
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)
    return name.replace("<locals>.", "")


class LongJob:
    """Base class for interleaved long-running hardware jobs."""

    name = "job"

    def __init__(self):
        self._stop = threading.Event()
        self.error: Optional[str] = None
        self.finished = threading.Event()
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None

    # --- to be implemented by subclasses ---------------------------------
    def start(self) -> None:
        """Called once on the worker thread before the first step."""

    def step(self) -> bool:
        """Do a small unit of work. Return True when the job is complete."""
        raise NotImplementedError

    def finish(self) -> None:
        """Called once on the worker thread after completion, stop or error."""

    def progress(self) -> Dict[str, Any]:
        return {}

    # --- control ----------------------------------------------------------
    def request_stop(self):
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()


class PeriodicTask:
    __slots__ = ("name", "fn", "interval", "next_run", "only_idle")

    def __init__(self, name: str, fn: Callable[[], None], interval: float, only_idle: bool):
        self.name = name
        self.fn = fn
        self.interval = interval
        self.next_run = 0.0
        self.only_idle = only_idle


class HardwareWorker:
    def __init__(self):
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="cw-hardware", daemon=True)
        self._running = False
        self._long: Optional[LongJob] = None
        self._long_lock = threading.Lock()
        self._periodic: List[PeriodicTask] = []
        self._periodic_lock = threading.Lock()
        self.on_long_job_done: Optional[Callable[[LongJob], None]] = None
        self.on_error: Optional[Callable[[BaseException], None]] = None  # called on the hardware thread with every exception a job, step or periodic task raised (Studio uses it to notice an unplugged scope)
        self.busy = False
        self._current: Optional[tuple] = None  # (name, monotonic start, future or None) of what the hardware thread is executing right now
        self._stuck: Optional[tuple] = None  # the running entry a caller gave up on: new calls fail fast until it returns

    # --- lifecycle --------------------------------------------------------
    def start(self):
        self._running = True
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._running = False
        self.stop_long_job()
        self._q.put((None, None, None, None))
        self._thread.join(timeout=timeout)

    @property
    def is_worker_thread(self) -> bool:
        return threading.current_thread() is self._thread

    # --- short jobs -------------------------------------------------------
    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        return self._submit(fn, args, kwargs, None)

    def _submit(self, fn: Callable, args: tuple, kwargs: dict, name: Optional[str]) -> Future:
        fut: Future = Future()
        fut.cw_name = name or _job_name(fn)  # what the busy and stuck messages call it
        if self.is_worker_thread:
            # Re-entrant call from a job on the worker thread: run inline.
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as e:  # noqa: BLE001
                fut.set_exception(e)
            return fut
        self._q.put((fn, args, kwargs, fut))
        return fut

    def running(self) -> Optional[Dict[str, Any]]:
        """What the hardware thread is executing now: name, seconds since it started, and whether a caller gave up waiting on it (stuck)."""
        cur, stuck = self._current, self._stuck
        if cur is None:
            return None
        return {"name": cur[0], "seconds": round(time.monotonic() - cur[1], 1), "stuck": stuck is not None and stuck is cur}

    def stuck(self) -> Optional[Dict[str, Any]]:
        cur = self.running()
        return cur if cur and cur["stuck"] else None

    def _busy_message(self, cur: tuple) -> str:
        return f"the hardware thread is busy with {cur[0]} since {time.monotonic() - cur[1]:.0f} s"

    def call(self, fn: Callable, *args, timeout: Optional[float] = 120.0, job_name: Optional[str] = None, **kwargs) -> Any:
        """Run `fn` on the hardware thread and wait for its result.

        Raises :class:`HardwareBusy` at once while an earlier job a caller already gave up on is still running (the USB transfer is stuck), and when `fn` waited its whole `timeout` in the queue behind another job. Raises :class:`HardwareTimeout` when `fn` itself started but did not finish within `timeout`; the thread is then marked stuck until it returns."""
        name = job_name or _job_name(fn)
        if not self.is_worker_thread:
            stuck = self._stuck
            if stuck is not None and stuck is self._current:
                raise HardwareBusy(f"{self._busy_message(stuck)} and has not returned; {name} was not run. Unplug the ChipWhisperer, plug it back in and restart Studio if it does not recover")
        fut = self._submit(fn, args, kwargs, name)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            if fut.done():  # finished just now, or fn itself raised a TimeoutError
                return fut.result()
            if fut.cancel():  # never started: it waited behind another job the whole time
                cur = self._current
                raise HardwareBusy(f"{self._busy_message(cur) if cur else 'the hardware thread is busy'}; {name} waited {timeout:g} s and was not run") from None
            cur = self._current
            if cur is not None and cur[2] is fut:
                self._stuck = cur
                log.warning("%s did not return within %g s; the hardware thread is stuck until it does", name, timeout)
            raise HardwareTimeout(f"the ChipWhisperer did not respond within {timeout:g} s ({name}); unplug it, plug it back in and restart Studio") from None

    # --- long job ---------------------------------------------------------
    def start_long_job(self, job: LongJob) -> bool:
        with self._long_lock:
            if self._long is not None and not self._long.finished.is_set():
                return False
            job.started_at = time.time()
            self._long = job
            self._q.put((self._begin_long, (job,), {}, Future()))
            return True

    def _begin_long(self, job: LongJob):
        try:
            job.start()
        except BaseException as e:  # noqa: BLE001
            job.error = f"{type(e).__name__}: {e}"
            log.error("Long job %s failed to start: %s", job.name, job.error)
            log.debug(traceback.format_exc())
            self._end_long(job)

    def _end_long(self, job: LongJob):
        try:
            job.finish()
        except BaseException as e:  # noqa: BLE001
            log.error("Long job %s finish() failed: %s", job.name, e)
        job.ended_at = time.time()
        job.finished.set()
        with self._long_lock:
            if self._long is job:
                self._long = None
        if self.on_long_job_done:
            try:
                self.on_long_job_done(job)
            except Exception:  # noqa: BLE001
                log.exception("on_long_job_done failed")

    def stop_long_job(self):
        with self._long_lock:
            job = self._long
        if job is not None:
            job.request_stop()

    @property
    def long_job(self) -> Optional[LongJob]:
        with self._long_lock:
            return self._long

    # --- periodic tasks ---------------------------------------------------
    def add_periodic(self, name: str, fn: Callable[[], None], interval: float, only_idle: bool = True):
        with self._periodic_lock:
            self._periodic = [p for p in self._periodic if p.name != name]
            self._periodic.append(PeriodicTask(name, fn, interval, only_idle))

    def remove_periodic(self, name: str):
        with self._periodic_lock:
            self._periodic = [p for p in self._periodic if p.name != name]

    # --- main loop --------------------------------------------------------
    def _run(self):
        while self._running:
            long = self.long_job
            active_long = long is not None and not long.finished.is_set()
            try:
                item = self._q.get(timeout=0.0 if active_long else 0.05)
            except queue.Empty:
                item = None
            if item is not None:
                fn, args, kwargs, fut = item
                if fn is None:
                    break
                if not fut.set_running_or_notify_cancel():
                    continue  # the caller gave up while it was queued
                self.busy = True
                self._current = (getattr(fut, "cw_name", None) or _job_name(fn), time.monotonic(), fut)
                try:
                    res = fn(*args, **kwargs)
                    fut.set_result(res)
                except BaseException as e:  # noqa: BLE001
                    log.debug("job %s raised: %s", getattr(fn, "__name__", fn), e)
                    fut.set_exception(e)
                    self._report(e)
                finally:
                    self._finished_running()
                continue
            if active_long:
                if long.stop_requested:
                    self._end_long(long)
                    continue
                self._current = (long.name, time.monotonic(), None)
                try:
                    done = long.step()
                except BaseException as e:  # noqa: BLE001
                    long.error = f"{type(e).__name__}: {e}"
                    log.error("Long job %s failed: %s", long.name, long.error)
                    log.debug(traceback.format_exc())
                    self._report(e)
                    done = True
                finally:
                    self._current = None
                if done:
                    self._end_long(long)
                continue
            # Idle: run periodic tasks.
            now = time.monotonic()
            with self._periodic_lock:
                tasks = list(self._periodic)
            for t in tasks:
                if t.next_run <= now and (not t.only_idle or not active_long):
                    t.next_run = now + t.interval
                    self._current = (t.name, time.monotonic(), None)
                    try:
                        t.fn()
                    except Exception as e:  # noqa: BLE001
                        log.debug("periodic %s failed: %s", t.name, e)
                        self._report(e)
                    finally:
                        self._current = None

    def _finished_running(self):
        cur = self._current
        self._current = None
        self.busy = False
        if cur is not None and self._stuck is cur:
            self._stuck = None
            log.warning("the hardware thread is free again: %s returned after %.0f s", cur[0], time.monotonic() - cur[1])

    def _report(self, e: BaseException):
        if self.on_error is not None:
            try:
                self.on_error(e)
            except Exception:  # noqa: BLE001
                log.debug("on_error hook failed", exc_info=True)
