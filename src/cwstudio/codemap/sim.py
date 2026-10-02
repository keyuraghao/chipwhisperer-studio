"""Real firmware on the simulator: when an ELF is programmed into the simulated target, its SimpleSerial commands run in the emulator and the simulated scope's traces come from the emulated execution (the power model of :mod:`cwstudio.codemap.power` at the ADC rate, with noise, gain, offset, pre-trigger samples and the trigger window of the firmware itself).
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import numpy as np

from cwstudio.codemap import power
from cwstudio.codemap.emu import EmuError, Firmware, Run
from cwstudio.codemap.program import Program, ProgramError

log = logging.getLogger("cwstudio.sim")


def elf_for(path: str) -> Optional[str]:
    """The ELF that belongs to a firmware file: the file itself, or the .elf with the same name next to a .hex/.bin (Studio's builds keep both)."""
    if not path:
        return None
    if path.lower().endswith(".elf") and os.path.isfile(path):
        return path
    cand = os.path.splitext(path)[0] + ".elf"
    return cand if os.path.isfile(cand) else None


_NONE64 = np.zeros(0, np.int64)
_NONE8 = np.zeros(0, np.int8)


def _lite(run: Run) -> Run:
    """What the simulated scope needs of a run (output, trigger window, length) without the per-instruction arrays, which would cost tens of bytes per instruction in the cache."""
    return Run(run.output, _NONE64, _NONE64, _NONE64, _NONE8, _NONE64, run.leak_bits, list(run.trig), run.total_cycles, run.trigger_source, run.halted)


class FirmwareSim:
    """The emulated firmware behind a simulated target, with a cache of recent runs (fixed plaintexts cost nothing after the first capture). The cache keeps only the per-cycle power (float32) and is bounded in entries and bytes."""

    CACHE = 256
    CACHE_BYTES = 48 << 20

    def __init__(self, elf: str):
        self.elf = os.path.abspath(elf)
        self.prog = Program(self.elf)
        self.fw = Firmware(self.prog)
        self.fw.machine  # boot now so programming reports start-up problems
        self._cache: "OrderedDict[Tuple[bytes, bytes, str], Tuple[Run, Optional[bytes], np.ndarray]]" = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0
        self._state = None  # firmware state for raw serial traffic
        self.runs = 0
        self.cache_hits = 0

    def info(self) -> Dict[str, Any]:
        d = self.fw.info()
        d.update({"elf": self.elf, "name": os.path.basename(self.elf), "sha256": self.prog.sha256, "runs": self.runs, "cache_hits": self.cache_hits, "cache_entries": len(self._cache), "cache_bytes": self._bytes})
        return d

    def command(self, cmd: str, data: bytes, key: Optional[bytes]) -> Tuple[Run, Optional[bytes], np.ndarray]:
        """Run ``cmd`` (after setting ``key``); returns the run, the 'r' response and the per-cycle model power."""
        ck = (bytes(key or b""), bytes(data), cmd)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None:
                self._cache.move_to_end(ck)
                self.cache_hits += 1
                return hit
        if cmd in ("p", "k") or key:
            run, resp = self.fw.encrypt(key if cmd != "k" else None, data, cmd)
        else:
            run, _ = self.fw.command(cmd, data)
            resp = self.fw.ss.response(run.output, "r")
        P = power.cycle_power(run).astype(np.float32)
        entry = (_lite(run), resp, P)
        size = P.nbytes + len(run.output) + 256
        with self._lock:
            self.runs += 1
            old = self._cache.pop(ck, None)
            if old is not None:
                self._bytes -= old[2].nbytes + len(old[0].output) + 256
            self._cache[ck] = entry
            self._bytes += size
            while len(self._cache) > 1 and (len(self._cache) > self.CACHE or self._bytes > self.CACHE_BYTES):
                _k, (r, _resp, p) = self._cache.popitem(last=False)
                self._bytes -= p.nbytes + len(r.output) + 256
        return entry

    def glitched(self, cmd: str, data: bytes, key: Optional[bytes], cycle: int, count: int = 1) -> Tuple[Run, Optional[bytes], np.ndarray]:
        """Run ``cmd`` with a clock glitch ``cycle`` target cycles after the trigger that makes the core skip ``count`` instructions (the instruction executing at that cycle and the next ones). Not cached. Raises EmuError when the glitched firmware crashes or hangs (the target needs a reset)."""
        if cmd in ("p", "k") or key:
            run, _ = self.fw.encrypt(key if cmd != "k" else None, data, cmd)
        else:
            run, _ = self.fw.command(cmd, data)
        hi, _lo = run.trigger_window()
        c = (hi or 0) + int(cycle)
        i = int(np.searchsorted(run.start, c, side="right")) - 1
        if i < 1 or i >= len(run.start) or c >= run.start[i] + run.dur[i]:
            return self.command(cmd, data, key)  # the glitch missed the code (after the command finished): nothing happens
        skip = (i, max(1, int(count)))
        limit = 4 * run.instructions + 100_000  # a glitch that sends the firmware into a loop counts as a crash quickly
        if cmd in ("p", "k") or key:
            grun, resp = self.fw.encrypt(key if cmd != "k" else None, data, cmd, skip=skip, limit=limit)
        else:
            grun, _ = self.fw.command(cmd, data, skip=skip, limit=limit)
            resp = self.fw.ss.response(grun.output, "r")
        with self._lock:
            self.runs += 1
        return _lite(grun), resp, power.cycle_power(grun).astype(np.float32)

    def raw(self, data: bytes) -> bytes:
        """Feed raw serial bytes (from the serial console) and return what the firmware sends back; the firmware keeps its state between calls."""
        run, self._state = self.fw.command_raw(data, self._state)
        return run.output


def load(scope, path: str) -> Dict[str, Any]:
    """Program ``path`` into the simulated target behind ``scope``: returns what the simulator will do with it."""
    elf = elf_for(path)
    if elf is None:
        scope.firmware = None
        return {"emulated": False, "reason": "no ELF next to this file; the simulator keeps its built-in AES model (build in Studio, or program the .elf)"}
    try:
        fs = FirmwareSim(elf)
    except (ProgramError, EmuError) as e:
        scope.firmware = None
        log.warning("Simulator: %s cannot run in the emulator (%s); using the built-in AES model", os.path.basename(elf), e)
        return {"emulated": False, "reason": str(e)}
    scope.firmware = fs
    info = fs.info()
    log.info("Simulator runs %s (%s, %s, SimpleSerial %s) in the emulator", info["name"], info["arch"], info["core_label"], info["protocol"])
    return {"emulated": True, **info}


def synth(scope, run: Run, P: np.ndarray) -> Tuple[np.ndarray, int]:
    """The trace the simulated scope records for an emulated run, and the trigger length in ADC samples."""
    hi, lo = run.trigger_window()
    t0 = int(hi) if hi is not None else 0
    try:
        adc_freq = float(scope.clock.adc_freq)
        tgt = float(scope.clock.clkgen_freq)
    except Exception:  # noqa: BLE001
        adc_freq, tgt = 4 * 7.37e6, 7.37e6
    dec = max(1, int(getattr(scope.adc, "decimate", 1) or 1))
    m = power.Mapping(spc=adc_freq / tgt / dec, adc_offset=int(scope.adc.offset), presamples=int(getattr(scope.adc, "presamples", 0) or 0), decimate=dec)
    gain = (0.6 + scope.gain.gain / 78.0) * (1.4 if scope.gain.mode == "high" else 0.7)
    wave = power.synth_trace(P, t0, m, int(scope.adc.samples), scope._rng, noise=scope.noise * 0.6, amp=0.03, gain=gain)
    trig = int(((lo if lo is not None else run.total_cycles) - t0) * m.spc)
    return wave, max(0, trig)
