"""In-memory trace storage with import/export.

Traces are appended as they are captured.  Waves are kept as individual float32 arrays (so a settings change mid-session does not break anything) and consolidated into a 2-D array on demand for analysis.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("cwstudio.traces")


class TraceStore:
    def __init__(self, max_traces: int = 200_000):
        self._lock = threading.RLock()
        self.max_traces = max_traces
        self.waves: List[np.ndarray] = []
        self.textins: List[bytes] = []
        self.textouts: List[bytes] = []
        self.keys: List[bytes] = []
        self.meta: Dict[str, Any] = {}
        self.created = time.time()
        self.generation = 0  # bumped on clear/import so clients can resync
        # Running totals so summary() is O(1) instead of a pass over every wave
        self._len_counts: Dict[int, int] = {}
        self._nbytes = 0
        # Running per-sample sums for mean/std/min/max of the whole set: each stats() call only folds in the traces added since the previous call, so the live mean and envelope cost O(new traces) instead of a pass (and two full copies) over everything.
        self._acc: Optional[Dict[str, Any]] = None
        self._acc_lock = threading.Lock()

    # --- basic ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.waves)

    def clear(self):
        with self._lock:
            self.waves.clear()
            self.textins.clear()
            self.textouts.clear()
            self.keys.clear()
            self._len_counts = {}
            self._nbytes = 0
            self._acc = None
            self.meta = {}
            self.generation += 1
        _release_memory()

    def append(self, wave: np.ndarray, textin: Optional[bytes], textout: Optional[bytes],
               key: Optional[bytes]) -> int:
        with self._lock:
            if len(self.waves) >= self.max_traces:
                raise RuntimeError(f"trace store full ({self.max_traces} traces)")
            w = np.asarray(wave, dtype=np.float32)
            self.waves.append(w)
            self._len_counts[len(w)] = self._len_counts.get(len(w), 0) + 1
            self._nbytes += w.nbytes
            self.textins.append(bytes(textin) if textin is not None else b"")
            self.textouts.append(bytes(textout) if textout is not None else b"")
            self.keys.append(bytes(key) if key is not None else b"")
            return len(self.waves) - 1

    def set_textout(self, i: int, textout: bytes) -> None:
        with self._lock:
            if 0 <= i < len(self.textouts):
                self.textouts[i] = bytes(textout or b"")

    def get(self, i: int) -> Tuple[np.ndarray, bytes, bytes, bytes]:
        with self._lock:
            return self.waves[i], self.textins[i], self.textouts[i], self.keys[i]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self.waves)
            lens = self._len_counts
            return {
                "count": n,
                "samples": (min(lens) if lens else 0),
                "uniform": len(lens) <= 1,
                "generation": self.generation,
                "memory_bytes": int(self._nbytes),
                "meta": self.meta,
            }

    # --- array views ------------------------------------------------------
    def as_arrays(self, start: int = 0, end: Optional[int] = None):
        """Return (waves[N,S], textins[N,16], textouts[N,16], keys[N,16]) as arrays.

        Waves are truncated to the shortest trace in the range; text fields are zero-padded/truncated to the most common length.
        """
        with self._lock:
            end = len(self.waves) if end is None else min(end, len(self.waves))
            waves = self.waves[start:end]
            tins = self.textins[start:end]
            touts = self.textouts[start:end]
            keys = self.keys[start:end]
        if not waves:
            return (np.zeros((0, 0), np.float32), np.zeros((0, 16), np.uint8),
                    np.zeros((0, 16), np.uint8), np.zeros((0, 16), np.uint8))
        return _waves_matrix(waves), _bytes_matrix(tins), _bytes_matrix(touts), _bytes_matrix(keys)

    def stats(self, start: int = 0, end: Optional[int] = None) -> Dict[str, np.ndarray]:
        with self._lock:
            n_all = len(self.waves)
            if start <= 0 and (end is None or end >= n_all) and n_all and len(self._len_counts) == 1:
                whole = True
            else:
                whole = False
                end = n_all if end is None else min(end, n_all)
                waves = self.waves[start:end]
        if whole:
            return self._running_stats()
        if not waves:
            return {}
        W = _waves_matrix(waves)
        if W.shape[0] == 0:
            return {}
        mean = W.mean(axis=0)
        # Same arithmetic as W.std(axis=0) but reuses the mean instead of summing W a second time
        dev = W - mean
        np.square(dev, out=dev)
        return {
            "mean": mean,
            "std": np.sqrt(dev.mean(axis=0)),
            "min": W.min(axis=0),
            "max": W.max(axis=0),
        }

    def _running_stats(self, chunk_bytes: int = 32 * 1024 * 1024) -> Dict[str, np.ndarray]:
        """mean/std/min/max of every stored trace from running sums (float64), updated with only the traces added since the last call."""
        with self._acc_lock:
            with self._lock:
                acc = self._acc
                if acc is None or acc["gen"] != self.generation or acc["n"] > len(self.waves) or acc["len"] != len(self.waves[0]):
                    s = len(self.waves[0])
                    acc = self._acc = {"gen": self.generation, "len": s, "n": 0, "sum": np.zeros(s), "sq": np.zeros(s),
                                       "min": np.full(s, np.inf, np.float32), "max": np.full(s, -np.inf, np.float32)}
                new = self.waves[acc["n"]:]
            rows = max(1, chunk_bytes // (12 * acc["len"]))  # float32 stack plus its float64 copy stay within chunk_bytes whatever the trace length
            for i in range(0, len(new), rows):
                W = np.stack(new[i:i + rows])
                np.minimum(acc["min"], W.min(axis=0), out=acc["min"])
                np.maximum(acc["max"], W.max(axis=0), out=acc["max"])
                acc["sum"] += W.sum(axis=0, dtype=np.float64)
                W64 = W.astype(np.float64)  # squares in float64 keep std accurate to about 1e-8; the chunk keeps this copy small
                acc["sq"] += np.einsum("ij,ij->j", W64, W64)
            acc["n"] += len(new)
            n = acc["n"]
            mean = acc["sum"] / n
            var = np.maximum(acc["sq"] / n - mean * mean, 0.0)
            return {"mean": mean.astype(np.float32), "std": np.sqrt(var).astype(np.float32), "min": acc["min"].copy(), "max": acc["max"].copy()}

    # --- export / import --------------------------------------------------
    def export(self, path: str, fmt: str = "npz") -> str:
        fmt = fmt.lower()
        W, tin, tout, key = self.as_arrays()
        path = os.path.expanduser(path)
        if fmt == "npz":
            if not path.endswith(".npz"):
                path += ".npz"
            _savez_fast(path, waves=W, textins=tin, textouts=tout, keys=key, meta=np.array(json.dumps(self.meta)))
        elif fmt == "npy":
            base = path[:-4] if path.endswith(".npy") else path
            np.save(base + "_waves.npy", W)
            np.save(base + "_textins.npy", tin)
            np.save(base + "_textouts.npy", tout)
            np.save(base + "_keys.npy", key)
            path = base + "_*.npy"
        elif fmt == "csv":
            if not path.endswith(".csv"):
                path += ".csv"
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["index", "textin", "textout", "key", "samples..."])
                for i in range(W.shape[0]):
                    w.writerow([i, tin[i].tobytes().hex(), tout[i].tobytes().hex(), key[i].tobytes().hex()]
                               + [f"{x:.6g}" for x in W[i]])
        elif fmt in ("cwp", "project"):
            import chipwhisperer as cw  # local import: heavy
            from chipwhisperer.common.traces import Trace
            if path.endswith(".cwp"):
                path = path[:-4]
            proj = cw.create_project(path, overwrite=True)
            for i in range(W.shape[0]):
                proj.traces.append(Trace(W[i], bytearray(tin[i].tobytes()), bytearray(tout[i].tobytes()),
                                         bytearray(key[i].tobytes())))
            proj.save()
            proj.close(save=True)
            path = path + ".cwp"
        else:
            raise ValueError(f"unknown export format {fmt}")
        log.info("Exported %d traces to %s", W.shape[0], path)
        return path

    def import_file(self, path: str, replace: bool = True) -> int:
        path = os.path.expanduser(path)
        if path.lower().endswith(".zip"):
            path = _unzip_project(path)
        if path.endswith(".npz"):
            d = np.load(path, allow_pickle=False)
            W = d["waves"]
            tin = d["textins"] if "textins" in d else np.zeros((W.shape[0], 0), np.uint8)
            tout = d["textouts"] if "textouts" in d else np.zeros((W.shape[0], 0), np.uint8)
            key = d["keys"] if "keys" in d else np.zeros((W.shape[0], 0), np.uint8)
            rows = [(W[i], tin[i].tobytes(), tout[i].tobytes(), key[i].tobytes()) for i in range(W.shape[0])]
        elif path.endswith(".cwp"):
            import chipwhisperer as cw
            proj = cw.open_project(path)
            rows = []
            for t in proj.traces:
                rows.append((np.asarray(t.wave, np.float32), _to_bytes(t.textin), _to_bytes(t.textout),
                             _to_bytes(t.key)))
            proj.close(save=False)
        elif path.endswith(".npy"):
            W = np.load(path)
            rows = [(W[i], b"", b"", b"") for i in range(W.shape[0])]
        else:
            raise ValueError("supported: .npz, .cwp, .npy, or a .zip holding a ChipWhisperer project or .npz")
        with self._lock:
            if replace:
                self.clear()
            for r in rows:
                self.append(*r)
            self.generation += 1
        log.info("Imported %d traces from %s", len(rows), path)
        return len(rows)


def _to_bytes(v) -> bytes:
    if v is None:
        return b""
    if isinstance(v, np.ndarray):
        return v.astype(np.uint8).tobytes()
    return bytes(v)


def _waves_matrix(waves: List[np.ndarray]) -> np.ndarray:
    """Stack waves into a float32 [N, S] array, truncated to the shortest wave."""
    s = min(len(w) for w in waves)
    return np.stack([w[:s] for w in waves]).astype(np.float32, copy=False)


def _bytes_matrix(items: List[bytes]) -> np.ndarray:
    if not items:
        return np.zeros((0, 16), np.uint8)
    lens = [len(b) for b in items]
    uniq = set(lens)
    if len(uniq) == 1 and lens[0]:
        # Common case (every trace has the same text length): one join instead of a loop
        L = lens[0]
        return np.frombuffer(b"".join(items), np.uint8).reshape(len(items), L).copy()
    L = max(uniq, key=lens.count)
    out = np.zeros((len(items), L), np.uint8)
    for i, b in enumerate(items):
        n = min(L, len(b))
        if n:
            out[i, :n] = np.frombuffer(b[:n], dtype=np.uint8)
    return out


def _unzip_project(path: str) -> str:
    """Extract a zip (as made by Download in browser for a ChipWhisperer project) next to it and return the .cwp or .npz inside."""
    import zipfile
    dest = path[:-4] + "_extracted"
    with zipfile.ZipFile(path) as z:
        root = os.path.realpath(dest)
        for name in z.namelist():
            if not os.path.realpath(os.path.join(dest, name)).startswith(root + os.sep):
                raise ValueError(f"unsafe path in zip: {name}")
        z.extractall(dest)
    for ext in (".cwp", ".npz"):
        for d, _dirs, files in os.walk(dest):
            for f in sorted(files):
                if f.endswith(ext):
                    return os.path.join(d, f)
    raise ValueError("the zip holds no .cwp project or .npz file")


def _savez_fast(path: str, **arrays) -> None:
    """Like np.savez_compressed, but with zlib level 1: on real ADC traces that is about 10 times faster than the default level 6 (57 instead of 6 MB/s) for files only slightly larger (44 instead of 39 percent of the raw size). np.load reads it the same way."""
    import zipfile
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for name, arr in arrays.items():
            with z.open(name + ".npy", "w", force_zip64=True) as f:
                np.lib.format.write_array(f, np.asanyarray(arr), allow_pickle=False)
    os.replace(tmp, path)  # never leave a half-written file under the final name


def _release_memory() -> None:
    """Give freed trace memory back to the operating system. Traces under 128 KB live on glibc's heap, which otherwise keeps the freed space after a clear (memory stays at its peak although nothing leaks)."""
    import sys
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass
