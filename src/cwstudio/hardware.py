"""Real-hardware helpers built on the public `chipwhisperer` API."""
from __future__ import annotations

import logging
import os
import platform
import sys
from typing import Any, Dict, List, Optional

log = logging.getLogger("cwstudio.hardware")

SCOPE_KINDS = {
    "auto": {"label": "Auto-detect", "name": None},
    "lite": {"label": "ChipWhisperer-Lite", "name": "Lite"},
    "pro": {"label": "ChipWhisperer-Pro (CW1200)", "name": "Pro"},
    "nano": {"label": "ChipWhisperer-Nano", "name": "Nano"},
    "husky": {"label": "ChipWhisperer-Husky", "name": "Husky"},
    "huskyplus": {"label": "ChipWhisperer-Husky Plus", "name": "HuskyPlus"},
    "sim": {"label": "Simulator (no hardware)", "name": None},
}

TARGET_KINDS = {
    "SimpleSerial2": {"label": "SimpleSerial v2 (default firmware)"},
    "SimpleSerial": {"label": "SimpleSerial v1 (legacy firmware)"},
    "SimpleSerial2_CDC": {"label": "SimpleSerial v2 over USB-CDC"},
    "CW305": {"label": "CW305 Artix FPGA board"},
    "sim": {"label": "Simulated AES target"},
}

PROGRAMMERS = {
    "STM32F": {"label": "STM32F (CWLITEARM, CW-Nano, CW308_STM32Fx, Husky targets)", "cls": "STM32FProgrammer"},
    "XMEGA": {"label": "XMEGA (CWLITEXMEGA, CW303, CW308_XMEGA)", "cls": "XMEGAProgrammer"},
    "AVR": {"label": "AVR (CW308_AVR, CW304 ATmega328P)", "cls": "AVRProgrammer"},
    "SAM4S": {"label": "SAM4S (CW308_SAM4S, Husky)", "cls": "SAM4SProgrammer"},
    "NEORV32": {"label": "NEORV32 (soft-core RISC-V)", "cls": "NEORV32Programmer"},
    "iCE40": {"label": "iCE40 FPGA bitstream (CW312T-iCE40, SPI slave load)", "cls": None, "fpga": "LatticeICE40", "accept": ".bin"},
    "XC7A35T": {"label": "XC7A35T FPGA bitstream (CW312T-A35, SPI slave load)", "cls": None, "fpga": "CW312T_XC7A35T", "accept": ".bit,.bin"},
}


class ScopeConnectError(RuntimeError):
    """A scope connect failed for a reason Studio explains in full (libusb missing, outdated firmware, wrong device kind)."""


NEWAE_VID = 0x2B3E
SAMBA_VID_PID = (0x03EB, 0x6124)  # an ATSAM in its SAM-BA bootloader: a ChipWhisperer whose firmware is erased (for example after an interrupted upgrade) shows up as this serial port
# product IDs of the scopes Studio connects to, and the Device choice each one matches
PID_KINDS = {0xACE2: "lite", 0xACE3: "pro", 0xACE0: "nano", 0xACE5: "husky", 0xACE6: "huskyplus"}
_PID_NAMES = {0xACE2: "ChipWhisperer-Lite", 0xACE3: "ChipWhisperer-CW1200", 0xC305: "CW305 Artix FPGA Board", 0xC310: "CW310 Bergen FPGA Board", 0xC340: "CW340 Luna FPGA Board",
              0xACE0: "ChipWhisperer-Nano", 0xACE5: "ChipWhisperer-Husky", 0xACE6: "ChipWhisperer-Husky-Plus", 0xC521: "CW521 Ballistic-Gel", 0xC610: "PhyWhisperer-USB"}
DRIVERS_URL = "https://chipwhisperer.readthedocs.io/en/latest/drivers.html"
FIRMWARE_URL = "https://chipwhisperer.readthedocs.io/en/latest/firmware.html"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _pid_name(pid: int) -> str:
    try:
        from chipwhisperer.hardware.naeusb.naeusb import NEWAE_PIDS
        return NEWAE_PIDS[pid]["name"]
    except Exception:  # noqa: BLE001
        return _PID_NAMES.get(pid, f"Unknown NewAE device (PID {pid:04x})")


def is_libusb_load_error(e: BaseException) -> bool:
    """True when libusb itself (the native library, or the usb1 Python binding) could not be loaded."""
    text = str(e).lower()
    if isinstance(e, ImportError):
        return "usb1" in text or "libusb" in text
    return isinstance(e, OSError) and ("libusb dll" in text or "libusb" in text and "load" in text)


def libusb_problem(e: BaseException) -> str:
    """A libusb load failure as a message with its real cause and advice that fits how Studio was installed (chipwhisperer's own text hides the cause and always says pip)."""
    cause = e.__cause__ or e.__context__
    why = f"{type(cause).__name__}: {cause}" if cause is not None else f"{type(e).__name__}: {e}"
    sysname = platform.system()
    if _frozen():
        fix = "The libusb library bundled with Studio could not be loaded; reinstall Studio" + (" or install the system libusb (sudo apt install libusb-1.0-0)" if sysname == "Linux" else "") + "."
    elif sysname == "Darwin":
        fix = "Install libusb with Homebrew (brew install libusb), then restart Studio."
    elif sysname == "Linux":
        fix = "Install libusb-1.0 (Debian/Ubuntu: sudo apt install libusb-1.0-0) and reinstall the binding (pip install --force-reinstall libusb1), then restart Studio."
    else:
        fix = "Reinstall the libusb1 Python package, whose wheel ships libusb-1.0.dll (pip install --force-reinstall libusb1), then restart Studio."
    return f"libusb could not be loaded ({why}). {fix}"


def _usb_error_hint(e: BaseException, name: str) -> Dict[str, Any]:
    """Why a NewAE device could not be opened, as a structured hint for the Connect tab."""
    sysname = platform.system()
    code = type(e).__name__
    text = str(e)
    if "ACCESS" in text or code == "USBErrorAccess":
        if sysname == "Linux":
            ph = platform_help()
            return {"problem": "access", "hint": f"Permission denied opening the {name}: install the udev rule once, log out and back in, then unplug and replug it.", "command": ph.get("udev_install_cmd")}
        return {"problem": "access", "hint": f"Could not open the {name}: another program has it open (another Studio, a Jupyter kernel or a Python script using chipwhisperer). Close it, or unplug and replug the device."}
    if "BUSY" in text or code == "USBErrorBusy":
        return {"problem": "busy", "hint": f"The {name} is in use by another program (another Studio, a Jupyter kernel or a Python script). Close it, or unplug and replug the device."}
    if "NOT_SUPPORTED" in text or code == "USBErrorNotSupported":
        if sysname == "Windows":
            return {"problem": "driver", "hint": f"Windows has no WinUSB driver bound to the {name}. In Device Manager, if it is listed under libusb-win32 devices, uninstall that driver (tick Delete the driver software for this device) and replug; see the driver instructions.", "url": DRIVERS_URL}
        return {"problem": "driver", "hint": f"The operating system does not let libusb open the {name} ({text})."}
    return {"problem": "error", "hint": f"Could not open the {name}: {code}: {text}"}


def _at91_ports() -> List[str]:
    try:
        from chipwhisperer.capture.scopes.cwhardware.ChipWhispererSAM3Update import get_at91_ports
        return list(get_at91_ports())
    except Exception:  # noqa: BLE001
        return []


def bootloader_hint(port: Optional[str] = None) -> str:
    where = f" on {port}" if port else ""
    cmd = "import chipwhisperer as cw; cw.program_sam_firmware(hardware_type='cwlite')"
    return (f"A ChipWhisperer is in SAM-BA bootloader mode{where}: its firmware is erased (an interrupted upgrade, or the erase jumper). Reprogram it from Python with {cmd} (hardware_type is cwlite, cwnano, cw1200, cwhusky or cwhuskyplus), "
            f"then unplug and replug it. See {FIRMWARE_URL}")


def list_devices(connected: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """NewAE USB devices, each with name, sn, hw_loc and kind (the Device choice that connects to it), plus entries for problems: devices that could not be opened (``error`` with ``problem`` and ``hint``), boards in bootloader mode and libusb failing to load.

    Opens its own libusb context, so it never waits for the hardware thread. ``connected`` is Studio's cached info about the scope it has open (sn, hw_loc, name): that device is reported from the cache and never opened again here (Windows lets only one handle open a WinUSB device)."""
    try:
        import usb1
        ctx = usb1.USBContext()
        ctx.open()
    except Exception as e:  # noqa: BLE001
        msg = libusb_problem(e) if is_libusb_load_error(e) or isinstance(e, (ImportError, OSError)) else f"USB enumeration failed: {type(e).__name__}: {e}"
        log.warning("USB enumeration failed: %s", msg)
        return [{"name": "USB enumeration failed", "sn": None, "hw_loc": None, "error": True, "problem": "libusb", "hint": msg}]
    out: List[Dict[str, Any]] = []
    boot: List[Dict[str, Any]] = []
    con = connected if connected and connected.get("connected") and not connected.get("simulated") else None
    con_loc = tuple(con["hw_loc"]) if con and con.get("hw_loc") else None
    try:
        for dev in ctx.getDeviceIterator(skip_on_error=True):
            try:
                vid, pid = dev.getVendorID(), dev.getProductID()
            except Exception:  # noqa: BLE001
                continue
            try:
                loc = [dev.getBusNumber(), dev.getDeviceAddress()]
            except Exception:  # noqa: BLE001
                loc = None
            if (vid, pid) == SAMBA_VID_PID:
                boot.append({"name": "ChipWhisperer in bootloader mode", "sn": None, "hw_loc": loc, "bootloader": True, "error": True, "problem": "bootloader"})
                continue
            if vid != NEWAE_VID:
                continue
            name = _pid_name(pid)
            entry: Dict[str, Any] = {"name": name, "sn": None, "hw_loc": loc, "kind": PID_KINDS.get(pid)}
            if con_loc is not None and loc is not None and tuple(loc) == con_loc:
                entry.update(sn=con.get("sn"), in_use=True, note="connected in Studio")
                out.append(entry)
                continue
            try:
                entry["sn"] = dev.getSerialNumber()
            except Exception as e:  # noqa: BLE001
                entry.update(error=True, **_usb_error_hint(e, name))
            out.append(entry)
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    if con is not None and con_loc is None and not any(d.get("in_use") for d in out):
        # the open scope's location is unknown: an entry that could not be opened (Windows refuses a second handle) and is the same model is the one Studio has open
        if not any(d.get("sn") == con.get("sn") for d in out):
            want = _norm(con.get("name"))
            errs = [d for d in out if d.get("error")]
            match = [d for d in errs if _norm(d["name"]) == want] or (errs if len(errs) == 1 else [])
            if match:
                d = match[0]
                for k in ("error", "problem", "hint", "command", "url"):
                    d.pop(k, None)
                d.update(sn=con.get("sn"), in_use=True, note="connected in Studio")
        for d in out:
            if d.get("sn") and d.get("sn") == con.get("sn"):
                d.update(in_use=True, note="connected in Studio")
    if boot:
        ports = _at91_ports()
        for i, b in enumerate(boot):
            port = ports[i] if len(ports) == len(boot) else None
            b.update(port=port, hint=bootloader_hint(port))
        out.extend(boot)
    for d in out:
        if d.get("error"):
            log.warning("%s: %s", d["name"], d.get("hint"))
    return out


def _norm(name: Optional[str]) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum()).replace("cw1200", "pro").replace("chipwhispererpro", "pro")


def empty_scan_hints() -> List[str]:
    """What to check when no NewAE device shows up at all."""
    hints = ["Use a data-capable USB cable (some cables only charge) and plug it straight into the computer, not through an unpowered hub.",
             "Try another USB port, and check that the board's status LED is on."]
    sysname = platform.system()
    if sysname == "Linux":
        hints.append("On Linux, install the udev rule (Platform card below), log out and back in, then replug.")
    elif sysname == "Windows":
        hints.append("On Windows, open Device Manager: the device should be under Universal Serial Bus devices. If it is under libusb-win32 devices, remove that driver and replug (Platform card below).")
    elif sysname == "Darwin":
        hints.append("On macOS, check System Information > USB lists the device.")
    return hints


def connect_failure_hint(kind: str, sn: Optional[str], error: BaseException, devices: List[Dict[str, Any]]) -> str:
    """Advice appended to a failed connect: which devices are attached when the chosen kind or serial number is not, and what is wrong with the ones that cannot be opened."""
    ok = [d for d in devices if not d.get("error")]
    problems = [d for d in devices if d.get("error")]
    label = SCOPE_KINDS.get(kind, {}).get("label", kind)
    attached = ", ".join(f"{d['name']} (sn {d['sn']})" if d.get("sn") else d["name"] for d in ok)
    parts: List[str] = []
    if ok:
        want = SCOPE_KINDS.get(kind, {}).get("name")
        if want and not any(d.get("kind") == kind for d in ok):
            kinds = sorted({SCOPE_KINDS[d["kind"]]["label"] for d in ok if d.get("kind") in SCOPE_KINDS})
            choose = " or ".join(["Auto-detect"] + kinds)
            parts.append(f"No {label} found; attached: {attached}. Choose {choose}.")
        elif sn and not any(d.get("sn") == sn for d in ok):
            parts.append(f"No device with serial number {sn}; attached: {attached}. Clear the serial number or use one of these.")
        elif len(ok) > 1 and not sn:
            parts.append(f"Several devices are attached: {attached}. Enter the serial number of the one to use (Scan USB, then use).")
    for d in problems:
        parts.append(d.get("hint") or d["name"])
    if not devices:
        parts.append("No NewAE USB device is attached. " + " ".join(empty_scan_hints()))
    return " ".join(parts)


def connect_scope(kind: str = "auto", sn: Optional[str] = None, force: bool = False, sim_model: Optional[str] = None, **kwargs):
    if kind == "sim":
        from cwstudio.capabilities import SIM_MODELS
        from cwstudio.simulator import SimScope
        if sim_model and sim_model not in SIM_MODELS:
            raise ValueError(f"sim_model must be one of {', '.join(SIM_MODELS)}")
        return SimScope(sim_model=sim_model or "husky")
    try:
        import chipwhisperer as cw
    except ImportError as e:
        if is_libusb_load_error(e):
            raise ScopeConnectError(libusb_problem(e)) from e
        raise
    name = SCOPE_KINDS.get(kind, {}).get("name")
    args: Dict[str, Any] = {}
    if name:
        args["name"] = name
    if sn:
        args["sn"] = sn
    if force:
        args["force"] = True
    args.update(kwargs)
    try:
        return cw.scope(**args)
    except OSError as e:
        if is_libusb_load_error(e):
            raise ScopeConnectError(libusb_problem(e)) from e
        raise


def connect_target(scope, kind: str = "SimpleSerial2", **kwargs):
    if kind == "sim":
        from cwstudio.simulator import SimTarget
        t = SimTarget()
        t.con(scope)
        return t
    import chipwhisperer as cw
    from chipwhisperer.capture import targets
    cls = getattr(targets, kind, None)
    if cls is None:
        raise ValueError(f"unknown target type {kind}")
    return cw.target(scope, cls, **kwargs)


def program_target(scope, programmer: str, fw_path: str, **kwargs):
    import chipwhisperer as cw
    entry = PROGRAMMERS.get(programmer)
    if entry is None:
        raise ValueError(f"unknown programmer {programmer}")
    if not os.path.isfile(fw_path):
        raise FileNotFoundError(fw_path)
    from cwstudio.capabilities import capabilities, require
    require(capabilities(scope)["programmers"].get(programmer), f"{programmer} programmer")
    if getattr(scope, "_getCWType", lambda: "")() == "cwsim":
        res = {"ok": True, "simulated": True, "bytes": os.path.getsize(fw_path)}
        if hasattr(scope, "load_firmware"):  # an ELF (or a .hex with its .elf) runs in the emulator: real responses and traces from the firmware's execution
            res["emulation"] = scope.load_firmware(fw_path)
        return res
    if entry.get("fpga"):
        from chipwhisperer.hardware.naeusb import programmer_targetfpga
        getattr(programmer_targetfpga, entry["fpga"])(scope).program(fw_path, sck_speed=float(kwargs.get("sck_speed", 10e6)))
        return {"ok": True, "bytes": os.path.getsize(fw_path)}
    cls = getattr(cw.programmers, entry["cls"])
    cw.program_target(scope, cls, fw_path, **kwargs)
    return {"ok": True, "bytes": os.path.getsize(fw_path)}


def scope_info(scope) -> Dict[str, Any]:
    """Name, type, serial number and firmware of a scope. Several of these are USB reads (the type is an FPGA register on OpenADC scopes), so call it only on the hardware thread; Studio caches the result at connect (Session.status reads the cache)."""
    if scope is None:
        return {"connected": False}
    info: Dict[str, Any] = {"connected": True}
    try:
        info["type"] = scope._getCWType()
    except Exception:  # noqa: BLE001
        info["type"] = type(scope).__name__
    for attr in ("sn", "fw_version"):
        try:
            v = getattr(scope, attr)
            info[attr] = v if isinstance(v, (str, int, float, dict, list)) else str(v)
        except Exception:  # noqa: BLE001
            pass
    try:
        info["name"] = scope.get_name()
    except Exception:  # noqa: BLE001
        info["name"] = _pretty_type(info.get("type", ""))
    info["is_husky"] = bool(getattr(scope, "_is_husky", False))
    info["simulated"] = info.get("type") == "cwsim"
    if info["simulated"]:
        info["sim_model"] = getattr(scope, "sim_model", None)
    else:
        try:
            info["hw_loc"] = list(scope._getNAEUSB().hw_location())  # lets the device list report this scope without opening it a second time
        except Exception:  # noqa: BLE001
            pass
    return info


_MISSING = object()


def firmware_outdated(scope) -> Optional[str]:
    """A message when ``cw.scope()`` returned a half-initialised scope because its firmware is too old for this chipwhisperer (newer libraries return it without ``adc`` and with ``fw_up2date`` False; 6.0.0 has neither and is never flagged). Call on the hardware thread."""
    try:
        up = getattr(scope._getNAEUSB(), "fw_up2date", True)
    except Exception:  # noqa: BLE001
        up = True
    try:
        no_adc = getattr(scope, "adc", _MISSING) is None
    except Exception:  # noqa: BLE001
        no_adc = False
    if up is not False and not no_adc:
        return None
    try:
        v = scope.fw_version
        ver = f"{v['major']}.{v['minor']}.{v.get('debug', 0)}"
    except Exception:  # noqa: BLE001
        ver = "on this scope"
    how = ("by following the steps at the link below with a Python install of chipwhisperer (Studio's standalone app does not upgrade firmware itself)" if _frozen() else "from a terminal with python -c \"import chipwhisperer as cw; cw.scope().upgrade_firmware()\"")
    return f"firmware {ver} is older than this ChipWhisperer library needs; upgrade it {how}, then unplug and replug the scope and connect again. See {FIRMWARE_URL}"


def is_device_lost(e: BaseException) -> bool:
    """True when an exception means the USB device went away (unplugged, reset or powered off)."""
    seen = 0
    while e is not None and seen < 5:
        name = type(e).__name__
        text = str(e)
        if name in ("USBErrorNoDevice", "USBErrorIO") or "LIBUSB_ERROR_NO_DEVICE" in text or "LIBUSB_ERROR_IO" in text or "No such device" in text:
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


def _pretty_type(t: str) -> str:
    return {"cwlite": "ChipWhisperer-Lite", "cw1200": "ChipWhisperer-Pro", "cwnano": "ChipWhisperer-Nano",
            "cwhusky": "ChipWhisperer-Husky", "cwhuskyplus": "ChipWhisperer-Husky Plus", "cwsim": "Simulator"}.get(t, t or "scope")


def target_info(target) -> Dict[str, Any]:
    if target is None:
        return {"connected": False}
    return {"connected": True, "type": type(target).__name__,
            "simulated": type(target).__name__ == "SimTarget"}


def platform_help() -> Dict[str, Any]:
    """OS-specific setup hints shown in the Connect tab."""
    sysname = platform.system()
    rules_path = _bundled_udev_rules()
    info: Dict[str, Any] = {"os": sysname, "python": sys.version.split()[0],
                            "frozen": bool(getattr(sys, "frozen", False))}
    if sysname == "Linux":
        info["udev_rules_path"] = rules_path
        info["udev_install_cmd"] = (
            f"sudo cp \"{rules_path}\" /etc/udev/rules.d/50-newae.rules && "
            "sudo groupadd -f chipwhisperer && sudo usermod -aG chipwhisperer $USER && "
            "sudo udevadm control --reload-rules && sudo udevadm trigger"
        )
        info["note"] = "Install the udev rule once, then log out and back in and re-plug the device."
    elif sysname == "Windows":
        info["driver_url"] = DRIVERS_URL
        info["note"] = ("No driver install is needed: ChipWhisperer firmware 0.23 / 1.23 or newer (since November 2020) makes Windows 10 and 11 load the WinUSB driver automatically. "
                        "If Device Manager lists the device under libusb-win32 devices (libusb0, the old NewAE driver package), it will not connect: right-click it, Uninstall device, tick Delete the driver software for this device, then unplug and replug. "
                        "For very old firmware, install WinUSB with Zadig, upgrade the firmware, then remove the Zadig driver the same way.")
    elif sysname == "Darwin":
        if info["frozen"]:
            info["note"] = "No driver needed on macOS; Studio bundles libusb. If the device is not detected, try another cable or port."
        else:
            info["note"] = "No driver needed on macOS, but chipwhisperer installed with pip needs libusb: brew install libusb. If the device is not detected, try another cable or port."
    info["empty_hints"] = empty_scan_hints()
    return info


def _bundled_udev_rules() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "resources", "50-newae.rules")
    return path if os.path.isfile(path) else "50-newae.rules"
