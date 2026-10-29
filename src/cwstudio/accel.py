"""Hardware acceleration for the number crunching: a CUDA GPU through CuPy when one is present, numpy otherwise.

``backend()`` picks once per process: CuPy (``pip install chipwhisperer-studio[gpu]``, which brings cupy-cuda12x with the CUDA headers it compiles kernels against; needs an NVIDIA driver for CUDA 12) with a working device, unless ``CWSTUDIO_GPU=0`` turns it off; a CuPy that imports but finds no device falls back silently. ``info()`` is what the UI and the MCP server report (Help tab, studio_options). The CPA uses ``xp`` for its accumulators so the same code runs on either; results on the GPU are computed in float32 products with float64 sums, which gives the same key ranking as the CPU's float64 path.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

import numpy as np

log = logging.getLogger("cwstudio.accel")

_lock = threading.Lock()
_backend: Optional[Dict[str, Any]] = None


def _probe() -> Dict[str, Any]:
    off = os.environ.get("CWSTUDIO_GPU", "1").strip().lower() in ("0", "no", "off", "false")
    cpu = {"name": "cpu", "xp": np, "label": "CPU (numpy)", "device": None, "reason": None, "cupy_version": None}
    if off:
        cpu["reason"] = "disabled with CWSTUDIO_GPU=0"
        return cpu
    try:
        import cupy as cp
    except Exception as e:  # noqa: BLE001  not installed, or its CUDA runtime is missing
        cpu["reason"] = f"CuPy not available ({type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''})".rstrip(": )") + ")"
        return cpu
    try:
        dev = cp.cuda.Device(0)
        dev.use()
        props = cp.cuda.runtime.getDeviceProperties(dev.id)
        name = props.get("name", b"GPU")
        name = name.decode() if isinstance(name, bytes) else str(name)
        mem = int(props.get("totalGlobalMem", 0))
        # one tiny elementwise kernel: proves the driver, runtime and device agree and that CuPy can compile kernels (it needs the CUDA headers: cupy-cuda12x[ctk])
        if float((cp.asarray(np.ones(4, np.float32)) * 2).astype(np.float64).sum()) != 8.0:
            raise RuntimeError("GPU arithmetic gave a wrong result")
        _warm_up(cp)
    except Exception as e:  # noqa: BLE001
        cpu["reason"] = f"no usable CUDA device ({type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''})"
        cpu["cupy_version"] = getattr(cp, "__version__", None)
        return cpu
    return {"name": "cuda", "xp": cp, "label": f"GPU: {name} (CuPy {cp.__version__})", "device": {"name": name, "memory_bytes": mem, "id": int(dev.id)}, "reason": None, "cupy_version": cp.__version__}


def _warm_up(cp) -> None:
    """Run each kernel the CPA uses once on tiny arrays: CuPy compiles kernels on first use (about a second each), which would otherwise land on the first attack."""
    a = cp.ones((8, 16), np.float32)
    h = cp.ones((8, 256), np.float32)
    s64 = cp.zeros((256, 16), np.float64)
    s64 += cp.matmul(h.T, a)
    s64 += a.sum(axis=0, dtype=np.float64)
    (a * a).sum(axis=0, dtype=np.float64)
    c = 3 * s64 - cp.ones(256, np.float64)[:, None] * cp.ones(16, np.float64)[None, :]
    c = cp.abs(c / cp.sqrt(c))
    c[~cp.isfinite(c)] = 0.0
    am = c.argmax(axis=1)
    c[cp.arange(256), am].get()
    c[0].astype(np.float32).get()


def backend(refresh: bool = False) -> Dict[str, Any]:
    """The compute backend in use: ``name`` ('cuda' or 'cpu'), ``xp`` (cupy or numpy), ``label``, ``device`` and, on the CPU, the ``reason`` no GPU is used."""
    global _backend
    with _lock:
        if _backend is None or refresh:
            _backend = _probe()
            if _backend["name"] == "cuda":
                log.info("compute on the %s", _backend["label"])
            else:
                log.debug("compute on the CPU: %s", _backend["reason"])
        return _backend


def gpu_available() -> bool:
    return backend()["name"] == "cuda"


def xp():
    """numpy or cupy, whichever the backend is."""
    return backend()["xp"]


def to_host(a) -> np.ndarray:
    """A numpy array from either backend's array."""
    get = getattr(a, "get", None)
    return get() if callable(get) and not isinstance(a, np.ndarray) else np.asarray(a)


def info() -> Dict[str, Any]:
    """What to show the user: the backend name and label, the device, and why the CPU is used when it is."""
    b = backend()
    out = {"backend": b["name"], "label": b["label"], "device": b["device"], "reason": b["reason"], "cupy_version": b["cupy_version"]}
    try:
        import numpy  # noqa: F401
        out["blas"] = _blas_name()
    except Exception:  # noqa: BLE001
        pass
    return out


def _blas_name() -> Optional[str]:
    try:
        cfg = np.show_config(mode="dicts")  # numpy >= 1.25
        name = cfg.get("Build Dependencies", {}).get("blas", {}).get("name")
        return str(name) if name else None
    except Exception:  # noqa: BLE001
        return None
