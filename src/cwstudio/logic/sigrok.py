"""External logic analysers and extra protocol decoders through ``sigrok-cli``.

Studio never needs sigrok: its own decoders and file formats work without it. When ``sigrok-cli`` is on the PATH (or ``CWSTUDIO_SIGROK_CLI`` points at it), Studio can list the analysers it sees (``--scan``), capture from one into a ``.sr`` session and import it, and run sigrok's protocol decoders (``-i file -P decoder``) on any capture.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

INSTALL = {
    "summary": "Install sigrok-cli to capture from external logic analysers (Saleae clones, DSLogic, fx2lafw, ...) and to use sigrok's protocol decoders. Studio's own decoders and file import work without it.",
    "linux": ["Debian/Ubuntu/Kali: sudo apt install sigrok-cli", "Fedora: sudo dnf install sigrok-cli", "Arch: sudo pacman -S sigrok-cli", "or the AppImage from https://sigrok.org/wiki/Downloads"],
    "darwin": ["Homebrew: brew install sigrok-cli"],
    "win32": ["the sigrok-cli installer from https://sigrok.org/wiki/Downloads (add its folder to PATH)"],
    "url": "https://sigrok.org/wiki/Downloads",
    "udev": "On Linux, USB analysers also need sigrok's udev rules (they come with the distribution package) and a replug.",
}

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def find_cli() -> Optional[str]:
    p = os.environ.get("CWSTUDIO_SIGROK_CLI")
    if p:
        return p if os.path.exists(p) else shutil.which(p)
    return shutil.which("sigrok-cli")


def _run(args: List[str], timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
    exe = find_cli()
    if not exe:
        raise RuntimeError("sigrok-cli is not installed")
    cmd = [exe] + args
    if exe.endswith(".py"):
        cmd = [sys.executable] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # no console window flashing up from the windowed Windows executable
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError(f"sigrok-cli failed: {msg[-1] if msg else 'exit code %d' % r.returncode}")
    return r


def status(refresh: bool = False) -> Dict[str, Any]:
    """{available, reason, path, version, install}. The version check runs once per sigrok-cli path."""
    exe = find_cli()
    install = {"summary": INSTALL["summary"], "steps": INSTALL.get(sys.platform if sys.platform in ("darwin", "win32") else "linux", []), "url": INSTALL["url"], "note": INSTALL["udev"] if sys.platform.startswith("linux") else None}
    if not exe:
        return {"available": False, "reason": "sigrok-cli is not installed; install it to use external logic analysers", "path": None, "version": None, "install": install}
    try:
        mtime = os.path.getmtime(exe)
    except OSError:
        mtime = 0
    key = f"{exe}:{mtime}"
    with _lock:
        if not refresh and _cache.get("key") == key:
            return dict(_cache["status"])
    try:
        r = _run(["--version"], timeout=10)
        first = (r.stdout or r.stderr).strip().splitlines()
        m = re.search(r"sigrok-cli\s+([\w.\-]+)", first[0] if first else "")
        st = {"available": True, "reason": None, "path": exe, "version": m.group(1) if m else (first[0] if first else "?"), "install": install}
    except Exception as e:  # noqa: BLE001
        st = {"available": False, "reason": f"sigrok-cli at {exe} does not run: {e}", "path": exe, "version": None, "install": install}
    with _lock:
        _cache["key"] = key
        _cache["status"] = st
    return dict(st)


def scan(timeout: float = 30) -> List[Dict[str, Any]]:
    """Analysers sigrok-cli finds: [{driver, conn, id, description, channels}]. ``id`` is what ``-d`` takes."""
    r = _run(["--scan"], timeout=timeout)
    out = []
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^\s*([\w\-]+)(?::conn=([^\s]+))?\s+-\s+(.*?)(?:\s+with\s+(\d+)\s+channels?:\s*(.*))?$", line)
        if not m or line.strip().startswith("The following"):
            continue
        driver, conn, desc, _n, chans = m.groups()
        out.append({"driver": driver, "conn": conn, "id": driver + (f":conn={conn}" if conn else ""), "description": desc.strip(), "channels": (chans or "").split()})
    return out


def decoders(timeout: float = 30) -> List[Dict[str, str]]:
    """sigrok's protocol decoders from ``sigrok-cli -L``: [{id, name}]."""
    with _lock:
        if "decoders" in _cache and _cache.get("dec_key") == _cache.get("key"):
            return list(_cache["decoders"])
    r = _run(["-L"], timeout=timeout)
    out, on = [], False
    for line in (r.stdout or "").splitlines():
        if line.strip().lower().startswith("supported protocol decoders"):
            on = True
            continue
        if on:
            if not line.startswith(" "):
                if line.strip():
                    on = False
                continue
            m = re.match(r"^\s+([\w\-]+)\s+(.*)$", line)
            if m:
                out.append({"id": m.group(1), "name": m.group(2).strip()})
    with _lock:
        _cache["decoders"] = out
        _cache["dec_key"] = _cache.get("key")
    return out


def capture_args(device: str, out_path: str, samplerate: Optional[float] = None, channels: Optional[List[str]] = None, samples: Optional[int] = None, time_ms: Optional[float] = None, triggers: Optional[str] = None, config: Optional[str] = None) -> List[str]:
    """The sigrok-cli arguments of a capture into ``out_path`` (.sr). ``triggers`` uses sigrok's syntax, e.g. ``D0=r,D1=1`` (0, 1, r, f, e)."""
    if not device:
        raise ValueError("choose a sigrok device (driver, e.g. fx2lafw or demo)")
    args = ["-d", device]
    cfg = []
    if samplerate:
        cfg.append(f"samplerate={int(samplerate)}")
    if config:
        cfg.append(config)
    if cfg:
        args += ["--config", ":".join(cfg)]
    if channels:
        args += ["-C", ",".join(channels)]
    if samples:
        args += ["--samples", str(int(samples))]
    elif time_ms:
        args += ["--time", f"{float(time_ms):g}ms"]
    else:
        args += ["--samples", "100000"]
    if triggers:
        if not re.fullmatch(r"\s*[\w\-]+=[01rfe](\s*,\s*[\w\-]+=[01rfe])*\s*", triggers):
            raise ValueError("sigrok triggers look like D0=r,D1=1 (levels 0/1, edges r/f/e)")
        args += ["--triggers", triggers.replace(" ", ""), "--wait-trigger"]
    return args + ["-o", out_path]


def start_capture(args: List[str]) -> subprocess.Popen:
    exe = find_cli()
    if not exe:
        raise RuntimeError("sigrok-cli is not installed")
    cmd = ([sys.executable] if exe.endswith(".py") else []) + [exe] + args
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def check_spec(spec: str) -> str:
    spec = (spec or "").strip()
    if not re.fullmatch(r"[\w\-]+(:[\w\-]+=[^\s,:]+)*(,[\w\-]+(:[\w\-]+=[^\s,:]+)*)*", spec):
        raise ValueError("a sigrok decoder spec looks like uart:rx=D0:baudrate=115200")
    return spec


def decode(sr_path: str, spec: str, timeout: float = 120) -> List[Dict[str, Any]]:
    """Run a sigrok protocol decoder stack (``-P uart:rx=D0:baudrate=115200``) on a .sr file and return [{s, e, row, text}]."""
    spec = check_spec(spec)
    r = _run(["-i", sr_path, "-P", spec, "--protocol-decoder-samplenum"], timeout=timeout)
    out = []
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^\s*(\d+)-(\d+)\s+([\w\-]+):\s*(.*)$", line)
        if not m:
            continue
        s, e, row, text = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4).strip()
        cm = re.match(r"^([\w\-]+):\s+(.*)$", text)
        if cm and " " not in cm.group(1):
            row, text = f"{row} {cm.group(1)}", cm.group(2)
        out.append({"s": s, "e": e, "row": row, "text": text.strip('"')})
    return out
