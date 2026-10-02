"""JTAG and SWD through OpenOCD: switching the scope into MPSSE mode, running an OpenOCD server with the ChipWhisperer interface config, talking to it over its TCL port and flashing with ``program``.

OpenOCD itself comes from the managed toolchains (the pinned xPack build in ``resources/toolchains.json``, id ``openocd``) or from the system ``PATH``. While MPSSE is on, the scope re-enumerates with an MPSSE (FTDI style) interface in place of its USB-CDC serial port: Studio releases the scope, native programmers and CDC serial are unavailable, and :meth:`OpenOCD.mpsse_disable` restores normal mode and reconnects.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from cwstudio.capabilities import Unsupported, capabilities, model_of, require

log = logging.getLogger("cwstudio.openocd")

RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
CW_CFG = "cw_openocd.cfg"
VID = 0x2B3E
PIDS = {"nano": 0xACE0, "lite": 0xACE2, "pro": 0xACE3, "husky": 0xACE5, "huskyplus": 0xACE6}
TOOLCHAIN_ID = "openocd"
DEFAULT_PORTS = {"gdb": 3333, "telnet": 4444, "tcl": 6666}
SYSTEM_SCRIPT_DIRS = ["/usr/share/openocd/scripts", "/usr/local/share/openocd/scripts", "/opt/homebrew/share/openocd/scripts"]


def tcl_query(port: int, command: str, timeout: float = 10.0, host: str = "127.0.0.1") -> str:
    """Send one command to OpenOCD's TCL RPC port and return its result (commands are terminated by 0x1a both ways)."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(command.encode("utf-8") + b"\x1a")
        buf = b""
        while not buf.endswith(b"\x1a"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return buf.rstrip(b"\x1a").decode("utf-8", "replace")


def wrap_command(command: str) -> str:
    """Wrap a user command so the reply says whether it failed and carries what it printed: ``<rc> <output>``. ``format`` (not ``list``) joins them, so the output comes back verbatim instead of Tcl-list quoted (``list`` would brace it, or backslash-escape it when it has an unbalanced brace)."""
    return "set _cws_rc [catch {capture {" + command + "}} _cws_out]; format {%s %s} $_cws_rc $_cws_out"


def parse_wrapped(reply: str) -> Dict[str, Any]:
    rc, _, rest = reply.lstrip().partition(" ")
    return {"ok": rc == "0", "output": rest.rstrip()}


def braces_balanced(text: str) -> bool:
    """True when every unescaped { has its } (the command is wrapped in braces, so an unbalanced one would break out of the wrapper)."""
    depth, i = 0, 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def tcl_word(text: str, always: bool = True) -> str:
    """Quote one word for an OpenOCD (Jim Tcl) command line: braces keep spaces, $ and [ literal. With ``always=False`` a plain word (letters, digits, ``_.:/-``) stays unquoted."""
    text = str(text)
    if not always and re.fullmatch(r"[A-Za-z0-9_.:/-]+", text):
        return text
    if text.count("{") != text.count("}") or "\\" in text:
        raise ValueError(f"cannot quote {text!r} for OpenOCD (unbalanced braces or backslashes)")
    return "{" + text + "}"


class OpenOCD:
    """One OpenOCD server per Studio session plus the scope's MPSSE state."""

    def __init__(self, session, publish: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.s = session
        self._publish = publish or (lambda kind, payload: None)
        self.proc: Optional[subprocess.Popen] = None
        self.ports = dict(DEFAULT_PORTS)
        self.target_cfg: Optional[str] = None
        self.transport: Optional[str] = None
        self.log: deque = deque(maxlen=2000)
        self.log_seq = 0
        self.mpsse: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    # --- discovery --------------------------------------------------------
    def binary(self) -> Optional[str]:
        """Path to the openocd executable: the managed install first, then the system PATH."""
        try:
            e = self.s.toolchains.entry(TOOLCHAIN_ID)
            st = self.s.toolchains.status(e)
            exe = "openocd.exe" if os.name == "nt" else "openocd"
            if st.get("installed") and st.get("bin"):
                return os.path.join(st["bin"], exe)
            if st.get("system"):
                return os.path.join(st["system"], exe)
        except (KeyError, AttributeError):  # no toolchain registry (tests, embedded use) or no openocd entry
            pass
        return shutil.which("openocd")

    def scripts_dir(self, binary: Optional[str] = None) -> Optional[str]:
        b = binary or self.binary()
        cands = []
        if b:
            root = os.path.dirname(os.path.dirname(os.path.realpath(b)))
            cands += [os.path.join(root, "openocd", "scripts"), os.path.join(root, "share", "openocd", "scripts"), os.path.join(root, "scripts")]
        cands += SYSTEM_SCRIPT_DIRS
        for c in cands:
            if os.path.isdir(os.path.join(c, "target")):
                return c
        return None

    def target_configs(self) -> List[str]:
        d = self.scripts_dir()
        if not d:
            return []
        return sorted(os.path.relpath(p, d).replace(os.sep, "/") for p in glob.glob(os.path.join(d, "target", "*.cfg")))

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> Dict[str, Any]:
        b = self.binary()
        return {"binary": b, "installed": bool(b), "toolchain_id": TOOLCHAIN_ID, "scripts": self.scripts_dir(b), "running": self.running(), "pid": self.proc.pid if self.running() else None,
                "exit_code": (self.proc.returncode if self.proc is not None and not self.running() else None), "ports": self.ports, "target_cfg": self.target_cfg, "transport": self.transport,
                "mpsse": self.mpsse, "log_seq": self.log_seq, "log": list(self.log)[-200:], "interface_cfg": os.path.join(RESOURCES, CW_CFG)}

    def log_since(self, since: int = 0) -> Dict[str, Any]:
        lines = [r for r in list(self.log) if r["seq"] > since]
        return {"seq": self.log_seq, "lines": lines}

    def _add_log(self, line: str, stream: str = "out"):
        with self._lock:
            self.log_seq += 1
            rec = {"seq": self.log_seq, "t": time.time(), "line": line, "stream": stream}
            self.log.append(rec)
        self._publish("openocd", {"kind": "log", **rec})

    # --- MPSSE mode ---------------------------------------------------------
    def _gate(self, transport: str, header: str) -> Dict[str, Any]:
        scope = self.s.scope
        if scope is None:
            raise Unsupported("connect a scope first")
        caps = self.s.worker.call(capabilities, scope, None, timeout=30)
        if transport not in ("jtag", "swd"):
            raise ValueError("transport must be 'jtag' or 'swd'")
        require(caps[transport], transport.upper())
        if header == "userio" and "USERIO 20-pin" not in caps[transport]["headers"]:
            raise Unsupported(f"{transport.upper()} on the USERIO header needs a ChipWhisperer-Husky" + (" with SAM firmware 1.4 or newer" if caps["model"] in ("husky", "huskyplus") else ""))
        return caps

    def mpsse_enable(self, transport: str = "jtag", header: str = "target") -> Dict[str, Any]:
        """Switch the connected scope into MPSSE mode. Studio releases the scope (it re-enumerates); use :meth:`mpsse_disable` to come back."""
        if self.mpsse:
            return self.mpsse
        caps = self._gate(transport, header)
        model = caps["model"]
        kind = self.s.scope_kind or model

        def _do():
            scope = self.s.scope
            sn = getattr(scope, "sn", None)
            husky_userio = transport if header == "userio" else None
            if model in ("husky", "huskyplus"):
                scope.enable_MPSSE(True, husky_userio=husky_userio)
            else:
                scope.enable_MPSSE(True)
            try:
                self.s.target and self.s.target.dis()
            except Exception:  # noqa: BLE001
                pass
            try:
                scope.dis()
            except Exception:  # noqa: BLE001
                pass
            self.s.target = None
            self.s.scope = None
            return sn
        sn = self.s.worker.call(_do, timeout=60)
        self.mpsse = {"enabled": True, "model": model, "pid": PIDS.get(model), "sn": sn, "transport": transport, "header": header, "kind": kind, "since": time.time(),
                      "warning": "MPSSE mode is on: the scope's USB-CDC serial port and the native programmers are unavailable until you restore normal mode."}
        self.s.scope_kind = None
        self.s.target_kind = None
        log.info("MPSSE (%s on the %s header) enabled; the scope is released for OpenOCD", transport.upper(), "USERIO" if header == "userio" else "20-pin target")
        self._publish("openocd", {"kind": "mpsse", "mpsse": self.mpsse})
        self.s._push_status()
        return self.mpsse

    def mpsse_disable(self, reconnect: bool = True) -> Dict[str, Any]:
        """Leave MPSSE mode: stop OpenOCD, reconnect to the scope, turn MPSSE off (the scope resets) and reconnect normally."""
        self.stop()
        info = self.mpsse or {}
        kind = info.get("kind") or "auto"
        sn = info.get("sn")
        from cwstudio import hardware

        def _off():
            sc = hardware.connect_scope(kind, sn=sn)
            try:
                sc.enable_MPSSE(False)
            finally:
                try:
                    sc.dis()
                except Exception:  # noqa: BLE001
                    pass
        if info:
            self.s.worker.call(_off, timeout=60)
        self.mpsse = None
        self._publish("openocd", {"kind": "mpsse", "mpsse": None})
        res: Dict[str, Any] = {"mpsse": None}
        if reconnect and info:
            last = None
            for _ in range(10):
                time.sleep(1.0)
                try:
                    res["scope"] = self.s.connect_scope(kind, sn)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
            else:
                raise RuntimeError(f"normal mode restored but reconnecting failed: {last}")
        log.info("MPSSE disabled; normal mode restored")
        return res

    # --- server -------------------------------------------------------------
    def command_line(self, target_cfg: Optional[str], transport: str, ports: Dict[str, int], extra: Optional[List[str]] = None, binary: Optional[str] = None) -> List[str]:
        info = self.mpsse or {}
        pid = info.get("pid") or PIDS["husky"]
        cmd = [binary or self.binary() or "openocd", "-s", RESOURCES]
        sd = self.scripts_dir(binary)
        if sd:
            cmd += ["-s", sd]
        cmd += ["-c", f"gdb_port {int(ports['gdb'])}", "-c", f"telnet_port {int(ports['telnet'])}", "-c", f"tcl_port {int(ports['tcl'])}",
                "-f", CW_CFG, "-c", f"ftdi vid_pid 0x{VID:04x} 0x{pid:04x}"]
        if info.get("sn"):
            cmd += ["-c", f"adapter serial {tcl_word(info['sn'], always=False)}"]
        cmd += ["-c", f"transport select {transport}"]
        if target_cfg:
            cmd += ["-f", target_cfg]
        for e in extra or []:
            cmd += ["-c", e]
        return cmd

    def start(self, target_cfg: Optional[str] = None, transport: Optional[str] = None, ports: Optional[Dict[str, int]] = None, extra: Optional[List[str]] = None) -> Dict[str, Any]:
        if self.running():
            raise RuntimeError("OpenOCD is already running; stop it first")
        if not self.mpsse:
            sim = getattr(self.s.scope, "sim_model", None)
            if sim:
                raise Unsupported("needs real hardware: OpenOCD cannot drive the simulator")
            raise Unsupported("enable MPSSE (JTAG/SWD) mode on the scope first")
        binary = self.binary()
        if not binary:
            raise Unsupported("OpenOCD is not installed: install it from the Firmware tab's toolchains (xPack OpenOCD) or put openocd on PATH")
        if target_cfg and not target_cfg.endswith(".cfg"):
            raise ValueError("target config must be a .cfg file")
        transport = transport or self.mpsse.get("transport", "jtag")
        if transport not in ("jtag", "swd"):
            raise ValueError("transport must be 'jtag' or 'swd'")
        if ports is not None and not isinstance(ports, dict):
            raise ValueError("ports must be an object such as {\"gdb\": 3333, \"telnet\": 4444, \"tcl\": 6666}")
        unknown = set(ports or {}) - set(DEFAULT_PORTS)
        if unknown:
            raise ValueError(f"unknown port names {sorted(unknown)}; use gdb, telnet and tcl")
        new_ports = {**DEFAULT_PORTS, **{k: int(v) for k, v in (ports or {}).items() if v}}
        for k, v in new_ports.items():
            if not 1 <= v <= 65535:
                raise ValueError(f"the {k} port must be 1 to 65535")
        if len(set(new_ports.values())) != len(new_ports):
            raise ValueError("the GDB, telnet and TCL ports must differ")
        if extra is not None and (not isinstance(extra, (list, tuple)) or not all(isinstance(e, str) for e in extra)):
            raise ValueError("extra must be a list of OpenOCD commands")
        self.ports = new_ports
        cmd = self.command_line(target_cfg, transport, self.ports, extra, binary)
        self._add_log("$ " + " ".join(cmd), "cmd")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=RESOURCES,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.target_cfg, self.transport = target_cfg, transport
        threading.Thread(target=self._reader, args=(self.proc,), name="openocd-log", daemon=True).start()
        self._publish("openocd", {"kind": "state", "running": True})
        return self.status()

    def _reader(self, proc: subprocess.Popen):
        for raw in iter(proc.stdout.readline, b""):
            self._add_log(raw.decode("utf-8", "replace").rstrip(), "out")
        proc.wait()
        self._add_log(f"OpenOCD exited with code {proc.returncode}", "cmd")
        self._publish("openocd", {"kind": "state", "running": False, "exit_code": proc.returncode})

    def stop(self) -> Dict[str, Any]:
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                tcl_query(self.ports["tcl"], "shutdown", timeout=2)
            except OSError:
                pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
        return self.status()

    def command(self, command: str, timeout: float = 30.0) -> Dict[str, Any]:
        """Run one OpenOCD command over the TCL port; returns ``ok`` and what it printed or the error."""
        if not self.running():
            raise RuntimeError("OpenOCD is not running")
        command = (command or "").strip()
        if not command:
            raise ValueError("empty command")
        if not braces_balanced(command):
            raise ValueError("unbalanced braces in the command (escape a literal brace as \\{)")
        self._add_log("> " + command, "cmd")
        res = parse_wrapped(tcl_query(self.ports["tcl"], wrap_command(command), timeout=timeout))
        if res["output"]:
            self._add_log(res["output"], "reply" if res["ok"] else "error")
        return {"command": command, **res}

    def program(self, path: str, verify: bool = True, reset: bool = True, address: Optional[str] = None, timeout: float = 300.0) -> Dict[str, Any]:
        """Flash a .hex/.elf/.bin with OpenOCD's ``program`` command (``address`` is needed for .bin files)."""
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        if path.lower().endswith(".bin") and not address:
            raise ValueError("a .bin file needs a flash address (for example 0x08000000)")
        if address not in (None, ""):
            address = str(address).strip()
            try:
                int(address, 0)
            except ValueError:
                raise ValueError(f"the flash address must be a number such as 0x08000000, not {address!r}") from None
        p = os.path.abspath(path).replace("\\", "/")
        cmd = "program " + tcl_word(p) + (" verify" if verify else "") + (" reset" if reset else "") + (f" {address}" if address else "")
        res = self.command(cmd, timeout=timeout)
        res["path"] = path
        return res

    def close(self):
        self.stop()


def model_pid(scope) -> Optional[int]:
    return PIDS.get(model_of(scope) or "")
