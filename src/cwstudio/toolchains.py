"""On-demand compiler toolchains for building target firmware.

Studio does not ship compilers inside its bundle (they would add hundreds of MB per platform).  Instead ``resources/toolchains.json`` pins official builds (xPack GCC/clang, Arduino's avr-gcc) by version, URL and SHA-256 for every host platform.  :class:`ToolchainManager` downloads one on request, verifies the checksum, unpacks it under ``<data_dir>/toolchains/<id>/<version>/`` and from then on works offline.

Users can also register their own toolchains ("custom"): either an archive URL (+ optional SHA-256) that is installed the same way, or an existing directory on disk.  Compilers already on ``PATH`` are detected and used as a fallback.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from typing import Any, Callable, Dict, List, Optional

from cwstudio import net

log = logging.getLogger("cwstudio.toolchains")

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "toolchains.json")
ARCHES = ("arm", "avr", "riscv")
COMPILERS = ("gcc", "clang")
MARKER = ".cwstudio-toolchain.json"
USER_AGENT = "ChipWhisperer-Studio"


class Cancelled(Exception):
    pass


def host_key() -> str:
    """Registry key for this machine, e.g. ``linux-x64`` or ``darwin-arm64``."""
    osname = {"linux": "linux", "darwin": "darwin", "win32": "win32", "cygwin": "win32"}.get(sys.platform, sys.platform)
    m = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
    return f"{osname}-{arch}"


def exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def load_registry(path: str = REGISTRY_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# download + extract helpers
# ----------------------------------------------------------------------------
class TooSlow(OSError):
    pass


def download(url: str, dest: str, sha256: Optional[str] = None, progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[threading.Event] = None, chunk: int = 1 << 18, min_speed: float = 0, grace: float = 15.0) -> str:
    """Stream ``url`` to ``dest`` (via ``dest.part``), verifying ``sha256`` if given. With ``min_speed`` (bytes/s), raise :class:`TooSlow` if the average rate after ``grace`` seconds stays below it, so the caller can try a mirror."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.time()
    with net.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = r.read(chunk)
            if not buf:
                break
            f.write(buf)
            h.update(buf)
            done += len(buf)
            if progress:
                progress(done, total)
            elapsed = time.time() - t0
            if min_speed and elapsed > grace and done / elapsed < min_speed and (not total or done < total * 0.8):
                raise TooSlow(f"{url} is too slow ({done / elapsed / 1024:.0f} KB/s)")
    digest = h.hexdigest()
    if sha256 and digest.lower() != sha256.lower():
        os.remove(part)
        raise ValueError(f"checksum mismatch for {os.path.basename(dest)}: expected {sha256}, got {digest}")
    os.replace(part, dest)
    return digest


def _inside(root: str, path: str) -> bool:
    root = os.path.realpath(root)
    return os.path.realpath(path) == root or os.path.realpath(path).startswith(root + os.sep)


def extract(archive: str, dest: str, members: Optional[Callable[[str], Optional[str]]] = None,
            cancel: Optional[threading.Event] = None) -> None:
    """Unpack a .zip / .tar.* archive into ``dest``, refusing paths that escape it.

    ``members`` may map each archive path to a new relative path (or None to skip it); it is used to pull a sub-folder out of a large source archive.
    """
    os.makedirs(dest, exist_ok=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                name = members(info.filename) if members else info.filename
                if not name:
                    continue
                target = os.path.join(dest, name)
                if not _inside(dest, target):
                    raise ValueError(f"unsafe path in archive: {info.filename}")
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                mode = (info.external_attr >> 16) & 0o777
                if mode and os.name != "nt":
                    os.chmod(target, mode)
        return
    with tarfile.open(archive, "r:*") as t:
        for m in t:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            name = members(m.name) if members else m.name
            if not name:
                continue
            m.name = name
            target = os.path.join(dest, name)
            if not _inside(dest, target):
                raise ValueError(f"unsafe path in archive: {m.name}")
            if m.issym() or m.islnk():
                link = m.linkname if m.issym() else (members(m.linkname) if members else m.linkname)
                if not link:
                    continue
                if m.islnk():
                    m.linkname = link
                base = os.path.dirname(target) if m.issym() else dest
                if os.path.isabs(link) or not _inside(dest, os.path.join(base, link)):
                    raise ValueError(f"unsafe link in archive: {m.name} -> {m.linkname}")
            elif not (m.isfile() or m.isdir()):
                continue  # devices, fifos
            m.mode &= 0o755 if m.isdir() or m.mode & 0o111 else 0o644
            if sys.platform == "win32" and m.issym():
                continue  # symlinks need privileges on Windows; toolchains we pin use zips there
            if hasattr(tarfile, "data_filter"):
                t.extract(m, dest, filter="fully_trusted")  # already validated above
            else:
                t.extract(m, dest)


def _single_root(d: str) -> str:
    """If ``d`` contains exactly one directory (a typical archive top folder), return it."""
    entries = [e for e in os.listdir(d) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(d, entries[0])):
        return os.path.join(d, entries[0])
    return d


def make_aliases(bin_dir: str, aliases: Dict[str, List[str]]) -> int:
    """Create extra tool names (e.g. ``riscv32-unknown-elf-gcc``) next to the real ones.

    Hard links keep the aliases in the same directory, so GCC still finds its internal programs relative to itself.  Falls back to copying.
    """
    n = 0
    for src_prefix, alts in (aliases or {}).items():
        for fn in os.listdir(bin_dir):
            if not fn.startswith(src_prefix):
                continue
            src = os.path.join(bin_dir, fn)
            if not os.path.isfile(src):
                continue
            for alt in alts:
                dst = os.path.join(bin_dir, alt + fn[len(src_prefix):])
                if os.path.exists(dst):
                    continue
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
                n += 1
    return n


# ----------------------------------------------------------------------------
# manager
# ----------------------------------------------------------------------------
class ToolchainManager:
    def __init__(self, data_dir: str, publish: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 registry: Optional[Dict[str, Any]] = None, host: Optional[str] = None):
        self.root = os.path.join(data_dir, "toolchains")
        os.makedirs(self.root, exist_ok=True)
        self.registry_override = os.path.join(self.root, "registry.json")
        self.registry = registry or self._best_registry()
        self.host = host or host_key()
        self._publish = publish or (lambda kind, payload: None)
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}      # id -> {state, done, total, error, cancel}
        self.custom_path = os.path.join(self.root, "custom.json")

    # --- registry -----------------------------------------------------------
    def _best_registry(self) -> Dict[str, Any]:
        """The bundled registry, or a newer one fetched by :meth:`refresh_registry`."""
        bundled = load_registry()
        try:
            fetched = load_registry(self.registry_override)
            if fetched.get("schema") == bundled.get("schema") and fetched.get("revision", 0) > bundled.get("revision", 0):
                return fetched
        except (OSError, ValueError):
            pass
        return bundled

    def refresh_registry(self) -> Dict[str, Any]:
        """Fetch the latest toolchain list from the Studio repository so new compiler versions need no new Studio release."""
        url = self.registry.get("update_url")
        if not url:
            raise RuntimeError("this registry has no update_url")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with net.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("schema") != self.registry.get("schema") or not isinstance(data.get("toolchains"), list):
            raise ValueError("the published toolchain list uses a format this version of Studio does not understand")
        for e in data["toolchains"]:
            for d in (e.get("downloads") or {}).values():
                urls = [d.get("url", "")] + list(d.get("mirrors") or [])
                if not re.fullmatch(r"[0-9a-f]{64}", str(d.get("sha256") or "")) or not all(str(u).startswith("https://") for u in urls):
                    raise ValueError(f"toolchain {e.get('id')} has a download without an https URL and SHA-256")
        updated = data.get("revision", 0) > self.registry.get("revision", 0)
        if updated:
            with open(self.registry_override, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            self.registry = data
            log.info("Toolchain list updated to revision %s", data.get("revision"))
        return {"updated": updated, "revision": self.registry.get("revision")}

    def _custom(self) -> List[Dict[str, Any]]:
        try:
            with open(self.custom_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def _save_custom(self, items: List[Dict[str, Any]]):
        with open(self.custom_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)

    def entries(self) -> List[Dict[str, Any]]:
        out = [dict(e, custom=False) for e in self.registry["toolchains"]]
        out += [dict(e, custom=True) for e in self._custom()]
        return out

    def entry(self, tc_id: str) -> Dict[str, Any]:
        for e in self.entries():
            if e["id"] == tc_id:
                return e
        raise KeyError(f"unknown toolchain {tc_id!r}")

    def download_for_host(self, e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        dl = e.get("downloads") or {}
        if self.host in dl:
            return dl[self.host]
        fb = (e.get("host_fallbacks") or {}).get(self.host)
        if fb and fb in dl:
            return dl[fb]
        return dl.get("any")

    def install_dir(self, e: Dict[str, Any]) -> str:
        return os.path.join(self.root, e["id"], str(e.get("version") or "custom"))

    def _installed_bin(self, e: Dict[str, Any]) -> Optional[str]:
        if e.get("path"):  # custom, pre-existing directory
            p = e["path"]
            b = os.path.join(p, "bin")
            return b if os.path.isdir(b) and not self._probe(p, e) else p
        d = self.install_dir(e)
        marker = os.path.join(d, MARKER)
        if not os.path.isfile(marker):
            return None
        try:
            with open(marker, "r", encoding="utf-8") as f:
                info = json.load(f)
            return os.path.join(d, info.get("bin_rel", "bin"))
        except (OSError, ValueError):
            return None

    def _probe_name(self, e: Dict[str, Any]) -> Optional[str]:
        if e.get("probe"):
            return exe(e["probe"])
        comp = e.get("compiler")
        if comp == "gcc":
            return exe(e.get("prefix", "") + "gcc")
        if comp == "clang":
            return exe("clang")
        if comp == "tools":
            return exe("make")
        return None

    def _probe(self, d: str, e: Dict[str, Any]) -> bool:
        name = self._probe_name(e)
        return bool(name) and os.path.isfile(os.path.join(d, name))

    def _system_bin(self, e: Dict[str, Any]) -> Optional[str]:
        name = self._probe_name(e)
        if not name or e.get("custom"):
            return None
        p = shutil.which(name)
        return os.path.dirname(p) if p else None

    def status(self, e: Dict[str, Any]) -> Dict[str, Any]:
        dl = self.download_for_host(e)
        bin_dir = self._installed_bin(e)
        with self._lock:
            job = {k: v for k, v in self._jobs.get(e["id"], {}).items() if k != "cancel"}
        st = {k: e.get(k) for k in ("id", "name", "compiler", "arch", "version", "source", "homepage", "prefix", "custom")}
        st.update({
            "available": dl is not None or bool(e.get("path")),
            "size": (dl or {}).get("size"),
            "installed": bool(bin_dir and self._probe(bin_dir, e)),
            "bin": bin_dir,
            "system": self._system_bin(e),
            "job": job or None,
        })
        if e.get("id") == "win-build-tools" and not self.host.startswith("win32"):
            st["available"] = False
            st["note"] = "Only needed on Windows; uses the system make here."
        return st

    def list(self) -> Dict[str, Any]:
        return {"host": self.host, "root": self.root, "revision": self.registry.get("revision"),
                "toolchains": [self.status(e) for e in self.entries()]}

    # --- install / remove -----------------------------------------------------
    def install(self, tc_id: str, wait: bool = False, force: bool = False) -> Dict[str, Any]:
        """Download and unpack a toolchain in the background; a no-op if it is already installed unless ``force``."""
        e = self.entry(tc_id)
        if not force and self.status(e)["installed"]:
            return self.status(e)
        dl = self.download_for_host(e)
        if dl is None:
            raise ValueError(f"{e['name']} has no download for {self.host}")
        with self._lock:
            j = self._jobs.get(tc_id)
            if j and j.get("state") in ("downloading", "extracting"):
                return self.status(e)
            job = {"state": "downloading", "done": 0, "total": dl.get("size") or 0, "error": None,
                   "cancel": threading.Event()}
            self._jobs[tc_id] = job
        th = threading.Thread(target=self._install, args=(e, dl, job), name=f"install-{tc_id}", daemon=True)
        th.start()
        if wait:
            th.join()
        return self.status(e)

    def _emit(self, e: Dict[str, Any]):
        self._publish("toolchain", self.status(e))

    def _install(self, e: Dict[str, Any], dl: Dict[str, Any], job: Dict[str, Any]):
        url = dl["url"]
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", url.rstrip("/").split("/")[-1].split("?")[0]) or "archive"
        if not re.search(r"\.(zip|tar\.gz|tgz|tar\.bz2|tar\.xz|txz)$", fname):
            fname += ".tar.gz" if "codeload" in url or "tar.gz" in url else ""
        dl_dir = os.path.join(self.root, ".downloads")
        archive = os.path.join(dl_dir, fname)
        final = self.install_dir(e)
        tmp = final + ".tmp"
        last = [0.0]

        def progress(done, total):
            job["done"], job["total"] = done, total or job["total"]
            now = time.time()
            if now - last[0] > 0.25:
                last[0] = now
                self._emit(e)

        try:
            self._emit(e)
            urls = [url] + list(dl.get("mirrors") or [])
            for i, u in enumerate(urls):
                last_try = i == len(urls) - 1
                log.info("Downloading %s %s from %s", e["name"], e.get("version", ""), u)
                try:
                    download(u, archive, dl.get("sha256"), progress, job["cancel"], min_speed=0 if last_try else 256 * 1024)
                    url = u
                    break
                except Cancelled:
                    raise
                except (OSError, ValueError) as ex:
                    if last_try:
                        raise
                    log.warning("%s; trying the next mirror", ex)
                    job["done"] = 0
            job["state"] = "extracting"
            self._emit(e)
            log.info("Unpacking %s", os.path.basename(archive))
            shutil.rmtree(tmp, ignore_errors=True)
            extract(archive, tmp, cancel=job["cancel"])
            content = _single_root(tmp)
            bin_rel = e.get("bin", "bin")
            if not self._probe(os.path.join(content, bin_rel), e):
                found = self._find_bin(content, e)
                if found is None:
                    raise FileNotFoundError(f"{self._probe_name(e)} not found in the archive")
                bin_rel = os.path.relpath(found, content)
            n = make_aliases(os.path.join(content, bin_rel), e.get("aliases") or {})
            if n:
                log.info("Created %d tool name aliases", n)
            shutil.rmtree(final, ignore_errors=True)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(content, final)
            shutil.rmtree(tmp, ignore_errors=True)
            with open(os.path.join(final, MARKER), "w", encoding="utf-8") as f:
                json.dump({"id": e["id"], "version": e.get("version"), "url": url, "sha256": dl.get("sha256"),
                           "bin_rel": bin_rel, "installed": time.time()}, f, indent=1)
            try:
                os.remove(archive)
            except OSError:
                pass
            job["state"] = "installed"
            log.info("%s installed in %s", e["name"], final)
        except Cancelled:
            job["state"], job["error"] = "cancelled", None
            log.info("Install of %s cancelled", e["name"])
            shutil.rmtree(tmp, ignore_errors=True)
            for p in (archive, archive + ".part"):
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception as ex:  # noqa: BLE001
            job["state"], job["error"] = "error", f"{type(ex).__name__}: {ex}"
            log.error("Installing %s failed: %s", e["name"], ex)
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            self._emit(e)

    def _find_bin(self, root: str, e: Dict[str, Any]) -> Optional[str]:
        name = self._probe_name(e)
        for dp, _dn, fns in os.walk(root):
            if name in fns:
                return dp
        return None

    def cancel(self, tc_id: str) -> Dict[str, Any]:
        with self._lock:
            j = self._jobs.get(tc_id)
        if j:
            j["cancel"].set()
        return self.status(self.entry(tc_id))

    def remove(self, tc_id: str) -> Dict[str, Any]:
        e = self.entry(tc_id)
        if e.get("path"):
            raise ValueError("this toolchain points at an existing folder; remove the custom entry instead")
        shutil.rmtree(self.install_dir(e), ignore_errors=True)
        with self._lock:
            self._jobs.pop(tc_id, None)
        log.info("Removed %s", e["name"])
        self._emit(e)
        return self.status(e)

    # --- custom toolchains ------------------------------------------------------
    def add_custom(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        name = (spec.get("name") or "").strip()
        compiler = spec.get("compiler", "gcc")
        if compiler not in COMPILERS:
            raise ValueError(f"compiler must be one of {COMPILERS}")
        arch = spec.get("arch") or []
        if isinstance(arch, str):
            arch = [a.strip() for a in arch.split(",") if a.strip()]
        prefix = (spec.get("prefix") or "").strip()
        if compiler == "gcc" and not prefix:
            raise ValueError("a GCC toolchain needs its tool prefix, e.g. arm-none-eabi-")
        url, path = (spec.get("url") or "").strip(), (spec.get("path") or "").strip()
        if bool(url) == bool(path):
            raise ValueError("give either an archive URL or a local folder")
        base = re.sub(r"[^a-z0-9]+", "-", (name or prefix or compiler).lower()).strip("-") or "toolchain"
        tc_id = "custom-" + base
        items = [c for c in self._custom() if c["id"] != tc_id]
        e: Dict[str, Any] = {"id": tc_id, "name": name or f"{prefix}{compiler}", "compiler": compiler, "arch": arch,
                             "prefix": prefix, "version": (spec.get("version") or "custom").strip(), "source": "custom",
                             "bin": spec.get("bin") or "bin"}
        if url:
            e["downloads"] = {"any": {"url": url, "sha256": (spec.get("sha256") or "").strip() or None}}
            e["homepage"] = url
        else:
            path = os.path.abspath(os.path.expanduser(path))
            e["path"] = path
            probe_dirs = [path, os.path.join(path, "bin")]
            if not any(self._probe(d, e) for d in probe_dirs):
                raise FileNotFoundError(f"{self._probe_name(e)} not found in {path} or {path}/bin")
        items.append(e)
        self._save_custom(items)
        log.info("Added custom toolchain %s", e["name"])
        return self.status(dict(e, custom=True))

    def remove_custom(self, tc_id: str) -> None:
        items = self._custom()
        e = next((c for c in items if c["id"] == tc_id), None)
        if e is None:
            raise KeyError(f"no custom toolchain {tc_id!r}")
        if not e.get("path"):
            shutil.rmtree(self.install_dir(e), ignore_errors=True)
        self._save_custom([c for c in items if c["id"] != tc_id])

    # --- resolution for builds --------------------------------------------------
    def find(self, arch: str, compiler: str) -> Optional[Dict[str, Any]]:
        """Best usable toolchain for ``arch``/``compiler``: installed > custom > system PATH."""
        best = None
        for e in self.entries():
            if e.get("compiler") != compiler or (arch and arch not in (e.get("arch") or [])):
                continue
            st = self.status(e)
            if st["installed"]:
                rank = 0 if not e.get("custom") else 1
                cand = dict(st, use_bin=st["bin"], rank=rank, entry=e)
            elif st["system"]:
                cand = dict(st, use_bin=st["system"], rank=2, entry=e)
            else:
                continue
            if best is None or cand["rank"] < best["rank"]:
                best = cand
        return best

    def make_bin(self) -> Optional[str]:
        """Directory holding ``make`` (bundled build tools on Windows, else system)."""
        try:
            e = self.entry("win-build-tools")
            b = self._installed_bin(e)
            if b and self._probe(b, e):
                return b
        except KeyError:
            pass
        m = shutil.which(exe("make")) or shutil.which("gmake")
        return os.path.dirname(m) if m else None
