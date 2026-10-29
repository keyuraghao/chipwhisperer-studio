"""Differential fault analysis of AES-128 from the ciphertexts a glitch sweep collects.

A glitch that corrupts one byte of the state between the two last MixColumns (the input of round 9) spreads to four bytes of the ciphertext, one whole MixColumns column after the last ShiftRows. For each of those four ciphertext bytes the tenth round key byte k must satisfy InvSBox(c ^ k) ^ InvSBox(c' ^ k) = m * d, with m the MixColumns coefficient of the faulted row and d the unknown fault difference after SubBytes, the same d for all four bytes (Piret and Quisquater, CHES 2003). One correct and faulty pair leaves a few candidate 4-tuples per column; a second pair on the same column almost always leaves one. Four columns give the tenth round key, and the key schedule run backwards gives the master key.
"""
from __future__ import annotations

from itertools import product
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np

from cwstudio.aes import INV_SBOX, _SHIFT_ROWS, _mul, master_key_from_round_key

# MixColumns matrix: the difference a fault in row r of a column leaves on that column is column r of this matrix times d
_MC = ((2, 3, 1, 1), (1, 2, 3, 1), (1, 1, 2, 3), (3, 1, 1, 2))
# ciphertext position fed by each pre-ShiftRows state position (inverse of the encryption's ShiftRows map)
_OUT_OF = {p: j for j, p in enumerate(_SHIFT_ROWS)}
# the four ciphertext positions each MixColumns column lands on
COLUMNS = [tuple(_OUT_OF[4 * c + i] for i in range(4)) for c in range(4)]
_MUL_TABLE = {m: np.array([_mul(m, d) for d in range(256)], np.uint8) for m in (1, 2, 3)}
_ISB = INV_SBOX.astype(np.uint8)
_K = np.arange(256, dtype=np.uint8)


def column_of(correct: bytes, faulty: bytes) -> Optional[int]:
    """Which MixColumns column a faulty ciphertext differs in, or None when the difference is not exactly the four bytes of one column (no fault, a fault in another round, or more than one byte faulted)."""
    diff = {i for i in range(16) if correct[i] != faulty[i]}
    for c, pos in enumerate(COLUMNS):
        if diff == set(pos):
            return c
    return None


def candidates_for_pair(correct: bytes, faulty: bytes, column: int) -> Set[Tuple[int, ...]]:
    """Candidate (k_j0, k_j1, k_j2, k_j3) tenth round key tuples for the four ciphertext positions of ``column`` consistent with one correct and faulty pair."""
    pos = COLUMNS[column]
    # D[i][k] = InvSBox(c ^ k) ^ InvSBox(c' ^ k) for the four positions and every k
    D = [(_ISB[np.uint8(correct[j]) ^ _K] ^ _ISB[np.uint8(faulty[j]) ^ _K]) for j in pos]
    found: Set[Tuple[int, ...]] = set()
    for r in range(4):  # the faulted row of the column
        coef = [_MC[i][r] for i in range(4)]
        tables = [_MUL_TABLE[m] for m in coef]
        for d in range(1, 256):
            sets = []
            for i in range(4):
                ks = np.flatnonzero(D[i] == tables[i][d])
                if ks.size == 0:
                    break
                sets.append(ks.tolist())
            else:
                found.update(product(*sets))
    return found


def analyse(correct: bytes, faulty: Iterable[bytes]) -> Dict[str, Any]:
    """Combine every usable faulty ciphertext with the correct one. Returns per column the candidate count and the pairs used, the tenth round key and master key once every column is unique, and which faulty ciphertexts were unusable and why."""
    correct = bytes(correct)
    if len(correct) != 16:
        raise ValueError("the correct ciphertext must be 16 bytes")
    cands: List[Optional[Set[Tuple[int, ...]]]] = [None] * 4
    used = [0] * 4
    unusable: List[Dict[str, Any]] = []
    seen: Set[bytes] = set()
    for f in faulty:
        f = bytes(f)
        if len(f) != 16:
            unusable.append({"ciphertext": f.hex(), "why": f"{len(f)} bytes, not 16"})
            continue
        if f in seen:
            continue  # the same faulty ciphertext twice adds nothing
        seen.add(f)
        c = column_of(correct, f)
        if c is None:
            n = sum(1 for i in range(16) if correct[i] != f[i])
            unusable.append({"ciphertext": f.hex(), "why": "identical to the correct ciphertext" if n == 0 else f"differs in {n} byte{'s' if n != 1 else ''} that are not one MixColumns column (the fault did not land on one byte entering round 9)"})
            continue
        if cands[c] is not None and len(cands[c]) == 1:
            continue  # this column is solved; skip the work
        s = candidates_for_pair(correct, f, c)
        if not s:
            unusable.append({"ciphertext": f.hex(), "why": "no key is consistent with a single-byte round 9 fault"})
            continue
        cands[c] = s if cands[c] is None else (cands[c] & s)
        used[c] += 1
        if cands[c] is not None and not cands[c]:
            cands[c] = None  # contradictory pairs (a different fault model): start the column over with the next pair
            unusable.append({"ciphertext": f.hex(), "why": "contradicts the earlier pairs on this column; column reset"})
            used[c] = 0
    columns = []
    k10 = [None] * 16
    for c in range(4):
        s = cands[c]
        col: Dict[str, Any] = {"column": c, "positions": list(COLUMNS[c]), "pairs": used[c], "candidates": 0 if s is None else len(s)}
        if s is not None and len(s) == 1:
            tup = next(iter(s))
            for j, k in zip(COLUMNS[c], tup):
                k10[j] = int(k)
            col["key_bytes"] = {str(j): int(k) for j, k in zip(COLUMNS[c], tup)}
        elif s is not None and len(s) <= 16:
            col["options"] = [[int(x) for x in t] for t in sorted(s)]
        columns.append(col)
    out: Dict[str, Any] = {"columns": columns, "unusable": unusable, "faulty_used": sum(used), "solved_columns": sum(1 for c in columns if "key_bytes" in c)}
    if all(k is not None for k in k10):
        rk = bytes(k10)  # type: ignore[arg-type]
        out["round_key_10"] = rk.hex()
        out["key"] = master_key_from_round_key(rk).hex()
    else:
        out["round_key_10_partial"] = "".join("??" if k is None else f"{k:02x}" for k in k10)
    return out


def from_glitch_results(results: List[Dict[str, Any]], plaintext: Optional[bytes], expected: Optional[bytes]) -> Dict[str, Any]:
    """Run the analysis on a glitch sweep's results: the correct ciphertext is ``expected`` or else the most common valid 16-byte reply, the faulty ones every other valid 16-byte reply. ``plaintext`` is only reported (DFA does not need it, but it must have been the same for every attempt)."""
    replies: List[bytes] = []
    for r in results:
        h = r.get("response")
        if not h or r.get("result") in ("reset", "garbled", "not_fired"):
            continue
        try:
            b = bytes.fromhex(h)
        except ValueError:
            continue
        if len(b) == 16:
            replies.append(b)
    if not replies:
        raise ValueError("no valid 16-byte replies in the glitch results: sweep with command p, a 16-byte plaintext and output_len 16")
    if expected is not None and len(expected) == 16:
        correct = expected
    else:
        counts: Dict[bytes, int] = {}
        for b in replies:
            counts[b] = counts.get(b, 0) + 1
        correct = max(counts, key=counts.get)
    faulty = [b for b in replies if b != correct]
    out = analyse(correct, faulty)
    out.update({"correct": correct.hex(), "plaintext": plaintext.hex() if plaintext else None, "replies": len(replies), "faulty": len(faulty)})
    return out
