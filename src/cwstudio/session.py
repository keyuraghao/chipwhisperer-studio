"""The single application state shared by all API handlers."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np

from cwstudio import hardware, settings as cwsettings
from cwstudio.analysis import CPAAttack, MODELS
from cwstudio.capture import CaptureJob
from cwstudio.events import EventBus, trace_event
from cwstudio.firmware import FirmwareManager
from cwstudio.notebook import KernelManager, NotebookStore, Tutorials
from cwstudio.tools import NotesStore
from cwstudio.glitch import GlitchJob
from cwstudio.toolchains import ToolchainManager
from cwstudio.traces import TraceStore
from cwstudio.worker import HardwareBusy, HardwareTimeout, HardwareWorker

log = logging.getLogger("cwstudio.session")


class BusLogHandler(logging.Handler):
    """Forward log records (ours and ChipWhisperer's) to the event bus."""

    def __init__(self, bus: EventBus):
        super().__init__(level=logging.INFO)
        self.bus = bus

    def emit(self, record: logging.LogRecord):
        try:
            self.bus.publish("log", {
                "level": record.levelname, "logger": record.name,
                "msg": record.getMessage(),
            }, droppable=record.levelno < logging.WARNING)
        except Exception:  # noqa: BLE001
            pass


class Session:
    def __init__(self, simulate: bool = False, data_dir: Optional[str] = None):
        self.bus = EventBus()
        self.worker = HardwareWorker()
        self._settings_read = None  # the in-flight settings tree read shared by concurrent callers (see scope_settings)
        self._settings_read_lock = threading.Lock()
        self.worker.on_long_job_done = self._on_long_job_done
        self.worker.on_error = self._on_hardware_error
        self.store = TraceStore()
        self.scope = None
        self.target = None
        self.scope_kind: Optional[str] = None
        self.target_kind: Optional[str] = None
        self._scope_info: Dict[str, Any] = {"connected": False}  # what status() reports about the scope, read from it once on the hardware thread at connect (its name, type and firmware are USB reads)
        self._scope_info_for = None  # the scope object _scope_info describes
        self._scope_info_pending = False
        self.scope_lost: Optional[str] = None  # why the scope went away (unplugged), until the next connect or disconnect
        self._connect_lock = threading.Lock()
        self._connect_gen = 0
        self._connect_done = 0  # the newest connect attempt whose scope was attached
        self._connect_abandoned: set = set()  # connect attempts whose caller gave up (timed out): a scope they open later is let go, not attached
        self.simulate_default = simulate
        self.data_dir = data_dir or os.path.join(os.path.expanduser("~"), "ChipWhispererStudio")
        os.makedirs(self.data_dir, exist_ok=True)
        self.cpa: Optional[CPAAttack] = None
        self.last_glitch: Optional[GlitchJob] = None
        self.last_job = None
        self.serial_buffer: List[Dict[str, Any]] = []
        self._serial_lock = threading.Lock()
        self.started = time.time()
        self._log_handler = BusLogHandler(self.bus)
        logging.getLogger().addHandler(self._log_handler)
        self._cw_loggers = []
        try:
            # ChipWhisperer's loggers do not propagate to root; attach directly and raise them to INFO
            from chipwhisperer.logging import chipwhisperer_loggers
            for lg in chipwhisperer_loggers:
                lg.addHandler(self._log_handler)
                if lg.level > logging.INFO:
                    lg.setLevel(logging.INFO)
                self._cw_loggers.append(lg)
        except Exception as e:  # noqa: BLE001
            log.debug("could not attach to chipwhisperer loggers: %s", e)
        publish = lambda kind, payload: self.bus.publish(kind, payload)  # noqa: E731
        self.toolchains = ToolchainManager(self.data_dir, publish=publish)
        self.firmware = FirmwareManager(self.data_dir, self.toolchains, publish=publish)
        self.notebooks = NotebookStore(os.path.join(self.data_dir, "notebooks"))
        self.notebooks.on_change = lambda ev: self.bus.publish("nb", ev)  # open notebook tabs in every window follow saves and deletes
        self.tutorials = Tutorials(self.notebooks, self.firmware, publish)
        self.notes = NotesStore(os.path.join(self.data_dir, "notes"))
        self.calc_vars: Dict[str, Any] = {}
        self.programmed: Optional[Dict[str, Any]] = None  # the firmware programmed in this session: path, its ELF, whether the simulator emulates it
        self.worker.start()
        self.kernels = KernelManager(self, self.notebooks.root)  # one kernel per notebook, all cells on the hardware thread in one queue
        self.worker.add_periodic("serial-poll", self._poll_serial, 0.1, only_idle=True)
        self.worker.add_periodic("status", self._push_status, 2.0, only_idle=False)
        self.worker.add_periodic("scope-alive", self._check_scope_alive, 3.0, only_idle=True)
        self._health: Dict[str, Any] = {"problems": []}
        self.worker.add_periodic("health", self._read_health, 3.0, only_idle=True)  # temperature, locks and sticky errors while nothing else talks to the scope

    # --- lifecycle --------------------------------------------------------
    def close(self):
        try:
            ifc = getattr(self, "interfaces", None)
            if ifc is not None:
                ifc.openocd.close()  # never leave an OpenOCD server running after Studio exits
            self.kernels.close()
            self.worker.stop_long_job()
            if self.cpa:
                self.cpa.stop()
            self.worker.call(self._disconnect_all, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.debug("close: %s", e)
        self.worker.stop()
        logging.getLogger().removeHandler(self._log_handler)
        for lg in self._cw_loggers:
            lg.removeHandler(self._log_handler)

    def _disconnect_all(self):
        if self.target is not None:
            try:
                self.target.dis()
            except Exception:  # noqa: BLE001
                pass
            self.target = None
        if self.scope is not None:
            try:
                self.scope.dis()
            except Exception:  # noqa: BLE001
                pass
            self.scope = None
        self._scope_info, self._scope_info_for = {"connected": False}, None

    # --- status -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        job = self.worker.long_job or self.last_job
        busy = job is not None and not job.finished.is_set()
        st = {
            "scope": self.scope_info(),
            "target": hardware.target_info(self.target),
            "scope_kind": self.scope_kind,
            "target_kind": self.target_kind,
            "traces": self.store.summary(),
            "job": {"name": job.name, "running": busy, "error": job.error, **job.progress()} if job else None,
            "cpa": self._cpa_summary(),
            "uptime": round(time.time() - self.started, 1),
            "simulate_default": self.simulate_default,
            "data_dir": self.data_dir,
            "clients": self.bus.subscriber_count,
            "hardware_stuck": self.worker.stuck(),
            "health": self._health,
        }
        return st

    def _read_health(self):
        """Hardware thread: the model's monitors (health.py); a change in the problem list pushes the status so the header chip follows."""
        from cwstudio.capabilities import model_of
        from cwstudio.health import read_health
        sc = self.scope
        if sc is None:
            if self._health.get("problems"):
                self._health = {"problems": []}
                self._push_status()
            return
        h = read_health(sc, model_of(sc))
        changed = h.get("problems") != self._health.get("problems")
        self._health = h
        if changed:
            self._push_status()

    def health(self, refresh: bool = False) -> Dict[str, Any]:
        """The last health reading (or a fresh one with refresh, read on the hardware thread)."""
        if refresh and self.scope is not None:
            self.worker.call(self._read_health, timeout=30)
        return dict(self._health)

    def clear_errors(self) -> Dict[str, Any]:
        """Clear the scope's sticky errors and alarms, then read the health again."""
        if self.scope is None:
            raise RuntimeError("scope not connected")
        from cwstudio.health import clear_errors
        res = self.worker.call(clear_errors, self.scope, timeout=30)
        self.worker.call(self._read_health, timeout=30)
        self._push_status()
        return {**res, "health": dict(self._health)}

    def _cpa_summary(self) -> Optional[Dict[str, Any]]:
        """The five fields status() shows about the CPA, read from the result directly (its full to_json() sorts every byte's guesses and copies the whole history, and status runs every 2 s)."""
        if not self.cpa:
            return None
        r = self.cpa.result
        return {"model": r.model, "traces_used": r.traces_used, "total": r.total, "done": r.done, "error": r.error}

    def scope_info(self) -> Dict[str, Any]:
        """The connected scope's name, type, serial number and firmware from the cache filled on the hardware thread; never touches USB from the calling thread."""
        sc = self.scope
        if sc is None:
            return {"connected": False, "lost": self.scope_lost} if self.scope_lost else {"connected": False}
        if self._scope_info_for is sc:
            return dict(self._scope_info)
        # a scope attached without connect_scope (OpenOCD, tests): read it on the hardware thread, report the basics until then
        if self.worker.is_worker_thread:
            self._refresh_scope_info()
            return dict(self._scope_info)
        if not self._scope_info_pending:
            self._scope_info_pending = True
            self.worker.submit(self._refresh_scope_info, True)
        return {"connected": True, "type": type(sc).__name__, "name": type(sc).__name__, "pending": True}

    def _refresh_scope_info(self, push: bool = False):
        """Re-read the scope's info into the cache. Hardware thread only."""
        self._scope_info_pending = False
        sc = self.scope
        info = hardware.scope_info(sc)
        if sc is not None:
            from cwstudio.capabilities import model_of
            info["firmware"] = hardware.firmware_status(sc, model_of(sc))
        self._scope_info, self._scope_info_for = info, sc
        if push:
            self._push_status()

    def upgrade_firmware(self) -> Dict[str, Any]:
        """Flash the SAM firmware this chipwhisperer library bundles (scope.upgrade_firmware). The real scope re-enumerates afterwards, so Studio releases it and asks for a replug and a new connect."""
        sc = self.scope
        if sc is None:
            raise RuntimeError("scope not connected")
        if not hasattr(sc, "upgrade_firmware"):
            raise RuntimeError("this scope object cannot upgrade its firmware")
        before = dict(self._scope_info.get("firmware") or {})

        def _do():
            sc.upgrade_firmware()
            if getattr(sc, "sim_model", None):
                self._refresh_scope_info()
                return {"upgraded": True, "before": before.get("version"), "after": (self._scope_info.get("firmware") or {}).get("version"), "replug": False}
            self._disconnect_all()
            self.scope_kind = None
            self.target_kind = None
            self.scope_lost = "the firmware was upgraded: unplug the ChipWhisperer, plug it back in and connect again"
            return {"upgraded": True, "before": before.get("version"), "after": before.get("library"), "replug": True}
        res = self.worker.call(_do, timeout=600)
        from cwstudio.capabilities import invalidate_gates
        invalidate_gates()
        self._push_status()
        return res

    def _on_hardware_error(self, e: BaseException):
        """Every exception a hardware job raised (on the hardware thread): a USB device that went away marks the scope lost."""
        sc = self.scope
        if sc is None or self._scope_info.get("simulated") or not hardware.is_device_lost(e):
            return
        self._mark_scope_lost(f"{type(e).__name__}: {e}")

    def _check_scope_alive(self):
        """Notice an unplugged scope while nothing else talks to it: one firmware-version read (a USB control transfer) every few seconds when idle."""
        sc = self.scope
        if sc is None or self._scope_info_for is not sc or self._scope_info.get("simulated"):
            return
        sc.fw_version  # noqa: B018  a failure reaches _on_hardware_error through the worker

    def _mark_scope_lost(self, reason: str):
        name = self._scope_info.get("name") or "scope"
        log.warning("The %s was unplugged or stopped responding (%s); Studio disconnected it. Plug it back in and connect again.", name, reason)
        self._disconnect_all()
        self._health = {"problems": []}
        from cwstudio.capabilities import invalidate_gates
        invalidate_gates()
        self.scope_kind = None
        self.target_kind = None
        self.scope_lost = f"the {name} was unplugged or stopped responding ({reason})"
        self._push_status()

    def _push_status(self):
        self.bus.publish("status", self.status(), droppable=True)

    def _on_long_job_done(self, job):
        if isinstance(job, GlitchJob):
            self.last_glitch = job
        self.last_job = job
        self._push_status()

    # --- scope ---------------------------------------------------------------
    def connect_scope(self, kind: str = "auto", sn: Optional[str] = None, force: bool = False,
                      default_setup: bool = True, sim_model: Optional[str] = None) -> Dict[str, Any]:
        if kind == "sim" and sim_model:
            from cwstudio.capabilities import SIM_MODELS
            if sim_model not in SIM_MODELS:  # refuse before the connected scope is let go
                raise ValueError(f"sim_model must be one of {', '.join(SIM_MODELS)}")
        self._stop_job_before_reconnect()
        with self._connect_lock:
            self._connect_gen += 1
            gen = self._connect_gen

        def _do():
            from cwstudio.capabilities import invalidate_gates
            invalidate_gates()  # what the settings tree offers follows the new scope
            if self.scope is not None:
                self._disconnect_all()
            self.scope_kind = self.target_kind = None  # nothing is attached until this attempt succeeds
            self.scope_lost = None
            sc = hardware.connect_scope(kind, sn=sn, force=force, sim_model=sim_model)
            old = None if kind == "sim" else hardware.firmware_outdated(sc)
            if old:
                try:
                    sc.dis()
                except Exception:  # noqa: BLE001
                    pass
                raise hardware.ScopeConnectError(old)
            warnings: List[str] = []
            if default_setup:
                try:
                    sc.default_setup()
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"default_setup() failed: {type(e).__name__}: {e}")
                    log.warning("default_setup failed: %s", e)
            info = hardware.scope_info(sc)
            with self._connect_lock:
                if gen in self._connect_abandoned:
                    # the caller was told this connect failed (timed out): do not attach a scope the UI shows as not connected, and leave the device free for the next attempt
                    self._connect_abandoned.discard(gen)
                    try:
                        sc.dis()
                    except Exception:  # noqa: BLE001
                        pass
                    log.warning("The %s answered after Studio had given up on connecting to it; it was released. Connect again.", info.get("name") or "scope")
                    self._push_status()
                    return None
                self.scope, self.scope_kind = sc, kind
                try:
                    from cwstudio.capabilities import model_of
                    info["firmware"] = hardware.firmware_status(sc, model_of(sc))  # current, library and upstream SAM firmware for the Connect card
                except Exception:  # noqa: BLE001
                    pass
                self._scope_info, self._scope_info_for = info, sc
                self._connect_done = gen
            log.info("Connected to %s", info.get("name"))
            return {**info, "warnings": warnings}
        try:
            res = self.worker.call(_do, timeout=180, job_name="connecting to a ChipWhisperer" if kind == "auto" else f"connecting to the {hardware.SCOPE_KINDS.get(kind, {}).get('label', kind)}")
        except HardwareTimeout as e:
            with self._connect_lock:
                if self._connect_done == gen:  # it finished just as the wait ran out
                    res = {**self.scope_info(), "warnings": []}
                else:
                    self._connect_abandoned.add(gen)
                    res = None
            if res is None:
                log.warning("Scope connect failed: %s", e)
                self._push_status()
                raise
        except HardwareBusy as e:
            log.warning("Scope connect failed: %s", e)
            raise
        except Exception as e:  # noqa: BLE001
            if self.scope is None:
                self.scope_kind = None
            msg = f"{type(e).__name__}: {e}"
            if kind != "sim" and not isinstance(e, hardware.ScopeConnectError):
                try:
                    hint = hardware.connect_failure_hint(kind, sn, e, hardware.list_devices())  # its own libusb context: safe off the hardware thread with no scope open
                except Exception as le:  # noqa: BLE001
                    hint = ""
                    log.debug("listing devices after a failed connect: %s", le)
                if hint:
                    log.warning("Scope connect failed: %s %s", msg, hint)
                    self._push_status()
                    raise hardware.ScopeConnectError(f"{e} {hint}".strip()) from e
            log.warning("Scope connect failed: %s", msg)
            self._push_status()
            raise
        self._push_status()
        return res

    def _stop_job_before_reconnect(self, wait: float = 15.0) -> None:
        """Stop a running capture, glitch sweep or logic capture before the scope it uses is replaced, so it ends as stopped instead of failing on a disconnected scope."""
        job = self.worker.long_job
        if job is not None and not job.finished.is_set():
            log.info("Stopping %s: the scope is being disconnected", job.name)
            self.worker.stop_long_job()
            job.finished.wait(wait)

    def disconnect_scope(self):
        self._stop_job_before_reconnect()
        self.worker.call(self._disconnect_all, job_name="disconnecting the scope")
        from cwstudio.capabilities import invalidate_gates
        invalidate_gates()
        self.scope_kind = None
        self.target_kind = None
        self.scope_lost = None
        self._push_status()

    def scope_settings(self) -> List[Dict[str, Any]]:
        """The scope's settings tree, read on the hardware thread. Callers that arrive while a read is in flight share its result instead of queueing another: the tree is about 160 ms of USB traffic on a Husky, and a burst of clients (every open browser tab, an agent polling) used to line up behind each other and hold a running capture back."""
        if self.scope is None:
            return []
        from cwstudio.capabilities import gate_settings
        with self._settings_read_lock:
            fut = self._settings_read
            if fut is None or fut.done():
                fut = self._settings_read = self.worker.submit(lambda: gate_settings(cwsettings.describe(self.scope), self.scope))  # per-model choices: unsupported values are listed but disabled with the reason
        return fut.result(timeout=120)

    def set_scope_setting(self, path: str, value: Any) -> Any:
        if self.scope is None:
            raise RuntimeError("scope not connected")
        from cwstudio.capabilities import check_setting
        self.worker.call(check_setting, self.scope, path, value)
        v = self.worker.call(cwsettings.set_value, self.scope, path, value)
        self.bus.publish("setting", {"target": "scope", "path": path, "value": v})
        return v

    def scope_action(self, action: str) -> Any:
        if self.scope is None:
            raise RuntimeError("scope not connected")
        def _do():
            if action == "default_setup":
                self.scope.default_setup()
            elif action == "reset_fpga" and hasattr(self.scope, "reset_fpga"):
                self.scope.reset_fpga()
            elif action == "glitch_disable" and hasattr(self.scope, "glitch_disable"):
                self.scope.glitch_disable()
            elif action == "cglitch_setup" and hasattr(self.scope, "cglitch_setup"):
                self.scope.cglitch_setup(default_setup=False)  # the library's clock glitch preset, without resetting the capture settings
            elif action in ("vglitch_lp", "vglitch_hp", "vglitch_both") and hasattr(self.scope, "vglitch_setup"):
                self.scope.vglitch_setup(action.split("_", 1)[1], default_setup=False)
            elif action == "arm_capture":
                self.scope.arm()
                to = self.scope.capture()
                return {"timeout": bool(to)}
            elif action in ("reset_fpga", "glitch_disable", "cglitch_setup", "vglitch_lp", "vglitch_hp", "vglitch_both"):
                raise ValueError(f"this scope does not support {action}")
            else:
                raise ValueError(f"unknown action {action}")
            return {"ok": True}
        return self.worker.call(_do, timeout=60)

    # --- target ---------------------------------------------------------------
    def connect_target(self, kind: str = "SimpleSerial2", **kwargs) -> Dict[str, Any]:
        def _do():
            if self.scope is None:
                raise RuntimeError("connect a scope first")
            if self.target is not None:
                try:
                    self.target.dis()
                except Exception:  # noqa: BLE001
                    pass
                self.target = None
            self.target = hardware.connect_target(self.scope, kind, **kwargs)
            self.target_kind = kind
            log.info("Target connected: %s", kind)
            return hardware.target_info(self.target)
        try:
            info = self.worker.call(_do, timeout=60, job_name=f"connecting the {kind} target")
        except Exception as e:  # noqa: BLE001
            log.warning("Target connect failed: %s: %s", type(e).__name__, e)  # reaches the Log panel and /api/logs, not only the caller
            if self.target is None:
                self.target_kind = None
            self._push_status()
            raise
        self._push_status()
        return info

    def disconnect_target(self):
        def _do():
            if self.target is not None:
                try:
                    self.target.dis()
                except Exception:  # noqa: BLE001
                    pass
            self.target = None
        self.worker.call(_do, job_name="disconnecting the target")
        self.target_kind = None
        self._push_status()

    def target_settings(self) -> List[Dict[str, Any]]:
        if self.target is None:
            return []
        return self.worker.call(cwsettings.describe, self.target)

    def set_target_setting(self, path: str, value: Any) -> Any:
        if self.target is None:
            raise RuntimeError("target not connected")
        v = self.worker.call(cwsettings.set_value, self.target, path, value)
        self.bus.publish("setting", {"target": "target", "path": path, "value": v})
        return v

    def program(self, programmer: str, fw_path: str, **kwargs) -> Dict[str, Any]:
        if self.scope is None:
            raise RuntimeError("connect a scope first")
        log.info("Programming target with %s using %s", os.path.basename(fw_path), programmer)
        try:
            res = self.worker.call(hardware.program_target, self.scope, programmer, fw_path, timeout=600, job_name=f"programming the target ({programmer})", **kwargs)
        except Exception as e:  # noqa: BLE001
            log.warning("Programming failed: %s: %s", type(e).__name__, e)
            raise
        log.info("Programming complete")
        self.note_programmed(fw_path, res.get("emulation"))
        return res

    def note_programmed(self, fw_path: str, emulation: Optional[Dict[str, Any]] = None) -> None:
        """Remember the firmware programmed in this session (the code map uses its ELF by default)."""
        from cwstudio.codemap.sim import elf_for
        self.programmed = {"path": os.path.abspath(fw_path), "elf": elf_for(fw_path), "emulated": bool(emulation and emulation.get("emulated")), "t": time.time()}
        self.bus.publish("programmed", self.programmed)

    def program_build(self, path: Optional[str] = None, programmer: Optional[str] = None) -> Dict[str, Any]:
        """Program the last successful firmware build (or ``path``) with the platform's programmer."""
        job = self.firmware.build_status()
        path = path or job.get("hex")
        if not path:
            raise RuntimeError("no firmware build yet: build one first or pass a .hex path")
        programmer = programmer or job.get("programmer")
        if not programmer:
            raise RuntimeError("this platform has no built-in programmer in Studio; choose one explicitly")
        res = self.program(programmer, path)
        res.update({"path": path, "programmer": programmer})
        return res

    def run_notebook(self, path: str, timeout: float = 1800, stop_on_error: bool = True, kernel: Optional[str] = None) -> Dict[str, Any]:
        """Run every code cell of a stored notebook in order, save the outputs into it, and summarise. Runs in the notebook's own kernel (the one its tab in the Notebook tab uses) unless ``kernel`` names another one."""
        nb = self.notebooks.load(path)
        kern = self.kernels.get(kernel or path)
        ran, failed = 0, None
        end = time.time() + timeout
        for c in nb["cells"]:
            if c["cell_type"] != "code" or not c["source"].strip():
                continue
            r = kern.execute_wait(c["source"], path, timeout=max(5.0, end - time.time()))
            c["outputs"], c["execution_count"] = r["outputs"], r["execution_count"]
            ran += 1
            if not r["ok"]:
                failed = c["id"]
                if stop_on_error:
                    break
        # merge the outputs into the file as it is now, so edits saved while the notebook ran (in its tab, by an agent) are kept
        results = {c["id"]: (c.get("outputs"), c.get("execution_count")) for c in nb["cells"] if c["cell_type"] == "code"}
        try:
            current = self.notebooks.load(path)
        except (OSError, ValueError):
            current = None
        if current is not None:
            for c in current["cells"]:
                if c["cell_type"] == "code" and c["id"] in results and c["source"] == next(x["source"] for x in nb["cells"] if x["id"] == c["id"]):
                    c["outputs"], c["execution_count"] = results[c["id"]]
            nb = current
            self.notebooks.save(path, nb)  # not when the notebook was deleted meanwhile
        return {"path": path, "kernel": kern.id, "cells_run": ran, "failed_cell": failed, "ok": failed is None, "notebook": nb}

    def save_upload(self, filename: str, content: bytes) -> str:
        d = os.path.join(self.data_dir, "firmware", "uploads")
        os.makedirs(d, exist_ok=True)
        safe = os.path.basename(filename) or "firmware.hex"
        path = os.path.join(d, safe)
        with open(path, "wb") as f:
            f.write(content)
        return path

    # --- serial console -----------------------------------------------------
    def serial_write(self, data: str, hex_mode: bool = False, newline: bool = True, eol: Optional[str] = None) -> int:
        """Write to the target UART. ``eol`` (none, lf, cr, crlf) picks the line ending appended to text; without it ``newline`` adds a line feed."""
        if self.target is None:
            raise RuntimeError("target not connected")
        payload = bytes.fromhex(data.replace(" ", "")) if hex_mode else data.encode()
        if eol is not None and not hex_mode:
            ends = {"none": b"", "lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}
            if eol not in ends:
                raise ValueError("eol must be none, lf, cr or crlf")
            payload += ends[eol]
        elif newline and not hex_mode and not payload.endswith(b"\n"):
            payload += b"\n"

        def _do():
            self.target.write(payload)
            self._record_serial("tx", payload)
            time.sleep(0.05)
            self._poll_serial()
            return len(payload)
        return self.worker.call(_do, timeout=10)

    def simpleserial(self, cmd: str, data_hex: str, read_cmd: str = "r", read_len: Optional[int] = None):
        if self.target is None:
            raise RuntimeError("target not connected")

        def _do():
            data = bytes.fromhex(data_hex.replace(" ", "")) if data_hex else b""
            self.target.simpleserial_write(cmd, bytearray(data))
            self._record_serial("tx", f"{cmd}{data.hex()}".encode())
            n = read_len if read_len is not None else getattr(self.target, "output_len", 16)
            resp = None
            try:
                resp = self.target.simpleserial_read(read_cmd, n, timeout=500)
            except Exception as e:  # noqa: BLE001
                log.warning("simpleserial read: %s", e)
            if resp is not None:
                self._record_serial("rx", f"{read_cmd}{bytes(resp).hex()}".encode())
            return {"response": bytes(resp).hex() if resp is not None else None}
        return self.worker.call(_do, timeout=10)

    def _poll_serial(self):
        t = self.target
        if t is None:
            return
        try:
            n = t.in_waiting()
        except Exception:  # noqa: BLE001
            return
        if n:
            try:
                data = t.read(n, timeout=10)
            except Exception:  # noqa: BLE001
                return
            if data:
                self._record_serial("rx", data.encode("latin-1", errors="replace") if isinstance(data, str) else bytes(data))  # the chipwhisperer readers decode bytes as latin-1, so this gives back the exact bytes

    def _record_serial(self, direction: str, data: bytes):
        rec = {"dir": direction, "data": data.decode("utf-8", errors="replace"), "hex": data.hex(),
               "t": time.time()}
        with self._serial_lock:
            self.serial_buffer.append(rec)
            if len(self.serial_buffer) > 2000:
                del self.serial_buffer[:1000]
        self.bus.publish("serial", rec)

    # --- capture --------------------------------------------------------------
    def start_capture(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.scope is None:
            raise RuntimeError("scope not connected")
        if params.get("clear"):
            self.store.clear()
            self.bus.publish("traces", self.store.summary())
        job = CaptureJob(self.scope, self.target, self.store, self.bus, params)
        if not self.worker.start_long_job(job):
            raise RuntimeError("another job is running")
        self.last_job = job
        self._push_status()  # windows show the running job (header chip, Stop button) right away, not only when it ends
        return {"started": True, **job.progress()}

    def stop_job(self) -> Dict[str, Any]:
        job = self.worker.long_job
        self.worker.stop_long_job()
        return {"stopping": job.name if job else None}

    def capture_single(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params["count"] = 1
        return self.start_capture(params)

    # --- traces ---------------------------------------------------------------
    def trace_bytes(self, i: int) -> bytes:
        wave, tin, tout, key = self.store.get(i)
        ev = trace_event("trace", wave, {"index": i, "textin": tin, "textout": tout, "key": key,
                                          "generation": self.store.generation})
        return ev.frame()

    def traces_block(self, start: int, end: int, step: int = 1) -> bytes:
        """Concatenate traces start..end (exclusive) as one frame: header + f32[n_traces*S]."""
        idx = list(range(start, min(end, len(self.store)), max(1, step)))
        if not idx:
            return trace_event("traces", np.zeros(0, np.float32), {"indices": [], "samples": 0}).frame()
        with self.store._lock:  # a clear() in between would make the indices dangle
            waves = [self.store.waves[i] for i in idx]
            generation = self.store.generation
        s = min(len(w) for w in waves)
        block = np.empty((len(waves), s), np.float32)  # one copy straight into the frame's sample block (the waves are float32 already)
        for k, w in enumerate(waves):
            block[k] = w[:s]
        return trace_event("traces", block.ravel(), {"indices": idx, "samples": s, "generation": generation}).frame()

    def stats_bytes(self, start: int = 0, end: Optional[int] = None) -> bytes:
        st = self.store.stats(start, end)
        if not st:
            return trace_event("stats", np.zeros(0, np.float32), {"fields": [], "samples": 0}).frame()
        fields = ["mean", "std", "min", "max"]
        block = np.stack([st[f] for f in fields]).astype(np.float32, copy=False)
        return trace_event("stats", block.ravel(), {"fields": fields, "samples": int(block.shape[1]),
                                                    "count": len(self.store)}).frame()

    def export_traces(self, path: str, fmt: str) -> str:
        if not os.path.isabs(os.path.expanduser(path)):
            path = os.path.join(self.data_dir, path)
        os.makedirs(os.path.dirname(os.path.abspath(os.path.expanduser(path))), exist_ok=True)
        out = self.store.export(path, fmt)
        self.bus.publish("traces", self.store.summary())
        return out

    def import_traces(self, path: str, replace: bool = True) -> int:
        path = os.path.expanduser(path)
        if not os.path.isabs(path) and not os.path.exists(path):
            path = os.path.join(self.data_dir, path)  # relative paths resolve like export_traces: inside the data folder
        n = self.store.import_file(path, replace)
        self.bus.publish("traces", self.store.summary())
        return n

    # --- analysis ---------------------------------------------------------------
    def start_cpa(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.cpa and not self.cpa.result.done:
            raise RuntimeError("CPA already running")
        start = int(params.get("trace_start", 0) or 0)
        end = params.get("trace_end")
        end = int(end) if end else None
        W, tin, tout, key = self.store.as_arrays(start, end)
        if W.shape[0] < 2:
            raise RuntimeError("need at least 2 traces")
        model = params.get("model", "sbox_hw")
        if model not in MODELS:
            raise ValueError(f"unknown model {model}")
        known = params.get("known_key")
        known_b = bytes.fromhex(known.replace(" ", "")) if known else None
        if known_b is None and params.get("use_stored_key", True) and key.shape[1] >= 16 and key.shape[0]:
            # Use the stored key if every trace shares it (fixed-key capture)
            if np.all(key == key[0]) and np.any(key[0]):
                known_b = key[0].tobytes()
                if model.startswith("lastround"):
                    from cwstudio.aes import last_round_key
                    known_b = last_round_key(known_b)
        pr = params.get("point_range")
        if pr:  # an open end (None or 0, as the UI and the MCP tool send it) means up to the last sample
            lo = int(pr[0] or 0)
            hi = int(pr[1]) if len(pr) > 1 and pr[1] else int(W.shape[1])
            pr = (lo, hi)
        else:
            pr = None

        def cb(res):
            self.bus.publish("cpa", res.to_json(), droppable=not res.done)
        self.cpa = CPAAttack(W, tin, tout, model=model, point_range=pr,
                             report_every=int(params.get("report_every", 50)), known_key=known_b, callback=cb,
                             bytes_to_attack=params.get("bytes"), params={"expr": params.get("expr")})
        self.cpa.start()
        return {"started": True, "traces": int(W.shape[0]), "samples": int(W.shape[1]), "model": model}

    def cpa_export(self, path: str) -> Dict[str, Any]:
        """Save the CPA result: the per-guess maxima and their samples, the best correlation trace per byte, the history and the summary, as .npz next to a .json."""
        if not self.cpa:
            raise RuntimeError("no CPA result")
        import json as _json
        r = self.cpa.result
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self.data_dir, path)
        base = path[:-4] if path.endswith(".npz") else path
        os.makedirs(os.path.dirname(base) or ".", exist_ok=True)
        best = np.zeros((r.n_bytes, max((t.shape[0] for t in r.best_corr_trace), default=0)), np.float32)
        for b, t in enumerate(r.best_corr_trace):
            best[b, :t.shape[0]] = t
        np.savez(base + ".npz", corr_max=r.corr_max, corr_argmax=r.corr_argmax, best_corr_trace=best, point_offset=np.int64(self.cpa.lo))
        with open(base + ".json", "w", encoding="utf-8") as f:
            _json.dump(r.to_json(), f, indent=1)
        return {"path": base + ".npz", "summary": base + ".json", "bytes": r.n_bytes, "samples": int(best.shape[1])}

    # --- leakage assessment and trace conditioning (leakage.py) ---------------------
    def _trace_matrix(self, start: int = 0, end: Optional[int] = None):
        W, tin, tout, key = self.store.as_arrays(start, end)
        if W.shape[0] < 2:
            raise RuntimeError("need at least 2 traces")
        return W, tin, tout, key

    def ttest(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """TVLA: fixed plaintext traces against random ones (the fixed text from the capture's tvla mode unless given)."""
        from cwstudio import leakage
        W, tin, _tout, _key = self._trace_matrix(int(params.get("trace_start", 0) or 0), params.get("trace_end"))
        fixed = params.get("fixed_text") or self.store.meta.get("tvla_fixed_text")
        if not fixed:
            raise ValueError("no fixed plaintext: capture with text mode tvla, or give fixed_text (hex)")
        res = leakage.ttest(W, tin, bytes.fromhex(str(fixed).replace(" ", "")))
        if res["fixed"] < 2 or res["random"] < 2:
            raise ValueError(f"the t-test needs at least 2 traces in each group (fixed {res['fixed']}, random {res['random']}): capture with text mode tvla")
        series, step = leakage.decimate_series(res.pop("t"), int(params.get("max_points", 4000)))
        return {**res, "traces": int(W.shape[0]), "series": [float(x) for x in series], "step": step, "fixed_text": str(fixed)}

    def snr(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Signal-to-noise ratio per sample, classes by a plaintext byte or by the S-box Hamming weight under the known (or stored) key."""
        from cwstudio import leakage
        W, tin, _tout, key = self._trace_matrix(int(params.get("trace_start", 0) or 0), params.get("trace_end"))
        byte = int(params.get("byte", 0))
        kind = str(params.get("classes", "sbox_hw"))
        known = params.get("known_key")
        kb = bytes.fromhex(known.replace(" ", "")) if known else (key[0].tobytes() if key.shape[0] and key.shape[1] >= 16 and np.all(key == key[0]) and np.any(key[0]) else None)
        cls = leakage.classes_for(kind, tin, byte, kb)
        out = leakage.snr(W, cls)
        series, step = leakage.decimate_series(out, int(params.get("max_points", 4000)))
        return {"byte": byte, "classes": kind, "traces": int(W.shape[0]), "max_snr": float(out.max()) if out.size else 0.0, "argmax": int(out.argmax()) if out.size else 0, "series": [float(x) for x in series], "step": step, "known_key": kb.hex() if kb else None}

    def align_traces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Static alignment: shift every trace so its window matches the reference trace's; preview returns the shifts, apply writes the shifted traces back."""
        from cwstudio import leakage
        with self.store._lock:
            n = len(self.store)
            if n < 2:
                raise RuntimeError("need at least 2 traces")
            ref_i = int(params.get("reference", 0))
            ref_i = ref_i if ref_i >= 0 else n + ref_i
            if not 0 <= ref_i < n:
                raise ValueError(f"reference trace {ref_i} does not exist")
            W = self.store.as_arrays()[0]
        S = W.shape[1]
        start, end = int(params.get("start", 0) or 0), int(params.get("end", 0) or S)
        if end <= 0 or end > S:
            end = S
        if not 0 <= start < end:
            raise ValueError("the window must lie inside the trace")
        max_shift = int(params.get("max_shift", 50))
        shifts = leakage.align(W, W[ref_i], start, end, max_shift)
        hist = np.bincount(shifts - shifts.min()) if shifts.size else np.zeros(0)
        out = {"traces": int(W.shape[0]), "reference": ref_i, "start": start, "end": end, "max_shift": max_shift, "shift_min": int(shifts.min()), "shift_max": int(shifts.max()), "shift_mean": float(shifts.mean()), "moved": int((shifts != 0).sum()), "histogram": {"from": int(shifts.min()), "counts": [int(x) for x in hist]}, "applied": False}
        if params.get("apply"):
            with self.store._lock:
                if len(self.store) != W.shape[0]:
                    raise RuntimeError("the trace set changed while aligning; run it again")
                for i, sh in enumerate(shifts):
                    if sh:
                        self.store.waves[i] = leakage.shift_trace(self.store.waves[i], int(sh))
                self.store._acc = None
                self.store.generation += 1
                self.store.meta.setdefault("conditioning", []).append({"align": {"reference": ref_i, "start": start, "end": end, "max_shift": max_shift, "moved": out["moved"]}})
            self.bus.publish("traces", self.store.summary())
            out["applied"] = True
        return out

    def filter_traces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Filter every stored trace in place: lowpass, highpass or bandpass (windowed-sinc FIR, cutoffs as a fraction of the sample rate or in Hz with sample_rate) or a moving average of N samples."""
        from cwstudio import leakage
        kind = str(params.get("kind", "lowpass"))
        cutoff = float(params.get("cutoff", 0.1))
        cutoff2 = params.get("cutoff2")
        sr = params.get("sample_rate")
        if sr:
            cutoff = cutoff / float(sr)
            cutoff2 = float(cutoff2) / float(sr) if cutoff2 is not None else None
        kernel = leakage.fir_kernel(kind, cutoff, int(params.get("taps", 63)), None if cutoff2 is None else float(cutoff2))
        with self.store._lock:
            if len(self.store) < 1:
                raise RuntimeError("no traces")
            W = self.store.as_arrays()[0]
            out = leakage.apply_filter(W, kernel)
            for i in range(out.shape[0]):
                self.store.waves[i] = out[i]
            self.store._acc = None
            self.store.generation += 1
            self.store.meta.setdefault("conditioning", []).append({"filter": {"kind": kind, "cutoff": cutoff, "cutoff2": cutoff2, "taps": int(kernel.size)}})
        self.bus.publish("traces", self.store.summary())
        return {"traces": int(out.shape[0]), "kind": kind, "cutoff": cutoff, "cutoff2": cutoff2, "taps": int(kernel.size)}

    def spectrum(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Magnitude spectrum of one stored trace (index, -1 newest) or of the mean trace, in dB below the peak; frequencies in Hz when the ADC rate is known."""
        from cwstudio import leakage
        with self.store._lock:
            n = len(self.store)
            if n < 1:
                raise RuntimeError("no traces")
            i = params.get("trace")
            if i is None:
                w = self.store.stats()["mean"]
            else:
                i = int(i)
                i = i if i >= 0 else n + i
                w = self.store.waves[i]
        sr = params.get("sample_rate")
        if sr is None and self.scope is not None:
            try:
                sr = float(self.worker.call(lambda: self.scope.clock.adc_freq, timeout=10))
            except Exception:  # noqa: BLE001
                sr = None
        f, db = leakage.spectrum(np.asarray(w), float(sr) if sr else None)
        mp = int(params.get("max_points", 4000))
        series, step = leakage.decimate_series(db, mp)
        freqs = f[::step][:series.size]
        return {"samples": int(np.asarray(w).size), "sample_rate": sr, "step": step, "freq": [float(x) for x in freqs], "db": [float(x) for x in series], "peak_hz": float(f[int(db.argmax())]) if f.size else None}

    def stop_cpa(self):
        if self.cpa:
            self.cpa.stop()

    def cpa_result(self) -> Optional[Dict[str, Any]]:
        return self.cpa.result.to_json() if self.cpa else None

    def cpa_corr_bytes(self, b: int) -> bytes:
        if not self.cpa:
            raise RuntimeError("no CPA result")
        r = self.cpa.result
        return trace_event("corr", r.best_corr_trace[b], {"byte": b, "offset": self.cpa.lo,
                                                          "guess": int(np.argmax(r.corr_max[b]))}).frame()

    # --- glitch -------------------------------------------------------------------
    def start_glitch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.scope is None:
            raise RuntimeError("scope not connected")
        from cwstudio.capabilities import check_setting
        for prm in params.get("parameters") or []:  # a path this model does not have (glitch.width on a Nano) is refused here with the reason, not on the first point of the sweep
            path = prm.get("path") if isinstance(prm, dict) else None
            if path:
                vals = prm.get("values") or [prm.get("start", 0)]
                self.worker.call(check_setting, self.scope, path, vals[0])
        job = GlitchJob(self.scope, self.target, self.bus, params)
        if not self.worker.start_long_job(job):
            raise RuntimeError("another job is running")
        self.last_glitch = job
        self.last_job = job
        self._push_status()
        return {"started": True}

    def glitch_results(self) -> Dict[str, Any]:
        j = self.last_glitch
        if j is None:
            return {"results": [], "parameters": [], "counts": {}}
        return {"results": j.results, "parameters": [p["path"] for p in j.params], "counts": j.counts,
                "running": not j.finished.is_set(), "error": j.error, **j.progress()}

    def export_glitch(self, path: str) -> str:
        if self.last_glitch is None:
            raise RuntimeError("no glitch results")
        if not os.path.isabs(os.path.expanduser(path)):
            path = os.path.join(self.data_dir, path)
        os.makedirs(os.path.dirname(os.path.abspath(os.path.expanduser(path))), exist_ok=True)
        return self.last_glitch.export_csv(path)
