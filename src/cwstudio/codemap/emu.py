"""Run a firmware ELF for given SimpleSerial input and record what executed when.

Arm Cortex-M (Thumb/Thumb-2) and RISC-V (RV32IMC) run in Unicorn; AVR and XMEGA run in :mod:`cwstudio.codemap.avr`. The emulation is HAL agnostic: instead of emulating peripherals, the ChipWhisperer firmware functions that talk to hardware are hooked by symbol name.

* ``getch`` / ``putch`` (also the AVR HALs' ``input_ch_0`` / ``output_ch_0``) are fed from and collected into byte buffers; a ``getch`` with nothing queued stops the run, which is where the firmware waits for the next command.
* ``trigger_high`` / ``trigger_low`` mark the trigger window (the AVR HALs raise the trigger with a port write instead, which is watched: XMEGA ``PORTA.OUTSET/OUTCLR`` bit 0, ATmega ``PORTC`` bit 0).
* ``platform_init``, ``init_uart``, ``trigger_setup``, the LED helpers and other clock and peripheral set-up functions return at once.
* Any other peripheral access is harmless: unmapped addresses far from RAM become I/O pages that read back the last value written (0 at reset) and ignore everything else; RISC-V CSR instructions Unicorn rejects are skipped.

A run records every executed instruction (program counter, start cycle and duration from the core's cycle model, instruction class) and a leakage value per instruction (the data byte or word it loaded, stored or computed, -1 for none), relative to the start of the run.
"""
from __future__ import annotations

import logging
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from cwstudio.codemap import cycles as cy
from cwstudio.codemap.program import Program

log = logging.getLogger("cwstudio.codemap")

SKIP_FUNCS = ("platform_init", "init_uart", "init_uart0", "trigger_setup", "led_ok", "led_error", "SystemInit", "SystemClock_Config", "HAL_Init", "system_init_flash", "SystemCoreClockUpdate", "neorv32_rte_setup")
GETCH = ("getch", "input_ch_0")
PUTCH = ("putch", "output_ch_0")
TRIG_HIGH = ("trigger_high",)
TRIG_LOW = ("trigger_low",)
FALLBACK_WINDOW = ("aes_indep_enc", "AES128_ECB_indp_crypto", "get_pt")
BOOT_LIMIT = 5_000_000
RUN_LIMIT = 4_000_000


class EmuError(RuntimeError):
    """The firmware cannot be emulated (unsupported architecture, missing getch/putch, fault); the message says why."""


@dataclass
class Run:
    """What one command did on the emulated target."""

    output: bytes
    pcs: np.ndarray  # byte address of each executed instruction
    start: np.ndarray  # start cycle of each instruction (cycle 0 = start of the run)
    dur: np.ndarray  # cycles each instruction took
    cls: np.ndarray  # instruction class (cycles.CLASS_NAMES)
    leak: np.ndarray  # data value per instruction (-1 none)
    leak_bits: int  # width of the leak values (8 on AVR, 32 on Arm and RISC-V)
    trig: List[Tuple[str, int]] = field(default_factory=list)  # ("high"|"low", cycle)
    total_cycles: int = 0
    trigger_source: str = "symbol"

    @property
    def instructions(self) -> int:
        return int(len(self.pcs))

    def trigger_window(self) -> Tuple[Optional[int], Optional[int]]:
        hi = next((c for k, c in self.trig if k == "high"), None)
        lo = next((c for k, c in self.trig if k == "low" and (hi is None or c >= hi)), None)
        return hi, lo


class SimpleSerial:
    """SimpleSerial framing on the target side: v2.1 (COBS frames with CRC, 'p' and 'k' as command 0x01 with sub-commands 1 and 2) or v1.x (ASCII hex lines)."""

    def __init__(self, version: str = "2.1"):
        if version not in ("2.1", "1.1", "1.0"):
            raise ValueError("SimpleSerial version must be 2.1, 1.1 or 1.0")
        self.version = version

    @staticmethod
    def crc(buf: Sequence[int]) -> int:
        c = 0
        for b in buf:
            c ^= b
            for _ in range(8):
                c = ((c << 1) ^ 0x4D) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        return c

    def frame(self, cmd: str, data: bytes, scmd: Optional[int] = None) -> bytes:
        data = bytes(data)
        if self.version != "2.1":
            return (cmd + data.hex() + "\n").encode()
        if scmd is None:
            if cmd == "p":
                c, scmd = 0x01, 0x01
            elif cmd == "k":
                c, scmd = 0x01, 0x02
            else:
                c, scmd = ord(cmd[0]), 0
        else:
            c = ord(cmd[0]) if isinstance(cmd, str) else int(cmd)
        buf = [0, c, scmd, len(data)] + list(data)
        buf.append(self.crc(buf[1:]))
        buf.append(0)
        last = 0
        for i in range(1, len(buf)):
            if buf[i] == 0:
                buf[last] = i - last
                last = i
        return bytes(buf)

    def parse(self, out: bytes) -> List[Dict[str, Any]]:
        """Split target output into responses: [{cmd, data (bytes), ok}]."""
        res: List[Dict[str, Any]] = []
        if self.version != "2.1":
            for line in out.decode("ascii", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    res.append({"cmd": line[0], "data": bytes.fromhex(line[1:]), "ok": True})
                except ValueError:
                    res.append({"cmd": line[0], "data": line[1:].encode(), "ok": False})
            return res
        frames = out.split(b"\x00")
        for fr in frames:
            if len(fr) < 4:
                continue
            buf = bytearray(fr + b"\x00")
            n = buf[0]
            buf[0] = 0
            while n < len(buf) - 1:
                nxt = buf[n]
                buf[n] = 0
                if nxt == 0:
                    break
                n += nxt
            cmd, ln = buf[1], buf[2]
            data = bytes(buf[3:3 + ln])
            ok = len(buf) >= 4 + ln and self.crc(buf[1:3 + ln]) == buf[3 + ln]
            res.append({"cmd": chr(cmd), "data": data, "ok": ok})
        return res

    def response(self, out: bytes, cmd: str = "r") -> Optional[bytes]:
        for r in self.parse(out):
            if r["cmd"] == cmd:
                return r["data"]
        return None


def detect_protocol(prog: Program) -> str:
    s = prog.symbols
    if "unstuff_data" in s or "stuff_data" in s:
        return "2.1"
    if "hex_decode" in s or "simpleserial_get" in s:
        return "1.1"
    return "2.1"


def _first(prog: Program, names: Sequence[str]) -> Optional[int]:
    for n in names:
        if n in prog.symbols:
            return prog.symbols[n]
    return None


def _hook_read_after(uc, fn) -> None:
    """UC_HOOK_MEM_READ_AFTER (the callback sees the value read); the Python binding leaves it out of hook_add, so register it the way hook_add does, falling back to a read hook that fetches the value itself."""
    import unicorn as U
    try:
        from unicorn.unicorn_py3 import unicorn as m
        cb = m.uccallback(uc, m.HOOK_MEM_ACCESS_CFUNC)(lambda uc_, access, addr, size, value, key: fn(uc_, access, addr, size, value, None))
        uc._Uc__do_hook_add(U.UC_HOOK_MEM_READ_AFTER, cb, 1, 0)
        return
    except Exception as e:  # noqa: BLE001
        log.debug("UC_HOOK_MEM_READ_AFTER unavailable (%s); reading values in a read hook", e)

    def rd(uc_, access, addr, size, value, ud):
        try:
            value = int.from_bytes(bytes(uc_.mem_read(addr, size)), "little")
        except Exception:  # noqa: BLE001
            value = 0
        fn(uc_, access, addr, size, value, ud)
    uc.hook_add(U.UC_HOOK_MEM_READ, rd)


# --- machines --------------------------------------------------------------------------------------------
class _Machine:
    leak_bits = 32

    def __init__(self, prog: Program, core: str, wait_states: int, options: Dict[str, Any]):
        self.prog = prog
        self.core = core
        self.ws = int(wait_states)
        self.opt = options
        self.inq = bytearray()
        self.out = bytearray()
        self.trig: List[Tuple[str, int]] = []
        self.snap = None
        self.blocked = False
        self.fault: Optional[str] = None
        self.getch = _first(prog, options.get("getch") or GETCH)
        self.putch = _first(prog, options.get("putch") or PUTCH)
        if self.getch is None or self.putch is None:
            raise EmuError("the firmware has no getch/putch function to feed SimpleSerial input through (expected one of " + ", ".join(GETCH) + "); give their names in the code map options")
        self.trigger_source = "symbol"

    def skip_list(self) -> List[str]:
        names = list(SKIP_FUNCS) + list(self.opt.get("skip") or [])
        return [n for n in names if n in self.prog.symbols and n not in (self.opt.get("noskip") or [])]


class UnicornMachine(_Machine):
    def __init__(self, prog: Program, core: str, wait_states: int, options: Dict[str, Any]):
        super().__init__(prog, core, wait_states, options)
        try:
            import unicorn as U
        except ImportError as e:  # pragma: no cover
            raise EmuError("the Unicorn engine is not installed (pip install unicorn)") from e
        self.U = U
        if prog.arch == "arm":
            from unicorn import arm_const as A
            self.A = A
            mode = U.UC_MODE_THUMB | U.UC_MODE_MCLASS
            self.uc = U.Uc(U.UC_ARCH_ARM, mode)
            model = {"cortex-m0": A.UC_CPU_ARM_CORTEX_M0, "cortex-m0+": A.UC_CPU_ARM_CORTEX_M0, "cortex-m3": A.UC_CPU_ARM_CORTEX_M3, "cortex-m33": A.UC_CPU_ARM_CORTEX_M33, "cortex-m7": A.UC_CPU_ARM_CORTEX_M7}.get(core, A.UC_CPU_ARM_CORTEX_M4)
            if model in (A.UC_CPU_ARM_CORTEX_M0,):
                model = A.UC_CPU_ARM_CORTEX_M4  # Unicorn's M0 lacks some system registers the start-up code touches; the M4 runs v6-M code unchanged
            try:
                self.uc.ctl_set_cpu_model(model)
            except Exception:  # noqa: BLE001
                pass
            self.PC, self.SP, self.LR, self.RET = A.UC_ARM_REG_PC, A.UC_ARM_REG_SP, A.UC_ARM_REG_LR, A.UC_ARM_REG_R0
            self.thumb = 1
        elif prog.arch == "riscv":
            from unicorn import riscv_const as RV
            self.A = RV
            if prog.elfclass != 32:
                raise EmuError("only 32-bit RISC-V (RV32) firmware is supported")
            self.uc = U.Uc(U.UC_ARCH_RISCV, U.UC_MODE_RISCV32)
            self.PC, self.SP, self.LR, self.RET = RV.UC_RISCV_REG_PC, RV.UC_RISCV_REG_SP, RV.UC_RISCV_REG_RA, RV.UC_RISCV_REG_A0
            self.thumb = 0
        else:  # pragma: no cover
            raise EmuError(f"Unicorn cannot run {prog.arch}")
        self.pages: Dict[int, str] = {}  # page -> "ram" | "io"
        self.io_latch: Dict[int, int] = {}
        self.pcs: List[int] = []
        self.mem_idx: List[int] = []
        self.mem_val: List[int] = []
        self.fault_addr: Optional[int] = None
        self.info: Dict[int, Tuple[int, int, int]] = {}
        self._map_image()
        self._install_hooks()

    # memory -------------------------------------------------------------------------------------------
    def _map(self, lo: int, hi: int, kind: str = "ram") -> None:
        lo &= ~0xFFF
        hi = min((hi + 0xFFF) & ~0xFFF, 1 << 32)
        start = None
        for p in range(lo, hi, 0x1000):
            if p in self.pages:
                if start is not None:
                    self._map_block(start, p, kind)
                    start = None
                continue
            if start is None:
                start = p
        if start is not None:
            self._map_block(start, hi, kind)

    def _map_block(self, lo: int, hi: int, kind: str) -> None:
        self.uc.mem_map(lo, hi - lo)
        for p in range(lo, hi, 0x1000):
            self.pages[p] = kind

    def _map_io_page(self, page: int) -> None:
        latch = self.io_latch

        def rd(uc, off, size, ud, base=page):
            v = 0
            for i in range(size):
                v |= latch.get(base + off + i, 0) << (8 * i)
            return v

        def wr(uc, off, size, value, ud, base=page):
            for i in range(size):
                latch[base + off + i] = (value >> (8 * i)) & 0xFF
        self.uc.mmio_map(page, 0x1000, rd, None, wr, None)
        self.pages[page] = "io"

    def _map_image(self) -> None:
        prog = self.prog
        for s in prog.segments:
            self._map(s.paddr, s.paddr + s.memsz)
            self._map(s.vaddr, s.vaddr + s.memsz)
        for s in prog.segments:
            if s.data:
                self.uc.mem_write(s.paddr, s.data)
                if s.vaddr != s.paddr:
                    self.uc.mem_write(s.vaddr, s.data)
        self.ram_ranges = [(s.vaddr, s.vaddr + s.memsz) for s in prog.segments if s.writable]
        if prog.arch == "arm":
            vt = self._vector_table()
            sp, reset = struct.unpack("<II", prog.read(vt, 8))
            self.initial_sp = sp
            self.reset = reset & ~1
            if sp:
                self._map(max(0, sp - 0x10000), sp + 0x100)
                self.ram_ranges.append((max(0, sp - 0x10000), sp))
        else:
            self.initial_sp = 0
            self.reset = prog.entry
        # stack and heap often live just past the last RAM section: map the neighbourhood of RAM
        for lo, hi in list(self.ram_ranges):
            self._map(lo, hi + 0x10000)

    def _vector_table(self) -> int:
        for name in (".isr_vector", ".vectors", ".vector_table", ".text.vectors", ".exception_table"):
            if name in self.prog.sections:
                return self.prog.sections[name][0]
        ex = [s for s in self.prog.segments if s.executable]
        return min(s.vaddr for s in ex) if ex else 0

    def _ram_page(self, addr: int) -> bool:
        return any(lo - 0x100000 <= addr < hi + 0x100000 for lo, hi in self.ram_ranges)

    # hooks ----------------------------------------------------------------------------------------------
    def _patch_return(self, addr: int, value_zero: bool, size: int) -> None:
        if self.prog.arch == "arm":
            code = b"\x00\x20\x70\x47" if value_zero and size >= 4 else b"\x70\x47"  # movs r0, #0; bx lr
        else:
            if value_zero and size >= 8:
                code = struct.pack("<II", 0x00000513, 0x00008067)  # li a0, 0; ret
            elif value_zero and size >= 4:
                code = struct.pack("<HH", 0x4501, 0x8082)  # c.li a0, 0; c.jr ra
            elif size >= 4 or size == 0:
                code = struct.pack("<I", 0x00008067)
            else:
                code = struct.pack("<H", 0x8082)
        self.uc.mem_write(addr, code)

    def _fsize(self, addr: int) -> int:
        f = self.prog.func_at(addr)
        return (f.hi - f.lo) if f is not None and f.lo == addr else 4

    def _install_hooks(self) -> None:
        U = self.U
        uc = self.uc
        pcs = self.pcs
        app = pcs.append

        def code(uc, addr, size, ud):
            app(addr)
        uc.hook_add(U.UC_HOOK_CODE, code)
        midx, mval = self.mem_idx.append, self.mem_val.append

        def mem(uc, access, addr, size, value, ud):
            midx(len(pcs) - 1)
            mval(value & 0xFFFFFFFF)
        uc.hook_add(U.UC_HOOK_MEM_WRITE, mem)
        _hook_read_after(uc, mem)

        def unmapped(uc, access, addr, size, value, ud):
            self.fault_addr = addr
            return False
        uc.hook_add(U.UC_HOOK_MEM_READ_UNMAPPED | U.UC_HOOK_MEM_WRITE_UNMAPPED, unmapped)

        def intr(uc, no, ud):
            pc = uc.reg_read(self.PC)
            if self.prog.arch == "riscv" and no == 2:
                try:
                    w = struct.unpack("<I", bytes(uc.mem_read(pc, 4)))[0]
                except Exception:  # noqa: BLE001
                    w = 0
                if w & 0x7F == 0x73:  # CSR access or WFI/ECALL-like system instruction Unicorn rejects: skip it
                    rd = (w >> 7) & 31
                    if rd and (w >> 12) & 7:
                        uc.reg_write(self.A.UC_RISCV_REG_X0 + rd, 0)
                    uc.reg_write(self.PC, pc + 4)
                    return
            self.fault = f"exception {no} at 0x{pc:x}" + (f" in {self.prog.func_at(pc).name}" if self.prog.func_at(pc) else "")
            uc.emu_stop()
        uc.hook_add(U.UC_HOOK_INTR, intr)

        for name in self.skip_list():
            a = self.prog.symbols[name]
            self._patch_return(a, True, self._fsize(a))
        for a in (self.getch, self.putch):
            self._patch_return(a, False, self._fsize(a))

        def h_getch(uc, addr, size, ud):
            if not self.inq:
                self.blocked = True
                if pcs:
                    pcs.pop()  # this instruction is not part of the run
                self.snap = self._save()
                uc.emu_stop()
                return
            uc.reg_write(self.RET, self.inq.pop(0))
        uc.hook_add(U.UC_HOOK_CODE, h_getch, begin=self.getch, end=self.getch)

        def h_putch(uc, addr, size, ud):
            self.out.append(uc.reg_read(self.RET) & 0xFF)
        uc.hook_add(U.UC_HOOK_CODE, h_putch, begin=self.putch, end=self.putch)
        th = _first(self.prog, self.opt.get("trigger_high") or TRIG_HIGH)
        tl = _first(self.prog, self.opt.get("trigger_low") or TRIG_LOW)
        if th is not None and tl is not None:
            for a, kind in ((th, "high"), (tl, "low")):
                self._patch_return(a, False, self._fsize(a))
                uc.hook_add(U.UC_HOOK_CODE, lambda uc, addr, size, ud, kind=kind: self.trig.append((kind, len(pcs) - 1)), begin=a, end=a)
        else:
            self.trigger_source = "function"

    def _save(self):
        ram = [(p, bytes(self.uc.mem_read(p, 0x1000))) for p, k in sorted(self.pages.items()) if k == "ram" and self._ram_page(p)]
        return (self.uc.context_save(), ram, dict(self.io_latch))

    def _load(self, snap) -> None:
        ctx, ram, latch = snap
        self.uc.context_restore(ctx)
        for p, b in ram:
            self.uc.mem_write(p, b)
        self.io_latch.clear()
        self.io_latch.update(latch)

    # running ----------------------------------------------------------------------------------------------
    def _go(self, begin: int, limit: int) -> None:
        U = self.U
        self.blocked = False
        self.fault = None
        while True:
            self.fault_addr = None
            n0 = len(self.pcs)
            try:
                self.uc.emu_start(begin | self.thumb, 0xFFFFFFFF if self.thumb else 0xFFFFFFFC, count=max(1, limit))
            except U.UcError as e:
                if e.errno in (U.UC_ERR_READ_UNMAPPED, U.UC_ERR_WRITE_UNMAPPED) and self.fault_addr is not None:
                    page = self.fault_addr & ~0xFFF
                    if page in self.pages:
                        raise EmuError(f"memory access at 0x{self.fault_addr:x} failed") from e
                    if self._ram_page(page):
                        self._map(page, page + 0x1000)
                    else:
                        self._map_io_page(page)
                    begin = self.uc.reg_read(self.PC)
                    if self.pcs and len(self.pcs) > n0:
                        self.pcs.pop()  # the faulting instruction runs again
                    limit -= len(self.pcs) - n0
                    continue
                pc = self.uc.reg_read(self.PC)
                f = self.prog.func_at(pc)
                raise EmuError(f"the firmware faulted ({e}) at 0x{pc:x}" + (f" in {f.name}" if f else "")) from e
            break
        if self.fault:
            raise EmuError("the firmware stopped: " + self.fault)

    def boot(self) -> None:
        self.uc.reg_write(self.SP, self.initial_sp or 0)
        self.pcs.clear()
        self._go(self.reset, BOOT_LIMIT)
        if not self.blocked:
            raise EmuError(f"the firmware did not reach getch within {BOOT_LIMIT} instructions after reset (stuck at 0x{self.uc.reg_read(self.PC):x}); a hardware init function may need to be skipped")
        self.pcs.clear()
        self.mem_idx.clear()
        self.mem_val.clear()

    def run(self, inp: bytes, snap) -> Run:
        self._load(snap)
        self.inq[:] = inp
        self.out.clear()
        self.trig.clear()
        self.pcs.clear()
        self.mem_idx.clear()
        self.mem_val.clear()
        self.snap = None
        self._go(self.getch, RUN_LIMIT)
        if not self.blocked:
            raise EmuError(f"the command did not finish within {RUN_LIMIT} instructions")
        pcs = np.asarray(self.pcs, np.int64)
        info = self.info
        for u in np.unique(pcs).tolist():
            if u not in info:
                info[u] = self._classify(u)
        dur, cls = cy.cycles_for(self.prog.arch, self.core, pcs, info, self.ws)
        start = np.zeros(len(pcs), np.int64)
        if len(pcs):
            np.cumsum(dur[:-1], out=start[1:])
        leak = np.full(len(pcs), -1, np.int64)
        if self.mem_idx:
            mi = np.asarray(self.mem_idx, np.int64)
            ok = mi >= 0
            leak[mi[ok]] = np.asarray(self.mem_val, np.int64)[ok]
        trig = [(k, int(start[i]) if 0 <= i < len(start) else 0) for k, i in self.trig]
        total = int(start[-1] + dur[-1]) if len(pcs) else 0
        return Run(bytes(self.out), pcs, start, dur, cls, leak, 32, trig, total, self.trigger_source)

    def _classify(self, pc: int) -> Tuple[int, int, int]:
        try:
            b = bytes(self.uc.mem_read(pc, 4))  # what actually ran, including the patched returns of hooked functions
        except Exception:  # noqa: BLE001
            b = self.prog.read(pc, 4)
        if self.prog.arch == "arm":
            hw1, hw2 = struct.unpack("<HH", b)
            return cy.classify_thumb(hw1, hw2)
        return cy.classify_riscv(struct.unpack("<I", b)[0])


class AVRMachine(_Machine):
    leak_bits = 8

    def __init__(self, prog: Program, core: str, wait_states: int, options: Dict[str, Any]):
        super().__init__(prog, core, wait_states, options)
        from cwstudio.codemap import avr
        self.avr = avr
        dev = prog.avr_device or {}
        xmega = core == "avrxmega"
        flash_size = int(dev.get("flash_size") or 0) or max((s.paddr + len(s.data) for s in prog.segments if s.paddr < 0x800000), default=0x8000)
        flash = bytearray(b"\xff" * max(flash_size, 2))
        data_image: Dict[int, bytes] = {}
        for s in prog.segments:
            if s.vaddr >= 0x800000:
                data_image[s.vaddr - 0x800000] = s.data
            if s.paddr < 0x800000 and s.data:
                end = min(len(flash), s.paddr + len(s.data))
                flash[s.paddr:end] = s.data[: end - s.paddr]
        sram_start = int(dev.get("sram_start") or (0x2000 if xmega else 0x100))
        sram_size = int(dev.get("sram_size") or (0x2000 if xmega else 0x800))
        pc22 = flash_size > 0x20000
        self.cpu = avr.AVRCore(bytes(flash), xmega=xmega, pc22=pc22, sram_start=sram_start, sram_size=sram_size, data_image=data_image)
        self.cpu.sp = sram_start + sram_size - 1
        self.tp: List[int] = []
        self.tc: List[int] = []
        self.tl: List[int] = []
        self._install_hooks()

    def _install_hooks(self) -> None:
        cpu, avr = self.cpu, self.avr
        R = cpu.R
        for name in self.skip_list():
            cpu.add_hook(self.prog.symbols[name], lambda c: (c.R.__setitem__(24, 0), None)[1])

        def getch(c):
            if not self.inq:
                self.blocked = True
                raise avr.Blocked()
            R[24] = self.inq.pop(0)
            R[25] = 0
            return None
        cpu.add_hook(self.getch, getch)

        def putch(c):
            self.out.append(R[24])
            return None
        cpu.add_hook(self.putch, putch)
        th = _first(self.prog, self.opt.get("trigger_high") or TRIG_HIGH)
        tl = _first(self.prog, self.opt.get("trigger_low") or TRIG_LOW)
        tp = self.tp
        if th is not None and tl is not None:
            cpu.add_hook(th, lambda c: (self.trig.append(("high", len(tp) - 1)), None)[1])
            cpu.add_hook(tl, lambda c: (self.trig.append(("low", len(tp) - 1)), None)[1])
            return
        port = self.opt.get("trigger_port")
        if port is None:
            port = ("xmega", 0x0600, 0) if cpu.xmega else ("avr", 0x28, 0)
        kind, base, bit = port
        m = 1 << bit
        if kind == "xmega":  # PORTx.OUT, OUTSET, OUTCLR, OUTTGL
            state = {"v": 0}

            def watch(c, a, v, base=base):
                old = state["v"]
                off = a - base
                new = v if off == 4 else (old | v) if off == 5 else (old & ~v) if off == 6 else (old ^ v)
                state["v"] = new & 0xFF
                if (old ^ new) & m:
                    self.trig.append(("high" if new & m else "low", len(tp) - 1))
            for off in (4, 5, 6, 7):
                cpu.write_watch[base + off] = watch
        else:
            state = {"v": 0}

            def watch(c, a, v):
                old = state["v"]
                state["v"] = v
                if (old ^ v) & m:
                    self.trig.append(("high" if v & m else "low", len(tp) - 1))
            cpu.write_watch[base] = watch
        self.trigger_source = "port"

    def boot(self) -> None:
        cpu = self.cpu
        cpu.pc = 0
        self.blocked = False
        try:
            cpu.run(BOOT_LIMIT)
        except self.avr.Fault as e:
            raise EmuError(f"the firmware faulted during start-up: {e}") from e
        if not self.blocked:
            raise EmuError(f"the firmware did not reach getch within {BOOT_LIMIT} instructions after reset (stuck at 0x{cpu.pc * 2:x}); a hardware init function may need to be skipped")
        self.snap = cpu.snapshot()

    def run(self, inp: bytes, snap) -> Run:
        cpu = self.cpu
        cpu.restore(snap)
        cpu.cycles = 0
        self.inq[:] = inp
        self.out.clear()
        self.trig.clear()
        tp, tc, tl = self.tp, self.tc, self.tl
        tp.clear()
        tc.clear()
        tl.clear()
        self.blocked = False
        try:
            cpu.run(RUN_LIMIT, (tp, tc, tl))
        except self.avr.Fault as e:
            raise EmuError(f"the firmware faulted: {e}") from e
        if not self.blocked:
            raise EmuError(f"the command did not finish within {RUN_LIMIT} instructions")
        self.snap = cpu.snapshot()
        pcs = np.asarray(tp, np.int64) * 2
        start = np.asarray(tc, np.int64)
        total = int(cpu.cycles)
        dur = np.empty(len(start), np.int64)
        if len(start):
            dur[:-1] = np.diff(start)
            dur[-1] = total - start[-1]
        kinds = np.asarray([cpu.kind[p] for p in tp], np.int8) if tp else np.zeros(0, np.int8)
        cls = np.where(kinds == 1, cy.CALL, np.where(kinds == 2, cy.RET, cy.ALU)).astype(np.int8)
        leak = np.asarray(tl, np.int64)
        trig = [(k, int(start[i]) if 0 <= i < len(start) else 0) for k, i in self.trig]
        return Run(bytes(self.out), pcs, start, dur, cls, leak, 8, trig, total, self.trigger_source)


# --- the runner the service and the simulator use ----------------------------------------------------
class Firmware:
    """An ELF ready to run SimpleSerial commands, with cached snapshots per key so each plaintext costs one emulated command."""

    def __init__(self, prog: Program, core: Optional[str] = None, protocol: Optional[str] = None, wait_states: int = 0, options: Optional[Dict[str, Any]] = None):
        if prog.arch not in ("arm", "riscv", "avr"):
            raise EmuError(f"{prog.machine_name or 'this architecture'} cannot be emulated (supported: Arm Cortex-M, RISC-V RV32, AVR/XMEGA)")
        self.prog = prog
        self.core = core or cy.default_core(prog)
        if self.core not in cy.ALL_CORES:
            raise EmuError(f"unknown core {self.core!r}; one of {', '.join(cy.ALL_CORES)}")
        want = {"arm": cy.ARM_CORES, "riscv": cy.RISCV_CORES, "avr": cy.AVR_CORES}[prog.arch]
        if self.core not in want:
            raise EmuError(f"core {self.core} does not match the {prog.arch} firmware")
        self.protocol = protocol or detect_protocol(prog)
        self.protocol_given = bool(protocol)
        self.ss = SimpleSerial(self.protocol)
        self.wait_states = int(wait_states)
        self.options = dict(options or {})
        self.lock = threading.RLock()
        self._m: Optional[_Machine] = None
        self._boot = None
        self._keys: "OrderedDict[bytes, Any]" = OrderedDict()
        self.machine  # boot now: start-up problems show up here, and the SimpleSerial version gets confirmed

    @property
    def machine(self) -> _Machine:
        if self._m is None:
            if self.prog.arch == "avr":
                m: _Machine = AVRMachine(self.prog, self.core, self.wait_states, self.options)
            else:
                m = UnicornMachine(self.prog, self.core, self.wait_states, self.options)
            m.boot()
            self._boot = m.snap
            self._m = m
            if not self.protocol_given:
                self._probe_protocol()
        return self._m

    def _probe_protocol(self) -> None:
        """Check the SimpleSerial version by asking the firmware for it ('v'); the symbol based guess fails when the compiler inlined the framing helpers."""
        first = self.protocol
        for ver in [first] + [v for v in ("2.1", "1.1") if v != first]:
            ss = SimpleSerial(ver)
            try:
                run = self._m.run(ss.frame("v", b""), self._boot)
            except EmuError:
                continue
            res = ss.parse(run.output)
            if (ver == "2.1" and any(r["ok"] for r in res)) or (ver != "2.1" and any(r["cmd"] in "zr" and r["ok"] for r in res)):
                self.protocol, self.ss = ver, ss
                return

    def info(self) -> Dict[str, Any]:
        m = self.machine
        return {"arch": self.prog.arch, "core": self.core, "core_label": cy.CORE_LABELS.get(self.core, self.core), "protocol": self.protocol, "wait_states": self.wait_states,
                "trigger": m.trigger_source, "skipped": m.skip_list(), "getch": self.prog.func_at(m.getch).name if self.prog.func_at(m.getch) else None}

    def command(self, cmd: str, data: bytes, snap=None) -> Tuple[Run, Any]:
        """Run one command from ``snap`` (default: just after boot); returns the run and the snapshot after it."""
        with self.lock:
            self.machine
            return self.command_raw(self.ss.frame(cmd, data), snap)

    def command_raw(self, data: bytes, snap=None) -> Tuple[Run, Any]:
        """Feed raw serial bytes from ``snap`` (default: just after boot) until the firmware waits for more; returns the run and the snapshot after it."""
        with self.lock:
            m = self.machine
            run = m.run(bytes(data), snap if snap is not None else self._boot)
            return run, m.snap

    def key_state(self, key: bytes):
        key = bytes(key)
        with self.lock:
            st = self._keys.get(key)
            if st is None:
                _run, st = self.command("k", key)
                self._keys[key] = st
                while len(self._keys) > 8:
                    self._keys.popitem(last=False)
            else:
                self._keys.move_to_end(key)
            return st

    def encrypt(self, key: Optional[bytes], pt: bytes, cmd: str = "p") -> Tuple[Run, Optional[bytes]]:
        """Send the key (cached) and then ``cmd`` with ``pt``; returns the run of the second command and its 'r' response."""
        with self.lock:
            st = self.key_state(key) if key else None
            run, _ = self.command(cmd, pt, st)
            if run.trigger_source == "function" and not run.trig:
                run.trig = _function_window(self.prog, run)
            return run, self.ss.response(run.output, "r")


def _function_window(prog: Program, run: Run) -> List[Tuple[str, int]]:
    """Trigger stand-in when the firmware has no trigger functions: the first call of the AES function."""
    for name in FALLBACK_WINDOW:
        f = prog.function(name)
        if f is None:
            continue
        hit = np.flatnonzero(run.pcs == f.lo)
        if not len(hit):
            continue
        i = int(hit[0])
        depth = 0
        for j in range(i, len(run.pcs)):
            c = run.cls[j]
            if c == cy.CALL:
                depth += 1
            elif c == cy.RET:
                if depth == 0:
                    end = int(run.start[j] + run.dur[j])
                    return [("high", int(run.start[i])), ("low", end)]
                depth -= 1
        return [("high", int(run.start[i])), ("low", run.total_cycles)]
    return []
