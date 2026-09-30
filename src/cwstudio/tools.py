"""Notes pad storage and the calculator (safe expression evaluation and statistics of selected values)."""
from __future__ import annotations

import ast
import math
import operator
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from cwstudio.aes import SBOX


# ----------------------------------------------------------------------------
# notes
# ----------------------------------------------------------------------------
class NotesStore:
    """Plain text notes, one file each under ``<data_dir>/notes``."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, name: str) -> str:
        name = re.sub(r"[\\/:*?\"<>|]+", "_", (name or "").strip()) or "Untitled"
        if not name.endswith((".md", ".txt")):
            name += ".md"
        return os.path.join(self.root, name)

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for fn in os.listdir(self.root):
            p = os.path.join(self.root, fn)
            if os.path.isfile(p) and fn.endswith((".md", ".txt")) and not fn.startswith("."):
                out.append({"name": fn, "bytes": os.path.getsize(p), "mtime": os.path.getmtime(p)})
        return sorted(out, key=lambda n: -n["mtime"])

    def get(self, name: str) -> Dict[str, Any]:
        p = self._path(name)
        with open(p, "r", encoding="utf-8") as f:
            return {"name": os.path.basename(p), "text": f.read(), "mtime": os.path.getmtime(p)}

    def put(self, name: str, text: str) -> Dict[str, Any]:
        p = self._path(name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text or "")
        os.replace(tmp, p)
        return {"name": os.path.basename(p), "bytes": os.path.getsize(p), "mtime": os.path.getmtime(p)}

    def create(self, name: Optional[str] = None) -> Dict[str, Any]:
        base = (name or time.strftime("Note %Y-%m-%d %H.%M")).strip()
        cand, n = base, 1
        while os.path.exists(self._path(cand)):
            n += 1
            cand = f"{base} {n}"
        return self.put(cand, "")

    def rename(self, old: str, new: str) -> Dict[str, Any]:
        src, dst = self._path(old), self._path(new)
        if os.path.exists(dst):
            raise FileExistsError(os.path.basename(dst))
        os.replace(src, dst)
        return {"name": os.path.basename(dst)}

    def delete(self, name: str) -> None:
        os.remove(self._path(name))


# ----------------------------------------------------------------------------
# calculator
# ----------------------------------------------------------------------------
def _hw(x: int) -> int:
    return bin(int(x) & ((1 << 4096) - 1)).count("1")


def _hd(a: int, b: int) -> int:
    return _hw(int(a) ^ int(b))


def _stat(fn):
    return lambda *a: float(fn(np.asarray(a[0] if len(a) == 1 and isinstance(a[0], (list, tuple, np.ndarray)) else a, dtype=float)))


FUNCS: Dict[str, Any] = {
    **{k: getattr(math, k) for k in ("sqrt", "exp", "log", "log2", "log10", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "floor", "ceil", "degrees", "radians", "factorial", "gcd", "hypot", "isqrt", "comb", "perm")},
    "abs": abs, "round": round, "int": int, "float": float, "hex": hex, "bin": bin, "oct": oct, "chr": chr, "ord": ord,
    "min": min, "max": max, "sum": lambda *a: sum(a[0]) if len(a) == 1 and isinstance(a[0], (list, tuple)) else sum(a),
    "mean": _stat(np.mean), "avg": _stat(np.mean), "median": _stat(np.median), "std": _stat(np.std), "var": _stat(np.var),
    "rms": _stat(lambda v: np.sqrt(np.mean(v ** 2))),
    "hw": _hw, "popcount": _hw, "hd": _hd, "sbox": lambda x: int(SBOX[int(x) & 0xFF]), "db": lambda r: 20 * math.log10(r), "undb": lambda d: 10 ** (d / 20),
}
CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.BitXor: operator.xor, ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.LShift: operator.lshift, ast.RShift: operator.rshift}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Invert: operator.invert, ast.Not: operator.not_}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def calc(expr: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate a calculator expression safely (numbers, + - * / // % ** ^ & | ~ << >>, comparisons, math functions, hw/hd/sbox for crypto work, ``ans`` and user variables). ``^`` is XOR as in C and Python. Assignments like ``x = 3`` store a variable."""
    variables = variables if variables is not None else {}
    text = (expr or "").strip()
    if not text:
        raise ValueError("empty expression")
    target = None
    m = re.match(r"^([A-Za-z_]\w*)\s*=(?!=)\s*(.+)$", text)
    if m:
        target, text = m.group(1), m.group(2)
        if target in FUNCS or target in CONSTS:
            raise ValueError(f"{target} is a built-in name")
    tree = ast.parse(text, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 400:
        raise ValueError("expression too long")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, complex, bool)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in variables:
                return variables[n.id]
            if n.id in CONSTS:
                return CONSTS[n.id]
            raise NameError(f"unknown name {n.id}")
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 4096:
                raise ValueError("exponent too large")
            if isinstance(n.op, ast.LShift) and b > 4096:
                raise ValueError("shift too large")
            return _BIN[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            return _UN[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _CMP:
            return _CMP[type(n.ops[0])](ev(n.left), ev(n.comparators[0]))
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(x) for x in n.elts]
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUNCS and not n.keywords:
            return FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"not allowed in the calculator: {ast.dump(n)[:60]}")

    val = ev(tree)
    if isinstance(val, bool):
        val = int(val)
    variables["ans"] = val
    if target:
        variables[target] = val
    out: Dict[str, Any] = {"value": val if not isinstance(val, complex) else str(val), "text": _fmt(val), "variable": target}
    if isinstance(val, int) and not isinstance(val, bool):
        out.update({"hex": hex(val), "bin": bin(val), "dec": str(val)})
    return out


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.10g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return str(v)


def stats(values: Sequence[float], sample_rate: Optional[float] = None) -> Dict[str, Any]:
    """Summary statistics of a list or array of numbers."""
    a = np.asarray(values, dtype=np.float64).ravel()
    a = a[np.isfinite(a)]
    n = int(a.size)
    if n == 0:
        return {"count": 0}
    out = {"count": n, "sum": float(a.sum()), "mean": float(a.mean()), "median": float(np.median(a)), "min": float(a.min()), "max": float(a.max()), "pk_pk": float(a.max() - a.min()), "std": float(a.std(ddof=1)) if n > 1 else 0.0, "variance": float(a.var(ddof=1)) if n > 1 else 0.0, "rms": float(np.sqrt(np.mean(a * a))), "argmin": int(a.argmin()), "argmax": int(a.argmax())}
    if sample_rate:
        out["duration_s"] = n / sample_rate
    return out
