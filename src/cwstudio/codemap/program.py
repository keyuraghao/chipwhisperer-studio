"""The program model behind the code map: an ELF file's memory image, functions, DWARF line table and inlined calls, and the source files they point to.

Addresses are byte addresses as the ELF has them (for AVR that is the byte address in flash; data addresses carry avr-gcc's 0x800000 offset). Per executable segment the model keeps flat arrays (one entry per byte address) of the function, the source line and the innermost inlined function, so mapping millions of executed program counters to code is a numpy gather instead of a search.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import struct
import subprocess
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger("cwstudio.codemap")

EM_ARM, EM_AVR, EM_RISCV = 40, 83, 243
ARCH_BY_MACHINE = {EM_ARM: "arm", EM_AVR: "avr", EM_RISCV: "riscv"}
MAX_SPAN = 32 * 1024 * 1024  # executable bytes covered by the flat lookup arrays


def _allow_16bit_dwarf() -> None:
    """pyelftools only parses DWARF with 4 or 8 byte addresses; clang's AVR output uses 2. Teach its struct factory 16-bit addresses (done once, only for address size 2)."""
    from elftools.dwarf import structs as S
    D = S.DWARFStructs
    if getattr(D, "_cwstudio_addr16", False):
        return
    orig_new, orig_create = D.__new__, D._create_structs

    def __new__(cls, little_endian, dwarf_format, address_size, dwarf_version=2):
        if address_size != 2:
            return orig_new(cls, little_endian, dwarf_format, address_size, dwarf_version)
        key = (little_endian, dwarf_format, address_size, dwarf_version)
        if key in cls._structs_cache:
            return cls._structs_cache[key]
        self = object.__new__(cls)
        self.little_endian, self.dwarf_format, self.address_size, self.dwarf_version = little_endian, dwarf_format, 2, dwarf_version
        self._create_structs()
        cls._structs_cache[key] = self
        return self

    def _create_structs(self):
        orig_create(self)
        if self.address_size == 2:
            self.Dwarf_target_addr = S.ULInt16 if self.little_endian else S.UBInt16
            self.the_Dwarf_target_addr = self.Dwarf_target_addr("")
            for m in ("_create_dw_form", "_create_lineprog_header", "_create_callframe_entry_headers", "_create_aranges_header"):
                if hasattr(self, m):
                    getattr(self, m)()
    D.__new__ = staticmethod(__new__)
    D._create_structs = _create_structs
    D._cwstudio_addr16 = True


class ProgramError(ValueError):
    """The file is not an ELF the code map can use (the message says why)."""


@dataclass
class Function:
    name: str
    lo: int
    hi: int  # exclusive
    file: int = -1  # index into Program.files of the declaration, -1 if unknown
    line: int = 0
    source: str = "symtab"

    def to_json(self, prog: "Program") -> Dict[str, Any]:
        return {"name": self.name, "lo": self.lo, "hi": self.hi, "file": prog.file_label(self.file) if self.file >= 0 else None, "file_index": self.file, "line": self.line}


@dataclass
class Segment:
    vaddr: int
    paddr: int
    data: bytes
    memsz: int
    flags: int  # PF_X=1, PF_W=2, PF_R=4

    @property
    def executable(self) -> bool:
        return bool(self.flags & 1)

    @property
    def writable(self) -> bool:
        return bool(self.flags & 2)


@dataclass
class _Span:
    lo: int
    hi: int
    func: np.ndarray = field(repr=False)
    file: np.ndarray = field(repr=False)
    line: np.ndarray = field(repr=False)
    inline: np.ndarray = field(repr=False)
    outer: np.ndarray = field(repr=False)  # outermost inlined function (the one inlined into the real function)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Program:
    """Parse ``path`` (an ELF with or without debug information)."""

    def __init__(self, path: str, source_roots: Optional[Sequence[str]] = None):
        from elftools.common.exceptions import ELFError
        from elftools.elf.elffile import ELFFile
        self.path = os.path.abspath(path)
        self.source_roots = [os.path.abspath(os.path.expanduser(r)) for r in (source_roots or []) if r]
        try:
            fh = open(self.path, "rb")
        except OSError as e:
            raise ProgramError(f"cannot open {path}: {e}") from e
        with fh:
            try:
                elf = ELFFile(fh)
            except ELFError as e:
                raise ProgramError(f"{os.path.basename(path)} is not an ELF file ({e}); give the .elf the build made next to the .hex") from e
            self.sha256 = _sha(self.path)
            self.machine = elf.header["e_machine"]
            mach_num = {"EM_ARM": EM_ARM, "EM_AVR": EM_AVR, "EM_RISCV": EM_RISCV}.get(self.machine, self.machine)
            self.arch = ARCH_BY_MACHINE.get(mach_num) if isinstance(mach_num, int) else None
            self.machine_name = elf.get_machine_arch()
            self.elfclass = elf.elfclass
            self.little_endian = elf.little_endian
            self.e_flags = elf.header["e_flags"]
            self.entry = elf.header["e_entry"]
            self.segments: List[Segment] = []
            for s in elf.iter_segments():
                if s["p_type"] == "PT_LOAD" and s["p_memsz"]:
                    self.segments.append(Segment(s["p_vaddr"], s["p_paddr"], s.data(), s["p_memsz"], s["p_flags"]))
            self.sections: Dict[str, Tuple[int, int, int]] = {}
            for sec in elf.iter_sections():
                if sec["sh_flags"] & 2 and sec.name:  # SHF_ALLOC
                    self.sections[sec.name] = (sec["sh_addr"], sec["sh_size"], sec["sh_flags"])
            self.attributes = self._attributes(elf)
            self.avr_device = self._avr_deviceinfo(elf) if self.arch == "avr" else None
            self.symbols: Dict[str, int] = {}
            self.data_symbols: Dict[str, Tuple[int, int]] = {}
            self.files: List[str] = []
            self._file_index: Dict[str, int] = {}
            self.functions: List[Function] = []
            self._read_symbols(elf)
            self.has_dwarf = elf.has_dwarf_info() and elf.get_section_by_name(".debug_info") is not None
            if self.has_dwarf and self.arch == "avr":
                _allow_16bit_dwarf()
            rows: List[Tuple[int, int, int, bool]] = []
            inlines: List[Tuple[int, int, int, str, int, int]] = []
            dwarf_funcs: List[Function] = []
            if self.has_dwarf:
                try:
                    self._read_dwarf(elf, rows, inlines, dwarf_funcs)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not read the DWARF information of %s: %s", path, e)
        self._merge_functions(dwarf_funcs)
        self.inline_names: List[str] = []
        self.inline_info: List[Tuple[str, int, int]] = []  # name, call file, call line
        self._build_spans(rows, inlines)
        self._sources: Dict[int, Optional[str]] = {}
        self._resolved: Dict[int, Optional[str]] = {}
        self.objdump: Optional[str] = None

    # --- ELF ----------------------------------------------------------------------------------
    @staticmethod
    def _attributes(elf) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name in (".ARM.attributes", ".riscv.attributes"):
            sec = elf.get_section_by_name(name)
            if sec is None or not hasattr(sec, "iter_subsections"):
                continue
            try:
                for sub in sec.iter_subsections():
                    for ss in sub.iter_subsubsections():
                        for a in ss.iter_attributes():
                            out[str(a.tag)] = a.value
            except Exception as e:  # noqa: BLE001
                log.debug("attributes: %s", e)
        return out

    @staticmethod
    def _avr_deviceinfo(elf) -> Optional[Dict[str, Any]]:
        sec = elf.get_section_by_name(".note.gnu.avr.deviceinfo")
        if sec is None:
            return None
        raw = sec.data()
        try:
            namesz, descsz, _type = struct.unpack_from("<III", raw, 0)
            off = 12 + ((namesz + 3) & ~3)
            fs, fz, ss, sz, es, ez, offlen = struct.unpack_from("<7I", raw, off)
            names = re.findall(rb"[A-Za-z][A-Za-z0-9_]{2,}", raw[off + 32:])  # after the offset table comes the string table with the device name
            name = names[0].decode("ascii") if names else None
            return {"device": name, "flash_start": fs, "flash_size": fz, "sram_start": ss, "sram_size": sz, "eeprom_size": ez}
        except struct.error:
            return None

    def _read_symbols(self, elf) -> None:
        tab = elf.get_section_by_name(".symtab")
        if tab is None:
            return
        thumb = self.arch == "arm"
        funcs: Dict[Tuple[int, str], Function] = {}
        for s in tab.iter_symbols():
            name = s.name
            if not name or name.startswith("$") or name.startswith(".L"):
                continue
            typ = s["st_info"]["type"]
            val, size = s["st_value"], s["st_size"]
            if typ == "STT_FUNC" or (typ == "STT_NOTYPE" and s["st_shndx"] not in ("SHN_UNDEF", "SHN_ABS") and self._in_exec(val & ~1 if thumb else val)):
                addr = val & ~1 if thumb else val
                if typ == "STT_FUNC" or name not in self.symbols:
                    self.symbols[name] = addr
                if size and typ == "STT_FUNC":
                    funcs[(addr, name)] = Function(name, addr, addr + size)
            elif typ == "STT_OBJECT":
                self.data_symbols[name] = (val, size)
        # NOTYPE labels with no size (assembly entry points) get a range up to the next symbol
        self.functions = sorted(funcs.values(), key=lambda f: (f.lo, -f.hi))

    def _in_exec(self, addr: int) -> bool:
        return any(s.executable and s.vaddr <= addr < s.vaddr + s.memsz for s in self.segments)

    def exec_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.executable]

    # --- DWARF --------------------------------------------------------------------------------
    def _file_id(self, path: str) -> int:
        path = os.path.normpath(path)
        i = self._file_index.get(path)
        if i is None:
            i = len(self.files)
            self.files.append(path)
            self._file_index[path] = i
        return i

    def _read_dwarf(self, elf, rows, inlines, dwarf_funcs) -> None:
        dw = elf.get_dwarf_info()
        rl = None
        try:
            rl = dw.range_lists()
        except Exception:  # noqa: BLE001
            rl = None
        rnl = None
        try:
            rnl = dw.rnglistsinfo if hasattr(dw, "rnglistsinfo") else None
        except Exception:  # noqa: BLE001
            rnl = None
        for cu in dw.iter_CUs():
            top = cu.get_top_DIE()
            comp_dir = _s(top.attributes.get("DW_AT_comp_dir"))
            lp = dw.line_program_for_CU(cu)
            fmap: Dict[int, int] = {}
            ver = cu["version"]
            if lp is not None:
                hdr = lp.header
                dirs = [_b(d) for d in (hdr.get("include_directory") or [])]
                for k, fe in enumerate(hdr.get("file_entry") or []):
                    name = _b(fe.name)
                    di = fe.dir_index
                    if ver >= 5:
                        d = dirs[di] if di < len(dirs) else ""
                    else:
                        d = comp_dir if di == 0 else (dirs[di - 1] if di - 1 < len(dirs) else "")
                    full = name if os.path.isabs(name) else os.path.join(d, name)
                    if not os.path.isabs(full) and comp_dir:
                        full = os.path.join(comp_dir, full)
                    fmap[k if ver >= 5 else k + 1] = self._file_id(full)
                for ent in lp.get_entries():
                    st = ent.state
                    if st is None:
                        continue
                    if st.end_sequence:
                        rows.append((st.address, -1, 0, True))
                    else:
                        rows.append((st.address, fmap.get(st.file, -1), st.line, False))
            # subprograms and inlined subroutines
            base = _attr_int(top, "DW_AT_low_pc") or 0
            self._walk_dies(top, cu, dw, rl, rnl, base, fmap, dwarf_funcs, inlines, 0)

    def _ranges(self, die, cu, dw, rl, rnl, base) -> List[Tuple[int, int]]:
        a = die.attributes
        if "DW_AT_low_pc" in a and "DW_AT_high_pc" in a:
            lo = a["DW_AT_low_pc"].value
            hv = a["DW_AT_high_pc"]
            hi = hv.value if hv.form == "DW_FORM_addr" else lo + hv.value
            return [(lo, hi)] if hi > lo else []
        if "DW_AT_ranges" in a:
            out = []
            try:
                if cu["version"] >= 5 and rnl is not None:
                    lst = rnl.get_range_list_at_offset(a["DW_AT_ranges"].value, cu=cu) if a["DW_AT_ranges"].form != "DW_FORM_rnglistx" else rnl.get_range_list_at_offset_ex(a["DW_AT_ranges"].value)
                elif rl is not None:
                    lst = rl.get_range_list_at_offset(a["DW_AT_ranges"].value, cu=cu)
                else:
                    return []
            except Exception:  # noqa: BLE001
                return []
            cur = base
            for e in lst:
                if hasattr(e, "base_address"):
                    cur = e.base_address
                    continue
                lo, hi = e.begin_offset, e.end_offset
                if not getattr(e, "is_absolute", False):
                    lo, hi = lo + cur, hi + cur
                if hi > lo:
                    out.append((lo, hi))
            return out
        return []

    def _die_name(self, die) -> Optional[str]:
        for _ in range(4):
            a = die.attributes
            if "DW_AT_name" in a:
                return _s(a["DW_AT_name"])
            ref = a.get("DW_AT_abstract_origin") or a.get("DW_AT_specification")
            if ref is None:
                return None
            try:
                die = die.get_DIE_from_attribute(ref.name)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _decl(self, die, fmap) -> Tuple[int, int]:
        for _ in range(4):
            a = die.attributes
            if "DW_AT_decl_file" in a:
                return fmap.get(a["DW_AT_decl_file"].value, -1), _attr_int(die, "DW_AT_decl_line") or 0
            ref = a.get("DW_AT_abstract_origin") or a.get("DW_AT_specification")
            if ref is None:
                break
            try:
                die = die.get_DIE_from_attribute(ref.name)
            except Exception:  # noqa: BLE001
                break
        return -1, 0

    def _walk_dies(self, die, cu, dw, rl, rnl, base, fmap, funcs, inlines, depth) -> None:
        for child in die.iter_children():
            tag = child.tag
            if tag == "DW_TAG_subprogram":
                rngs = self._ranges(child, cu, dw, rl, rnl, base)
                name = self._die_name(child)
                if rngs and name:
                    f, ln = self._decl(child, fmap)
                    for lo, hi in rngs:
                        funcs.append(Function(name, lo, hi, f, ln, "dwarf"))
                self._walk_dies(child, cu, dw, rl, rnl, base, fmap, funcs, inlines, 0)
            elif tag == "DW_TAG_inlined_subroutine":
                rngs = self._ranges(child, cu, dw, rl, rnl, base)
                name = self._die_name(child) or "?"
                cf = fmap.get(_attr_int(child, "DW_AT_call_file") or 0, -1)
                cl = _attr_int(child, "DW_AT_call_line") or 0
                for lo, hi in rngs:
                    inlines.append((lo, hi, depth + 1, name, cf, cl))
                self._walk_dies(child, cu, dw, rl, rnl, base, fmap, funcs, inlines, depth + 1)
            elif tag in ("DW_TAG_lexical_block", "DW_TAG_namespace", "DW_TAG_class_type", "DW_TAG_structure_type"):
                self._walk_dies(child, cu, dw, rl, rnl, base, fmap, funcs, inlines, depth)

    def _merge_functions(self, dwarf_funcs: List[Function]) -> None:
        by_lo: Dict[int, Function] = {f.lo: f for f in self.functions}
        for d in dwarf_funcs:
            f = by_lo.get(d.lo)
            if f is not None and f.name == d.name:
                f.file, f.line = d.file, d.line
                f.hi = max(f.hi, d.hi) if f.hi > f.lo else d.hi
            elif f is None:
                self.functions.append(d)
                by_lo[d.lo] = d
                self.symbols.setdefault(d.name, d.lo)
        # label-only entry points (e.g. assembly reset handlers) get a range up to the next known function
        known = {f.lo for f in self.functions}
        labels = sorted((a, n) for n, a in self.symbols.items() if a not in known and self._in_exec(a))
        starts = sorted(known | {a for a, _ in labels})
        for a, n in labels:
            i = bisect_right(starts, a)
            end = starts[i] if i < len(starts) else self._exec_end(a)
            if end > a:
                self.functions.append(Function(n, a, end, source="label"))
        self.functions.sort(key=lambda f: (f.lo, -f.hi))

    def _exec_end(self, a: int) -> int:
        for s in self.segments:
            if s.executable and s.vaddr <= a < s.vaddr + s.memsz:
                return s.vaddr + s.memsz
        return a

    def _build_spans(self, rows, inlines) -> None:
        self.spans: List[_Span] = []
        total = 0
        for seg in self.exec_segments():
            lo, hi = seg.vaddr, seg.vaddr + seg.memsz
            if total + (hi - lo) > MAX_SPAN:
                log.warning("code map: executable segment at 0x%x is too large to index", lo)
                continue
            total += hi - lo
            n = hi - lo
            self.spans.append(_Span(lo, hi, np.full(n, -1, np.int32), np.full(n, -1, np.int32), np.zeros(n, np.int32), np.full(n, -1, np.int32), np.full(n, -1, np.int32)))
        # functions: paint large ranges first so nested/smaller ones win
        for idx in sorted(range(len(self.functions)), key=lambda i: -(self.functions[i].hi - self.functions[i].lo)):
            f = self.functions[idx]
            self._paint(f.lo, f.hi, "func", idx)
        # line table: rows come in sequence order; each row covers up to the next row's address
        prev = None
        for r in rows:
            if prev is not None and not prev[3] and r[0] > prev[0]:
                self._paint(prev[0], r[0], "fl", (prev[1], prev[2]))
            prev = r
        # inlined subroutines: paint shallow first so the innermost call wins
        for lo, hi, depth, name, cf, cl in sorted(inlines, key=lambda t: t[2]):
            self.inline_names.append(name)
            self.inline_info.append((name, cf, cl))
            self._paint(lo, hi, "inline", len(self.inline_names) - 1)
            if depth == 1:
                self._paint(lo, hi, "outer", len(self.inline_names) - 1)

    def _paint(self, lo: int, hi: int, what: str, value) -> None:
        for sp in self.spans:
            a, b = max(lo, sp.lo), min(hi, sp.hi)
            if a >= b:
                continue
            if what == "func":
                sp.func[a - sp.lo:b - sp.lo] = value
            elif what == "inline":
                sp.inline[a - sp.lo:b - sp.lo] = value
            elif what == "outer":
                sp.outer[a - sp.lo:b - sp.lo] = value
            else:
                sp.file[a - sp.lo:b - sp.lo] = value[0]
                sp.line[a - sp.lo:b - sp.lo] = value[1]

    # --- lookups ------------------------------------------------------------------------------
    def lookup(self, addrs: np.ndarray) -> Dict[str, np.ndarray]:
        """Vectorised: function index, file index, line and inline index (-1 when unknown) for byte addresses."""
        addrs = np.asarray(addrs, dtype=np.int64)
        out = {k: np.full(addrs.shape, -1, np.int32) for k in ("func", "file", "line", "inline", "outer")}
        out["line"][:] = 0
        for sp in self.spans:
            m = (addrs >= sp.lo) & (addrs < sp.hi)
            if not m.any():
                continue
            off = addrs[m] - sp.lo
            out["func"][m] = sp.func[off]
            out["file"][m] = sp.file[off]
            out["line"][m] = sp.line[off]
            out["inline"][m] = sp.inline[off]
            out["outer"][m] = sp.outer[off]
        return out

    def func_at(self, addr: int) -> Optional[Function]:
        i = int(self.lookup(np.array([addr]))["func"][0])
        return self.functions[i] if i >= 0 else None

    def line_at(self, addr: int) -> Tuple[int, int]:
        r = self.lookup(np.array([addr]))
        return int(r["file"][0]), int(r["line"][0])

    def function(self, name: str) -> Optional[Function]:
        for f in self.functions:
            if f.name == name:
                return f
        return None

    def ranges_for_line(self, file_index: int, line: int) -> List[Tuple[int, int]]:
        out: List[Tuple[int, int]] = []
        for sp in self.spans:
            m = (sp.file == file_index) & (sp.line == line)
            if not m.any():
                continue
            idx = np.flatnonzero(m)
            breaks = np.flatnonzero(np.diff(idx) != 1)
            starts = np.concatenate([[0], breaks + 1])
            ends = np.concatenate([breaks, [len(idx) - 1]])
            out += [(sp.lo + int(idx[s]), sp.lo + int(idx[e]) + 1) for s, e in zip(starts, ends)]
        return out

    def lines_in_file(self, file_index: int) -> List[int]:
        s = set()
        for sp in self.spans:
            s.update(np.unique(sp.line[sp.file == file_index]).tolist())
        s.discard(0)
        return sorted(s)

    def file_label(self, i: int) -> str:
        if i < 0 or i >= len(self.files):
            return "?"
        return self.files[i]

    def short_label(self, i: int) -> str:
        return os.path.basename(self.file_label(i))

    def find_file(self, name: str) -> int:
        """Index of a file given its full path, a path suffix or a base name (-1 if unknown)."""
        if not name:
            return -1
        norm = os.path.normpath(name)
        if norm in self._file_index:
            return self._file_index[norm]
        cands = [i for i, p in enumerate(self.files) if p.endswith(os.sep + norm) or p == norm or os.path.basename(p) == os.path.basename(norm)]
        exact = [i for i in cands if self.files[i].endswith(norm)]
        pick = exact or cands
        if not pick:
            return -1
        with_code = [i for i in pick if self.lines_in_file(i)]
        return (with_code or pick)[0]

    # --- sources ----------------------------------------------------------------------------
    def set_source_roots(self, roots: Sequence[str]) -> None:
        self.source_roots = [os.path.abspath(os.path.expanduser(r)) for r in roots if r]
        self._sources.clear()
        self._resolved.clear()
        self._by_name = None

    def resolve_source(self, i: int) -> Optional[str]:
        if i in self._resolved:
            return self._resolved[i]
        path = self.file_label(i)
        found = None
        parts = path.replace("\\", "/").split("/")
        for root in self.source_roots:  # a chosen source folder wins over the build paths
            for k in range(1, len(parts)):
                cand = os.path.join(root, *parts[k:])
                if os.path.isfile(cand):
                    found = cand
                    break
            if found:
                break
        if found is None and os.path.isfile(path):
            found = path
        if found is None and self.source_roots:
            found = self._name_index().get(parts[-1])
        self._resolved[i] = found
        return found

    def _name_index(self) -> Dict[str, str]:
        """File name to path for everything under the source folders, walked once (not once per missing file: a big folder such as the home directory would take seconds each time). Skips hidden and build folders and stops after 200000 files."""
        if getattr(self, "_by_name", None) is None:
            idx: Dict[str, str] = {}
            n = 0
            for root in self.source_roots:
                for d, dirs, files in os.walk(root):
                    dirs[:] = sorted(x for x in dirs if not x.startswith((".", "objdir", "__pycache__", "node_modules")))
                    for f in files:
                        idx.setdefault(f, os.path.join(d, f))
                    n += len(files)
                    if n > 200_000:
                        break
                if n > 200_000:
                    break
            self._by_name = idx
        return self._by_name

    def source(self, i: int) -> Optional[str]:
        if i not in self._sources:
            p = self.resolve_source(i)
            text = None
            if p:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    text = None
            self._sources[i] = text
        return self._sources[i]

    # --- memory -------------------------------------------------------------------------------
    def read(self, addr: int, n: int) -> bytes:
        """Bytes of the load image at a virtual (execution) address; zero where nothing is loaded."""
        out = bytearray(n)
        for s in self.segments:
            a, b = max(addr, s.vaddr), min(addr + n, s.vaddr + len(s.data))
            if a < b:
                out[a - addr:b - addr] = s.data[a - s.vaddr:b - s.vaddr]
        return bytes(out)

    # --- disassembly ----------------------------------------------------------------------------
    def disassemble(self, lo: int, hi: int, limit: int = 400) -> List[Dict[str, Any]]:
        """Instructions in [lo, hi): address, bytes, text. Uses the toolchain's objdump when known, else capstone, else Studio's own AVR decoder."""
        lo, hi = int(lo), int(hi)
        if hi <= lo:
            return []
        if self.arch == "avr":
            from cwstudio.codemap.avr import disassemble as avr_dis
            return avr_dis(self.read(lo, hi - lo), lo)[:limit]
        if self.objdump and os.path.isfile(self.objdump):
            try:
                return _objdump(self.objdump, self.path, lo, hi, self.arch)[:limit]
            except Exception as e:  # noqa: BLE001
                log.debug("objdump failed: %s", e)
        try:
            import capstone
        except ImportError:
            return [{"addr": lo, "bytes": self.read(lo, min(hi - lo, 16)).hex(), "text": "(install the toolchain or capstone for disassembly)"}]
        if self.arch == "arm":
            md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        else:
            md = capstone.Cs(capstone.CS_ARCH_RISCV, capstone.CS_MODE_RISCV32 | capstone.CS_MODE_RISCVC)
        out = []
        for ins in md.disasm(self.read(lo, hi - lo), lo):
            out.append({"addr": ins.address, "bytes": ins.bytes.hex(), "text": f"{ins.mnemonic} {ins.op_str}".strip()})
            if len(out) >= limit:
                break
        return out

    # --- summary --------------------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        text = sum(s.memsz for s in self.segments if s.executable)
        return {"path": self.path, "name": os.path.basename(self.path), "sha256": self.sha256, "arch": self.arch, "machine": self.machine_name,
                "entry": self.entry, "functions": len(self.functions), "files": len(self.files), "has_dwarf": self.has_dwarf, "text_bytes": text,
                "avr_device": self.avr_device, "attributes": {k: v for k, v in self.attributes.items() if k in ("TAG_CPU_NAME", "TAG_CPU_ARCH", "TAG_CPU_ARCH_PROFILE", "TAG_RISCV_arch")}}


def _b(v) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v) if v is not None else ""


def _s(attr) -> str:
    if attr is None:
        return ""
    return _b(attr.value)


def _attr_int(die, name) -> Optional[int]:
    a = die.attributes.get(name)
    if a is None:
        return None
    try:
        return int(a.value)
    except (TypeError, ValueError):
        return None


_OBJ_LINE = re.compile(r"^\s*([0-9a-f]+):\s+((?:[0-9a-f]{2,8}\s)+)\s*(.*)$")


def _objdump(objdump: str, elf: str, lo: int, hi: int, arch: str) -> List[Dict[str, Any]]:
    args = [objdump, "-d", f"--start-address=0x{lo:x}", f"--stop-address=0x{hi:x}", elf]
    r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    out = []
    for ln in r.stdout.splitlines():
        m = _OBJ_LINE.match(ln)
        if not m:
            continue
        text = m.group(3).split("\t", 1)
        txt = " ".join(t.strip() for t in text if t.strip())
        txt = re.sub(r"\s+", " ", txt)
        out.append({"addr": int(m.group(1), 16), "bytes": m.group(2).replace(" ", ""), "text": txt})
    return out


def find_objdump(toolchains, arch: str) -> Optional[str]:
    """The objdump of the GCC toolchain Studio uses for ``arch`` (None when none is installed)."""
    try:
        tc = toolchains.find(arch, "gcc")
    except Exception:  # noqa: BLE001
        return None
    if not tc:
        return None
    name = (tc.get("prefix") or "") + "objdump"
    from cwstudio.toolchains import exe
    p = os.path.join(tc["use_bin"], exe(name))
    if os.path.isfile(p):
        return p
    return shutil.which(name)
