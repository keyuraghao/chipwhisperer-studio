"""Build ChipWhisperer target firmware from inside Studio.

* Sources: ChipWhisperer's ``firmware/mcu`` tree, downloaded on request at the version pinned in ``resources/toolchains.json`` (plus the ``chipwhisperer-fw-extra`` HALs), or any folder the user points at, e.g. a git clone with local changes.
* Projects are the folders whose makefile includes ``Makefile.inc`` (simpleserial-aes, simpleserial-glitch, ...); platforms are read from ``hal/Makefile.hal``.
* A build runs ChipWhisperer's own makefiles with a toolchain from :mod:`cwstudio.toolchains`: GCC directly, or clang through :mod:`cwstudio.ccwrap` (clang compiles, GCC assembles the startup files and links against newlib/avr-libc).  The resulting ``.hex`` is copied to ``<data_dir>/firmware/builds`` and can be programmed straight away.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from cwstudio import ccwrap
from cwstudio import net
from cwstudio.toolchains import USER_AGENT, Cancelled, ToolchainManager, download, exe, extract

log = logging.getLogger("cwstudio.firmware")

# HAL name -> CPU architecture.  Anything not listed is an Arm Cortex-M part.
HAL_ARCH = {
    "avr": "avr", "xmega": "avr",
    "neorv32": "riscv", "ibex": "riscv", "fe310": "riscv",
    "aurix": "tricore", "mpc5676r": "ppc", "pic24f": "pic24", "rx65n": "rx",
}
# Platform -> Studio programmer (see hardware.PROGRAMMERS).
PROGRAMMER_BY_HAL = {
    "stm32f0": "STM32F", "stm32f0_nano": "STM32F", "stm32f1": "STM32F", "stm32f2": "STM32F", "stm32f3": "STM32F",
    "stm32f4": "STM32F", "xmega": "XMEGA", "avr": "AVR", "sam4s": "SAM4S", "neorv32": "NEORV32",
}
CLANG_TRIPLE = {"arm": "arm-none-eabi", "avr": "avr", "riscv": "riscv32-unknown-elf"}
# Newer GCC turns these old-style C warnings into errors; ChipWhisperer's HALs predate that.
COMPAT_CFLAGS = ["-Wno-error=implicit-function-declaration", "-Wno-error=incompatible-pointer-types",
                 "-Wno-error=int-conversion", "-fcommon"]
CRYPTO_TARGETS = ["TINYAES128C", "AVRCRYPTOLIB", "MBEDTLS", "AESSIMPLE", "MASKEDAES", "HWAES", "MICROECC", "NONE"]
SS_VERSIONS = ["SS_VER_2_1", "SS_VER_1_1", "SS_VER_1_0"]  # SS_VER_2_0 is deprecated and stops the build with an #error in simpleserial.c
SOURCE_MARKER = ".cwstudio-source.json"
TARGET_RE = re.compile(r"^\s*TARGET\s*[:?]?=\s*(\S+)", re.M)


def parse_platforms(makefile_hal: str) -> List[Dict[str, Any]]:
    """Read PLATFORM blocks (name, HAL, MCU, description) from ``hal/Makefile.hal``."""
    with open(makefile_hal, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        m = re.match(r"\s*(?:else\s+)?ifeq\s+\(\$\(PLATFORM\),\s*([A-Za-z0-9_]+)\s*\)", line)
        if m:
            cur = {"name": m.group(1), "hal": None, "mcu": None, "label": m.group(1), "extra": False}
            out.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("#"):
            continue
        kv = re.match(r"(HAL|MCU|PLTNAME|EXTRAPATH)\s*[:?]?=\s*(.*)$", s)
        if kv and cur.get("_" + kv.group(1)) is None:
            k, v = kv.group(1), kv.group(2).strip()
            cur["_" + k] = True
            if k == "HAL":
                cur["hal"] = v
            elif k == "MCU":
                cur["mcu"] = v or None
            elif k == "PLTNAME":
                cur["label"] = v.replace("\\(", "(").replace("\\)", ")")
            elif k == "EXTRAPATH":
                cur["extra"] = bool(v)
        elif s.startswith("endif"):
            cur = None
    seen, res = set(), []
    for p in out:
        if p["name"] in seen or not p["hal"]:
            continue
        seen.add(p["name"])
        p = {k: v for k, v in p.items() if not k.startswith("_")}
        p["arch"] = HAL_ARCH.get(p["hal"], "arm")
        p["programmer"] = "SAM4S" if p["name"] in ("CWHUSKY", "CW312_SAM4S") else PROGRAMMER_BY_HAL.get(p["hal"])
        res.append(p)
    return res


def quote(parts: List[str]) -> str:
    """Join a command for a makefile variable (make runs it through sh)."""
    return " ".join(shlex.quote(p.replace("\\", "/")) if os.name == "nt" else shlex.quote(p) for p in parts)


def gcc_include_dirs(gcc: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The C library include directories a GCC cross compiler uses (not its private builtins)."""
    try:
        r = subprocess.run([gcc, "-xc", "-E", "-v", "-"], input="", capture_output=True, text=True, timeout=30,
                           env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    dirs, on = [], False
    for line in r.stderr.splitlines():
        if line.startswith("#include <...> search starts here"):
            on = True
            continue
        if line.startswith("End of search list"):
            break
        if on:
            d = os.path.normpath(line.strip())
            parts = d.replace("\\", "/").split("/")
            if "gcc" in parts and parts.index("gcc") > 0 and parts[parts.index("gcc") - 1] == "lib":
                continue  # lib/gcc/<triple>/<ver>/include(-fixed): GCC's own builtins
            if os.path.isdir(d):
                dirs.append(d)
    return dirs


class FirmwareManager:
    def __init__(self, data_dir: str, toolchains: ToolchainManager,
                 publish: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.dir = os.path.join(data_dir, "firmware")
        self.builds_dir = os.path.join(self.dir, "builds")
        os.makedirs(self.builds_dir, exist_ok=True)
        self.tc = toolchains
        self._publish = publish or (lambda kind, payload: None)
        self.sources_cfg = toolchains.registry.get("firmware_sources", {})
        self.settings_path = os.path.join(self.dir, "settings.json")
        self._lock = threading.Lock()
        self.sources_job: Dict[str, Any] = {}
        self.build_job: Dict[str, Any] = {}
        self.build_lines: List[str] = []
        self._proc: Optional[subprocess.Popen] = None
        self._include_cache: Dict[str, List[str]] = {}
        self.last_check: Optional[Dict[str, Any]] = None

    # --- sources ---------------------------------------------------------------
    # Sources are not bundled: Studio downloads firmware/mcu straight from NewAE's GitHub repository at the commit the chosen channel points to (a branch such as "develop", "latest-release", or any tag or commit), so new upstream examples and fixes arrive without a new Studio release. The registry's "known_good" commit is used when GitHub cannot be reached.
    def _settings(self) -> Dict[str, Any]:
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_settings(self, s: Dict[str, Any]):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)

    @property
    def default_root(self) -> str:
        return os.path.join(self.dir, "chipwhisperer")

    @property
    def root(self) -> str:
        return self._settings().get("root") or self.default_root

    @property
    def channel(self) -> str:
        return self._settings().get("channel") or self.sources_cfg.get("default_channel", "develop")

    def is_valid_root(self, root: Optional[str] = None) -> bool:
        root = root or self.root
        return os.path.isfile(os.path.join(root, "Makefile.inc")) and os.path.isfile(
            os.path.join(root, "hal", "Makefile.hal"))

    def set_root(self, path: Optional[str]) -> Dict[str, Any]:
        s = self._settings()
        if path:
            path = os.path.abspath(os.path.expanduser(path))
            for cand in (path, os.path.join(path, "firmware", "mcu")):
                if self.is_valid_root(cand):
                    path = cand
                    break
            else:
                raise FileNotFoundError(f"{path} is not a ChipWhisperer firmware folder (it needs Makefile.inc and hal/Makefile.hal; point at firmware/mcu of a ChipWhisperer checkout)")
            s["root"] = path
        else:
            s.pop("root", None)
        self._save_settings(s)
        return self.sources_status()

    def set_channel(self, channel: str) -> Dict[str, Any]:
        channel = (channel or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", channel):
            raise ValueError("channel must be a branch, tag, commit or 'latest-release'")
        s = self._settings()
        s["channel"] = channel
        self._save_settings(s)
        self.last_check = None
        return self.sources_status()

    def installed_source(self, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
        try:
            with open(os.path.join(root or self.default_root, SOURCE_MARKER), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def sources_status(self) -> Dict[str, Any]:
        custom = bool(self._settings().get("root"))
        root = self.root
        installed = None if custom else self.installed_source()
        return {"root": root, "custom": custom, "valid": self.is_valid_root(root),
                "fw_extra": os.path.isdir(os.path.join(root, "hal", "chipwhisperer-fw-extra", "stm32f4")),
                "repo": self.sources_cfg.get("repo"), "channel": self.channel, "installed": installed,
                "last_check": self.last_check,
                "job": {k: v for k, v in self.sources_job.items() if k != "cancel"} or None}

    # GitHub lookups
    def _gh(self, path: str) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:  # optional: lifts GitHub's 60 requests/hour limit for anonymous API calls
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request("https://api.github.com" + path, headers=headers)
        with net.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def resolve(self, channel: Optional[str] = None) -> Dict[str, Any]:
        """Ask GitHub which commit ``channel`` points to, plus the fw-extra submodule commit it pins."""
        repo = self.sources_cfg["repo"]
        ref = channel or self.channel
        tag = None
        if ref == "latest-release":
            tag = self._gh(f"/repos/{repo}/releases/latest")["tag_name"]
            ref = tag
        c = self._gh(f"/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}")
        info = {"repo": repo, "channel": channel or self.channel, "ref": ref, "tag": tag, "commit": c["sha"],
                "date": c["commit"]["committer"]["date"], "message": c["commit"]["message"].split("\n")[0][:200]}
        extra = self.sources_cfg.get("fw_extra")
        if extra:
            try:
                sub = self._gh(f"/repos/{repo}/contents/{extra['path']}?ref={c['sha']}")
                info["fw_extra_commit"] = sub.get("sha")
            except Exception as e:  # noqa: BLE001
                log.warning("could not resolve chipwhisperer-fw-extra for %s: %s", c["sha"][:7], e)
        return info

    def check_updates(self) -> Dict[str, Any]:
        """Compare the downloaded sources with the channel's current upstream commit."""
        if self._settings().get("root"):
            raise RuntimeError("using a custom firmware folder; update it with git instead")
        installed = self.installed_source()
        latest = self.resolve()
        self.last_check = {"checked": time.time(), "latest": latest, "update_available": not installed or installed.get("commit") != latest["commit"]}
        st = self.sources_status()
        self._publish("firmware_sources", st)
        return st

    def fetch_sources(self, ref: Optional[str] = None, wait: bool = False) -> Dict[str, Any]:
        """Download (or update to) the channel's current commit, or ``ref`` if given."""
        with self._lock:
            if self.sources_job.get("state") in ("resolving", "downloading", "extracting"):
                return self.sources_status()
            self.sources_job = {"state": "resolving", "done": 0, "total": 0, "error": None, "cancel": threading.Event()}
        th = threading.Thread(target=self._fetch_sources, args=(ref,), name="fetch-firmware", daemon=True)
        th.start()
        if wait:
            th.join()
        return self.sources_status()

    def _fetch_sources(self, ref: Optional[str]):
        job = self.sources_job
        cfg = self.sources_cfg
        dest = self.default_root
        tmp, old = dest + ".tmp", dest + ".old"
        dl_dir = os.path.join(self.dir, ".downloads")
        last = [0.0]

        def progress(done, total):
            job["done"], job["total"] = done, total
            if time.time() - last[0] > 0.25:
                last[0] = time.time()
                self._publish("firmware_sources", self.sources_status())

        def strip_to(sub):
            def fn(name):
                parts = name.split("/", 1)
                if len(parts) < 2:
                    return None
                rest = parts[1]
                if not sub:
                    return rest or None
                if rest == sub or not rest.startswith(sub + "/"):
                    return None
                return rest[len(sub) + 1:] or None
            return fn

        try:
            self._publish("firmware_sources", self.sources_status())
            try:
                info = self.resolve(ref)
            except Exception as e:  # noqa: BLE001
                kg = cfg.get("known_good") or {}
                if not kg.get("commit"):
                    raise
                log.warning("GitHub lookup failed (%s); using the known good commit %s", e, kg["commit"][:7])
                info = {"repo": cfg["repo"], "channel": ref or self.channel, "ref": kg["commit"], "tag": None, "commit": kg["commit"], "date": kg.get("date"), "message": "known good fallback", "fw_extra_commit": kg.get("fw_extra_commit")}
            job.update({"state": "downloading", "commit": info["commit"]})
            shutil.rmtree(tmp, ignore_errors=True)
            a1 = os.path.join(dl_dir, f"chipwhisperer-{info['commit'][:12]}.tar.gz")
            log.info("Downloading %s firmware at %s (%s)", cfg["repo"], info["commit"][:7], info["ref"])
            download(f"https://codeload.github.com/{cfg['repo']}/tar.gz/{info['commit']}", a1, None, progress, job["cancel"])
            job["state"] = "extracting"
            self._publish("firmware_sources", self.sources_status())
            extract(a1, tmp, members=strip_to(cfg.get("subdir", "firmware/mcu")), cancel=job["cancel"])
            extra = cfg.get("fw_extra")
            if extra and info.get("fw_extra_commit"):
                job["state"] = "downloading"
                a2 = os.path.join(dl_dir, f"fw-extra-{info['fw_extra_commit'][:12]}.tar.gz")
                log.info("Downloading %s at %s", extra["repo"], info["fw_extra_commit"][:7])
                download(f"https://codeload.github.com/{extra['repo']}/tar.gz/{info['fw_extra_commit']}", a2, None, progress, job["cancel"])
                job["state"] = "extracting"
                sub = os.path.join(tmp, extra["dest"])
                shutil.rmtree(sub, ignore_errors=True)
                extract(a2, sub, members=strip_to(""), cancel=job["cancel"])
            if not self.is_valid_root(tmp):
                raise FileNotFoundError("the downloaded archive does not contain firmware/mcu")
            info["installed"] = time.time()
            with open(os.path.join(tmp, SOURCE_MARKER), "w", encoding="utf-8") as f:
                json.dump(info, f, indent=1)
            shutil.rmtree(old, ignore_errors=True)
            if os.path.isdir(dest):
                os.replace(dest, old)
            os.replace(tmp, dest)
            shutil.rmtree(old, ignore_errors=True)
            shutil.rmtree(dl_dir, ignore_errors=True)
            if self.last_check:
                self.last_check["update_available"] = self.last_check["latest"]["commit"] != info["commit"]
            job["state"] = "installed"
            log.info("Firmware sources at %s ready in %s", info["commit"][:7], dest)
        except Cancelled:
            job["state"] = "cancelled"
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            job["state"], job["error"] = "error", f"{type(e).__name__}: {e}"
            log.error("Fetching firmware sources failed: %s", e)
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            self._publish("firmware_sources", self.sources_status())

    def cancel_sources(self):
        if self.sources_job.get("cancel"):
            self.sources_job["cancel"].set()
        return self.sources_status()

    # --- catalogue -------------------------------------------------------------
    def projects(self) -> List[Dict[str, Any]]:
        root = self.root
        if not self.is_valid_root(root):
            return []
        out = []
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            mk = next((os.path.join(d, m) for m in ("makefile", "Makefile") if os.path.isfile(os.path.join(d, m))), None)
            if not mk:
                continue
            try:
                with open(mk, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if "Makefile.inc" not in text:
                continue
            m = TARGET_RE.search(text)
            out.append({"name": name, "target": m.group(1) if m else name,
                        "crypto": "CRYPTO_TARGET" in text or "Makefile.crypto" in text,
                        "simpleserial": "simpleserial" in text.lower()})
        return out

    def platforms(self) -> List[Dict[str, Any]]:
        mk = os.path.join(self.root, "hal", "Makefile.hal")
        if not os.path.isfile(mk):
            return []
        plats = parse_platforms(mk)
        fw_extra = os.path.isdir(os.path.join(self.root, "hal", "chipwhisperer-fw-extra", "stm32f4"))
        for p in plats:
            p["available"] = fw_extra or not p["extra"]
        return plats

    def platform(self, name: str) -> Dict[str, Any]:
        for p in self.platforms():
            if p["name"] == name:
                return p
        raise KeyError(f"unknown platform {name!r}")

    def catalogue(self) -> Dict[str, Any]:
        return {"sources": self.sources_status(), "projects": self.projects(), "platforms": self.platforms(),
                "crypto_targets": CRYPTO_TARGETS, "ss_versions": SS_VERSIONS}

    # --- build ----------------------------------------------------------------
    def _includes(self, gcc_path: str, env) -> List[str]:
        if gcc_path not in self._include_cache:
            self._include_cache[gcc_path] = gcc_include_dirs(gcc_path, env)
        return self._include_cache[gcc_path]

    def plan(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Work out the make command, environment and output file for a build (no side effects)."""
        root = self.root
        if not self.is_valid_root(root):
            raise FileNotFoundError("no firmware sources yet: download them or choose a firmware folder")
        project = params.get("project") or "simpleserial-aes"
        pdir = os.path.join(root, project)
        if not os.path.isdir(pdir) or os.path.dirname(os.path.abspath(pdir)) != os.path.abspath(root):
            raise FileNotFoundError(f"no project {project!r} in {root}")
        platform_name = params.get("platform") or "CWLITEARM"
        plat = self.platform(platform_name)
        if not plat["available"]:
            raise FileNotFoundError(f"{platform_name} needs the chipwhisperer-fw-extra HALs; download the sources "
                                    "again or use a checkout with that submodule")
        arch = params.get("arch") or plat["arch"]
        compiler = params.get("compiler") or "gcc"
        if compiler not in ("gcc", "clang"):
            raise ValueError("compiler must be gcc or clang")
        gcc = self.tc.find(arch, "gcc")
        if gcc is None:
            raise FileNotFoundError(f"no GCC toolchain for {arch}: install one on the Toolchains card "
                                    "(clang builds also need it for the C library and linker)")
        make_dir = self.tc.make_bin()
        if make_dir is None:
            hint = "install 'GNU make + sh' on the Toolchains card" if os.name == "nt" else \
                "install make (e.g. 'sudo apt install make' or 'xcode-select --install')"
            raise FileNotFoundError("make not found: " + hint)
        prefix = gcc["prefix"] or ""
        gbin = gcc["use_bin"]
        paths = [gbin]
        env = dict(os.environ)
        cflags = list(COMPAT_CFLAGS)
        asflags: List[str] = []
        if arch == "riscv":
            asflags.append("-misa-spec=2.2")
        tool = lambda t: prefix + t  # noqa: E731
        make_vars = {"PLATFORM": platform_name}
        if params.get("crypto_target"):
            make_vars["CRYPTO_TARGET"] = params["crypto_target"]
        if params.get("ss_ver"):
            if params["ss_ver"] not in SS_VERSIONS:
                raise ValueError(f"SimpleSerial version {params['ss_ver']} cannot be built; use one of {', '.join(SS_VERSIONS)} (SS_VER_2_0 is deprecated, use SS_VER_2_1)")
            make_vars["SS_VER"] = params["ss_ver"]
        for var, t in (("OBJCOPY", "objcopy"), ("OBJDUMP", "objdump"), ("SIZE", "size"), ("NM", "nm")):
            make_vars[var] = tool(t)
        clang = None
        if compiler == "clang":
            clang = self.tc.find(arch, "clang")
            if clang is None:
                raise FileNotFoundError(f"no clang toolchain for {arch}: install one on the Toolchains card")
            e = clang["entry"]
            cbin = clang["use_bin"]
            paths.insert(0, cbin)
            base = [os.path.join(cbin, exe(c)) if i == 0 else c for i, c in enumerate(e.get("clang_cmd") or ["clang"])]
            triple = CLANG_TRIPLE.get(arch)
            if not triple:
                raise ValueError(f"clang builds are not supported for {arch}")
            base += [f"--target={triple}"]
            if e.get("resource_dir"):
                base += ["-resource-dir", os.path.join(os.path.dirname(cbin) if e.get("bin", "bin") != "." else cbin,
                                                       e["resource_dir"])]
            env_probe = dict(env, PATH=os.pathsep.join(paths + [env.get("PATH", "")]))
            for d in self._includes(os.path.join(gbin, exe(tool("gcc"))), env_probe):
                base += ["-isystem", d]
            base += ["-Wno-unknown-warning-option", "-Wno-unused-command-line-argument"]
            env[ccwrap.ENV_CC] = json.dumps(base)
            env[ccwrap.ENV_GCC] = json.dumps([tool("gcc")])
            env["ZIG_GLOBAL_CACHE_DIR"] = os.path.join(self.tc.root, ".zig-cache")
            env["ZIG_LOCAL_CACHE_DIR"] = env["ZIG_GLOBAL_CACHE_DIR"]
            make_vars["CC"] = quote(ccwrap.wrapper_command())
            if not getattr(sys, "frozen", False):
                pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(ccwrap.__file__)))
                env["PYTHONPATH"] = os.pathsep.join([pkg_parent] + [x for x in [env.get("PYTHONPATH")] if x])
            make_vars["LINK_COMPILER"] = tool("gcc")
        else:
            make_vars["CC"] = tool("gcc")
            make_vars["CXX"] = tool("g++")
            if arch == "riscv":
                cflags.append("-misa-spec=2.2")
        if make_dir not in paths:
            paths.append(make_dir)
        env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
        extra_cflags = (params.get("cflags") or "").strip()
        env["CFLAGS"] = " ".join(cflags + ([extra_cflags] if extra_cflags else []))
        if asflags:
            env["ASFLAGS"] = " ".join(asflags)
        for k in ("CPPFLAGS", "LDFLAGS", "MAKEFLAGS", "MFLAGS"):
            env.pop(k, None)
        jobs = int(params.get("jobs") or os.cpu_count() or 2)
        make = os.path.join(make_dir, exe("make")) if os.path.isfile(os.path.join(make_dir, exe("make"))) else \
            shutil.which("gmake") or "make"
        args = [f"{k}={v}" for k, v in make_vars.items()]
        extra = shlex.split(params.get("make_args") or "", posix=True)
        target = next((p["target"] for p in self.projects() if p["name"] == project), project)
        return {
            "project": project, "platform": platform_name, "arch": arch, "compiler": compiler,
            "cwd": pdir, "make": make, "build_cmd": [make, f"-j{jobs}"] + args + extra,
            "clean_cmd": [make, f"PLATFORM={platform_name}"] + [a for a in args if a.startswith(("CRYPTO_TARGET=", "SS_VER="))] + ["clean"],
            "env": env, "hex": os.path.join(pdir, f"{target}-{platform_name}.hex"),
            "elf": os.path.join(pdir, f"{target}-{platform_name}.elf"),
            "programmer": plat.get("programmer"), "mcu": plat.get("mcu"), "label": plat.get("label"),
            "toolchains": {"gcc": {"id": gcc["id"], "bin": gbin, "version": gcc.get("version")},
                           "clang": {"id": clang["id"], "bin": clang["use_bin"], "version": clang.get("version")}
                           if clang else None},
        }

    def build_status(self) -> Dict[str, Any]:
        with self._lock:
            return {k: v for k, v in self.build_job.items() if k not in ("plan",)} or {"state": "idle"}

    def build_log(self, since: int = 0) -> Dict[str, Any]:
        with self._lock:
            return {"lines": self.build_lines[since:], "next": len(self.build_lines)}

    def build(self, params: Dict[str, Any], wait: bool = False) -> Dict[str, Any]:
        plan = self.plan(params)
        with self._lock:
            if self.build_job.get("state") == "running":
                raise RuntimeError("a build is already running")
            self.build_lines = []
            self.build_job = {"state": "running", "project": plan["project"], "platform": plan["platform"],
                              "compiler": plan["compiler"], "arch": plan["arch"], "started": time.time(),
                              "command": " ".join(plan["build_cmd"]), "toolchains": plan["toolchains"],
                              "programmer": plan["programmer"], "error": None, "hex": None}
        th = threading.Thread(target=self._build, args=(plan, bool(params.get("clean", True))), name="fw-build",
                              daemon=True)
        th.start()
        if wait:
            th.join()
        return self.build_status()

    def _emit_lines(self, lines: List[str], start: int):
        if lines:
            self._publish("build_log", {"start": start, "lines": lines})

    def _run(self, cmd: List[str], plan: Dict[str, Any]) -> int:
        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(cmd, cwd=plan["cwd"], env=plan["env"], stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1, **kw)
        pending: List[str] = []
        start = len(self.build_lines)
        last = time.time()
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            with self._lock:
                self.build_lines.append(line)
                if len(self.build_lines) > 20000:
                    del self.build_lines[:5000]
            pending.append(line)
            if time.time() - last > 0.15 or len(pending) > 200:
                self._emit_lines(pending, start)
                start += len(pending)
                pending, last = [], time.time()
        self._emit_lines(pending, start)
        return self._proc.wait()

    def _build(self, plan: Dict[str, Any], clean: bool):
        job = self.build_job
        t0 = time.time()
        try:
            self._publish("build", self.build_status())
            log.info("Building %s for %s with %s", plan["project"], plan["platform"], plan["compiler"])
            if clean:
                with self._lock:
                    self.build_lines.append("$ " + " ".join(plan["clean_cmd"]))
                self._run(plan["clean_cmd"], plan)
            with self._lock:
                self.build_lines.append("$ " + " ".join(plan["build_cmd"]))
            if job.get("state") == "cancelled":
                return
            rc = self._run(plan["build_cmd"], plan)
            if job.get("state") == "cancelled":
                return
            if rc != 0 or not os.path.isfile(plan["hex"]):
                errs = [ln for ln in self.build_lines if re.search(r"\berror\b|Error \d|\*\*\*", ln)]
                job["state"] = "failed"
                job["error"] = (errs[0] if errs else f"make exited with code {rc}")[:500]
                log.error("Build failed: %s", job["error"])
                return
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = f"{plan['project']}-{plan['platform']}-{plan['compiler']}"
            out_hex = os.path.join(self.builds_dir, f"{base}.hex")
            shutil.copy2(plan["hex"], out_hex)
            if os.path.isfile(plan["elf"]):
                shutil.copy2(plan["elf"], os.path.join(self.builds_dir, f"{base}.elf"))
            size = self._size(plan)
            job.update({"state": "ok", "hex": out_hex, "hex_bytes": os.path.getsize(out_hex), "size": size,
                        "built": stamp})
            log.info("Build OK: %s", out_hex)
        except Exception as e:  # noqa: BLE001
            job["state"], job["error"] = "failed", f"{type(e).__name__}: {e}"
            log.error("Build failed: %s", e)
        finally:
            job["seconds"] = round(time.time() - t0, 2)
            self._proc = None
            self._publish("build", self.build_status())

    def _size(self, plan: Dict[str, Any]) -> Optional[Dict[str, int]]:
        sz = plan["env"].get("PATH", "")
        tool = shutil.which(exe(plan_tool(plan, "size")), path=sz)
        if not tool or not os.path.isfile(plan["elf"]):
            return None
        try:
            r = subprocess.run([tool, plan["elf"]], capture_output=True, text=True, timeout=20)
            nums = r.stdout.strip().splitlines()[-1].split()
            return {"text": int(nums[0]), "data": int(nums[1]), "bss": int(nums[2])}
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def cancel_build(self) -> Dict[str, Any]:
        p = self._proc
        if self.build_job.get("state") == "running":
            self.build_job["state"] = "cancelled"
            if p is not None:
                try:
                    p.terminate()
                except OSError:
                    pass
            log.info("Build cancelled")
        return self.build_status()

    def list_builds(self) -> List[Dict[str, Any]]:
        out = []
        for fn in sorted(os.listdir(self.builds_dir)):
            if fn.endswith(".hex"):
                p = os.path.join(self.builds_dir, fn)
                out.append({"name": fn, "path": p, "bytes": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return sorted(out, key=lambda b: -b["mtime"])


def plan_tool(plan: Dict[str, Any], tool: str) -> str:
    for a in plan["build_cmd"]:
        if a.startswith(tool.upper() + "="):
            return a.split("=", 1)[1]
    return tool


__all__ = ["FirmwareManager", "parse_platforms", "gcc_include_dirs", "CRYPTO_TARGETS", "SS_VERSIONS"]
