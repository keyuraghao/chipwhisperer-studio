"""Offline tests for the toolchain manager, the clang wrapper and firmware catalogue parsing."""
import hashlib
import io
import json
import os
import pathlib
import tarfile
import zipfile

import pytest

from cwstudio import ccwrap
from cwstudio.firmware import FirmwareManager, parse_platforms
from cwstudio.toolchains import ToolchainManager, exe, extract, load_registry


def _tarball(path: pathlib.Path, files: dict, top: str = "tc-1.0") -> str:
    with tarfile.open(path, "w:gz") as t:
        for name, (data, mode) in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), mode
            t.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(url: str, sha: str, aliases=None):
    e = {"id": "fake-gcc", "name": "Fake GCC", "compiler": "gcc", "arch": ["arm"], "version": "1.0", "prefix": "fake-", "bin": "bin", "downloads": {"any": {"url": url, "sha256": sha, "size": 1}}}
    if aliases:
        e["aliases"] = aliases
    return {"schema": 1, "revision": 1, "toolchains": [e], "firmware_sources": {}}


def test_bundled_registry_is_pinned():
    reg = load_registry()
    ids = {e["id"] for e in reg["toolchains"]}
    assert {"arm-gcc", "avr-gcc", "riscv-gcc", "clang", "win-build-tools"} <= ids
    for e in reg["toolchains"]:
        for host, d in e["downloads"].items():
            assert d["url"].startswith("https://"), (e["id"], host)
            assert len(d["sha256"]) == 64, (e["id"], host)
    for e in reg["toolchains"]:
        if e["id"] in ("arm-gcc", "riscv-gcc", "clang"):
            assert {"linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64"} <= set(e["downloads"])


def test_install_verify_alias_remove(tmp_path):
    arc = tmp_path / "fake.tar.gz"
    sha = _tarball(arc, {"bin/" + exe("fake-gcc"): (b"#!/bin/sh\necho gcc\n", 0o755), "bin/" + exe("fake-objcopy"): (b"x", 0o755), "lib/readme": (b"hi", 0o644)})
    tm = ToolchainManager(str(tmp_path / "data"), registry=_registry(arc.as_uri(), sha, {"fake-": ["alias-"]}), host="linux-x64")
    st = tm.install("fake-gcc", wait=True)
    assert st["installed"], st
    b = pathlib.Path(st["bin"])
    assert (b / exe("fake-gcc")).exists() and (b / exe("alias-gcc")).exists() and (b / exe("alias-objcopy")).exists()
    assert os.name == "nt" or os.access(b / "fake-gcc", os.X_OK)
    found = tm.find("arm", "gcc")
    assert found and found["id"] == "fake-gcc" and found["use_bin"] == str(b)
    assert not tm.remove("fake-gcc")["installed"]


def test_mirror_fallback(tmp_path):
    arc = tmp_path / "fake.tar.gz"
    sha = _tarball(arc, {"bin/" + exe("fake-gcc"): (b"x", 0o755)})
    reg = _registry((tmp_path / "missing.tar.gz").as_uri(), sha)
    reg["toolchains"][0]["downloads"]["any"]["mirrors"] = [arc.as_uri()]
    tm = ToolchainManager(str(tmp_path / "data"), registry=reg, host="linux-x64")
    assert tm.install("fake-gcc", wait=True)["installed"]


def test_checksum_mismatch_is_rejected(tmp_path):
    arc = tmp_path / "fake.tar.gz"
    _tarball(arc, {"bin/" + exe("fake-gcc"): (b"x", 0o755)})
    tm = ToolchainManager(str(tmp_path / "data"), registry=_registry(arc.as_uri(), "0" * 64), host="linux-x64")
    st = tm.install("fake-gcc", wait=True)
    assert not st["installed"] and st["job"]["state"] == "error" and "checksum" in st["job"]["error"]


def test_extract_refuses_path_traversal(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("../escape.txt", "no")
    with pytest.raises(ValueError):
        extract(str(z), str(tmp_path / "out"))
    assert not (tmp_path / "escape.txt").exists()


def test_custom_toolchain_from_folder(tmp_path):
    bin_dir = tmp_path / "mytc" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / exe("tricore-elf-gcc")).write_text("x")
    tm = ToolchainManager(str(tmp_path / "data"), registry={"schema": 1, "revision": 1, "toolchains": []}, host="linux-x64")
    st = tm.add_custom({"name": "TriCore GCC", "compiler": "gcc", "arch": "tricore", "prefix": "tricore-elf-", "path": str(tmp_path / "mytc")})
    assert st["installed"] and st["custom"]
    assert tm.find("tricore", "gcc")["use_bin"] == str(bin_dir)
    tm.remove_custom(st["id"])
    assert tm.find("tricore", "gcc") is None
    with pytest.raises(FileNotFoundError):
        tm.add_custom({"name": "missing", "prefix": "nope-", "path": str(tmp_path)})


def test_ccwrap_translates_gcc_flags():
    args = ["-mcpu=cortex-m4", "-mno-fdiv", "-misa-spec=2.2", "-Wa,-adhlns=obj/x.lst", "-Wa,-gstabs,-mfoo", "-march=rv32i", "-mapcs", "--param", "max-inline=3", "-c", "x.c"]
    out = ccwrap.clang_args(args)
    assert out == ["-mcpu=cortex-m4", "-Wa,-mfoo", "-march=rv32i_zicsr_zifencei", "-c", "x.c"]
    assert ccwrap.clang_args(["-mmcu=atxmega128d3", "-Wa,-mmcu=atxmega128d3"]) == ["-mmcu=atxmega128d3"]
    assert ccwrap.clang_args(["-march=rv32imac_zicsr"]) == ["-march=rv32imac_zicsr_zifencei"]
    assert ccwrap.clang_args(["-march=rv32gc"]) == ["-march=rv32gc"]
    env = {ccwrap.ENV_CC: json.dumps(["clang", "--target=avr"]), ccwrap.ENV_GCC: json.dumps(["avr-gcc"])}
    assert ccwrap.command(["-x", "assembler-with-cpp", "-c", "start.S"], env)[0] == "avr-gcc"
    assert ccwrap.command(["-c", "main.c", "-mcall-prologues"], env) == ["clang", "--target=avr", "-c", "main.c"]


MAKEFILE_HAL = """
PLATFORM_LIST = CWLITEARM CWLITEXMEGA
  ifeq ($(PLATFORM),CW303)
  MCU = atxmega128d3
  HAL = xmega
  PLTNAME = CW-Lite XMEGA
  EXTRAPATH=
  else ifeq ($(PLATFORM),CWLITEARM)
  HAL = stm32f3
  PLTNAME = CW-Lite Arm \\(STM32F3\\)
  EXTRAPATH=
  else ifeq ($(PLATFORM), CW308_K82F)
  HAL = k82f
  PLTNAME = CW308T: Kinetis MK82F Target
  EXTRAPATH=chipwhisperer-fw-extra
  else ifeq ($(PLATFORM),CW308_NEORV32)
  HAL = neorv32
  PLTNAME = neorv32
  endif
"""


def test_parse_platforms(tmp_path):
    mk = tmp_path / "Makefile.hal"
    mk.write_text(MAKEFILE_HAL)
    plats = {p["name"]: p for p in parse_platforms(str(mk))}
    assert plats["CW303"]["arch"] == "avr" and plats["CW303"]["programmer"] == "XMEGA" and plats["CW303"]["mcu"] == "atxmega128d3"
    assert plats["CWLITEARM"]["label"] == "CW-Lite Arm (STM32F3)" and plats["CWLITEARM"]["programmer"] == "STM32F"
    assert plats["CW308_K82F"]["extra"] and plats["CW308_K82F"]["arch"] == "arm"
    assert plats["CW308_NEORV32"]["arch"] == "riscv" and plats["CW308_NEORV32"]["programmer"] == "NEORV32"


def test_firmware_folder_and_plan_errors(tmp_path):
    root = tmp_path / "fw"
    (root / "hal").mkdir(parents=True)
    (root / "Makefile.inc").write_text("")
    (root / "hal" / "Makefile.hal").write_text(MAKEFILE_HAL)
    (root / "simpleserial-aes").mkdir()
    (root / "simpleserial-aes" / "makefile").write_text("TARGET = simpleserial-aes\nCRYPTO_TARGET ?= TINYAES128C\ninclude ../Makefile.inc\n")
    tm = ToolchainManager(str(tmp_path / "data"), registry={"schema": 1, "revision": 1, "toolchains": []}, host="linux-x64")
    fm = FirmwareManager(str(tmp_path / "data"), tm)
    st = fm.set_root(str(root))
    assert st["valid"] and st["custom"]
    assert [p["name"] for p in fm.projects()] == ["simpleserial-aes"]
    with pytest.raises(FileNotFoundError, match="no GCC toolchain"):
        fm.plan({"project": "simpleserial-aes", "platform": "CWLITEARM"})
    with pytest.raises(FileNotFoundError):
        fm.plan({"project": "../../etc", "platform": "CWLITEARM"})
    with pytest.raises(FileNotFoundError):
        fm.set_root(str(tmp_path))
