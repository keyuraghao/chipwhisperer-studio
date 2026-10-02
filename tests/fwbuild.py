"""Build ChipWhisperer firmware ELFs for the code map tests, cached across test runs.

Toolchains and firmware sources come from a Studio data folder that already has them: ``$CWSTUDIO_FW_DATA``, the CI firmware job's ``build/fw-ci``, a ``.studio-dev/data`` folder next to the repository or the default ``~/ChipWhispererStudio``. The sources are copied (without build outputs) into a private folder so the original tree is never touched, and the toolchains folder is linked. Built ELFs are kept in ``<tmp>/cwstudio-codemap-elfs`` keyed by platform, compiler, crypto target and SimpleSerial version, so only the first run pays for the builds.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(tempfile.gettempdir(), "cwstudio-codemap-elfs")
_lock = threading.Lock()


def data_candidates():
    env = os.environ.get("CWSTUDIO_FW_DATA")
    if env:
        yield env
    yield os.path.join(REPO, "build", "fw-ci")
    yield os.path.join(os.path.dirname(REPO), ".studio-dev", "data")
    yield os.path.join(os.path.expanduser("~"), "ChipWhispererStudio")


def find_data() -> Optional[str]:
    for d in data_candidates():
        if os.path.isfile(os.path.join(d, "firmware", "chipwhisperer", "Makefile.inc")) and os.path.isdir(os.path.join(d, "toolchains")):
            return d
    return None


def _work_dir(src_data: str) -> str:
    """A data folder with a private copy of the firmware sources and a link to the toolchains."""
    work = os.path.join(CACHE, "data")
    fw = os.path.join(work, "firmware", "chipwhisperer")
    if not os.path.isfile(os.path.join(fw, "Makefile.inc")):
        os.makedirs(os.path.join(work, "firmware"), exist_ok=True)
        tmp = fw + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)

        def ignore(d, names):
            return [n for n in names if n.startswith("objdir") or os.path.splitext(n)[1] in (".elf", ".hex", ".bin", ".eep", ".lss", ".map", ".sym", ".o", ".d")]
        shutil.copytree(os.path.join(src_data, "firmware", "chipwhisperer"), tmp, ignore=ignore, symlinks=True)
        os.replace(tmp, fw)
    tc = os.path.join(work, "toolchains")
    if not os.path.exists(tc):
        try:
            os.symlink(os.path.abspath(os.path.join(src_data, "toolchains")), tc, target_is_directory=True)
        except OSError:  # no symlinks (Windows without developer mode): the builds use the toolchains where they are
            pass
    return work


def build_elf(platform: str, compiler: str = "gcc", crypto: str = "TINYAES128C", ss_ver: str = "SS_VER_2_1", opt: Optional[str] = None, project: str = "simpleserial-aes") -> str:
    """Path of a cached ELF, building it first if needed. Raises RuntimeError (with the reason) when it cannot be built."""
    name = f"{project}-{platform}-{compiler}-{crypto}-{ss_ver}" + (f"-O{opt}" if opt else "") + ".elf"
    out = os.path.join(CACHE, name)
    if os.path.isfile(out):
        return out
    src = find_data()
    if src is None:
        raise RuntimeError("no toolchains and firmware sources found (set CWSTUDIO_FW_DATA to a Studio data folder that has them)")
    with _lock:
        if os.path.isfile(out):
            return out
        from cwstudio.firmware import FirmwareManager
        from cwstudio.toolchains import ToolchainManager
        work = _work_dir(src)
        tm = ToolchainManager(work if os.path.exists(os.path.join(work, "toolchains")) else src)
        fm = FirmwareManager(work, tm)
        params = {"project": project, "platform": platform, "compiler": compiler, "crypto_target": crypto, "ss_ver": ss_ver, "jobs": 4}
        if opt:
            params["make_args"] = f"OPT={opt}"
        try:
            r = fm.build(params, wait=True)
        except (FileNotFoundError, ValueError, KeyError) as e:
            raise RuntimeError(f"cannot build {platform} with {compiler}: {e}") from e
        if r.get("state") != "ok":
            tail = "\n".join(fm.build_log()["lines"][-15:])
            raise RuntimeError(f"build of {platform}/{compiler}/{crypto} failed: {r.get('error')}\n{tail}")
        elf = os.path.join(fm.builds_dir, f"{project}-{platform}-{compiler}.elf")
        os.makedirs(CACHE, exist_ok=True)
        shutil.copy2(elf, out + ".tmp")
        os.replace(out + ".tmp", out)
        return out


if __name__ == "__main__":  # python tests/fwbuild.py PLATFORM [compiler] [crypto] [ss_ver]: warm the cache
    import sys
    sys.path.insert(0, os.path.join(REPO, "src"))
    print(build_elf(*sys.argv[1:]))
