"""Jupyter-style notebooks inside Studio.

Cells run in one persistent Python namespace on Studio's hardware thread, so notebook code shares the scope and target the UI is connected to instead of fighting over the USB device. Inside a notebook ``import chipwhisperer as cw`` returns a thin proxy of the real library: ``cw.scope()`` and ``cw.target()`` hand back Studio's connection (connecting first if needed), and ``cw.capture_trace()`` also stores each trace in Studio's trace store, so captures show up live in the waveform view and the Capture tab.

IPython conveniences used by ChipWhisperer's tutorials work too: ``%run other.ipynb``, ``!make ...`` with Studio's downloaded compilers on ``PATH`` and ``{var}`` expansion, ``%%bash``, ``%cd``, ``%env``, ``%time``, ``%%writefile`` and friends, ``display()``, ``IPython.display``, ``tqdm.notebook`` and inline matplotlib figures. Notebooks are stored as standard ``.ipynb`` files.
"""
from __future__ import annotations

import ast
import base64
import builtins
import ctypes
import io
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import types
import uuid
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from cwstudio.events import trace_event

log = logging.getLogger("cwstudio.notebook")
OutputFn = Callable[[Dict[str, Any]], None]


# ----------------------------------------------------------------------------
# .ipynb files
# ----------------------------------------------------------------------------
def _src(s) -> str:
    return "".join(s) if isinstance(s, list) else (s or "")


def _lines(s: str) -> List[str]:
    parts = s.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def normalize(nb: Dict[str, Any]) -> Dict[str, Any]:
    """Return a notebook with string sources and a stable id on every cell."""
    cells = []
    for c in nb.get("cells", []):
        cell = {"id": c.get("id") or uuid.uuid4().hex[:8], "cell_type": c.get("cell_type", "code"), "source": _src(c.get("source")), "metadata": c.get("metadata") or {}}
        if cell["cell_type"] == "code":
            outs = []
            for o in c.get("outputs") or []:
                o = dict(o)
                if "text" in o:
                    o["text"] = _src(o["text"])
                if isinstance(o.get("data"), dict):
                    o["data"] = {k: _src(v) if isinstance(v, list) else v for k, v in o["data"].items()}
                outs.append(o)
            cell["outputs"] = outs
            cell["execution_count"] = c.get("execution_count")
        cells.append(cell)
    return {"cells": cells, "metadata": nb.get("metadata") or {}, "nbformat": 4, "nbformat_minor": max(5, int(nb.get("nbformat_minor") or 5))}


def to_ipynb(nb: Dict[str, Any]) -> Dict[str, Any]:
    out = normalize(nb)
    for c in out["cells"]:
        c["source"] = _lines(c["source"])
        for o in c.get("outputs", []):
            if "text" in o:
                o["text"] = _lines(o["text"])
    md = dict(out["metadata"])
    md.setdefault("kernelspec", {"name": "python3", "display_name": "Python 3 (ChipWhisperer Studio)", "language": "python"})
    md.setdefault("language_info", {"name": "python"})
    out["metadata"] = md
    return out


def new_notebook() -> Dict[str, Any]:
    return normalize({"cells": [
        {"cell_type": "markdown", "source": "# New notebook\n\nCells run inside ChipWhisperer Studio: `cw.scope()` and `cw.target()` use the devices connected in Studio, and traces captured with `cw.capture_trace()` appear in the Capture tab."},
        {"cell_type": "code", "source": "import chipwhisperer as cw\nimport numpy as np\n\nscope = cw.scope()\ntarget = cw.target(scope)\nscope"},
        {"cell_type": "code", "source": "key = bytearray(range(16))\nfor i in range(50):\n    trace = cw.capture_trace(scope, target, bytearray(np.random.bytes(16)), key)\nprint('captured', len(studio.traces), 'traces in Studio')"},
    ]})


class NotebookStore:
    """``.ipynb`` files under ``<data_dir>/notebooks`` (sub-folders allowed)."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def path(self, rel: str) -> str:
        p = os.path.abspath(os.path.join(self.root, rel or ""))
        if p != os.path.abspath(self.root) and not p.startswith(os.path.abspath(self.root) + os.sep):
            raise ValueError("path outside the notebooks folder")
        return p

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for dp, dn, fns in os.walk(self.root):
            dn[:] = sorted(d for d in dn if not d.startswith(".") and d != "__pycache__")
            for fn in sorted(fns):
                if fn.endswith(".ipynb") and not fn.startswith("."):
                    p = os.path.join(dp, fn)
                    out.append({"path": os.path.relpath(p, self.root).replace(os.sep, "/"), "bytes": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return out

    def load(self, rel: str) -> Dict[str, Any]:
        with open(self.path(rel), "r", encoding="utf-8") as f:
            return normalize(json.load(f))

    def save(self, rel: str, nb: Dict[str, Any]) -> Dict[str, Any]:
        if not rel.endswith(".ipynb"):
            rel += ".ipynb"
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(to_ipynb(nb), f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)
        return {"path": os.path.relpath(p, self.root).replace(os.sep, "/"), "mtime": os.path.getmtime(p)}

    def create(self, name: str) -> Dict[str, Any]:
        name = (name or "Untitled").strip().replace("\\", "/")
        if not name.endswith(".ipynb"):
            name += ".ipynb"
        base, n = name[:-6], 1
        while os.path.exists(self.path(name)):
            n += 1
            name = f"{base} {n}.ipynb"
        return self.save(name, new_notebook())

    def import_bytes(self, filename: str, content: bytes) -> Dict[str, Any]:
        nb = normalize(json.loads(content.decode("utf-8")))
        name = os.path.basename(filename or "imported.ipynb")
        rel, n = "imported/" + name, 1
        while os.path.exists(self.path(rel)):
            n += 1
            rel = f"imported/{name[:-6]} {n}.ipynb"
        return self.save(rel, nb)

    def delete(self, rel: str) -> None:
        os.remove(self.path(rel))


# ----------------------------------------------------------------------------
# output capture
# ----------------------------------------------------------------------------
class _Router(io.TextIOBase):
    """sys.stdout/stderr replacement: writes from the thread running a cell go to that cell, everything else to the real stream."""

    def __init__(self, name: str, real):
        self.name, self.real = name, real
        self.sinks: Dict[int, Callable[[str, str], None]] = {}

    def write(self, s):
        sink = self.sinks.get(threading.get_ident())
        if sink is not None:
            sink(self.name, s)
            return len(s)
        return self.real.write(s) if self.real else len(s)

    def flush(self):
        if self.real and threading.get_ident() not in self.sinks:
            self.real.flush()

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self.real, "encoding", None) or "utf-8"

    @property
    def buffer(self):  # binary writers (e.g. the MCP stdio transport) bypass cell capture
        return self.real.buffer

    def fileno(self):
        return self.real.fileno()

    def __getattr__(self, name):
        return getattr(self.__dict__["real"], name)


_ROUTERS: Dict[str, _Router] = {}


def _install_routers():
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name)
        if not isinstance(cur, _Router):
            r = _Router(name, cur)
            setattr(sys, name, r)
            _ROUTERS[name] = r


def rich_bundle(obj: Any) -> Dict[str, Any]:
    """Jupyter-style mime bundle for an object (text/plain always, plus HTML, PNG, SVG, markdown when the object provides them)."""
    data: Dict[str, Any] = {}
    if hasattr(obj, "_repr_mimebundle_"):
        try:
            b = obj._repr_mimebundle_()
            if isinstance(b, tuple):
                b = b[0]
            data.update({k: v for k, v in (b or {}).items() if isinstance(v, (str, bytes))})
        except Exception:  # noqa: BLE001
            pass
    for meth, mime in (("_repr_html_", "text/html"), ("_repr_svg_", "image/svg+xml"), ("_repr_markdown_", "text/markdown"), ("_repr_png_", "image/png"), ("_repr_jpeg_", "image/jpeg")):
        if mime not in data and hasattr(obj, meth):
            try:
                v = getattr(obj, meth)()
                if v is not None:
                    data[mime] = v
            except Exception:  # noqa: BLE001
                pass
    for mime in ("image/png", "image/jpeg"):
        if isinstance(data.get(mime), bytes):
            data[mime] = base64.b64encode(data[mime]).decode()
    if isinstance(obj, np.ndarray):
        with np.printoptions(threshold=200, edgeitems=5):
            data["text/plain"] = repr(obj)
    else:
        try:
            data["text/plain"] = repr(obj)
        except Exception as e:  # noqa: BLE001
            data["text/plain"] = f"<unrepresentable {type(obj).__name__}: {e}>"
    return data


# ----------------------------------------------------------------------------
# IPython syntax
# ----------------------------------------------------------------------------
_MAGIC_LINE = re.compile(r"^(\s*)(?:(\w+)\s*=\s*)?([%!])(.*)$")


def transform(code: str) -> str:
    """Turn IPython line magics and shell escapes into calls to the kernel's helpers, keeping indentation so they work inside blocks."""
    out = []
    lines = code.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _MAGIC_LINE.match(line)
        if m and not line.strip().startswith(("%%",)):
            indent, var, kind, rest = m.groups()
            while rest.endswith("\\") and i + 1 < len(lines):  # continued magic/shell line
                i += 1
                rest = rest[:-1] + " " + lines[i].strip()
            if kind == "!":
                call = f"__studio_shell__({rest!r}, capture={bool(var)})"
            else:
                name, _, arg = rest.partition(" ")
                call = f"__studio_magic__({name!r}, {arg!r})"
            out.append(f"{indent}{var} = {call}" if var else f"{indent}{call}")
        elif line.rstrip().endswith("?") and not line.lstrip().startswith("#") and re.match(r"^\s*[\w.]+\?{1,2}\s*$", line):
            out.append(re.sub(r"^(\s*)([\w.]+)\?{1,2}\s*$", r"\1help(\2)", line))
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


def expand(cmd: str, ns: Dict[str, Any]) -> str:
    """IPython-style ``{expr}`` and ``$name`` expansion in shell commands (``{{`` and ``$$`` escape)."""
    def brace(m):
        expr = m.group(1)
        try:
            return str(eval(expr, ns))  # noqa: S307 - user's own notebook code
        except Exception:  # noqa: BLE001
            return m.group(0)
    cmd = re.sub(r"(?<!\{)\{([^{}]+)\}(?!\})", brace, cmd)
    cmd = re.sub(r"(?<!\$)\$([A-Za-z_]\w*)", lambda m: str(ns[m.group(1)]) if m.group(1) in ns else m.group(0), cmd)
    return cmd.replace("{{", "{").replace("}}", "}").replace("$$", "$")


# ----------------------------------------------------------------------------
# kernel
# ----------------------------------------------------------------------------
class _Exec:
    def __init__(self, cell_id: str, code: str, cwd: str, batch: str):
        self.id = uuid.uuid4().hex[:10]
        self.cell_id, self.code, self.cwd, self.batch = cell_id, code, cwd, batch
        self.outputs: List[Dict[str, Any]] = []
        self.count: Optional[int] = None
        self.ok = True
        self._buf: Dict[str, str] = {}
        self._last_flush = 0.0


class Kernel:
    def __init__(self, session, notebooks_root: str):
        self.session = session
        self.bus = session.bus
        self.root = notebooks_root
        self.q: "queue.Queue[_Exec]" = queue.Queue()
        self.lock = threading.Lock()
        self.current: Optional[_Exec] = None
        self.pending: List[_Exec] = []
        self.count = 0
        self.store_traces = True
        self.pending_textout: Optional[int] = None
        self.scope_proxy = StudioScope(self, "scope")
        self.target_proxy = StudioTarget(self, "target")
        self._last_trace_pub = 0.0
        self._last_trace: Optional[tuple] = None
        self.ns: Dict[str, Any] = {}
        _install_routers()
        os.environ.setdefault("MPLBACKEND", "Agg")
        self.restart(publish=False)
        self._thread = threading.Thread(target=self._dispatch, name="notebook-kernel", daemon=True)
        self._thread.start()

    # --- public API ----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        with self.lock:
            cur = self.current
            return {"busy": cur is not None, "cell": cur.cell_id if cur else None, "queued": [e.cell_id for e in self.pending], "execution_count": self.count, "store_traces": self.store_traces}

    def execute(self, cells: List[Dict[str, Any]], path: Optional[str] = None) -> Dict[str, Any]:
        cwd = os.path.dirname(os.path.join(self.root, path)) if path else self.root
        batch = uuid.uuid4().hex[:8]
        ids = []
        for c in cells:
            e = _Exec(c.get("id") or uuid.uuid4().hex[:8], c.get("code") or c.get("source") or "", cwd, batch)
            with self.lock:
                self.pending.append(e)
            self.q.put(e)
            ids.append({"cell": e.cell_id, "exec": e.id})
            self._event("queued", e)
        return {"queued": ids, "batch": batch}

    def execute_wait(self, code: str, path: Optional[str] = None, timeout: float = 600) -> Dict[str, Any]:
        """Run one cell and wait for it (used by the MCP server and tests)."""
        done = threading.Event()
        res: Dict[str, Any] = {}
        cid = "mcp-" + uuid.uuid4().hex[:6]

        def watch(ev):
            if ev.get("cell") == cid and ev.get("kind") == "done":
                res.update(ev)
                done.set()
        self._watchers.append(watch)
        try:
            self.execute([{"id": cid, "code": code}], path)
            if not done.wait(timeout):
                self.interrupt()
                raise TimeoutError("cell did not finish in time")
        finally:
            self._watchers.remove(watch)
        return {"ok": res.get("ok"), "execution_count": res.get("execution_count"), "outputs": res.get("outputs", [])}

    def interrupt(self) -> Dict[str, Any]:
        with self.lock:
            dropped = list(self.pending)
            self.pending.clear()
            cur = self.current
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        for e in dropped:
            self._event("cancelled", e)
        if cur is not None:
            tid = self.session.worker._thread.ident
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt))
            log.info("Interrupting notebook cell")
        return self.status()

    def restart(self, publish: bool = True) -> Dict[str, Any]:
        if self.current is not None:
            self.interrupt()
            for _ in range(50):
                if self.current is None:
                    break
                time.sleep(0.05)
        self._watchers: List[Callable[[Dict[str, Any]], None]] = getattr(self, "_watchers", [])
        self.count = 0
        self.ns = self._fresh_namespace()
        if publish:
            log.info("Notebook kernel restarted")
            self.bus.publish("nb", {"kind": "restarted", **self.status()})
        return self.status()

    def variables(self) -> List[Dict[str, Any]]:
        out = []
        for k, v in self.ns.items():
            if k.startswith("_") or isinstance(v, (types.ModuleType, type, types.FunctionType, types.BuiltinFunctionType)) or k in ("studio", "display", "get_ipython", "In", "Out", "exit", "quit"):
                continue
            shape = getattr(v, "shape", None)
            try:
                r = repr(v)
            except Exception:  # noqa: BLE001
                r = "?"
            try:
                tname = v.__class__.__name__  # Studio's scope/target stand-ins report the real class
            except Exception:  # noqa: BLE001
                tname = type(v).__name__
            out.append({"name": k, "type": tname, "shape": list(shape) if isinstance(shape, tuple) else None, "repr": r[:120]})
        return out

    # --- namespace -------------------------------------------------------------
    def _fresh_namespace(self) -> Dict[str, Any]:
        kernel = self
        real_import = builtins.__import__

        def studio_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0:
                if name == "chipwhisperer" and not fromlist:
                    real_import(name, globals, locals, fromlist, level)
                    return kernel._cw_proxy()
                if name == "chipwhisperer" and fromlist:
                    mod = real_import(name, globals, locals, fromlist, level)
                    return _ProxyWithFrom(kernel._cw_proxy(), mod)
                if name in ("IPython.display", "IPython.core.display") or (name == "IPython" and fromlist and "display" in fromlist):
                    return kernel._ipython_display_module(as_package=name == "IPython")
                if name == "IPython":
                    return kernel._ipython_package()
                if name in ("tqdm.notebook", "tqdm.auto", "tqdm.autonotebook"):
                    return kernel._tqdm_module()
                if name == "tqdm" and fromlist and any(x in ("notebook", "auto", "autonotebook") for x in fromlist):
                    pkg = types.ModuleType("tqdm")
                    real = real_import("tqdm")
                    pkg.__dict__.update(real.__dict__)
                    for x in ("notebook", "auto", "autonotebook"):
                        setattr(pkg, x, kernel._tqdm_module())
                    return pkg
            return real_import(name, globals, locals, fromlist, level)

        bi = dict(builtins.__dict__)
        bi["__import__"] = studio_import
        bi["input"] = lambda prompt="": (_ for _ in ()).throw(RuntimeError("input() is not supported in Studio notebooks; set the value in code instead"))
        ns: Dict[str, Any] = {"__name__": "__main__", "__builtins__": bi, "__studio_shell__": self._shell, "__studio_magic__": self._magic, "display": self._display, "get_ipython": lambda: _IPythonStub(self), "studio": StudioHelper(self), "In": [], "Out": {}}
        return ns

    def _cw_proxy(self):
        if getattr(self, "_proxy", None) is None:
            import chipwhisperer as cw
            self._proxy = _CWProxy(cw, self)
        return self._proxy

    def _ipython_display_module(self, as_package: bool = False):
        m = types.ModuleType("IPython.display")
        k = self

        class _Rich:
            def __init__(self, data=None, url=None, filename=None, **kw):
                self.data, self.url, self.filename = data, url, filename

            def __repr__(self):
                return f"<IPython.core.display.{type(self).__name__} object>"

            def _text(self):
                if self.filename:
                    with open(self.filename, "rb") as f:
                        return f.read()
                return self.data

        class HTML(_Rich):
            def _repr_html_(self):
                d = self._text()
                return d.decode() if isinstance(d, bytes) else d

        class Markdown(_Rich):
            def _repr_markdown_(self):
                d = self._text()
                return d.decode() if isinstance(d, bytes) else d

        class Image(_Rich):
            def _repr_png_(self):
                return self._text()

        class SVG(_Rich):
            def _repr_svg_(self):
                d = self._text()
                return d.decode() if isinstance(d, bytes) else d

        m.display = k._display
        m.HTML, m.Markdown, m.Image, m.SVG = HTML, Markdown, Image, SVG
        m.clear_output = lambda wait=False: k._emit({"output_type": "clear_output"})
        m.Javascript = lambda *a, **kw: None
        if as_package:
            p = types.ModuleType("IPython")
            p.display = m
            p.get_ipython = lambda: _IPythonStub(k)
            return p
        return m

    def _ipython_package(self):
        return self._ipython_display_module(as_package=True)

    def _tqdm_module(self):
        import tqdm as _tq
        m = types.ModuleType("tqdm.notebook")
        m.tqdm, m.trange, m.tnrange = _tq.tqdm, _tq.trange, _tq.trange
        m.tqdm_notebook = _tq.tqdm
        return m

    # --- execution ----------------------------------------------------------------
    def _dispatch(self):
        while True:
            e = self.q.get()
            with self.lock:
                if e not in self.pending:
                    continue  # cancelled
                self.pending.remove(e)
                self.current = e
            self._event("running", e)
            try:
                self.session.worker.call(self._run, e, timeout=None)
            except BaseException as ex:  # noqa: BLE001
                e.ok = False
                self._emit_to(e, {"output_type": "error", "ename": type(ex).__name__, "evalue": str(ex), "traceback": [f"{type(ex).__name__}: {ex}"]})
            finally:
                with self.lock:
                    self.current = None
                if not e.ok:  # like Jupyter's run all: stop the rest of the batch after an error
                    with self.lock:
                        dropped = [p for p in self.pending if p.batch == e.batch]
                        self.pending = [p for p in self.pending if p.batch != e.batch]
                    for p in dropped:
                        self._event("cancelled", p)
                self._event("done", e)
                self._finish_traces()

    def _event(self, kind: str, e: _Exec):
        ev = {"kind": kind, "cell": e.cell_id, "exec": e.id}
        if kind == "done":
            ev.update({"ok": e.ok, "execution_count": e.count, "outputs": e.outputs})
        self.bus.publish("nb", ev)
        for w in list(self._watchers):
            try:
                w(ev)
            except Exception:  # noqa: BLE001
                pass

    def _emit_to(self, e: _Exec, out: Dict[str, Any]):
        if out.get("output_type") == "clear_output":
            e.outputs.clear()
        elif out.get("output_type") == "stream" and e.outputs and e.outputs[-1].get("output_type") == "stream" and e.outputs[-1].get("name") == out["name"]:
            e.outputs[-1]["text"] += out["text"]
        else:
            e.outputs.append(dict(out))
        self.bus.publish("nb", {"kind": "output", "cell": e.cell_id, "exec": e.id, "output": out})

    def _emit(self, out: Dict[str, Any]):
        e = self.current
        if e is not None:
            self._flush_streams(e, force=True)
            self._emit_to(e, out)

    def _stream(self, e: _Exec, name: str, text: str):
        other = "stderr" if name == "stdout" else "stdout"
        if e._buf.get(other):  # keep stdout/stderr interleaving in order
            self._emit_to(e, {"output_type": "stream", "name": other, "text": e._buf.pop(other)})
        e._buf[name] = e._buf.get(name, "") + text
        self._flush_streams(e)

    def _flush_streams(self, e: _Exec, force: bool = False):
        now = time.time()
        if not force and now - e._last_flush < 0.1:
            return
        e._last_flush = now
        for name in ("stdout", "stderr"):
            t = e._buf.pop(name, "")
            if t:
                self._emit_to(e, {"output_type": "stream", "name": name, "text": t})

    def _display(self, *objs, **kw):
        for o in objs:
            self._emit({"output_type": "display_data", "data": rich_bundle(o), "metadata": {}})

    def _run(self, e: _Exec):
        """Runs on the hardware thread."""
        tid = threading.get_ident()
        sink = lambda name, s: self._stream(e, name, s)  # noqa: E731
        _install_routers()  # something may have replaced sys.stdout/stderr since the last cell
        for r in _ROUTERS.values():
            r.sinks[tid] = sink
        old_cwd = os.getcwd()
        self.count += 1
        e.count = self.count
        self.ns["In"].append(e.code)
        try:
            os.makedirs(e.cwd, exist_ok=True)
            os.chdir(e.cwd)
            self._run_source(e.code, e)
        except KeyboardInterrupt:
            e.ok = False
            self._flush_streams(e, force=True)
            self._emit_to(e, {"output_type": "error", "ename": "KeyboardInterrupt", "evalue": "interrupted", "traceback": ["KeyboardInterrupt: interrupted"]})
        except BaseException as ex:  # noqa: BLE001
            e.ok = False
            self._flush_streams(e, force=True)
            tb = traceback.format_exception(type(ex), ex, ex.__traceback__)
            tb = [t for t in tb if "cwstudio/notebook.py" not in t.replace("\\", "/") and "cwstudio/worker.py" not in t.replace("\\", "/")]
            self._emit_to(e, {"output_type": "error", "ename": type(ex).__name__, "evalue": str(ex), "traceback": tb})
        finally:
            try:
                self._flush_figures()
            except Exception:  # noqa: BLE001
                pass
            self._flush_streams(e, force=True)
            for r in _ROUTERS.values():
                r.sinks.pop(tid, None)
            try:
                os.chdir(old_cwd)
            except OSError:
                pass

    def _run_source(self, code: str, e: Optional[_Exec] = None, allow_result: bool = True):
        stripped = code.lstrip()
        if stripped.startswith("%%"):
            first, _, body = stripped.partition("\n")
            name, _, arg = first[2:].partition(" ")
            return self._cell_magic(name.strip(), arg.strip(), body, e)
        tree = ast.parse(transform(code), filename="<cell>", mode="exec")
        last = None
        if allow_result and tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        if tree.body:
            exec(compile(tree, "<cell>", "exec"), self.ns)  # noqa: S102 - running the user's notebook is the point
        if last is not None:
            val = eval(compile(last, "<cell>", "eval"), self.ns)  # noqa: S307
            if val is not None and e is not None:
                self.ns["_"] = val
                self.ns["Out"][e.count] = val
                if _is_figure(val):
                    return
                self._flush_streams(e, force=True)
                self._emit_to(e, {"output_type": "execute_result", "execution_count": e.count, "data": rich_bundle(val), "metadata": {}})

    def _flush_figures(self):
        plt = sys.modules.get("matplotlib.pyplot")
        if plt is None:
            return
        for num in plt.get_fignums():
            fig = plt.figure(num)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
            plt.close(fig)
            self._emit({"output_type": "display_data", "data": {"image/png": base64.b64encode(buf.getvalue()).decode(), "text/plain": f"<Figure {num}>"}, "metadata": {}})

    # --- shell and magics ------------------------------------------------------------
    def _shell_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        extra = []
        try:
            for t in self.session.toolchains.list()["toolchains"]:
                if t.get("installed") and t.get("bin"):
                    extra.append(t["bin"])
            mk = self.session.toolchains.make_bin()
            if mk:
                extra.append(mk)
        except Exception:  # noqa: BLE001
            pass
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
        from cwstudio.firmware import COMPAT_CFLAGS
        env["CFLAGS"] = " ".join(COMPAT_CFLAGS + ([env["CFLAGS"]] if env.get("CFLAGS") else []))  # ChipWhisperer HALs with current GCC
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _shell(self, cmd: str, capture: bool = False, shell_exe: Optional[str] = None):
        cmd = expand(cmd, self.ns)
        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(cmd, shell=True, cwd=os.getcwd(), env=self._shell_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", executable=shell_exe, **kw)
        lines = []
        try:
            assert p.stdout is not None
            for line in p.stdout:
                if capture:
                    lines.append(line.rstrip("\n"))
                else:
                    sys.stdout.write(line)
            p.wait()
        except KeyboardInterrupt:
            p.kill()
            raise
        if capture:
            return lines
        return None

    def _script(self, name: str, arg: str, body: str):
        """``%%bash [-s arg ...]``: run the cell body with bash (or sh), passing ``-s`` arguments as $1, $2, ... after {var}/$var expansion."""
        args = shlex.split(expand(arg, self.ns)) if arg else []
        interp = None
        if name == "script" and args:
            interp = args.pop(0)
        pos = []
        if "-s" in args:
            pos = args[args.index("-s") + 1:]
        exe = shutil.which(interp or name) or shutil.which("bash") or shutil.which("sh")
        if exe is None:
            raise FileNotFoundError(f"%%{name}: no {interp or name} or sh found; on Windows install 'GNU make + sh' in the Firmware tab")
        kw: Dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen([exe, "-s", *pos], cwd=os.getcwd(), env=self._shell_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", **kw)
        assert p.stdin is not None and p.stdout is not None
        p.stdin.write(body.replace("\r\n", "\n"))
        p.stdin.close()
        try:
            for line in p.stdout:
                sys.stdout.write(line)
            p.wait()
        except KeyboardInterrupt:
            p.kill()
            raise
        if p.returncode:
            print(f"(exit status {p.returncode})", file=sys.stderr)
        return None

    def _magic(self, name: str, arg: str):
        arg = arg.strip()
        if name in ("matplotlib", "load_ext", "reload_ext", "config", "autoreload", "aimport", "precision", "xmode", "colors", "pylab", "gui"):
            return None
        if name == "run":
            return self._magic_run(arg)
        if name == "cd":
            target = os.path.expanduser(expand(arg.strip("'\""), self.ns) or "~")
            os.chdir(target)
            e = self.current
            if e is not None:
                e.cwd = os.getcwd()
            return None
        if name == "pwd":
            return os.getcwd()
        if name == "env":
            if not arg:
                return dict(os.environ)
            if "=" in arg or " " in arg:
                k, _, v = arg.replace("=", " ", 1).partition(" ")
                os.environ[k.strip()] = expand(v.strip(), self.ns)
                return None
            return os.environ.get(arg)
        if name in ("time", "timeit"):
            t0 = time.perf_counter()
            n = 1
            if name == "timeit":
                n = 7
                for _ in range(n):
                    exec(compile(transform(arg), "<timeit>", "exec"), self.ns)  # noqa: S102
            else:
                exec(compile(transform(arg), "<time>", "exec"), self.ns)  # noqa: S102
            dt = (time.perf_counter() - t0) / n
            print(f"Wall time: {dt * 1e3:.3f} ms" + (f" per loop ({n} loops)" if n > 1 else ""))
            return None
        if name in ("who", "whos"):
            for v in self.variables():
                print(f"{v['name']:20} {v['type']:15} {v['repr'][:60]}")
            return None
        if name in ("pip", "conda"):
            if getattr(sys, "frozen", False):
                print("Installing packages is not possible in the standalone bundle; use a pip install of chipwhisperer-studio for that.")
                return None
            return self._shell(f'"{sys.executable}" -m pip {arg}')
        if name in ("sx", "system"):
            return self._shell(arg, capture=True)
        if name == "bash" or name == "sh":
            return self._shell(arg)
        raise NameError(f"UsageError: line magic %{name} is not supported in Studio notebooks")

    def _magic_run(self, arg: str):
        parts = shlex.split(arg, posix=os.name != "nt")
        paths = [p for p in parts if not p.startswith("-")]
        if not paths:
            raise ValueError("%run needs a file")
        path = os.path.expanduser(expand(paths[0], self.ns))
        if not os.path.isfile(path):
            raise FileNotFoundError(f"%run: {path} not found (cwd {os.getcwd()})")
        if path.endswith(".ipynb"):
            with open(path, "r", encoding="utf-8") as f:
                nb = normalize(json.load(f))
            for c in nb["cells"]:
                if c["cell_type"] == "code" and c["source"].strip():
                    self._run_source(c["source"], None, allow_result=False)
        else:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            self.ns["__file__"] = os.path.abspath(path)  # like %run -i: the script shares the notebook namespace
            exec(compile(transform(src), path, "exec"), self.ns)  # noqa: S102
        return None

    def _cell_magic(self, name: str, arg: str, body: str, e: Optional[_Exec]):
        if name in ("bash", "sh", "script"):
            return self._script(name, arg, body)
        if name == "time":
            t0 = time.perf_counter()
            self._run_source(body, e)
            print(f"Wall time: {(time.perf_counter() - t0) * 1e3:.3f} ms")
            return None
        if name == "capture":
            buf = io.StringIO()
            tid = threading.get_ident()
            saved = {k: r.sinks.get(tid) for k, r in _ROUTERS.items()}
            for r in _ROUTERS.values():
                r.sinks[tid] = lambda n, s: buf.write(s)
            try:
                self._run_source(body, None, allow_result=False)
            finally:
                for k, r in _ROUTERS.items():
                    if saved[k] is not None:
                        r.sinks[tid] = saved[k]
            if arg:
                self.ns[arg.split()[0]] = buf.getvalue()
            return None
        if name == "writefile":
            append = arg.startswith("-a")
            fn = expand(arg.split()[-1], self.ns)
            with open(fn, "a" if append else "w", encoding="utf-8") as f:
                f.write(body if body.endswith("\n") else body + "\n")
            print(("Appended to " if append else "Writing ") + fn)
            return None
        if name in ("html", "HTML"):
            self._emit({"output_type": "display_data", "data": {"text/html": body, "text/plain": body}, "metadata": {}})
            return None
        if name == "markdown":
            self._emit({"output_type": "display_data", "data": {"text/markdown": body, "text/plain": body}, "metadata": {}})
            return None
        raise NameError(f"UsageError: cell magic %%{name} is not supported in Studio notebooks")

    # --- trace publication --------------------------------------------------------------
    def record_trace(self, wave, textin=None, textout=None, key=None) -> Optional[int]:
        w = np.asarray(wave, np.float32)
        idx = -1
        if self.store_traces:
            idx = self.session.store.append(w, bytes(textin or b""), bytes(textout or b""), bytes(key or b""))
        self._last_trace = (w, idx, textin, textout, key)
        now = time.time()
        if now - self._last_trace_pub > 0.04:
            self._last_trace_pub = now
            self._publish_trace(droppable=True)
        return idx if idx >= 0 else None

    def _publish_trace(self, droppable: bool):
        if self._last_trace is None:
            return
        w, idx, tin, tout, key = self._last_trace
        meta = {"index": idx, "stored": idx >= 0, "generation": self.session.store.generation, "source": "notebook"}
        for k, v in (("textin", tin), ("textout", tout), ("key", key)):
            if v is not None:
                meta[k] = bytes(v).hex()
        self.bus.publish_event(trace_event("trace", w, meta, droppable=droppable))

    def _finish_traces(self):
        if self._last_trace is not None:
            self._publish_trace(droppable=False)
            self._last_trace = None
            self.bus.publish("traces", self.session.store.summary())
            try:
                self.session._push_status()
            except Exception:  # noqa: BLE001
                pass


def _is_figure(v) -> bool:
    return type(v).__module__.startswith("matplotlib") and type(v).__name__ in ("Figure", "AxesImage", "Line2D") or (isinstance(v, list) and v and type(v[0]).__module__.startswith("matplotlib"))


class _IPythonStub:
    def __init__(self, k: Kernel):
        self.k = k

    def run_line_magic(self, name, line):
        return self.k._magic(name, line)

    def run_cell_magic(self, name, line, cell):
        return self.k._cell_magic(name, line, cell, self.k.current)

    def system(self, cmd):
        return self.k._shell(cmd)

    def getoutput(self, cmd):
        return self.k._shell(cmd, capture=True)

    user_ns = property(lambda self: self.k.ns)


class _CWProxy(types.ModuleType):
    """``chipwhisperer`` as seen from a Studio notebook."""

    def __init__(self, real, kernel: Kernel):
        super().__init__("chipwhisperer")
        self.__dict__["_real"] = real
        self.__dict__["_k"] = kernel

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)

    def __dir__(self):
        return dir(self.__dict__["_real"])

    def scope(self, name=None, sn=None, **kwargs):
        s = self._k.session
        if s.scope is None:
            kind = str(name).lower() if name else ("sim" if s.simulate_default else "auto")
            kind = {"openadc": "auto", "cwnano": "nano", "cwlite": "lite", "cw1200": "pro"}.get(kind, kind)
            s.connect_scope(kind, sn=sn, force=bool(kwargs.get("force")), default_setup=False)
            print(f"Connected Studio to the {s.scope_kind} scope")
        return self._k.scope_proxy

    def target(self, scope=None, target_type=None, **kwargs):
        s = self._k.session
        if s.scope is None:
            self.scope()
        want = getattr(target_type, "__name__", None) if target_type is not None else None
        cur = s.target
        if cur is None or not (want is None or type(cur).__name__ in (want, "SimTarget")):
            kind = "sim" if s.scope_kind == "sim" else (want or "SimpleSerial2")
            s.connect_target(kind, **{k: v for k, v in kwargs.items() if v is not None})
        return self._k.target_proxy

    def capture_trace(self, scope, target, plaintext, key=None, ack=True, **kwargs):
        tr = self._real.capture_trace(unwrap(scope), unwrap(target), plaintext, key, ack=ack, **kwargs)
        if tr is not None:
            self._k.record_trace(tr.wave, tr.textin, tr.textout, tr.key)
        return tr

    def program_target(self, scope, prog_type, fw_path, **kwargs):
        s = self._k.session
        if s.scope_kind == "sim":
            if not os.path.isfile(fw_path):
                raise FileNotFoundError(fw_path)
            print(f"Simulator: pretending to program {os.path.basename(fw_path)} ({os.path.getsize(fw_path)} bytes)")
            return True
        return self._real.program_target(unwrap(scope), prog_type, fw_path, **kwargs)

    def plot(self, *args, **kwargs):
        """Show a trace in Studio's waveform view and inline in the notebook (replaces the holoviews based cw.plot)."""
        y = np.asarray(args[-1] if args else kwargs.get("y"), dtype=np.float32).ravel()
        self._k.bus.publish_event(trace_event("trace", y, {"index": -1, "stored": False, "source": "notebook"}, droppable=False))
        try:
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(9, 3))
            ax.plot(y, lw=0.8)
            ax.grid(alpha=0.3)
            return None
        except ImportError:
            return None


def unwrap(obj):
    return obj._live() if isinstance(obj, _Live) or type(obj) in (StudioScope, StudioTarget) else obj


class _Live:
    """Stand-in for Studio's scope or target: forwards everything to the object Studio is connected to right now (so it survives reconnects) and reports that object's class, so the library's isinstance checks still pass."""

    def __init__(self, kernel: "Kernel", attr: str):
        object.__setattr__(self, "_k", kernel)
        object.__setattr__(self, "_attr", attr)

    def _live(self):
        obj = getattr(object.__getattribute__(self, "_k").session, object.__getattribute__(self, "_attr"))
        if obj is None:
            raise RuntimeError(f"no {object.__getattribute__(self, '_attr')} connected in Studio")
        return obj

    @property
    def __class__(self):
        return type(self._live())

    def __getattr__(self, name):
        return getattr(self._live(), name)

    def __setattr__(self, name, value):
        setattr(self._live(), name, value)

    def __dir__(self):
        return dir(self._live())

    def __repr__(self):
        try:
            return repr(self._live())
        except Exception as e:  # noqa: BLE001
            return f"<not connected: {e}>"

    def __str__(self):
        try:
            return str(self._live())
        except Exception as e:  # noqa: BLE001
            return f"<not connected: {e}>"

    def _repr_html_(self):
        return None


class StudioScope(_Live):
    def get_last_trace(self, *args, **kwargs):
        wave = self._live().get_last_trace(*args, **kwargs)
        k = object.__getattribute__(self, "_k")
        if wave is not None and len(wave):
            t = k.target_proxy
            k.pending_textout = k.record_trace(wave, t._state.get("text"), None, t._state.get("key"))
        return wave


class StudioTarget(_Live):
    def __init__(self, kernel, attr):
        super().__init__(kernel, attr)
        object.__setattr__(self, "_state", {})

    def simpleserial_write(self, cmd, data=b"", *args, **kwargs):
        st = object.__getattribute__(self, "_state")
        c = cmd.decode() if isinstance(cmd, (bytes, bytearray)) else str(cmd)
        if c == "p":
            st["text"] = bytes(data)
        elif c == "k":
            st["key"] = bytes(data)
        object.__getattribute__(self, "_k").pending_textout = None if c == "p" else object.__getattribute__(self, "_k").pending_textout
        return self._live().simpleserial_write(cmd, data, *args, **kwargs)

    def set_key(self, key, *args, **kwargs):
        object.__getattribute__(self, "_state")["key"] = bytes(key)
        return self._live().set_key(key, *args, **kwargs)

    def simpleserial_read(self, *args, **kwargs):
        resp = self._live().simpleserial_read(*args, **kwargs)
        k = object.__getattribute__(self, "_k")
        idx = k.pending_textout
        if resp is not None and idx is not None:
            try:
                k.session.store.set_textout(idx, bytes(resp))
            except Exception:  # noqa: BLE001
                pass
            k.pending_textout = None
        return resp


class _ProxyWithFrom(types.ModuleType):
    """Result of ``from chipwhisperer import X``: the proxy's scope/target/capture_trace, everything else from the real module."""

    def __init__(self, proxy, real):
        super().__init__("chipwhisperer")
        self.__dict__["_p"], self.__dict__["_r"] = proxy, real

    def __getattr__(self, name):
        if name in ("scope", "target", "capture_trace", "plot", "program_target"):
            return getattr(self.__dict__["_p"], name)
        return getattr(self.__dict__["_r"], name)


class StudioHelper:
    """The ``studio`` object available in every notebook."""

    def __init__(self, k: Kernel):
        self._k = k

    @property
    def session(self):
        return self._k.session

    @property
    def traces(self):
        """Stored traces as a TraceSet: ``len(studio.traces)``, ``studio.traces.waves`` (2-D array), ``.textins``, ``.textouts``, ``.keys``."""
        return _TraceView(self._k.session.store)

    def add_trace(self, wave, textin=None, textout=None, key=None) -> Optional[int]:
        """Store a trace (any 1-D array) so it shows in the waveform view and Capture tab."""
        return self._k.record_trace(wave, textin, textout, key)

    def clear_traces(self):
        self._k.session.store.clear()
        self._k.bus.publish("traces", self._k.session.store.summary())

    def show(self, wave):
        """Display a waveform in Studio's main plot without storing it."""
        self._k.bus.publish_event(trace_event("trace", np.asarray(wave, np.float32), {"index": -1, "stored": False, "source": "notebook"}, droppable=False))

    def store_traces(self, enabled: bool = True):
        """Choose whether cw.capture_trace() results are added to Studio's trace store (default True)."""
        self._k.store_traces = bool(enabled)

    def build_firmware(self, project="simpleserial-aes", platform="CWLITEARM", compiler="gcc", **options) -> str:
        """Build firmware with Studio's toolchains and return the .hex path (raises with the build log tail on failure)."""
        fm = self._k.session.firmware
        r = fm.build(dict(options, project=project, platform=platform, compiler=compiler), wait=True)
        if r.get("state") != "ok":
            tail = "\n".join(fm.build_log()["lines"][-30:])
            raise RuntimeError(f"build failed: {r.get('error')}\n{tail}")
        print(f"Built {r['hex']} ({r.get('size')})")
        return r["hex"]

    def program(self, hex_path=None, programmer=None):
        """Program the target with a .hex (default: the last Studio build)."""
        return self._k.session.program_build(hex_path, programmer)

    def __repr__(self):
        s = self._k.session
        return f"<Studio scope={s.scope_kind} target={s.target_kind} traces={len(s.store)}>"


class _TraceView:
    def __init__(self, store):
        self._s = store

    def __len__(self):
        return len(self._s)

    def __getitem__(self, i):
        return self._s.get(i)

    @property
    def waves(self):
        return self._s.as_arrays()[0] if len(self._s) else np.zeros((0, 0), np.float32)

    @property
    def textins(self):
        return self._s.as_arrays()[1] if len(self._s) else np.zeros((0, 0), np.uint8)

    @property
    def textouts(self):
        return self._s.as_arrays()[2] if len(self._s) else np.zeros((0, 0), np.uint8)

    @property
    def keys(self):
        return self._s.as_arrays()[3] if len(self._s) else np.zeros((0, 0), np.uint8)

    def __repr__(self):
        return f"<{len(self)} traces in Studio>"


# ----------------------------------------------------------------------------
# ChipWhisperer tutorial notebooks
# ----------------------------------------------------------------------------
class Tutorials:
    """Downloads newaetech/chipwhisperer-jupyter (the version pinned by the installed firmware sources) into the notebooks folder and links Studio's firmware tree where the tutorials expect ``../../../firmware/mcu``."""

    REPO = "newaetech/chipwhisperer-jupyter"
    DIR = "chipwhisperer-jupyter"

    def __init__(self, store: NotebookStore, firmware, publish: Callable[[str, Dict[str, Any]], None]):
        self.store, self.fm, self._publish = store, firmware, publish
        self.job: Dict[str, Any] = {}

    @property
    def dest(self) -> str:
        return os.path.join(self.store.root, self.DIR)

    def status(self) -> Dict[str, Any]:
        info = None
        try:
            with open(os.path.join(self.dest, ".cwstudio-source.json"), "r", encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            pass
        link = os.path.join(self.store.root, "firmware", "mcu")
        return {"installed": info, "folder": self.DIR, "firmware_linked": os.path.isfile(os.path.join(link, "Makefile.inc")), "job": {k: v for k, v in self.job.items() if k != "cancel"} or None}

    def fetch(self, wait: bool = False) -> Dict[str, Any]:
        if self.job.get("state") in ("resolving", "downloading", "extracting"):
            return self.status()
        from cwstudio.toolchains import Cancelled  # noqa: F401
        self.job = {"state": "resolving", "done": 0, "total": 0, "error": None, "cancel": threading.Event()}
        th = threading.Thread(target=self._fetch, name="fetch-tutorials", daemon=True)
        th.start()
        if wait:
            th.join()
        return self.status()

    def _fetch(self):
        from cwstudio.toolchains import download, extract
        job = self.job
        tmp = self.dest + ".tmp"
        try:
            self._publish("tutorials", self.status())
            src = self.fm.installed_source() or {}
            ref = src.get("commit")
            if not ref:
                ref = self.fm.resolve()["commit"]
            sha = self.fm._gh(f"/repos/{self.fm.sources_cfg['repo']}/contents/jupyter?ref={ref}")["sha"]
            job["state"] = "downloading"
            archive = os.path.join(self.store.root, ".downloads", f"chipwhisperer-jupyter-{sha[:12]}.tar.gz")
            log.info("Downloading ChipWhisperer tutorial notebooks at %s", sha[:7])

            def progress(done, total):
                job["done"], job["total"] = done, total
            download(f"https://codeload.github.com/{self.REPO}/tar.gz/{sha}", archive, None, progress, job["cancel"])
            job["state"] = "extracting"
            self._publish("tutorials", self.status())
            shutil.rmtree(tmp, ignore_errors=True)
            extract(archive, tmp, members=lambda n: n.split("/", 1)[1] if "/" in n and n.split("/", 1)[1] else None, cancel=job["cancel"])
            with open(os.path.join(tmp, ".cwstudio-source.json"), "w", encoding="utf-8") as f:
                json.dump({"repo": self.REPO, "commit": sha, "firmware_commit": ref, "installed": time.time()}, f)
            shutil.rmtree(self.dest, ignore_errors=True)
            os.replace(tmp, self.dest)
            shutil.rmtree(os.path.join(self.store.root, ".downloads"), ignore_errors=True)
            self.link_firmware()
            job["state"] = "installed"
            log.info("Tutorial notebooks ready in %s", self.dest)
        except Exception as e:  # noqa: BLE001
            job["state"], job["error"] = "error", f"{type(e).__name__}: {e}"
            log.error("Fetching tutorial notebooks failed: %s", e)
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            self._publish("tutorials", self.status())

    def link_firmware(self) -> bool:
        """Make ``<notebooks>/firmware/mcu`` point at Studio's firmware sources (symlink, or a junction on Windows)."""
        root = self.fm.root
        if not self.fm.is_valid_root(root):
            log.warning("Firmware sources are not downloaded yet; tutorial build cells need them (Firmware tab)")
            return False
        parent = os.path.join(self.store.root, "firmware")
        link = os.path.join(parent, "mcu")
        os.makedirs(parent, exist_ok=True)
        if os.path.islink(link) or os.path.isfile(link):
            os.remove(link)
        elif os.path.isdir(link):
            if os.path.abspath(os.path.realpath(link)) == os.path.abspath(os.path.realpath(root)):
                return True
            try:
                os.rmdir(link)  # an old junction or empty folder
            except OSError:
                shutil.rmtree(link, ignore_errors=True)
        try:
            os.symlink(root, link, target_is_directory=True)
            return True
        except OSError:
            if os.name == "nt":
                r = subprocess.run(["cmd", "/c", "mklink", "/J", link, root], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    return True
            log.warning("Could not link the firmware folder for the tutorials")
            return False
