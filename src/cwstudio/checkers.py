"""Pluggable glitch result checkers.

The sweep classifies every attempt as normal, success, reset, garbled or not_fired from the SimpleSerial reply alone. A checker refines that with what the target's answer means: a loop counter that came out short, an AES ciphertext with a usable fault, a flag byte a password check set, or anything a one-line expression or a Python file can say. Built-in checkers live here; ``<data_dir>/checkers/*.py`` adds more, each a module with ``check(ctx)`` (see ``CONTEXT_HELP``), reloaded when the sweep starts.
"""
from __future__ import annotations

import importlib.util
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

OUTCOMES = ("normal", "success", "reset", "garbled", "not_fired")

CONTEXT_HELP = ("ctx is a dict: resp (reply bytes, or None when nothing valid came back), hex (its hex), valid (a well formed reply), timed_out (the scope saw no trigger), "
                "expected (the expected bytes or None), value (resp as a little-endian unsigned int, or None), expected_value (the same for expected), fired (Husky glitch counter delta or None), "
                "outcome (the default class) and n (the attempt index). Return the new outcome (normal, success, reset, garbled, not_fired), a (outcome, detail) tuple, True for success, False for normal, or None to keep the default.")

Checker = Callable[[Dict[str, Any]], Any]


def _value(b: Optional[bytes]) -> Optional[int]:
    return int.from_bytes(b, "little") if b else None


def _loop_counter(ctx: Dict[str, Any]):
    """simpleserial-glitch style loop: the counter in the reply is lower than expected when iterations were skipped."""
    v, e = ctx.get("value"), ctx.get("expected_value")
    if v is None or ctx.get("outcome") in ("reset", "garbled", "not_fired"):
        return None
    if e is None:
        return None
    if v == e:
        return "normal"
    return ("success", f"counter {v} instead of {e} ({e - v} skipped)" if v < e else f"counter {v} instead of {e}")


def _aes_dfa(ctx: Dict[str, Any]):
    """A 16-byte ciphertext that differs from the correct one in exactly one MixColumns column is a usable round 9 fault for the DFA; any other difference is garbled."""
    from cwstudio.dfa import column_of
    resp, exp = ctx.get("resp"), ctx.get("expected")
    if resp is None or ctx.get("outcome") in ("reset", "garbled", "not_fired"):
        return None
    if exp is None or len(exp) != 16:
        return None
    if len(resp) != 16:
        return ("garbled", f"{len(resp)}-byte reply, not a ciphertext")
    if resp == exp:
        return "normal"
    c = column_of(exp, resp)
    if c is None:
        n = sum(1 for i in range(16) if resp[i] != exp[i])
        return ("garbled", f"{n} bytes differ, not one MixColumns column: fault outside round 9")
    return ("success", f"usable fault, column {c}")


def _flag(ctx: Dict[str, Any]):
    """A check that answers with a flag byte: any non-zero byte in the reply (a bypassed password or signature check that reports 1) is a success."""
    resp = ctx.get("resp")
    if resp is None or ctx.get("outcome") in ("reset", "garbled", "not_fired"):
        return None
    return ("success", f"flag set: {resp.hex()}") if any(resp) else "normal"


BUILTIN: Dict[str, Tuple[str, Optional[Checker]]] = {
    "expected": ("Compare with the expected reply (the sweep's own rule)", None),
    "loop_counter": ("Loop counter (simpleserial-glitch): success when the little-endian counter is lower than expected", _loop_counter),
    "aes_dfa": ("AES fault: success only when the ciphertext differs from the expected one in exactly one MixColumns column (a usable round 9 fault for the DFA)", _aes_dfa),
    "flag": ("Flag byte: success when any byte of the reply is non-zero (a bypassed check that answers 1)", _flag),
    "expr": ("Python expression over ctx (see the help)", None),
}


def make_expr(expr: str) -> Checker:
    expr = (expr or "").strip()
    if not expr:
        raise ValueError("the expr checker needs an expression. " + CONTEXT_HELP)
    code = compile(expr, "<checker>", "eval")

    def check(ctx: Dict[str, Any]):
        return eval(code, {"__builtins__": __builtins__}, dict(ctx))  # noqa: S307  the user's own machine and their own expression, like a notebook cell
    check.__doc__ = expr
    return check


def user_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "checkers")


def load_user(data_dir: str) -> Dict[str, Tuple[str, Checker]]:
    """Every ``<data_dir>/checkers/<name>.py`` with a ``check`` function, by file name (a broken file is reported as its error text, not loaded)."""
    out: Dict[str, Tuple[str, Checker]] = {}
    d = user_dir(data_dir)
    if not os.path.isdir(d):
        return out
    for f in sorted(os.listdir(d)):
        if not f.endswith(".py") or f.startswith("_"):
            continue
        name = f[:-3]
        path = os.path.join(d, f)
        try:
            spec = importlib.util.spec_from_file_location(f"cwstudio_checker_{name}", path)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            fn = getattr(mod, "check", None)
            if not callable(fn):
                raise AttributeError("no check(ctx) function")
            out[name] = ((fn.__doc__ or mod.__doc__ or f).strip().splitlines()[0], fn)
        except Exception as e:  # noqa: BLE001
            out[name] = (f"broken: {type(e).__name__}: {e}", _broken(name, e))
    return out


def _broken(name: str, e: Exception) -> Checker:
    def check(ctx):
        raise RuntimeError(f"checker {name} failed to load: {type(e).__name__}: {e}")
    return check


def available(data_dir: str) -> List[Dict[str, Any]]:
    rows = [{"name": k, "doc": v[0], "builtin": True} for k, v in BUILTIN.items()]
    rows += [{"name": k, "doc": v[0], "builtin": False, "path": os.path.join(user_dir(data_dir), k + ".py")} for k, v in load_user(data_dir).items()]
    return rows


def resolve(name: Optional[str], expr: Optional[str], data_dir: str) -> Optional[Checker]:
    """The checker function for a sweep, or None for the default rule."""
    name = (name or "expected").strip()
    if name == "expected":
        return None
    if name == "expr":
        return make_expr(expr or "")
    if name in BUILTIN:
        return BUILTIN[name][1]
    user = load_user(data_dir)
    if name in user:
        return user[name][1]
    raise ValueError(f"unknown checker {name!r}: " + ", ".join(list(BUILTIN) + list(user)) + f" (add your own as {user_dir(data_dir)}/{name}.py with a check(ctx) function)")


def apply(checker: Optional[Checker], ctx: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Run the checker on one attempt: (outcome, detail). A checker that raises turns the attempt into garbled with the error as detail, so one bad expression does not stop a long sweep."""
    outcome = ctx["outcome"]
    if checker is None:
        return outcome, None
    try:
        r = checker(ctx)
    except Exception as e:  # noqa: BLE001
        return "garbled", f"checker error: {type(e).__name__}: {e}"
    detail = None
    if isinstance(r, tuple) and len(r) == 2:
        r, detail = r[0], (None if r[1] is None else str(r[1]))
    if r is None:
        return outcome, detail
    if r is True:
        return "success", detail
    if r is False:
        return "normal", detail
    r = str(r)
    if r not in OUTCOMES:
        return "garbled", f"checker returned {r!r}, not one of {', '.join(OUTCOMES)}"
    return r, detail
