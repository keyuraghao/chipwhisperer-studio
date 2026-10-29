"""Progressive CPA (Correlation Power Analysis) for AES-128.

Implemented with numpy accumulators so results can be reported every `report_every` traces without recomputation.  For each key byte b and each of the 256 guesses g we track the sums needed for Pearson correlation between the hypothetical leakage h(g, textin) and every sample of the trace.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from cwstudio import accel
from cwstudio.aes import HW, INV_SBOX, INV_SHIFT, SBOX

log = logging.getLogger("cwstudio.analysis")

MODELS: Dict[str, Dict[str, Any]] = {
    "sbox_hw": {"label": "HW of SBox output (round 1, software AES)", "uses": "textin"},
    "ptkey_hw": {"label": "HW of plaintext XOR key (round 1)", "uses": "textin"},
    "invsbox_hw": {"label": "HW of inverse SBox output (decryption, round 1)", "uses": "textin"},
    "lastround_hd": {"label": "HD of last round state (hardware AES, uses ciphertext)", "uses": "textout"},
    "lastround_hw": {"label": "HW of last round SBox input (uses ciphertext)", "uses": "textout"},
}

GUESSES = np.arange(256, dtype=np.uint8)

# Elements (float64) per block when turning the CPA sums into |corr|; 256 KiB-ish blocks stay in cache.
_REPORT_BLOCK = 1 << 15


def hypotheses(model: str, tin: np.ndarray, tout: np.ndarray, b: int, dtype=np.float32) -> np.ndarray:
    """Return a (n_traces, 256) matrix of hypothetical leakage for byte b, in ``dtype`` (one conversion from the uint8 tables instead of two)."""
    if model == "sbox_hw":
        x = tin[:, b][:, None] ^ GUESSES[None, :]
        return HW[SBOX[x]].astype(dtype)
    if model == "ptkey_hw":
        x = tin[:, b][:, None] ^ GUESSES[None, :]
        return HW[x].astype(dtype)
    if model == "invsbox_hw":
        x = tin[:, b][:, None] ^ GUESSES[None, :]
        return HW[INV_SBOX[x]].astype(dtype)
    if model in ("lastround_hd", "lastround_hw"):
        # ciphertext byte b, state before last SubBytes at position INV_SHIFT[b]
        ct_b = tout[:, b][:, None]
        st = INV_SBOX[ct_b ^ GUESSES[None, :]]
        if model == "lastround_hw":
            return HW[st].astype(dtype)
        ct_prev = tout[:, INV_SHIFT[b]][:, None]
        return HW[st ^ ct_prev].astype(dtype)
    raise ValueError(f"unknown model {model}")


def recover_key_from_lastround(rk10: List[int]) -> Optional[bytes]:
    """Invert the AES key schedule from round-10 key bytes."""
    try:
        from cwstudio.aes import RCON
        rk = [list(rk10)]
        k = list(rk10)
        for rnd in range(10, 0, -1):
            prev = [0] * 16
            for i in range(15, 3, -1):
                prev[i] = k[i] ^ k[i - 4]
            t = prev[13:16] + prev[12:13]
            t = [int(SBOX[x]) for x in t]
            t[0] ^= RCON[rnd - 1]
            for i in range(4):
                prev[i] = k[i] ^ t[i]
            k = prev
            rk.append(k)
        return bytes(k)
    except Exception:  # noqa: BLE001
        return None


class CPAResult:
    def __init__(self, model: str, n_bytes: int = 16):
        self.model = model
        self.n_bytes = n_bytes
        self.traces_used = 0
        self.total = 0
        self.corr_max = np.zeros((n_bytes, 256), np.float32)   # max |corr| over samples per guess
        self.corr_argmax = np.zeros((n_bytes, 256), np.int32)
        self.best_corr_trace: List[np.ndarray] = [np.zeros(0, np.float32)] * n_bytes  # corr vs sample, best guess
        self.history: List[Dict[str, Any]] = []   # [{traces, pge:[16], ranks..}]
        self.known_key: Optional[bytes] = None
        self.done = False
        self.error: Optional[str] = None
        self.started = time.time()
        self.elapsed = 0.0
        self.backend = "cpu"  # 'cuda' when the accumulation ran on the GPU (cwstudio.accel)

    def best_guesses(self, top: int = 5) -> List[Dict[str, Any]]:
        out = []
        for b in range(self.n_bytes):
            order = np.argsort(-self.corr_max[b])
            guesses = [{"guess": int(g), "corr": float(self.corr_max[b, g]), "sample": int(self.corr_argmax[b, g])}
                       for g in order[:top]]
            row = {"byte": b, "top": guesses, "best": int(order[0])}
            if self.known_key is not None and len(self.known_key) > b:
                kb = int(self.known_key[b])
                row["known"] = kb
                row["pge"] = int(np.where(order == kb)[0][0])
            out.append(row)
        return out

    def to_json(self) -> Dict[str, Any]:
        best = [int(np.argmax(self.corr_max[b])) for b in range(self.n_bytes)]
        payload = {
            "model": self.model,
            "traces_used": self.traces_used,
            "total": self.total,
            "done": self.done,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "backend": self.backend,
            "best_key": bytes(best).hex(),
            "bytes": self.best_guesses(),
            "history": self.history,
            "known_key": self.known_key.hex() if self.known_key else None,
        }
        if self.model.startswith("lastround"):
            rk = recover_key_from_lastround(best)
            payload["recovered_master_key"] = rk.hex() if rk else None
        return payload


# Traces per accumulation step: a GEMM over 50 traces is bound by writing the (256, S) product and reading it back into the sums, so small steps cost twice what 500-trace steps do; the report interval no longer decides the step
ACCUMULATE_CHUNK = 500
# A report (the correlations of all 16 x 256 guesses over every sample) costs as much as accumulating a few hundred traces, so progress is reported at most this often, and always at the end
REPORT_MIN_SECONDS = 0.5


class CPAAttack:
    """Runs on its own thread; call `start()` then poll `result` / listen to callback.

    ``report_every`` is the granularity of the progress reports in traces; reports are also spaced at least ``REPORT_MIN_SECONDS`` apart (the last one always comes), so a long attack spends its time attacking.
    """

    def __init__(self, waves: np.ndarray, tin: np.ndarray, tout: np.ndarray, model: str = "sbox_hw",
                 point_range: Optional[tuple] = None, report_every: int = 50,
                 known_key: Optional[bytes] = None, callback: Optional[Callable[[CPAResult], None]] = None,
                 bytes_to_attack: Optional[List[int]] = None):
        self.waves = waves
        self.tin = tin
        self.tout = tout
        self.model = model
        if point_range:
            lo, hi = int(point_range[0]), int(point_range[1])
            self.lo, self.hi = max(0, lo), min(waves.shape[1], hi if hi > 0 else waves.shape[1])
        else:
            self.lo, self.hi = 0, waves.shape[1]
        self.report_every = max(1, int(report_every))
        self.callback = callback
        self.bytes_to_attack = bytes_to_attack or list(range(16))
        self.result = CPAResult(model)
        self.result.total = int(waves.shape[0])
        self.result.known_key = known_key
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cw-cpa", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        r = self.result
        try:
            self._attack(accel.gpu_available())
        except Exception as e:  # noqa: BLE001
            if r.backend == "cuda" and not self._stop.is_set():
                log.warning("CPA on the GPU failed (%s: %s); running it on the CPU", type(e).__name__, e)
                try:
                    self._attack(False)
                except Exception as e2:  # noqa: BLE001
                    log.exception("CPA failed")
                    r.error = f"{type(e2).__name__}: {e2}"
            else:
                log.exception("CPA failed")
                r.error = f"{type(e).__name__}: {e}"
        r.done = True
        r.elapsed = time.time() - r.started
        if self.callback:
            self.callback(r)

    def _attack(self, gpu: bool):
        r = self.result
        if r.history:  # a retry on the CPU after the GPU failed starts clean
            r.history.clear()
        uses = MODELS[self.model]["uses"]
        src = self.tin if uses == "textin" else self.tout
        if src.shape[1] < 16:
            raise ValueError(f"model needs 16 bytes of {uses}, traces have {src.shape[1]}")
        N, S = self.waves.shape[0], self.hi - self.lo
        if N < 2 or S < 1:
            raise ValueError("need at least 2 traces and 1 sample")
        nb = len(self.bytes_to_attack)
        gpu = gpu and N * S >= 200_000  # tiny jobs finish faster on the CPU than the transfers take
        xp = accel.xp() if gpu else np
        r.backend = "cuda" if gpu else "cpu"
        # the GPU multiplies in float32 (GeForce float64 is slow) and sums in float64; the CPU keeps the all-float64 path, bit for bit as before
        fdt = np.float32 if gpu else np.float64
        sum_h = xp.zeros((nb, 256), np.float64)
        sum_h2 = xp.zeros((nb, 256), np.float64)
        sum_t = xp.zeros(S, np.float64)
        sum_t2 = xp.zeros(S, np.float64)
        sum_ht = xp.zeros((nb, 256, S), np.float64)
        n = 0
        every = max(self.report_every, 1)
        chunk = min(max(every, ACCUMULATE_CHUNK), N)
        prod = xp.empty((256, S), fdt)   # reused GEMM output, avoids a large allocation per byte per chunk
        last_report = 0.0
        for start in range(0, N, chunk):
            if self._stop.is_set():
                break
            end = min(N, start + chunk)
            W = xp.asarray(self.waves[start:end, self.lo:self.hi]).astype(fdt)
            sum_t += W.sum(axis=0, dtype=np.float64)
            sum_t2 += (W * W).sum(axis=0, dtype=np.float64)
            tin, tout = self.tin[start:end], self.tout[start:end]
            for i, b in enumerate(self.bytes_to_attack):
                H = xp.asarray(hypotheses(self.model, tin, tout, b, fdt))
                sum_h[i] += H.sum(axis=0, dtype=np.float64)
                sum_h2[i] += (H * H).sum(axis=0, dtype=np.float64)
                xp.matmul(H.T, W, out=prod)
                sum_ht[i] += prod
            n = end
            now = time.monotonic()
            if n >= N or (n % every == 0 or chunk > every) and now - last_report >= REPORT_MIN_SECONDS:
                last_report = now
                self._report(n, sum_h, sum_h2, sum_t, sum_t2, sum_ht, xp)
        if r.traces_used != n:  # stopped between reports: report what was accumulated
            self._report(n, sum_h, sum_h2, sum_t, sum_t2, sum_ht, xp)

    def _report(self, n, sum_h, sum_h2, sum_t, sum_t2, sum_ht, xp=np):
        r = self.result
        S = sum_t.shape[0]
        with np.errstate(divide="ignore", invalid="ignore"):
            var_h = n * sum_h2 - sum_h ** 2                     # (nb,256)
            var_t = n * sum_t2 - sum_t ** 2                     # (S,)
        if xp is not np:
            self._report_gpu(xp, n, sum_h, sum_t, sum_ht, var_h, var_t)
            return self._finish_report(n)
        # corr = (n*sum_ht - sum_h*sum_t) / sqrt(var_h*var_t), evaluated with exactly the same operations as before (so results are bit-identical) but a few guesses at a time in small reused buffers: full (nb,256,S) num/den/corr arrays cost several passes over hundreds of MB per report
        rows = max(1, min(256, _REPORT_BLOCK // max(S, 1)))
        blk = np.empty((rows, S), np.float64)
        den = np.empty((rows, S), np.float64)
        for i, b in enumerate(self.bytes_to_attack):
            for g0 in range(0, 256, rows):
                g1 = min(256, g0 + rows)
                c, d = blk[:g1 - g0], den[:g1 - g0]
                with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                    np.multiply(sum_h[i, g0:g1, None], sum_t[None, :], out=c)
                    np.subtract(n * sum_ht[i, g0:g1], c, out=c)
                    np.multiply(var_h[i, g0:g1, None], var_t[None, :], out=d)
                    np.sqrt(d, out=d)
                    np.divide(c, d, out=c)
                    np.abs(c, out=c)
                    c[~np.isfinite(c)] = 0.0  # zero variance (constant sample or hypothesis) or non-finite input: those correlations count as 0
                    am = c.argmax(axis=1)
                    mx = c[np.arange(g1 - g0), am]
                r.corr_max[b, g0:g1] = mx
                r.corr_argmax[b, g0:g1] = am + self.lo
            best = int(np.argmax(r.corr_max[b]))
            with np.errstate(divide="ignore", invalid="ignore"):
                num = n * sum_ht[i, best] - sum_h[i, best] * sum_t
                r.best_corr_trace[b] = (num / np.sqrt(var_h[i, best] * var_t)).astype(np.float32)
        self._finish_report(n)

    def _report_gpu(self, xp, n, sum_h, sum_t, sum_ht, var_h, var_t):
        """The same correlations on the GPU, all guesses of a byte at once (the device has the bandwidth the CPU path blocks for); only the per-guess maxima and the best guess's correlation trace come back to the host."""
        r = self.result
        for i, b in enumerate(self.bytes_to_attack):
            c = n * sum_ht[i] - sum_h[i, :, None] * sum_t[None, :]
            d = xp.sqrt(var_h[i, :, None] * var_t[None, :])
            c = xp.abs(c / d)
            c[~xp.isfinite(c)] = 0.0
            am = c.argmax(axis=1)
            mx = c[xp.arange(256), am]
            r.corr_max[b] = accel.to_host(mx)
            r.corr_argmax[b] = accel.to_host(am) + self.lo
            best = int(np.argmax(r.corr_max[b]))
            num = n * sum_ht[i, best] - sum_h[i, best] * sum_t
            r.best_corr_trace[b] = accel.to_host((num / xp.sqrt(var_h[i, best] * var_t)).astype(np.float32))

    def _finish_report(self, n):
        r = self.result
        pge = []
        for b in self.bytes_to_attack:
            if r.known_key is not None and len(r.known_key) > b:
                order = np.argsort(-r.corr_max[b])
                pge.append(int(np.where(order == int(r.known_key[b]))[0][0]))
        r.traces_used = int(n)
        r.elapsed = time.time() - r.started
        entry = {"traces": int(n), "best_key": bytes(int(np.argmax(r.corr_max[b])) for b in range(16)).hex(),
                 "max_corr": [float(r.corr_max[b].max()) for b in range(16)]}
        if pge:
            entry["pge"] = pge
        r.history.append(entry)
        if self.callback:
            self.callback(r)

