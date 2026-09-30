"""Compiler wrapper used when building ChipWhisperer firmware with clang.

ChipWhisperer's makefiles are written for GCC.  Firmware builds with clang set ``CC`` to this wrapper, which

* compiles C/C++ with clang (``CWSTUDIO_CC``: the clang command as a JSON list, e.g. ``["zig", "clang", "--target=avr", ...]``), dropping GCC-only options and GNU assembler listing flags that clang's integrated assembler rejects, and spelling out RISC-V extensions that newer tools no longer imply (``rv32i`` -> ``rv32i_zicsr_zifencei``);
* hands hand-written assembly (``.S``/``.s``) and anything else it does not recognise to the GCC driver (``CWSTUDIO_GCC``), whose GNU syntax the startup files are written in.

Linking is done by GCC directly (the build sets ``LINK_COMPILER``), so the firmware still uses newlib/avr-libc and ChipWhisperer's linker scripts.

Run as ``python -m cwstudio.ccwrap <args>`` or, from the frozen bundle, ``ChipWhispererStudio --ccwrap <args>``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import List

ENV_CC = "CWSTUDIO_CC"
ENV_GCC = "CWSTUDIO_GCC"

# GCC options with no clang equivalent (exact match or prefix).
DROP_EXACT = {
    "-mno-fdiv",            # neorv32 HAL
    "-mcall-prologues",     # AVR size optimisation
    "-mrelax",
    "-fno-tree-loop-distribute-patterns",
    "-fno-strict-volatile-bitfields",
    "-Wno-discarded-qualifiers",
    "-fno-reorder-blocks-and-partition",
    "-mapcs", "-mapcs-frame", "-mno-apcs-frame", "-mno-sched-prolog",
}
DROP_PREFIX = ("-misa-spec=", "--param=", "-fstack-usage=")
# GNU as options (listing files, stabs) that the integrated assembler rejects.
WA_DROP = re.compile(r"^-(a[cdhlmns]*(=.*)?|gstabs.*|-gdwarf.*|mmcu=.*)$")


def is_assembly(args: List[str]) -> bool:
    if "assembler-with-cpp" in args or "assembler" in args:
        return True
    return any(a.endswith((".S", ".s", ".sx")) and not a.startswith("-") for a in args)


def _fix_march(a: str) -> str:
    m = re.match(r"^-march=(rv(?:32|64)[a-z]+)(.*)$", a)
    if not m:
        return a
    base, rest = m.groups()
    if "g" in base[4:]:
        return a  # 'g' already includes zicsr/zifencei
    extra = "".join(f"_{x}" for x in ("zicsr", "zifencei") if x not in rest)
    return f"-march={base}{rest}{extra}"


def clang_args(args: List[str]) -> List[str]:
    out = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in DROP_EXACT or a.startswith(DROP_PREFIX):
            continue
        if a == "--param":
            skip = True
            continue
        if a.startswith("-Wa,"):
            keep = [x for x in a[4:].split(",") if x and not WA_DROP.match(x)]
            if keep:
                out.append("-Wa," + ",".join(keep))
            continue
        out.append(_fix_march(a))
    return out


def command(argv: List[str], env=None) -> List[str]:
    env = os.environ if env is None else env
    cc, gcc = env.get(ENV_CC), env.get(ENV_GCC)
    if not cc:
        raise RuntimeError(f"{ENV_CC} is not set")
    if gcc and is_assembly(argv):
        return json.loads(gcc) + argv
    return json.loads(cc) + clang_args(argv)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        cmd = command(argv)
    except (RuntimeError, ValueError) as e:
        sys.stderr.write(f"ccwrap: {e}\n")
        return 2
    try:
        return subprocess.call(cmd)
    except OSError as e:
        sys.stderr.write(f"ccwrap: cannot run {cmd[0]}: {e}\n")
        return 127


def wrapper_command() -> List[str]:
    """How to invoke this wrapper from a makefile on this installation."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ccwrap"]
    return [sys.executable, "-m", "cwstudio.ccwrap"]


if __name__ == "__main__":
    sys.exit(main())
