"""A small cycle-accurate AVR (classic AVRe/AVRe+ and XMEGA AVRxm) instruction set emulator for the code map and the simulator.

It runs avr-gcc and clang output: the whole instruction set those compilers emit (arithmetic and logic with exact SREG flags, multiplies, skips and branches, the X/Y/Z load and store modes, LPM/ELPM, the stack, calls and returns with 16 or 22 bit program counters, XMEGA's XCH/LAS/LAC/LAT). SPM, SLEEP, WDR, BREAK and DES do nothing. Peripherals are not emulated: firmware functions that touch hardware are hooked by address (see :mod:`cwstudio.codemap.emu`), I/O registers read back what was last written (0 after reset) except the CPU registers that live in I/O space (SP, SREG, RAMP*, EIND), and every I/O write can be watched (the XMEGA and ATmega HALs raise the trigger with a port write).

Cycle counts follow the AVR Instruction Set Manual: separate columns for AVRe and AVRxm (XMEGA loads from internal SRAM take one extra cycle; PUSH, ST, SBI/CBI and the calls are faster; SBIC/SBIS take one more), and 3-byte return addresses with their extra cycle on parts with more than 128 KB of flash.

Each flash word is decoded once into a Python closure; the run loop adds the static cycle count and calls the closure, which returns the next program counter (in words). Flags of the 8-bit add and subtract instructions come from precomputed tables.
"""
from __future__ import annotations

from array import array
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

# SREG bits
C, Z, N, V, S, H, T, I = 1, 2, 4, 8, 16, 32, 64, 128


def _tables():
    a = np.arange(256, dtype=np.int32)[:, None]
    b = np.arange(256, dtype=np.int32)[None, :]
    out = []
    for kind in ("add", "sub"):
        t = np.zeros((2, 256, 256), np.uint16)
        for cin in (0, 1):
            if kind == "add":
                r = a + b + cin
                r8 = r & 0xFF
                cf = r >> 8
                hf = ((a & 0xF) + (b & 0xF) + cin) >> 4
                vf = ((~(a ^ b)) & (a ^ r8) & 0x80) != 0
            else:
                r = a - b - cin
                r8 = r & 0xFF
                cf = (r < 0).astype(np.int32)
                hf = (((a & 0xF) - (b & 0xF) - cin) < 0).astype(np.int32)
                vf = ((a ^ b) & (a ^ r8) & 0x80) != 0
            nf = (r8 >> 7) & 1
            zf = (r8 == 0).astype(np.int32)
            vf = vf.astype(np.int32)
            sf = nf ^ vf
            flags = cf | (zf << 1) | (nf << 2) | (vf << 3) | (sf << 4) | (hf << 5)
            t[cin] = r8 | (flags << 8)
        out.append(array("H", t.reshape(-1).tobytes()))
    logic = []
    for r in range(256):
        n = r >> 7
        logic.append(((r == 0) << 1) | (n << 2) | (n << 4))
    hw = [bin(i).count("1") for i in range(256)]
    return out[0], out[1], logic, hw


ADD_T, SUB_T, LOGIC_F, HW8 = _tables()


class Blocked(Exception):
    """Raised by a hook to stop the run (e.g. getch with no input queued); the program counter stays on the hooked instruction."""


class Fault(RuntimeError):
    """The firmware did something the emulator cannot continue from (unknown opcode, runaway program counter)."""


class AVRCore:
    """One AVR CPU with its flash, registers and data space.

    ``xmega`` selects the AVRxm timing and the XMEGA I/O map (registers not in data space, I/O at 0x0000-0x0FFF); ``pc22`` selects 3-byte return addresses (parts with more than 128 KB of flash).
    """

    def __init__(self, flash: bytes, *, xmega: bool, pc22: bool = False, sram_start: int = 0x100, sram_size: int = 0x800, data_image: Optional[Dict[int, bytes]] = None):
        if len(flash) % 2:
            flash = flash + b"\xff"
        self.xmega = bool(xmega)
        self.pc22 = bool(pc22)
        self.flash = bytes(flash)
        self.words = array("H", self.flash)
        self.nwords = len(self.words)
        self.sram_start = sram_start
        self.data_size = max(sram_start + sram_size, 0x2000 if xmega else 0x100)
        self.R = bytearray(32)
        self.D = bytearray(self.data_size + 0x100)
        self.io_end = 0x2000 if xmega else 0x100  # XMEGA: I/O 0x0000-0x0FFF plus the mapped EEPROM 0x1000-0x1FFF
        if data_image:
            for addr, blob in data_image.items():
                if 0 <= addr < self.data_size:
                    self.D[addr:addr + len(blob)] = blob[: self.data_size - addr]
        self.sp = self.data_size - 1
        self.sreg = 0
        self.pc = 0
        self.xc = 0  # extra cycles from data dependent timing (branches taken, skips, SRAM loads on XMEGA)
        io = 0 if xmega else 0x20
        self.A_SPL, self.A_SPH, self.A_SREG = io + 0x3D, io + 0x3E, io + 0x3F
        self.A_RAMPZ, self.A_EIND = io + 0x3B, io + 0x3C
        self.io_off = io
        self.rampz = 0
        self.eind = 0
        self.ops: List[Optional[Callable[[int], int]]] = [None] * (self.nwords + 2)
        self.base: List[int] = [0] * (self.nwords + 2)
        self.leak: List[int] = [-1] * (self.nwords + 2)
        self.kind: List[int] = [0] * (self.nwords + 2)  # 1 call, 2 return
        self.hooks: Dict[int, Callable[["AVRCore"], Optional[int]]] = {}
        self.write_watch: Dict[int, Callable[["AVRCore", int, int], None]] = {}
        self.io_reads: Dict[int, Callable[["AVRCore", int], int]] = {}
        self.cycles = 0
        self.retsz = 3 if pc22 else 2

    # --- data space -----------------------------------------------------------------------------
    def rd(self, a: int) -> int:
        if a >= self.io_end:
            if a < self.data_size:
                return self.D[a]
            return 0
        return self._io_read(a)

    def _io_read(self, a: int) -> int:
        if not self.xmega and a < 32:
            return self.R[a]
        if a == self.A_SPL:
            return self.sp & 0xFF
        if a == self.A_SPH:
            return (self.sp >> 8) & 0xFF
        if a == self.A_SREG:
            return self.sreg
        if a == self.A_RAMPZ:
            return self.rampz
        if a == self.A_EIND:
            return self.eind
        f = self.io_reads.get(a)
        if f is not None:
            return f(self, a) & 0xFF
        return self.D[a]  # peripherals are not emulated: a register reads back what the firmware last wrote (0 at reset)

    def wr(self, a: int, v: int) -> None:
        if a >= self.io_end:
            if a < self.data_size:
                self.D[a] = v
            return
        self._io_write(a, v)

    def _io_write(self, a: int, v: int) -> None:
        if not self.xmega and a < 32:
            self.R[a] = v
            return
        if a == self.A_SPL:
            self.sp = (self.sp & 0xFF00) | v
        elif a == self.A_SPH:
            self.sp = (self.sp & 0xFF) | (v << 8)
        elif a == self.A_SREG:
            self.sreg = v
        elif a == self.A_RAMPZ:
            self.rampz = v
        elif a == self.A_EIND:
            self.eind = v
        else:
            self.D[a] = v
        f = self.write_watch.get(a)
        if f is not None:
            f(self, a, v)

    def push(self, v: int) -> None:
        self.D[self.sp] = v
        self.sp -= 1

    def pop(self) -> int:
        self.sp += 1
        return self.D[self.sp]

    def push_pc(self, ret: int) -> None:
        D = self.D
        sp = self.sp
        D[sp] = ret & 0xFF
        D[sp - 1] = (ret >> 8) & 0xFF
        if self.pc22:
            D[sp - 2] = (ret >> 16) & 0x3F
            self.sp = sp - 3
        else:
            self.sp = sp - 2

    def pop_pc(self) -> int:
        D = self.D
        sp = self.sp
        if self.pc22:
            v = (D[sp + 1] << 16) | (D[sp + 2] << 8) | D[sp + 3]
            self.sp = sp + 3
        else:
            v = (D[sp + 1] << 8) | D[sp + 2]
            self.sp = sp + 2
        return v

    def do_return(self) -> int:
        """Emulate RET (used by hooks that replace a function): returns the new word address and counts RET's cycles."""
        self.xc += 5 if self.pc22 else 4
        return self.pop_pc()

    def add_hook(self, byte_addr: int, fn: Callable[["AVRCore"], Optional[int]]) -> None:
        """Replace the function at ``byte_addr``: ``fn(core)`` runs instead and the function returns (``fn`` may return a word address to continue at instead, or raise :class:`Blocked`)."""
        pc = byte_addr // 2
        self.hooks[pc] = fn
        if 0 <= pc < len(self.ops):
            self.ops[pc] = None
            self.kind[pc] = 2  # a hooked function returns right away: for call depth it is its own return

    def snapshot(self) -> Tuple:
        return (bytes(self.R), bytes(self.D), self.sp, self.sreg, self.pc, self.rampz, self.eind, self.cycles)

    def restore(self, snap: Tuple) -> None:
        R, D, self.sp, self.sreg, self.pc, self.rampz, self.eind, self.cycles = snap
        self.R[:] = R
        self.D[:] = D
        self.xc = 0

    def word(self, pc: int) -> int:
        return self.words[pc] if 0 <= pc < self.nwords else 0xFFFF

    # --- running --------------------------------------------------------------------------------
    def _op(self, pc: int):
        op = self.ops[pc]
        if op is None:
            if pc in self.hooks:
                hook = self.hooks[pc]

                def op(p, hook=hook, c=self):
                    r = hook(c)
                    return c.do_return() if r is None else r
                self.ops[pc] = op
                self.base[pc] = 0
            else:
                if not 0 <= pc < self.nwords:
                    raise Fault(f"program counter outside flash: 0x{pc * 2:x}")
                op = _build(self, pc)
        return op

    def run(self, max_instr: int = 10_000_000, trace: Optional[Tuple[list, list, list]] = None) -> int:
        """Run until a hook raises :class:`Blocked` or ``max_instr`` instructions have executed. With ``trace`` = (pcs, cycles, leaks) lists, append every executed instruction's word address, start cycle and leak value (the byte it wrote or stored, -1 for none). Returns the number of instructions executed."""
        ops, base, leak, R = self.ops, self.base, self.leak, self.R
        pc = self.pc
        cy = self.cycles
        n = 0
        try:
            if trace is None:
                for n in range(max_instr):
                    op = ops[pc] or self._op(pc)
                    cy += base[pc]
                    pc = op(pc)
                n = max_instr
            else:
                tp, tc, tl = trace
                ap, ac, al = tp.append, tc.append, tl.append
                for n in range(max_instr):
                    op = ops[pc] or self._op(pc)
                    ap(pc)
                    ac(cy + self.xc)
                    b = base[pc]
                    lk = leak[pc]
                    cy += b
                    pc = op(pc)
                    al(R[lk] if lk >= 0 else -1)
                n = max_instr
        except Blocked:
            if trace is not None:  # the hooked instruction did not run
                trace[0].pop()
                trace[1].pop()
        except IndexError as e:
            raise Fault(f"runaway program counter near 0x{pc * 2:x}") from e
        finally:
            self.pc = pc
            self.cycles = cy + self.xc
            self.xc = 0
        return n


# --- decoding -------------------------------------------------------------------------------------
def decode(w: int, w2: int) -> Tuple[str, Dict[str, int], int]:
    """(mnemonic, operands, size in words) of the instruction word ``w`` (``w2`` is the next word, for two-word instructions)."""
    hi4 = w >> 12
    d5 = (w >> 4) & 0x1F
    r5 = (w & 0xF) | ((w >> 5) & 0x10)
    if hi4 == 0:
        if w == 0:
            return "nop", {}, 1
        top = (w >> 8) & 0xF
        if top == 1:
            return "movw", {"d": ((w >> 4) & 0xF) * 2, "r": (w & 0xF) * 2}, 1
        if top == 2:
            return "muls", {"d": 16 + ((w >> 4) & 0xF), "r": 16 + (w & 0xF)}, 1
        if top == 3:
            d, r = 16 + ((w >> 4) & 7), 16 + (w & 7)
            return ["mulsu", "fmul", "fmuls", "fmulsu"][((w >> 6) & 2) | ((w >> 3) & 1)], {"d": d, "r": r}, 1
        return ["", "cpc", "sbc", "add"][(w >> 10) & 3], {"d": d5, "r": r5}, 1
    if hi4 == 1:
        return ["cpse", "cp", "sub", "adc"][(w >> 10) & 3], {"d": d5, "r": r5}, 1
    if hi4 == 2:
        return ["and", "eor", "or", "mov"][(w >> 10) & 3], {"d": d5, "r": r5}, 1
    if 3 <= hi4 <= 7 or hi4 == 0xE:
        k = ((w >> 4) & 0xF0) | (w & 0xF)
        return {3: "cpi", 4: "sbci", 5: "subi", 6: "ori", 7: "andi", 0xE: "ldi"}[hi4], {"d": 16 + ((w >> 4) & 0xF), "k": k}, 1
    if hi4 in (8, 0xA):  # LDD/STD with displacement (LD/ST Y and Z without displacement are q=0)
        q = (w & 7) | ((w >> 7) & 0x18) | ((w >> 8) & 0x20)
        ptr = "y" if w & 8 else "z"
        return ("std" if w & 0x200 else "ldd"), {"d": d5, "q": q, "p": ptr}, 1
    if hi4 == 9:
        sub = (w >> 8) & 0xF
        if sub in (0, 1):  # loads
            low = w & 0xF
            if low == 0:
                return "lds", {"d": d5, "k": w2}, 2
            name = {1: "ld z+", 2: "ld -z", 4: "lpm", 5: "lpm+", 6: "elpm", 7: "elpm+", 9: "ld y+", 0xA: "ld -y", 0xC: "ld x", 0xD: "ld x+", 0xE: "ld -x", 0xF: "pop"}.get(low)
            return (name or "?"), {"d": d5}, 1
        if sub in (2, 3):  # stores
            low = w & 0xF
            if low == 0:
                return "sts", {"d": d5, "k": w2}, 2
            name = {1: "st z+", 2: "st -z", 4: "xch", 5: "las", 6: "lac", 7: "lat", 9: "st y+", 0xA: "st -y", 0xC: "st x", 0xD: "st x+", 0xE: "st -x", 0xF: "push"}.get(low)
            return (name or "?"), {"d": d5}, 1
        if sub in (4, 5):
            low = w & 0xF
            if low < 8 and low != 4:
                return ["com", "neg", "swap", "inc", "?", "asr", "lsr", "ror"][low], {"d": d5}, 1
            if low == 0xA:
                return "dec", {"d": d5}, 1
            if low == 0xB and sub == 4:
                return "des", {"k": (w >> 4) & 0xF}, 1
            if low in (0xC, 0xD):
                k = ((((w >> 4) & 0x1F) << 1) | (w & 1)) << 16 | w2
                return "jmp", {"k": k}, 2
            if low in (0xE, 0xF):
                k = ((((w >> 4) & 0x1F) << 1) | (w & 1)) << 16 | w2
                return "call", {"k": k}, 2
            if low == 8:
                if sub == 4:
                    return ("bclr" if w & 0x80 else "bset"), {"s": (w >> 4) & 7}, 1
                return {0x9508: "ret", 0x9518: "reti", 0x9588: "sleep", 0x9598: "break", 0x95A8: "wdr", 0x95C8: "lpm0", 0x95D8: "elpm0", 0x95E8: "spm", 0x95F8: "spm"}.get(w, "?"), {}, 1
            if low == 9:
                return {0x9409: "ijmp", 0x9419: "eijmp", 0x9509: "icall", 0x9519: "eicall"}.get(w, "?"), {}, 1
            return "?", {}, 1
        if sub in (6, 7):
            return ("sbiw" if sub == 7 else "adiw"), {"d": 24 + ((w >> 3) & 6), "k": ((w >> 2) & 0x30) | (w & 0xF)}, 1
        if sub in (8, 9, 0xA, 0xB):
            return ["cbi", "sbic", "sbi", "sbis"][sub - 8], {"a": (w >> 3) & 0x1F, "b": w & 7}, 1
        return "mul", {"d": d5, "r": r5}, 1
    if hi4 == 0xB:
        a = (w & 0xF) | ((w >> 5) & 0x30)
        return ("out" if w & 0x800 else "in"), {"d": d5, "a": a}, 1
    if hi4 in (0xC, 0xD):
        k = w & 0xFFF
        if k & 0x800:
            k -= 0x1000
        return ("rjmp" if hi4 == 0xC else "rcall"), {"k": k}, 1
    # 0xF
    sub = (w >> 9) & 7
    if sub < 4:
        k = (w >> 3) & 0x7F
        if k & 0x40:
            k -= 0x80
        return ("brbc" if sub & 2 else "brbs"), {"s": w & 7, "k": k}, 1
    return ["bld", "bst", "sbrc", "sbrs"][sub - 4], {"d": d5, "b": w & 7}, 1


def _is_2word(w: int) -> bool:
    return (w & 0xFE0E) in (0x940C, 0x940E) or (w & 0xFC0F) in (0x9000, 0x9200)


def _build(c: AVRCore, pc: int):
    """Decode the instruction at ``pc`` into a closure and fill the cycle, leak and kind tables."""
    w = c.words[pc]
    w2 = c.words[pc + 1] if pc + 1 < c.nwords else 0
    name, o, size = decode(w, w2)
    R, D = c.R, c.D
    xm = c.xmega
    p22 = c.pc22
    d = o.get("d", 0)
    r = o.get("r", 0)
    cyc = 1
    leak = d if "d" in o else -1
    kind = 0
    io = c.io_off
    sram = c.sram_start
    rd, wr = c.rd, c.wr
    add_t, sub_t, logic = ADD_T, SUB_T, LOGIC_F

    def skip_target(p):  # address after the instruction that a skip jumps over
        return p + (2 if _is_2word(c.word(p)) else 1)

    if name == "nop" or name in ("sleep", "break", "wdr", "spm", "des"):
        def f(p):
            return p + 1
    elif name == "movw":
        def f(p, d=d, r=r):
            R[d] = R[r]
            R[d + 1] = R[r + 1]
            return p + 1
    elif name in ("add", "adc"):
        carry = name == "adc"

        def f(p, d=d, r=r, carry=carry):
            x = add_t[((c.sreg & 1) << 16 if carry else 0) | (R[d] << 8) | R[r]]
            R[d] = x & 0xFF
            c.sreg = (c.sreg & 0xC0) | (x >> 8)
            return p + 1
    elif name in ("sub", "sbc", "cp", "cpc", "subi", "sbci", "cpi"):
        carry = name in ("sbc", "cpc", "sbci")
        store = name in ("sub", "sbc", "subi", "sbci")
        imm = name in ("subi", "sbci", "cpi")
        k = o.get("k", 0)

        def f(p, d=d, r=r, carry=carry, store=store, imm=imm, k=k):
            s = c.sreg
            x = sub_t[((s & 1) << 16 if carry else 0) | (R[d] << 8) | (k if imm else R[r])]
            fl = x >> 8
            if carry:
                fl &= ~2 | (s & 2)  # Z stays set only if it was set before
            if store:
                R[d] = x & 0xFF
            c.sreg = (s & 0xC0) | fl
            return p + 1
        if not store:
            leak = -1
    elif name in ("and", "or", "eor", "andi", "ori"):
        imm = name in ("andi", "ori")
        k = o.get("k", 0)
        op = {"and": 0, "andi": 0, "or": 1, "ori": 1, "eor": 2}[name]

        def f(p, d=d, r=r, imm=imm, k=k, op=op):
            b = k if imm else R[r]
            v = (R[d] & b) if op == 0 else ((R[d] | b) if op == 1 else (R[d] ^ b))
            R[d] = v
            c.sreg = (c.sreg & 0xE1) | logic[v]
            return p + 1
    elif name == "mov":
        def f(p, d=d, r=r):
            R[d] = R[r]
            return p + 1
    elif name == "ldi":
        k = o["k"]

        def f(p, d=d, k=k):
            R[d] = k
            return p + 1
    elif name == "cpse":
        def f(p, d=d, r=r):
            if R[d] == R[r]:
                t = skip_target(p + 1)
                c.xc += t - p - 1
                return t
            return p + 1
        leak = -1
    elif name in ("com", "neg", "swap", "inc", "dec", "asr", "lsr", "ror"):
        if name == "com":
            def f(p, d=d):
                v = (~R[d]) & 0xFF
                R[d] = v
                n = v >> 7
                c.sreg = (c.sreg & 0xE0) | 1 | ((v == 0) << 1) | (n << 2) | (n << 4)
                return p + 1
        elif name == "neg":
            def f(p, d=d):
                x = sub_t[R[d]]  # 0 - Rd: index (0 << 8) | Rd
                R[d] = x & 0xFF
                c.sreg = (c.sreg & 0xC0) | (x >> 8)
                return p + 1
        elif name == "swap":
            def f(p, d=d):
                v = R[d]
                R[d] = ((v << 4) | (v >> 4)) & 0xFF
                return p + 1
        elif name in ("inc", "dec"):
            inc = name == "inc"

            def f(p, d=d, inc=inc):
                o_ = R[d]
                v = (o_ + 1) & 0xFF if inc else (o_ - 1) & 0xFF
                R[d] = v
                n = v >> 7
                vv = (o_ == 0x7F) if inc else (o_ == 0x80)
                c.sreg = (c.sreg & 0xE1) | ((v == 0) << 1) | (n << 2) | (vv << 3) | ((n ^ vv) << 4)
                return p + 1
        else:
            mode = name

            def f(p, d=d, mode=mode):
                o_ = R[d]
                cf = o_ & 1
                if mode == "lsr":
                    v = o_ >> 1
                elif mode == "ror":
                    v = (o_ >> 1) | ((c.sreg & 1) << 7)
                else:
                    v = (o_ >> 1) | (o_ & 0x80)
                R[d] = v
                n = v >> 7
                vv = n ^ cf
                c.sreg = (c.sreg & 0xE0) | cf | ((v == 0) << 1) | (n << 2) | (vv << 3) | ((n ^ vv) << 4)
                return p + 1
    elif name in ("adiw", "sbiw"):
        k = o["k"]
        sub = name == "sbiw"
        cyc = 2

        def f(p, d=d, k=k, sub=sub):
            o_ = R[d] | (R[d + 1] << 8)
            v = ((o_ - k) if sub else (o_ + k)) & 0xFFFF
            R[d] = v & 0xFF
            R[d + 1] = v >> 8
            h7, r15 = (o_ >> 15) & 1, v >> 15
            if sub:
                vv, cf = h7 & (r15 ^ 1), r15 & (h7 ^ 1)
            else:
                vv, cf = (h7 ^ 1) & r15, (r15 ^ 1) & h7
            c.sreg = (c.sreg & 0xE0) | cf | ((v == 0) << 1) | (r15 << 2) | (vv << 3) | ((r15 ^ vv) << 4)
            return p + 1
    elif name in ("mul", "muls", "mulsu", "fmul", "fmuls", "fmulsu"):
        cyc = 2
        sd = name in ("muls", "fmuls", "fmulsu", "mulsu")
        sr = name in ("muls", "fmuls")
        frac = name.startswith("f")

        def f(p, d=d, r=r, sd=sd, sr=sr, frac=frac):
            a = R[d]
            b = R[r]
            if sd and a & 0x80:
                a -= 256
            if sr and b & 0x80:
                b -= 256
            v = (a * b) & 0xFFFF
            cf = v >> 15
            if frac:
                v = (v << 1) & 0xFFFF
            R[0] = v & 0xFF
            R[1] = v >> 8
            c.sreg = (c.sreg & 0xFC) | cf | ((v == 0) << 1)
            return p + 1
        leak = 0
    elif name in ("brbs", "brbc"):
        bit = 1 << o["s"]
        k = o["k"]
        want = name == "brbs"
        leak = -1

        def f(p, bit=bit, k=k, want=want):
            if bool(c.sreg & bit) == want:
                c.xc += 1
                return p + 1 + k
            return p + 1
    elif name in ("bset", "bclr"):
        bit = 1 << o["s"]
        leak = -1
        if name == "bset":
            def f(p, bit=bit):
                c.sreg |= bit
                return p + 1
        else:
            def f(p, bit=bit):
                c.sreg &= ~bit & 0xFF
                return p + 1
    elif name in ("bst", "bld"):
        b = o["b"]
        if name == "bst":
            leak = -1

            def f(p, d=d, b=b):
                if R[d] >> b & 1:
                    c.sreg |= T
                else:
                    c.sreg &= ~T & 0xFF
                return p + 1
        else:
            def f(p, d=d, b=b):
                if c.sreg & T:
                    R[d] |= 1 << b
                else:
                    R[d] &= ~(1 << b) & 0xFF
                return p + 1
    elif name in ("sbrc", "sbrs"):
        b = o["b"]
        want = 1 if name == "sbrs" else 0
        leak = -1

        def f(p, d=d, b=b, want=want):
            if (R[d] >> b & 1) == want:
                t = skip_target(p + 1)
                c.xc += t - p - 1
                return t
            return p + 1
    elif name in ("sbic", "sbis"):
        a = o["a"] + io
        b = o["b"]
        want = 1 if name == "sbis" else 0
        cyc = 2 if xm else 1
        leak = -1

        def f(p, a=a, b=b, want=want):
            if (rd(a) >> b & 1) == want:
                t = skip_target(p + 1)
                c.xc += t - p - 1
                return t
            return p + 1
    elif name in ("sbi", "cbi"):
        a = o["a"] + io
        m = 1 << o["b"]
        cyc = 1 if xm else 2
        leak = -1
        if name == "sbi":
            def f(p, a=a, m=m):
                wr(a, rd(a) | m)
                return p + 1
        else:
            def f(p, a=a, m=m):
                wr(a, rd(a) & ~m & 0xFF)
                return p + 1
    elif name in ("in", "out"):
        a = o["a"] + io
        if name == "in":
            def f(p, d=d, a=a):
                R[d] = rd(a)
                return p + 1
        else:
            def f(p, d=d, a=a):
                wr(a, R[d])
                return p + 1
    elif name in ("rjmp", "rcall"):
        k = o["k"]
        leak = -1
        if name == "rjmp":
            cyc = 2

            def f(p, k=k):
                return (p + 1 + k) % c.nwords
        else:
            cyc = (3 if p22 else 2) if xm else (4 if p22 else 3)
            kind = 1

            def f(p, k=k):
                c.push_pc(p + 1)
                return (p + 1 + k) % c.nwords
    elif name in ("jmp", "call"):
        k = o["k"]
        leak = -1
        if name == "jmp":
            cyc = 3

            def f(p, k=k):
                return k
        else:
            cyc = (4 if p22 else 3) if xm else (5 if p22 else 4)
            kind = 1

            def f(p, k=k):
                c.push_pc(p + 2)
                return k
    elif name in ("ijmp", "eijmp", "icall", "eicall"):
        leak = -1
        ext = name.startswith("e")
        if name.endswith("jmp"):
            cyc = 2

            def f(p, ext=ext):
                return (R[30] | (R[31] << 8)) | ((c.eind << 16) if ext else 0)
        else:
            kind = 1
            cyc = ((3 if ext or p22 else 2) if xm else (4 if ext or p22 else 3))

            def f(p, ext=ext):
                c.push_pc(p + 1)
                return (R[30] | (R[31] << 8)) | ((c.eind << 16) if ext else 0)
    elif name in ("ret", "reti"):
        cyc = 5 if p22 else 4
        kind = 2
        leak = -1
        reti = name == "reti"

        def f(p, reti=reti):
            if reti:
                c.sreg |= I
            return c.pop_pc()
    elif name == "push":
        cyc = 1 if xm else 2

        def f(p, d=d):
            D[c.sp] = R[d]
            c.sp -= 1
            return p + 1
    elif name == "pop":
        cyc = 2

        def f(p, d=d):
            c.sp += 1
            R[d] = D[c.sp]
            return p + 1
    elif name in ("lds", "sts"):
        k = o["k"]
        cyc = 2
        if name == "lds":
            def f(p, d=d, k=k):
                if xm and k >= sram:
                    c.xc += 1
                R[d] = rd(k)
                return p + 2
        else:
            def f(p, d=d, k=k):
                wr(k, R[d])
                return p + 2
    elif name in ("ldd", "std"):
        q = o["q"]
        ptr = 28 if o["p"] == "y" else 30
        if name == "ldd":
            cyc = (2 if q else 1) if xm else 2

            def f(p, d=d, q=q, ptr=ptr):
                a = (R[ptr] | (R[ptr + 1] << 8)) + q
                if xm and a >= sram:
                    c.xc += 1
                R[d] = rd(a)
                return p + 1
        else:
            cyc = (2 if q else 1) if xm else 2

            def f(p, d=d, q=q, ptr=ptr):
                wr((R[ptr] | (R[ptr + 1] << 8)) + q, R[d])
                return p + 1
    elif name.startswith("ld ") or name.startswith("st "):
        mode = name[3:]
        ptr = {"x": 26, "y": 28, "z": 30}[mode.strip("+-")]
        pre = mode.startswith("-")
        post = mode.endswith("+")
        load = name.startswith("ld")
        if load:
            cyc = (2 if pre else 1) if xm else 2
        else:
            cyc = (2 if pre else 1) if xm else 2

        def f(p, d=d, ptr=ptr, pre=pre, post=post, load=load):
            a = R[ptr] | (R[ptr + 1] << 8)
            if pre:
                a = (a - 1) & 0xFFFF
            if load:
                if xm and a >= sram:
                    c.xc += 1
                v = rd(a)
            else:
                wr(a, R[d])
            if post:
                a2 = (a + 1) & 0xFFFF
                R[ptr] = a2 & 0xFF
                R[ptr + 1] = a2 >> 8
            elif pre:
                R[ptr] = a & 0xFF
                R[ptr + 1] = a >> 8
            if load:
                R[d] = v
            return p + 1
    elif name in ("lpm", "lpm+", "elpm", "elpm+", "lpm0", "elpm0"):
        cyc = 3
        ext = name.startswith("e")
        post = name.endswith("+")
        dst = 0 if name.endswith("0") else d
        leak = dst
        flash = c.flash

        def f(p, dst=dst, ext=ext, post=post):
            z = R[30] | (R[31] << 8)
            a = z | ((c.rampz << 16) if ext else 0)
            R[dst] = flash[a] if a < len(flash) else 0xFF
            if post:
                a += 1
                R[30] = a & 0xFF
                R[31] = (a >> 8) & 0xFF
                if ext:
                    c.rampz = (a >> 16) & 0xFF
            return p + 1
    elif name in ("xch", "las", "lac", "lat"):
        cyc = 2
        mode = name

        def f(p, d=d, mode=mode):
            a = R[30] | (R[31] << 8)
            m = rd(a)
            if mode == "xch":
                wr(a, R[d])
            elif mode == "las":
                wr(a, m | R[d])
            elif mode == "lac":
                wr(a, m & (~R[d] & 0xFF))
            else:
                wr(a, m ^ R[d])
            R[d] = m
            return p + 1
    else:
        word = w

        def f(p, word=word):
            raise Fault(f"unknown AVR instruction 0x{word:04x} at 0x{p * 2:x}")
    c.ops[pc] = f
    c.base[pc] = cyc
    c.leak[pc] = leak
    c.kind[pc] = kind
    return f


# --- disassembly ------------------------------------------------------------------------------------
def disassemble(code: bytes, addr: int) -> List[Dict[str, Any]]:
    """Instructions in ``code`` (starting at byte address ``addr``) as {addr, bytes, text}."""
    if len(code) % 2:
        code = code + b"\xff"
    words = array("H", code)
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        w2 = words[i + 1] if i + 1 < len(words) else 0
        name, o, size = decode(w, w2)
        out.append({"addr": addr + 2 * i, "bytes": bytes(code[2 * i:2 * (i + size)]).hex(), "text": format_insn(name, o, addr + 2 * i)})
        i += size
    return out


def format_insn(name: str, o: Dict[str, int], addr: int) -> str:
    def reg(x):
        return f"r{x}"
    if name in ("ld x", "ld x+", "ld -x", "ld y+", "ld -y", "ld z+", "ld -z"):
        return f"ld {reg(o['d'])}, {name[3:].upper()}"
    if name.startswith("st "):
        return f"st {name[3:].upper()}, {reg(o['d'])}"
    if name in ("ldd", "std"):
        ptr = o["p"].upper()
        m = f"{ptr}+{o['q']}" if o["q"] else ptr
        return f"ldd {reg(o['d'])}, {m}" if name == "ldd" else f"std {m}, {reg(o['d'])}"
    if name in ("lpm", "lpm+", "elpm", "elpm+"):
        return f"{name.rstrip('+')} {reg(o['d'])}, Z{'+' if name.endswith('+') else ''}"
    if name in ("lpm0", "elpm0"):
        return name[:-1]
    if name in ("rjmp", "rcall"):
        return f"{name} .{o['k'] * 2:+d} ; 0x{addr + 2 + o['k'] * 2:x}"
    if name in ("jmp", "call"):
        return f"{name} 0x{o['k'] * 2:x}"
    if name in ("brbs", "brbc"):
        names = {("brbs", 0): "brcs", ("brbs", 1): "breq", ("brbs", 2): "brmi", ("brbs", 3): "brvs", ("brbs", 4): "brlt", ("brbs", 5): "brhs", ("brbs", 6): "brts", ("brbs", 7): "brie",
                 ("brbc", 0): "brcc", ("brbc", 1): "brne", ("brbc", 2): "brpl", ("brbc", 3): "brvc", ("brbc", 4): "brge", ("brbc", 5): "brhc", ("brbc", 6): "brtc", ("brbc", 7): "brid"}
        return f"{names[(name, o['s'])]} .{o['k'] * 2:+d} ; 0x{addr + 2 + o['k'] * 2:x}"
    if name in ("bset", "bclr"):
        return ("se" if name == "bset" else "cl") + "cznvshti"[o["s"]]
    if name in ("lds",):
        return f"lds {reg(o['d'])}, 0x{o['k']:04x}"
    if name in ("sts",):
        return f"sts 0x{o['k']:04x}, {reg(o['d'])}"
    if name in ("in",):
        return f"in {reg(o['d'])}, 0x{o['a']:02x}"
    if name in ("out",):
        return f"out 0x{o['a']:02x}, {reg(o['d'])}"
    if name in ("sbi", "cbi", "sbic", "sbis"):
        return f"{name} 0x{o['a']:02x}, {o['b']}"
    if name in ("sbrc", "sbrs", "bst", "bld"):
        return f"{name} {reg(o['d'])}, {o['b']}"
    if name in ("adiw", "sbiw"):
        return f"{name} r{o['d'] + 1}:r{o['d']}, {o['k']}"
    if name == "movw":
        return f"movw r{o['d'] + 1}:r{o['d']}, r{o['r'] + 1}:r{o['r']}"
    if "k" in o and "d" in o:
        return f"{name} {reg(o['d'])}, 0x{o['k']:02x}"
    if "r" in o:
        return f"{name} {reg(o['d'])}, {reg(o['r'])}"
    if "d" in o:
        return f"{name} {reg(o['d'])}"
    if name == "?":
        return ".word ?"
    return name
