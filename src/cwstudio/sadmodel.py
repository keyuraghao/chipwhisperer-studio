"""The SAD trigger as the hardware scores it, run offline on stored traces: shows where a reference would fire before the scope is armed.

Two engines: the ChipWhisperer-Pro sums absolute differences over 128 samples of 10 bits and fires when the sum is under the threshold; the Husky (ESAD with interval matching) compares 256 samples (512 in extended mode) at 8 bits, counts the samples whose difference exceeds the interval threshold, and fires when that count is at or under the threshold (0 to 127). A per-sample mask (enabled_samples) leaves samples out of the count on the Husky.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

ENGINES = {
    "pro": {"samples": 128, "bits": 10, "mode": "sum", "threshold_max": 100000, "interval": False, "mask": False, "emode": False},
    "husky": {"samples": 256, "samples_emode": 512, "bits": 8, "mode": "interval", "threshold_max": 127, "interval": True, "mask": True, "emode": True},
}
ENGINES["huskyplus"] = ENGINES["husky"]


def engine(model: Optional[str]) -> Optional[Dict[str, Any]]:
    return ENGINES.get(model or "")


def reference_length(model: Optional[str], emode: bool = False) -> int:
    e = engine(model) or ENGINES["husky"]
    return int(e["samples_emode"] if emode and e.get("emode") else e["samples"])


def parse_mask(text: Optional[str], n: int) -> Optional[List[bool]]:
    """``enabled_samples`` from ranges such as "0-99, 150-255" (inclusive, within the reference): None for no mask (every sample counts)."""
    if text is None or not str(text).strip():
        return None
    mask = [False] * n
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        if lo < 0 or hi >= n or lo > hi:
            raise ValueError(f"mask range {part} is outside the {n}-sample reference")
        for i in range(lo, hi + 1):
            mask[i] = True
    return mask


def quantize(x: np.ndarray, bits: int) -> np.ndarray:
    return np.floor((np.clip(np.asarray(x, np.float64), -0.5, 0.5 - 1e-9) + 0.5) * (1 << bits))


def scores(wave: np.ndarray, ref: np.ndarray, model: Optional[str], interval: int = 10, mask: Optional[List[bool]] = None) -> np.ndarray:
    """The score at every position of ``ref`` sliding over ``wave`` (lower is a better match): the sum of absolute differences on the Pro, the count of samples over the interval on the Husky."""
    e = engine(model) or ENGINES["husky"]
    n = int(ref.size)
    w = quantize(wave, e["bits"])
    r = quantize(ref, e["bits"])
    if n == 0 or w.size < n:
        return np.zeros(0, np.float64)
    win = np.lib.stride_tricks.sliding_window_view(w, n)
    diff = np.abs(win - r)
    if e["mode"] == "sum":
        return diff.sum(axis=1)
    over = diff > max(0, int(interval))
    if mask is not None and len(mask) == n:
        over = over & np.asarray(mask, bool)[None, :]
    return over.sum(axis=1).astype(np.float64)


def first_hit(score: np.ndarray, threshold: int) -> Optional[int]:
    """Where the trigger fires: the best position of the first run of positions at or under the threshold, as the hardware's hold-off does (None when it never fires)."""
    under = np.flatnonzero(score <= threshold)
    if not under.size:
        return None
    i0 = int(under[0])
    i1 = i0
    while i1 + 1 < score.size and score[i1 + 1] <= threshold:
        i1 += 1
    return i0 + int(np.argmin(score[i0:i1 + 1]))


def preview(waves: List[np.ndarray], ref: np.ndarray, model: Optional[str], threshold: int, interval: int = 10, mask: Optional[List[bool]] = None, expected: Optional[int] = None) -> Dict[str, Any]:
    """Run the reference over every trace: per trace the best score and where it would fire; overall how many traces fire, how many fire at the expected position (within 2 samples) and the score spread, so the threshold can be chosen with the traces in hand."""
    rows = []
    fires = on_time = 0
    mins: List[float] = []
    for i, w in enumerate(waves):
        sc = scores(np.asarray(w, np.float64), ref, model, interval, mask)
        if not sc.size:
            rows.append({"trace": i, "fires": False, "at": None, "min": None})
            continue
        hit = first_hit(sc, threshold)
        best = float(sc.min())
        mins.append(best)
        rows.append({"trace": i, "fires": hit is not None, "at": None if hit is None else int(hit + ref.size), "min": best, "argmin": int(sc.argmin())})
        if hit is not None:
            fires += 1
            if expected is not None and abs(hit - expected) <= 2:
                on_time += 1
    e = engine(model) or ENGINES["husky"]
    out = {"traces": len(waves), "fires": fires, "on_time": on_time if expected is not None else None, "threshold": threshold, "mode": e["mode"], "reference_samples": int(ref.size), "rows": rows}
    if mins:
        out["score_min"], out["score_median"], out["score_max"] = float(min(mins)), float(np.median(mins)), float(max(mins))
        # a threshold that would catch every trace at its best position, and one that would leave the worst trace out: the two ends of the useful range
        out["suggested_threshold"] = int(np.ceil(max(mins)))
    return out
