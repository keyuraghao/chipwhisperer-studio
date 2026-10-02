"""Instruction classes and cycle models for the cores the code map runs through Unicorn (Arm Cortex-M, RISC-V).

Unicorn executes the instructions; it does not count clock cycles. These tables turn the executed program counters into cycle counts: every distinct instruction is classified once (load, store, load/store multiple with its register count, branch, call, return, multiply, divide, ...) and the per-core tables below give its cost, with the data dependent parts (branch taken or not, Cortex-M3/M4 load/store pipelining) worked out from the executed sequence.

Sources: the Cortex-M0, M0+, M3 and M4 Technical Reference Manuals (instruction timing tables; the pipeline refill P is taken as 2 cycles plus the flash wait states), the Ibex documentation and the NEORV32 data sheet (instruction timing). Cortex-M7 is dual issue and uses the M4 table (an upper bound). Divides use a fixed middle value of their data dependent range. These are models, good to a few percent on straight-line code; the trace alignment fits the remaining scale.
"""
from __future__ import annotations

import os
from typing import Dict, Tuple

import numpy as np

# instruction classes
ALU, LOAD, STORE, LDM, STM, BRANCH, CALL, RET, JUMPREG, MUL, MLA, MULL, DIV, TBB, IT, SYSTEM, SHIFT, LOADPC = range(18)
CLASS_NAMES = ["alu", "load", "store", "ldm", "stm", "branch", "call", "return", "jump", "mul", "mla", "mull", "div", "table branch", "it", "system", "shift", "load pc"]

ARM_CORES = ("cortex-m0", "cortex-m0+", "cortex-m3", "cortex-m4", "cortex-m7", "cortex-m33")
RISCV_CORES = ("rv32-generic", "ibex", "neorv32")
AVR_CORES = ("avr", "avrxmega")
ALL_CORES = ARM_CORES + RISCV_CORES + AVR_CORES
CORE_LABELS = {"cortex-m0": "Arm Cortex-M0", "cortex-m0+": "Arm Cortex-M0+", "cortex-m3": "Arm Cortex-M3", "cortex-m4": "Arm Cortex-M4", "cortex-m7": "Arm Cortex-M7 (M4 timing)", "cortex-m33": "Arm Cortex-M33 (M4 timing)",
               "rv32-generic": "RISC-V RV32 (generic in-order)", "ibex": "RISC-V Ibex", "neorv32": "RISC-V NEORV32", "avr": "AVR (classic AVRe/AVRe+)", "avrxmega": "AVR XMEGA (AVRxm)"}


def _popcount(x: int) -> int:
    return bin(x).count("1")


def classify_thumb(hw1: int, hw2: int) -> Tuple[int, int, int]:
    """(size in bytes, class, registers moved) of the Thumb instruction starting with halfword ``hw1``."""
    top5 = hw1 >> 11
    if top5 in (0b11101, 0b11110, 0b11111):
        return (4,) + _thumb32(hw1, hw2)
    if (hw1 >> 10) == 0b010000:
        return 2, (MUL if ((hw1 >> 6) & 0xF) == 0b1101 else ALU), 0
    if (hw1 >> 10) == 0b010001:  # special data processing / branch exchange
        op = (hw1 >> 8) & 3
        if op == 3:
            rm = (hw1 >> 3) & 0xF
            if hw1 & 0x80:
                return 2, CALL, 0  # BLX Rm
            return 2, (RET if rm == 14 else JUMPREG), 0
        rd = (hw1 & 7) | ((hw1 >> 4) & 8)
        if op in (0, 2) and rd == 15:
            return 2, JUMPREG, 0  # ADD/MOV pc, Rm
        return 2, ALU, 0
    if (hw1 >> 11) == 0b01001:
        return 2, LOAD, 1  # LDR literal
    if (hw1 >> 12) == 0b0101:
        opb = (hw1 >> 9) & 7
        return 2, (STORE if opb < 3 else LOAD), 1
    if (hw1 >> 13) == 0b011 or (hw1 >> 12) == 0b1000 or (hw1 >> 12) == 0b1001:
        return 2, (LOAD if hw1 & 0x800 else STORE), 1
    if (hw1 >> 12) == 0b1011:
        if (hw1 & 0x0E00) == 0x0400:  # PUSH
            return 2, STM, _popcount(hw1 & 0xFF) + ((hw1 >> 8) & 1)
        if (hw1 & 0x0E00) == 0x0C00:  # POP
            n = _popcount(hw1 & 0xFF) + ((hw1 >> 8) & 1)
            return 2, (LDM if not hw1 & 0x100 else RET), n
        if (hw1 & 0x0500) == 0x0100:  # CBZ/CBNZ
            return 2, BRANCH, 0
        if (hw1 & 0x0F00) == 0x0F00:
            return 2, (IT if hw1 & 0xF else ALU), 0  # IT or hint
        return 2, ALU, 0
    if (hw1 >> 12) == 0b1100:
        n = _popcount(hw1 & 0xFF)
        return 2, (LDM if hw1 & 0x800 else STM), n
    if (hw1 >> 12) == 0b1101:
        cond = (hw1 >> 8) & 0xF
        if cond == 0xF:
            return 2, SYSTEM, 0  # SVC
        if cond == 0xE:
            return 2, SYSTEM, 0  # UDF
        return 2, BRANCH, 0
    if top5 == 0b11100:
        return 2, BRANCH, 0
    return 2, ALU, 0


def _thumb32(hw1: int, hw2: int) -> Tuple[int, int]:
    op1 = (hw1 >> 11) & 3
    op2 = (hw1 >> 4) & 0x7F
    if op1 == 1:
        if (op2 & 0x64) == 0x00:  # load/store multiple
            lst = hw2
            n = _popcount(lst)
            if hw1 & 0x10:
                return (RET if lst & 0x8000 else LDM), n
            return STM, n
        if (op2 & 0x64) == 0x04:  # load/store dual, exclusive, table branch
            if (hw1 & 0xFFF0) == 0xE8D0 and (hw2 & 0xFFE0) == 0xF000:
                return TBB, 1
            return (LOAD if hw1 & 0x10 else STORE), 2
        if op2 & 0x40:
            return ALU, 0  # coprocessor / FPU data processing (one cycle as a model)
        return ALU, 0  # data processing (shifted register)
    if op1 == 2:
        if hw2 & 0x8000:  # branches and miscellaneous control
            op = (hw2 >> 12) & 7
            if op & 5 == 5:
                return CALL, 0  # BL
            if op & 5 == 4:
                return CALL, 0  # BLX immediate
            if op & 5 == 1:
                return BRANCH, 0  # B.W
            if ((hw1 >> 7) & 7) != 7:
                return BRANCH, 0  # conditional B.W
            return SYSTEM, 0  # MSR/MRS/hints/barriers
        return ALU, 0
    # op1 == 3
    if (op2 & 0x71) == 0x00 or (op2 & 0x71) == 0x10:
        return STORE, 1
    if (op2 & 0x67) in (0x01, 0x03, 0x05):
        rt = (hw2 >> 12) & 0xF
        if (op2 & 0x67) == 0x05 and rt == 15:
            return LOADPC, 1
        return LOAD, 1
    if (op2 & 0x70) == 0x20:
        return ALU, 0  # data processing (register)
    if (op2 & 0x78) == 0x30:  # multiply, multiply accumulate
        ra = (hw2 >> 12) & 0xF
        return (MUL if ra == 15 else MLA), 0
    if (op2 & 0x78) == 0x38:  # long multiply, divide
        o = (hw1 >> 4) & 7
        if o in (1, 3):
            return DIV, 0
        return MULL, 0
    if op2 & 0x40:
        return ALU, 0  # coprocessor
    return ALU, 0


def classify_riscv(w: int) -> Tuple[int, int, int]:
    """(size in bytes, class, 0) of the RISC-V instruction whose first 32 bits (little endian) are ``w``."""
    if w & 3 != 3:  # compressed
        h = w & 0xFFFF
        q = h & 3
        f3 = h >> 13
        if q == 0:
            return 2, (LOAD if f3 in (1, 2, 3) else STORE if f3 in (5, 6, 7) else ALU), 0
        if q == 1:
            if f3 == 1:
                return 2, CALL, 0  # C.JAL (RV32)
            if f3 == 5:
                return 2, BRANCH, 0  # C.J
            if f3 in (6, 7):
                return 2, BRANCH, 0  # C.BEQZ/C.BNEZ
            if f3 == 4 and ((h >> 10) & 3) in (0, 1):
                return 2, SHIFT, 0  # C.SRLI/C.SRAI
            return 2, ALU, 0
        if f3 == 0:
            return 2, SHIFT, 0  # C.SLLI
        if f3 in (1, 2, 3):
            return 2, LOAD, 0
        if f3 in (5, 6, 7):
            return 2, STORE, 0
        if f3 == 4:
            rs1 = (h >> 7) & 31
            rs2 = (h >> 2) & 31
            if rs2 == 0 and rs1 != 0:
                if h & 0x1000:
                    return 2, CALL, 0  # C.JALR
                return 2, (RET if rs1 == 1 else JUMPREG), 0  # C.JR
            if rs2 == 0 and rs1 == 0:
                return 2, SYSTEM, 0  # C.EBREAK
            return 2, ALU, 0
        return 2, ALU, 0
    op = w & 0x7F
    rd = (w >> 7) & 31
    f3 = (w >> 12) & 7
    rs1 = (w >> 15) & 31
    if op == 0x03 or op == 0x07:
        return 4, LOAD, 0
    if op == 0x23 or op == 0x27:
        return 4, STORE, 0
    if op == 0x63:
        return 4, BRANCH, 0
    if op == 0x6F:
        return 4, (CALL if rd in (1, 5) else BRANCH), 0
    if op == 0x67:
        if rd in (1, 5):
            return 4, CALL, 0
        return 4, (RET if rs1 in (1, 5) and rd == 0 else JUMPREG), 0
    if op == 0x33 and (w >> 25) == 1:
        return 4, (MUL if f3 < 4 else DIV), 0
    if op in (0x13, 0x33) and f3 in (1, 5):
        return 4, SHIFT, 0
    if op == 0x73:
        return 4, SYSTEM, 0
    if op == 0x2F:
        return 4, LOAD, 0
    return 4, ALU, 0


# cycle tables -------------------------------------------------------------------------------------
def _arm_params(core: str, ws: int) -> Dict[str, int]:
    if core == "cortex-m0":
        return {"alu": 1, "ls": 2, "pipe": 0, "b_taken": 3 + ws, "bl": 4 + ws, "bx": 3 + ws, "pop_pc": 3 + ws, "mul": 1, "mla": 1, "mull": 1, "div": 1, "tbb": 4 + ws, "refill": 2 + ws}
    if core == "cortex-m0+":
        return {"alu": 1, "ls": 2, "pipe": 0, "b_taken": 2 + ws, "bl": 3 + ws, "bx": 2 + ws, "pop_pc": 3 + ws, "mul": 1, "mla": 1, "mull": 1, "div": 1, "tbb": 3 + ws, "refill": 1 + ws}
    m3 = core == "cortex-m3"
    P = 2 + ws
    return {"alu": 1, "ls": 2, "pipe": 1, "b_taken": 1 + P, "bl": 1 + P, "bx": 1 + P, "pop_pc": 1 + P, "mul": 1, "mla": 2 if m3 else 1, "mull": 4 if m3 else 1, "div": 7, "tbb": 2 + P, "refill": P}


def _riscv_params(core: str, ws: int) -> Dict[str, int]:
    if core == "neorv32":
        return {"alu": 2, "shift": 4, "load": 4 + ws, "store": 4 + ws, "b_taken": 5 + ws, "b_not": 3, "jal": 5 + ws, "jalr": 5 + ws, "mul": 36, "div": 36, "system": 3}
    if core == "ibex":
        return {"alu": 1, "shift": 1, "load": 2 + ws, "store": 2 + ws, "b_taken": 3 + ws, "b_not": 1, "jal": 2 + ws, "jalr": 2 + ws, "mul": 3, "div": 37, "system": 1}
    return {"alu": 1, "shift": 1, "load": 2 + ws, "store": 1, "b_taken": 3 + ws, "b_not": 1, "jal": 2 + ws, "jalr": 3 + ws, "mul": 2, "div": 34, "system": 1}


def cycles_for(arch: str, core: str, pcs: np.ndarray, info: Dict[int, Tuple[int, int, int]], wait_states: int = 0) -> Tuple[np.ndarray, np.ndarray]:
    """Per executed instruction: (cycles it took, class). ``info`` maps every program counter in ``pcs`` to its (size, class, registers) from the classifiers above."""
    n = len(pcs)
    if n == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int8)
    uniq, inv = np.unique(pcs, return_inverse=True)
    size = np.array([info[int(u)][0] for u in uniq], np.int64)[inv]
    cls = np.array([info[int(u)][1] for u in uniq], np.int8)[inv]
    nreg = np.array([info[int(u)][2] for u in uniq], np.int64)[inv]
    nxt = np.empty(n, np.int64)
    nxt[:-1] = pcs[1:]
    nxt[-1] = -1
    taken = (nxt != pcs + size) & (nxt >= 0)
    cyc = np.ones(n, np.int64)
    if arch == "arm":
        p = _arm_params(core, wait_states)
        ls = (cls == LOAD) | (cls == STORE)
        c_ls = np.full(n, p["ls"], np.int64)
        if p["pipe"]:
            prev = np.zeros(n, bool)
            prev[1:] = ls[:-1]
            c_ls[prev] = p["ls"] - 1  # neighbouring single loads and stores pipeline on the M3/M4
        cyc = np.where(ls, c_ls, cyc)
        cyc = np.where(cls == LOADPC, p["ls"] + p["refill"], cyc)
        cyc = np.where((cls == LDM) | (cls == STM), 1 + nreg, cyc)
        cyc = np.where(cls == RET, np.where(nreg > 0, nreg + p["pop_pc"], p["bx"]), cyc)
        cyc = np.where(cls == BRANCH, np.where(taken, p["b_taken"], 1), cyc)
        cyc = np.where(cls == CALL, p["bl"], cyc)
        cyc = np.where(cls == JUMPREG, p["bx"], cyc)
        cyc = np.where(cls == MUL, p["mul"], cyc)
        cyc = np.where(cls == MLA, p["mla"], cyc)
        cyc = np.where(cls == MULL, p["mull"], cyc)
        cyc = np.where(cls == DIV, p["div"], cyc)
        cyc = np.where(cls == TBB, p["tbb"], cyc)
    else:
        p = _riscv_params(core, wait_states)
        table = {ALU: p["alu"], SHIFT: p["shift"], LOAD: p["load"], STORE: p["store"], CALL: p["jal"], RET: p["jalr"], JUMPREG: p["jalr"], MUL: p["mul"], DIV: p["div"], SYSTEM: p["system"]}
        for k, v in table.items():
            cyc = np.where(cls == k, v, cyc)
        cyc = np.where(cls == BRANCH, np.where(taken, p["b_taken"], p["b_not"]), cyc)
    return cyc, cls


def default_core(prog) -> str:
    """Best guess of the core from the ELF (architecture attributes, AVR device, RISC-V symbols)."""
    if prog.arch == "avr":
        flags = prog.e_flags & 0x7F
        dev = (prog.avr_device or {}).get("device") or ""
        return "avrxmega" if 100 < flags < 110 or "xmega" in dev else "avr"
    if prog.arch == "riscv":
        names = " ".join(prog.symbols).lower() + " " + os.path.basename(prog.path).lower()  # Studio's builds carry the platform in the file name
        if "neorv32" in names:
            return "neorv32"
        if "ibex" in names:
            return "ibex"
        return "rv32-generic"
    arch = prog.attributes.get("TAG_CPU_ARCH")
    name = str(prog.attributes.get("TAG_CPU_NAME") or "").upper()
    if arch == 11 or "6-M" in name or "6S-M" in name:
        return "cortex-m0"
    if arch in (16, 17) or "8-M" in name:
        return "cortex-m33"
    if arch == 10 or name == "7-M" or "CORTEX-M3" in name:
        return "cortex-m3"
    return "cortex-m4"
