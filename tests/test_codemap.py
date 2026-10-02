"""Code on the waveform: the AVR emulator's instruction semantics and cycle counts, DWARF line mapping, firmware emulation per architecture (AES output against Python's AES), symbol hooking, the cycle to sample mapping, the alignment, the simulator running real firmware, the region and lookup queries, the HTTP API, the MCP tools and the exact mode decoder.

Tests that need a firmware build use ``tests/fwbuild.py`` (real toolchains and ChipWhisperer sources; ELFs cached between runs) and are skipped with the reason when no toolchain is available.
"""
import os
import shutil
import subprocess
import tempfile
import time

import numpy as np
import pytest

from cwstudio.aes import encrypt_block
from cwstudio.codemap import avr as A
from cwstudio.codemap import cycles as cy
from cwstudio.codemap import power
from cwstudio.codemap import swo

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")


def _fwbuild():
    try:
        from tests import fwbuild
    except ImportError:
        import fwbuild
    return fwbuild


def elf_or_skip(*args, **kw):
    fwbuild = _fwbuild()
    try:
        return fwbuild.build_elf(*args, **kw)
    except RuntimeError as e:
        pytest.skip(f"firmware build not available: {str(e).splitlines()[0]}")


# ----- AVR instruction encodings used by the unit tests ---------------------------------------------------
def ldi(d, k): return 0xE000 | ((k & 0xF0) << 4) | ((d - 16) << 4) | (k & 0xF)
def rr(base, d, r): return base | ((r & 0x10) << 5) | (d << 4) | (r & 0xF)
def one(base, d): return base | (d << 4)
def adiw(base, d, k): return base | ((k & 0x30) << 2) | (((d - 24) // 2) << 4) | (k & 0xF)
def brbs(s, k): return 0xF000 | ((k & 0x7F) << 3) | s
def brbc(s, k): return 0xF400 | ((k & 0x7F) << 3) | s


ADD, ADC, SUB, SBC, CP, CPC, MUL = 0x0C00, 0x1C00, 0x1800, 0x0800, 0x1400, 0x0400, 0x9C00
INC, DEC, LSR, ROR, ASR, COM, NEG = 0x9403, 0x940A, 0x9406, 0x9407, 0x9405, 0x9400, 0x9401
SEC, SEZ, CLZ, NOP, RET = 0x9408, 0x9418, 0x9498, 0x0000, 0x9508


def core(words, xmega=False, pc22=False):
    flash = b"".join(w.to_bytes(2, "little") for w in words) + b"\xff\xff" * 64
    sram = 0x2000 if xmega else 0x100
    c = A.AVRCore(flash, xmega=xmega, pc22=pc22, sram_start=sram, sram_size=0x800)
    c.sp = sram + 0x7FF
    return c


def run(c, n):
    tr = ([], [], [])
    c.run(n, tr)
    cyc = tr[1] + [c.cycles]
    return [cyc[i + 1] - cyc[i] for i in range(len(tr[1]))]


def test_avr_add_adc_flags():
    c = core([ldi(16, 0x7F), ldi(17, 1), rr(ADD, 16, 17)])
    run(c, 3)
    assert c.R[16] == 0x80 and c.sreg == A.H | A.V | A.N  # half carry, signed overflow, negative; S = N ^ V = 0
    c = core([ldi(18, 0xFF), ldi(19, 1), rr(ADD, 18, 19), ldi(20, 0), ldi(21, 0), rr(ADC, 20, 21)])
    run(c, 3)
    assert c.R[18] == 0 and c.sreg == A.H | A.Z | A.C
    run(c, 3)
    assert c.R[20] == 1 and c.sreg == 0  # the carry went into ADC


def test_avr_compare_with_carry_keeps_z():
    # 16-bit compare of equal values: Z stays set through CPC; unequal high bytes clear it
    c = core([ldi(16, 5), ldi(17, 5), ldi(18, 0), ldi(19, 0), rr(CP, 16, 17), rr(CPC, 18, 19)])
    run(c, 6)
    assert c.sreg & A.Z and not c.sreg & A.C
    c = core([ldi(16, 0), ldi(17, 1), ldi(18, 0), ldi(19, 0), rr(CP, 16, 17), rr(CPC, 18, 19)])
    run(c, 5)
    assert c.sreg == A.C | A.N | A.S | A.H
    run(c, 1)
    assert c.sreg == A.C | A.N | A.S | A.H and c.R[18] == 0  # CPC does not store
    c = core([ldi(16, 3), ldi(17, 3), ldi(18, 1), ldi(19, 0), rr(CP, 16, 17), rr(CPC, 18, 19)])
    run(c, 6)
    assert not c.sreg & A.Z
    c = core([ldi(16, 0x10), ldi(17, 0x01), rr(SUB, 16, 17), ldi(18, 0), ldi(19, 0), rr(SBC, 18, 19)])
    run(c, 6)
    assert c.R[16] == 0x0F and c.R[18] == 0 and not c.sreg & A.Z  # the 16-bit result 0x000F is not zero: SBC keeps SUB's cleared Z


def test_avr_inc_dec_shifts():
    c = core([ldi(16, 0x7F), one(INC, 16)])
    run(c, 2)
    assert c.R[16] == 0x80 and c.sreg == A.V | A.N
    c = core([ldi(17, 0x80), one(DEC, 17)])
    run(c, 2)
    assert c.R[17] == 0x7F and c.sreg == A.V | A.S
    c = core([ldi(16, 1), one(LSR, 16)])
    run(c, 2)
    assert c.R[16] == 0 and c.sreg == A.C | A.Z | A.V | A.S
    c = core([SEC, ldi(17, 2), one(ROR, 17)])
    run(c, 3)
    assert c.R[17] == 0x81 and c.sreg == A.N | A.V
    c = core([ldi(18, 0x81), one(ASR, 18)])
    run(c, 2)
    assert c.R[18] == 0xC0 and c.sreg == A.C | A.N | A.S
    c = core([ldi(19, 0x00), one(NEG, 19), ldi(20, 0x01), one(NEG, 20), ldi(21, 0x55), one(COM, 21)])
    run(c, 2)
    assert c.R[19] == 0 and c.sreg & A.Z and not c.sreg & A.C
    run(c, 2)
    assert c.R[20] == 0xFF and c.sreg & A.C and c.sreg & A.N
    run(c, 2)
    assert c.R[21] == 0xAA and c.sreg & A.C


def test_avr_word_and_multiply():
    c = core([ldi(24, 0xFF), ldi(25, 0x7F), adiw(0x9600, 24, 1), ldi(26, 0), ldi(27, 0), adiw(0x9700, 26, 1)])
    d = run(c, 3)
    assert (c.R[24], c.R[25]) == (0, 0x80) and c.sreg == A.V | A.N and d[2] == 2
    run(c, 3)
    assert (c.R[26], c.R[27]) == (0xFF, 0xFF) and c.sreg == A.C | A.N | A.S
    c = core([ldi(16, 200), ldi(17, 200), rr(MUL, 16, 17), ldi(18, 0xFF), ldi(19, 2), 0x0200 | (2 << 4) | 3, ldi(20, 0x80), ldi(21, 0x80), 0x0308 | (4 << 4) | 5])
    d = run(c, 3)
    assert (c.R[0], c.R[1]) == (0x40, 0x9C) and c.sreg & A.C and d[2] == 2
    run(c, 3)  # MULS -1 * 2
    assert (c.R[0], c.R[1]) == (0xFE, 0xFF) and c.sreg & A.C
    run(c, 3)  # FMUL 0.5 * 0.5 (1.7 format) = 0.25 in 1.15 format, shifted
    assert (c.R[0], c.R[1]) == (0x00, 0x80) and not c.sreg & A.C


@pytest.mark.parametrize("xmega", [False, True])
def test_avr_branch_skip_cycles(xmega):
    # taken branch 2 cycles, not taken 1; a skip over a two-word instruction 3
    c = core([SEZ, brbs(1, 1), NOP, CLZ, brbs(1, 1), NOP, ldi(16, 1), 0xFE00 | (16 << 4), 0x9000 | (1 << 4), 0x0100, NOP], xmega=xmega)
    d = run(c, 8)
    assert d[1] == 2 and d[3] == 1 and d[4] == 1  # sez; breq taken; clz; breq not taken; nop
    assert d[6] == 3  # sbrs skipping the two-word lds
    assert c.pc == 11  # ... and the nop after it ran


@pytest.mark.parametrize("xmega,pc22,call,ret,rcall,push", [(False, False, 4, 4, 3, 2), (True, False, 3, 4, 2, 1), (True, True, 4, 5, 3, 1), (False, True, 5, 5, 4, 2)])
def test_avr_call_return_push_cycles(xmega, pc22, call, ret, rcall, push):
    # call f; rcall f; push r16; pop r17; f: ret
    words = [0x940E, 6, 0xD000 | 3, ldi(16, 0x5A), one(0x920F, 16), one(0x900F, 17), RET]
    c = core(words, xmega=xmega, pc22=pc22)
    sp0 = c.sp
    d = run(c, 1)
    assert d[0] == call and c.pc == 6
    assert c.D[sp0] == 2 and c.sp == sp0 - (3 if pc22 else 2)  # return address, low byte at the top of the stack
    d = run(c, 1)
    assert d[0] == ret and c.pc == 2 and c.sp == sp0
    d = run(c, 1)
    assert d[0] == rcall and c.pc == 6
    run(c, 1)  # ret
    d = run(c, 3)  # ldi, push, pop
    assert d[1] == push and d[2] == 2 and c.R[17] == 0x5A and c.sp == sp0


@pytest.mark.parametrize("xmega,ld,st,ldd,lds,sbi", [(False, 2, 2, 2, 2, 2), (True, 2, 1, 3, 3, 1)])
def test_avr_memory_cycles(xmega, ld, st, ldd, lds, sbi):
    sram = 0x2000 if xmega else 0x100
    words = [ldi(26, sram & 0xFF), ldi(27, sram >> 8), ldi(16, 0x33), one(0x920D, 16), one(0x900D, 17)] + \
            [ldi(28, sram & 0xFF), ldi(29, sram >> 8), 0x8008 | (18 << 4), one(0x9000, 19), sram, 0x9A00 | (5 << 3) | 2]
    c = core(words, xmega=xmega)
    d = run(c, 11)
    assert d[3] == st and d[4] == ld
    assert c.R[17] == 0 and c.D[sram] == 0x33 and c.R[26] == (sram + 2) & 0xFF  # ST X+ then LD X+ (the next byte)
    assert d[7] == (ldd - (1 if xmega else 0)) and c.R[18] == 0x33  # LDD Y+0 is LD Y
    assert d[8] == lds and c.R[19] == 0x33
    assert d[9] == sbi


def test_avr_lpm_and_disassembly():
    words = [ldi(30, 0x08), ldi(31, 0x00), one(0x9005, 16), one(0x9005, 17), 0xAA55]
    c = core(words)
    d = run(c, 4)
    assert (c.R[16], c.R[17]) == (0x55, 0xAA) and d[2] == 3 and c.R[30] == 0x0A
    text = [i["text"] for i in A.disassemble(b"".join(w.to_bytes(2, "little") for w in [ldi(16, 0x12), rr(ADD, 16, 17), brbc(1, -2), 0x940E, 0x100, RET]), 0)]
    assert text[0] == "ldi r16, 0x12" and text[1] == "add r16, r17" and text[2].startswith("brne") and text[3] == "call 0x200" and text[4] == "ret"


def test_thumb_and_riscv_classifiers():
    assert cy.classify_thumb(0xB510, 0)[1:] == (cy.STM, 2)  # push {r4, lr}
    assert cy.classify_thumb(0xBD10, 0)[1:] == (cy.RET, 2)  # pop {r4, pc}
    assert cy.classify_thumb(0x4770, 0)[1] == cy.RET  # bx lr
    assert cy.classify_thumb(0xF000, 0xF800) == (4, cy.CALL, 0)  # bl
    assert cy.classify_thumb(0xD0FE, 0)[1] == cy.BRANCH  # beq
    assert cy.classify_thumb(0x6818, 0)[1] == cy.LOAD  # ldr r0, [r3]
    assert cy.classify_thumb(0xFB00, 0xF001)[1] == cy.MUL  # mul.w
    assert cy.classify_thumb(0xFBB0, 0xF0F1)[1] == cy.DIV  # udiv
    assert cy.classify_riscv(0x00008067)[1] == cy.RET  # ret
    assert cy.classify_riscv(0x8082)[:2] == (2, cy.RET)  # c.jr ra
    assert cy.classify_riscv(0x0000A503)[1] == cy.LOAD  # lw a0, 0(ra)
    assert cy.classify_riscv(0x02B50533)[1] == cy.MUL  # mul a0, a0, a1
    # the M3/M4 cycle model: taken branches refill the pipeline, neighbouring loads pipeline
    pcs = np.array([0, 2, 4, 8, 10], np.int64)
    info = {0: (2, cy.LOAD, 1), 2: (2, cy.LOAD, 1), 4: (2, cy.BRANCH, 0), 8: (2, cy.ALU, 0), 10: (2, cy.BRANCH, 0)}
    d, _ = cy.cycles_for("arm", "cortex-m4", pcs, info)
    assert d.tolist() == [2, 1, 3, 1, 1]
    d0, _ = cy.cycles_for("arm", "cortex-m0", pcs, info)
    assert d0.tolist() == [2, 2, 3, 1, 1]


# ----- mapping and alignment (no firmware needed) -------------------------------------------------------------
def test_mapping_arithmetic():
    from cwstudio.simulator import SimScope
    sc = SimScope(seed=1)
    sc.default_setup()
    sc.adc.offset = 100
    sc.adc.presamples = 20
    m = power.Mapping.from_scope(sc)
    assert m.spc == pytest.approx(4.0) and m.adc_offset == 100 and m.presamples == 20
    assert m.sample(0) == pytest.approx(-80) and m.sample(25) == pytest.approx(20)
    assert m.cycle(m.sample(1234.5)) == pytest.approx(1234.5)
    sc.adc.decimate = 2
    assert power.Mapping.from_scope(sc).spc == pytest.approx(2.0)
    m.scale, m.shift = 1.02, 7.5
    assert m.sample(100) == pytest.approx(100 * 4 * 1.02 - 80 + 7.5)
    P = np.array([1.0, 3.0, 1.0, 1.0])
    ms = power.model_samples(P, 0, power.Mapping(spc=4.0), 16)
    assert ms.mean() == pytest.approx(P.mean(), rel=0.1) and ms[4:8].mean() > ms[0:4].mean()


@pytest.mark.parametrize("scale,shift", [(1.0, 0.0), (1.025, 41.0), (0.97, -133.0)])
def test_alignment_recovers_offset_and_scale(scale, shift):
    rng = np.random.default_rng(7)
    P = 1.0 + 0.6 * rng.random(3000) + np.repeat(rng.random(60) > 0.7, 50) * 1.5  # irregular activity like real code
    truth = power.Mapping(spc=4.0, scale=scale, shift=shift)
    mean = np.mean([power.synth_trace(P, 200, truth, 6000, rng) for _ in range(8)], axis=0)
    res = power.align(mean, P, 200, power.Mapping(spc=4.0))
    assert res["scale"] == pytest.approx(scale, abs=0.0025)
    assert res["shift"] == pytest.approx(shift, abs=2.0)
    assert res["inverted"] and res["confidence"] > 0.3


# ----- exact mode decoding ----------------------------------------------------------------------------------------
def test_itm_decoder_skips_other_packets():
    pc = 0x080012AB
    payload = [0x00, 0x00, 0x00, 0x00, 0x00, 0x80,  # synchronisation
               0xC0, 0x85, 0x12,  # local timestamp with continuation bytes
               0x01, 0x41,  # instrumentation (ITM port 0) one byte
               swo.PC_HEADER, pc & 0xFF, (pc >> 8) & 0xFF, (pc >> 16) & 0xFF, pc >> 24,
               swo.PC_SLEEP, 0x00,
               swo.PC_HEADER, 0x10, 0x00, 0x00, 0x08]
    raw = [[3, b, swo.CMD_STAT] for b in payload]
    raw.insert(10, [0x10, 0x01, swo.CMD_TIME])
    samples = swo.decode_pc_samples(swo.raw_bytes(raw))
    assert [p for _t, p in samples] == [pc, 0x08000010]
    assert samples[0][0] == 3 * 12 + 0x110


# ----- firmware emulation (needs a toolchain) -------------------------------------------------------------------
TARGETS = [
    ("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1", "cortex-m4"),
    ("CWLITEARM", "clang", "TINYAES128C", "SS_VER_2_1", "cortex-m4"),
    ("CWLITEARM", "gcc", "MBEDTLS", "SS_VER_2_1", "cortex-m4"),
    ("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_1_1", "cortex-m4"),
    ("CWNANO", "gcc", "TINYAES128C", "SS_VER_2_1", "cortex-m0"),
    ("CWLITEXMEGA", "gcc", "TINYAES128C", "SS_VER_2_1", "avrxmega"),
    ("CWLITEXMEGA", "clang", "TINYAES128C", "SS_VER_2_1", "avrxmega"),
    ("CWLITEXMEGA", "gcc", "AVRCRYPTOLIB", "SS_VER_2_1", "avrxmega"),
    ("CW304", "gcc", "TINYAES128C", "SS_VER_1_1", "avr"),
    ("CW308_NEORV32", "gcc", "TINYAES128C", "SS_VER_2_1", "neorv32"),
    ("CW312_IBEX", "gcc", "TINYAES128C", "SS_VER_2_1", "ibex"),
]


@pytest.mark.parametrize("platform,compiler,crypto,ss,core", TARGETS, ids=["-".join(t[:4]) for t in TARGETS])
def test_emulated_aes_matches_python(platform, compiler, crypto, ss, core):
    from cwstudio.codemap.emu import Firmware
    from cwstudio.codemap.program import Program
    prog = Program(elf_or_skip(platform, compiler, crypto, ss))
    fw = Firmware(prog)
    assert fw.core == core and fw.protocol == ss[7:].replace("_", ".")
    rng = np.random.default_rng(3)
    for _ in range(4):
        key, pt = rng.bytes(16), rng.bytes(16)
        run, ct = fw.encrypt(key, pt)
        assert ct == encrypt_block(key, pt)
        hi, lo = run.trigger_window()
        assert hi is not None and lo is not None and 300 < lo - hi < 60000  # the AES runs between trigger_high and trigger_low
    info = fw.info()
    assert info["getch"] in ("getch", "input_ch_0")
    if prog.arch != "avr":
        assert "platform_init" in info["skipped"] and info["trigger"] == "symbol"
    else:
        assert info["trigger"] == "port"


def test_dwarf_lines_match_addr2line():
    from cwstudio.codemap.program import Program
    elf = elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    prog = Program(elf)
    f = prog.function("SubBytes")
    assert f is not None and prog.short_label(f.file) == "aes.c" and 200 < f.line < 260
    assert prog.func_at(f.lo + 4).name == "SubBytes"
    assert prog.short_label(prog.line_at(prog.function("get_pt").lo)[0]) == "simpleserial-aes.c"
    data = _fwbuild().find_data()
    a2l = shutil.which("arm-none-eabi-addr2line", path=os.path.join(data, "toolchains", "arm-gcc", sorted(os.listdir(os.path.join(data, "toolchains", "arm-gcc")))[0], "bin")) if data else None
    if not a2l:
        pytest.skip("arm-none-eabi-addr2line not found")
    addrs = []
    for name in ("SubBytes", "ShiftRows", "AddRoundKey", "Cipher", "get_pt", "simpleserial_get"):
        fn = prog.function(name)
        addrs += list(range(fn.lo, fn.hi, max(2, (fn.hi - fn.lo) // 6)))
    out = subprocess.run([a2l, "-e", elf] + [hex(a) for a in addrs], capture_output=True, text=True, timeout=30).stdout.splitlines()
    agree = 0
    for a, ref in zip(addrs, out):
        fi, ln = prog.line_at(a)
        ref_file, _, ref_line = ref.rpartition(":")
        ref_line = int(ref_line.split()[0]) if ref_line.split()[0].isdigit() else 0
        agree += os.path.basename(ref_file) == prog.short_label(fi) and ref_line == ln
    assert agree / len(addrs) > 0.95


def test_timeline_region_and_lookup():
    from cwstudio.codemap.emu import Firmware
    from cwstudio.codemap.program import Program
    from cwstudio.codemap.timeline import Timeline
    prog = Program(elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1"))
    run, _ = Firmware(prog).encrypt(KEY, bytes(16))
    tl = Timeline.build(prog, run)
    sb = tl.function_ranges("SubBytes")
    assert len(sb) == 10  # one SubBytes per AES-128 round
    reg = tl.region(sb[0][0], sb[0][1])
    own = [f for f in reg["functions"] if f["self_cycles"] > 0]
    assert own[0]["name"] == "SubBytes" and own[0]["share"] > 0.9
    callers = {f["name"] for f in reg["functions"]}
    assert {"Cipher", "get_pt", "aes"} <= callers  # (AES128_ECB_indp_crypto tail-calls Cipher, so Cipher replaces it at that depth)
    assert any(ln["file"] == "aes.c" and "getSBoxValue" in (ln["text"] or "") for ln in reg["lines"])
    win = tl.region(0, tl.t1 - tl.t0)
    names = {f["name"] for f in win["functions"] if f["self_cycles"] > 0}
    assert {"SubBytes", "ShiftRows", "AddRoundKey", "Cipher"} <= names
    assert any(ln["inline"] == "MixColumns" or ln["func"] == "MixColumns" for ln in win["lines"])  # -Os inlines MixColumns into Cipher; DWARF still names it
    fi = prog.find_file("aes.c")
    line = next(ln["line"] for ln in reg["lines"] if "getSBoxValue" in (ln["text"] or ""))
    rng = tl.line_ranges(fi, line)
    assert len(rng) >= 160 and all(a < b for a, b in rng)  # 16 bytes x 10 rounds
    at = tl.at(sb[0][0] + 1)
    assert at["func"] == "SubBytes"


def test_inlined_functions_are_found():
    # clang inlines SubBytes, ShiftRows' helpers and xtime into Cipher: the DWARF inline records still place them on the timeline
    from cwstudio.codemap.emu import Firmware
    from cwstudio.codemap.program import Program
    from cwstudio.codemap.timeline import Timeline
    prog = Program(elf_or_skip("CWLITEARM", "clang", "TINYAES128C", "SS_VER_2_1"))
    run, _ = Firmware(prog).encrypt(KEY, bytes(16))
    tl = Timeline.build(prog, run)
    assert prog.function("SubBytes") is None and "SubBytes" in prog.inline_names
    sb = tl.function_ranges("SubBytes")
    assert len(sb) >= 10 and all(d == -1 for _a, _b, d in sb)
    reg = tl.region(0, tl.t1 - tl.t0)
    inl = {d["name"]: d for d in reg["inlined"]}
    assert "SubBytes" in inl and "Cipher" in inl["SubBytes"]["into"] and inl["SubBytes"]["cycles"] > 500


def test_simulator_runs_programmed_firmware(tmp_path):
    from cwstudio.session import Session
    elf = elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    s = Session(simulate=True, data_dir=str(tmp_path))
    try:
        s.connect_scope("sim")
        s.connect_target("sim")
        hexf = tmp_path / "fw.hex"
        hexf.write_text(":00000001FF\n")
        assert s.program("STM32F", str(hexf)).get("emulation", {}).get("emulated") is False  # a .hex without its .elf keeps the built-in model
        shutil.copy(elf, tmp_path / "fw.elf")
        r = s.program("STM32F", str(hexf))
        assert r["emulation"]["emulated"] and r["emulation"]["core"] == "cortex-m4" and s.programmed["elf"].endswith("fw.elf")
        t0 = time.time()
        s.start_capture({"count": 120, "key_mode": "fixed", "text_mode": "random"})
        while s.worker.long_job and not s.worker.long_job.finished.is_set():
            time.sleep(0.05)
        rate = 120 / (time.time() - t0)
        W, tin, tout, key = s.store.as_arrays()
        assert W.shape == (120, 5000)
        assert all(encrypt_block(bytes(key[i]), bytes(tin[i])) == bytes(tout[i]) for i in range(len(W)))  # responses computed by the emulated firmware
        assert rate > 40, f"{rate:.0f} traces/s"
        # the traces follow the emulated execution: the code map's power model aligns at shift 0, scale 1 with a high correlation
        from cwstudio.codemap.service import CodeMapService
        svc = CodeMapService(s)
        st = svc.build({})
        assert st["aes_ok"] and st["matches_stored"] and st["sim_emulates"]
        al = st["alignment"]
        assert al["abs_r"] > 0.9 and abs(al["shift"]) < 2 and al["scale"] == pytest.approx(1.0, abs=0.003)
        # and they leak: CPA on the first-round S-box finds the key
        from cwstudio.analysis import CPAAttack
        cpa = CPAAttack(W, tin, tout, model="sbox_hw")
        cpa.start()
        cpa._thread.join(60)
        assert cpa.result.to_json()["best_key"] == KEY.hex()
        # raw serial traffic reaches the firmware too (SimpleSerial 2.1 version request)
        from cwstudio.codemap.emu import SimpleSerial
        ss = SimpleSerial("2.1")
        def ask():  # on the hardware thread, so the serial poll does not take the reply first
            s.target.flush()
            s.target.write(ss.frame("v", b""))
            return bytes(s.target._rx)
        resp = ss.parse(s.worker.call(ask))
        assert resp and resp[0]["cmd"] == "r" and resp[0]["ok"]
    finally:
        s.close()


# ----- HTTP API and MCP --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def api():
    from starlette.testclient import TestClient
    from cwstudio.app import create_app
    from cwstudio.session import Session
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    with TestClient(create_app(session)) as c:
        c.session = session
        yield c


def test_api_gating(api, tmp_path):
    assert api.get("/api/codemap").json()["ready"] is False
    r = api.post("/api/codemap/region", json={"start": 0, "end": 10})
    assert r.status_code == 404 and "build one first" in r.json()["detail"]
    junk = tmp_path / "x.elf"
    junk.write_bytes(b"not an elf at all")
    r = api.post("/api/codemap/build", json={"elf": str(junk)})
    assert r.status_code == 400 and "Unsupported" in r.json()["detail"] and "not an ELF" in r.json()["detail"]
    host = shutil.which("ls") or "/bin/ls"
    if os.path.isfile(host) and open(host, "rb").read(4) == b"\x7fELF":
        r = api.post("/api/codemap/build", json={"elf": host})
        assert r.status_code == 400 and "cannot be emulated" in r.json()["detail"]
    assert api.post("/api/codemap/build", json={}).status_code in (400, 404)  # no ELF anywhere yet
    r = api.post("/api/codemap/upload", files={"file": ("fw.hex", b":00000001FF\n")})
    assert r.status_code == 400 and "not an ELF" in r.json()["detail"]
    eng = api.get("/api/codemap").json()["engines"]
    assert eng["avr"]["available"] and eng["arm"]["available"] == bool(eng["unicorn"])


def test_api_build_region_lookup(api):
    elf = elf_or_skip("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    api.post("/api/scope/connect", json={"kind": "sim"})
    api.post("/api/target/connect", json={"kind": "sim"})
    assert api.post("/api/target/program", json={"programmer": "STM32F", "path": elf}).json()["emulation"]["emulated"]
    api.post("/api/capture/start", json={"count": 30, "clear": True})
    for _ in range(300):
        if not api.get("/api/status").json()["job"]["running"]:
            break
        time.sleep(0.05)
    assert api.get("/api/codemap/elfs").json()[0]["source"] == "programmed"
    up = api.post("/api/codemap/upload", files={"file": ("mine.elf", open(elf, "rb").read())}).json()["path"]
    assert os.path.basename(up) == "mine.elf" and api.post("/api/codemap/build", json={"elf": os.path.dirname(up), "align": False}).json()["name"] == "mine.elf"  # a folder: its newest ELF
    st = api.post("/api/codemap/build", json={"elf": elf}).json()
    assert st["ready"] and st["trace"] == 29 and st["aes_ok"] and st["matches_stored"] and "band" in st
    assert st["alignment"]["label"] in ("high", "medium") and abs(st["alignment"]["shift"]) < 2
    band = api.get("/api/codemap/band").json()["band"]
    assert len(band["spans"]["start"]) == len(band["spans"]["func"]) > 50 and band["t1"] > 1000
    sb = api.post("/api/codemap/lookup", json={"function": "SubBytes"}).json()["ranges"]
    a, b = sb[0]["samples"]
    reg = api.post("/api/codemap/region", json={"start": a, "end": b}).json()
    assert any(f["name"] == "SubBytes" and f["share"] > 0.9 for f in reg["functions"])
    assert all("samples" in ln for ln in reg["lines"])
    line = api.post("/api/codemap/lookup", json={"file": "aes.c", "line": reg["lines"][0]["line"]}).json()
    assert line["ranges"] and line["path"].endswith("aes.c")
    src = api.get("/api/codemap/source", params={"file": "aes.c"}).json()
    assert "SubBytes" in src["text"] and src["executed"]
    dis = api.get("/api/codemap/disasm", params={"function": "SubBytes"}).json()["instructions"]
    assert dis and any(i["executed"] >= 10 for i in dis)
    m0 = api.get("/api/codemap").json()["mapping"]["shift"]
    m1 = api.put("/api/codemap/mapping", json={"dshift": 4}).json()["mapping"]["shift"]
    assert m1 == pytest.approx(m0 + 4)
    al = api.post("/api/codemap/align", json={"source": "trace"}).json()["alignment"]
    assert abs(al["shift"]) < 2
    pc = api.post("/api/codemap/pctrace", json={"interval": 64}).json()
    assert pc["mode"] == "simulated" and pc["samples"] > 50 and pc["agreement"] > 0.95 and pc["scale"] == pytest.approx(1.0, abs=0.01)
    assert api.get("/api/codemap/at", params={"sample": a + 4}).json()["func"] == "SubBytes"
    r = api.get("/api/codemap/model")
    assert r.status_code == 200 and len(r.content) > 5000 * 4


def test_mcp_codemap_tools():
    from cwstudio.mcp_server import StudioError, build_server

    class FakeClient:
        base = "http://fake"

        def post(self, path, body=None, timeout=None):
            raise StudioError(f"POST {path} failed (400): Unsupported: ppc firmware cannot be emulated")

        get = put = post

    m = build_server(FakeClient())
    names = {t["name"] for t in m._tools_list({})["tools"]}
    assert {"code_map_build", "code_map_region", "code_map_lookup", "code_map_align", "code_map_disassemble", "code_map_pc_trace"} <= names
    r = m._tools_call({"name": "code_map_build", "arguments": {}})
    assert not r["isError"] and '"supported": false' in r["content"][0]["text"] and "cannot be emulated" in r["content"][0]["text"]
