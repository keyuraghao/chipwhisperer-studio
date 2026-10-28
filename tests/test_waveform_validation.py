"""What a correct simulated waveform is, checked against the generators Studio has (its own numpy code in simulator.py, codemap/power.py and logic/synth.py; no third-party signal library is involved).

Correct means: the trace has exactly the samples the ADC settings ask for, within the ADC's range, deterministic for a seed; the AES leakage sits where the model says with the documented amplitude so a CPA recovers the key; adc.offset, adc_mul and the gain act like on the hardware; the fast and slow synthesis paths agree; the emulated-firmware trace aligns at the nominal mapping; protocol lines are rendered at the exact bit times; and a trace costs well under a millisecond.
"""
import time

import numpy as np
import pytest

from cwstudio.aes import HW, SBOX
from cwstudio.analysis import CPAAttack
from cwstudio.simulator import SimScope, SimTarget

KEY = bytes(range(16))
PT = bytes([0x11] * 16)


def scope(seed=1, **kw):
    s = SimScope(seed=seed, **kw)
    s.default_setup()
    return s


def gain_scale(s):
    return (0.6 + s.gain.gain / 78.0) * (1.4 if s.gain.mode == "high" else 0.7)


def test_shape_dtype_range_and_determinism():
    a, b = scope(seed=7), scope(seed=7)
    for n in (1, 100, 5000, 24400):
        a.adc.samples = b.adc.samples = n
        wa, wb = a._synth(PT, KEY), b._synth(PT, KEY)
        assert wa.shape == (n,) and wa.dtype == np.float32
        assert wa.min() >= -0.5 and wa.max() <= 0.5  # the ADC's full scale, like scope.get_last_trace() on hardware
        assert np.array_equal(wa, wb)  # same seed, same settings, same trace
    assert not np.array_equal(scope(seed=1)._synth(PT, KEY), scope(seed=2)._synth(PT, KEY))


def test_leakage_amplitude_and_position_match_the_model():
    """The S-box leak of byte b is a Gaussian dip of width 6 centred at leak_start + b * leak_spacing whose depth is leak_amplitude * (HW - 4) + 0.15, times the gain; measured as the difference between HW=8 and HW=0 inputs with the noise turned off."""
    s = scope()
    s.noise = 0.0
    hw8 = next(x for x in range(256) if HW[SBOX[x]] == 8)
    hw0 = next(x for x in range(256) if HW[SBOX[x]] == 0)
    for b in (0, 5, 15):
        pt8, pt0 = bytearray(16), bytearray(16)
        pt8[b], pt0[b] = hw8 ^ KEY[b], hw0 ^ KEY[b]
        d = s._synth(bytes(pt0), KEY) - s._synth(bytes(pt8), KEY)  # HW 0 sits higher than HW 8 (current drop on the shunt)
        c = s.leak_start + b * s.leak_spacing
        assert int(np.argmax(d)) == c
        expected = s.leak_amplitude * 8 * gain_scale(s)
        assert d[c] == pytest.approx(expected, rel=1e-3)
        assert d[c + 3 * 6 + 1] == 0 and d[c - 3 * 6 - 1] == 0  # the bump ends 3 widths out
        assert abs(d[c + 6]) == pytest.approx(expected * np.exp(-0.5), rel=1e-2)


def test_cpa_recovers_the_key_from_simulated_traces():
    s, t = scope(seed=3), SimTarget()
    t.con(s)
    s.adc.samples = 1200
    rng = np.random.default_rng(0)
    W, tin = [], []
    for _ in range(80):
        pt = rng.integers(0, 256, 16, dtype=np.uint8).tobytes()
        t.simpleserial_write("p", bytearray(pt))
        s.arm(); s.capture()
        W.append(s.get_last_trace()); tin.append(np.frombuffer(pt, np.uint8))
    cpa = CPAAttack(np.array(W), np.array(tin), np.zeros((80, 16), np.uint8), model="sbox_hw")
    cpa.start(); cpa._thread.join(60)
    assert cpa.result.to_json()["best_key"] == KEY.hex()


def test_adc_offset_shifts_and_adc_mul_stretches_like_hardware():
    s = scope()
    s.noise = 0.0
    base = s._synth(PT, KEY)
    s.adc.offset = 40
    shifted = s._synth(PT, KEY)
    # with offset 40 the trace starts 40 samples later: the leaks move left by 40 (the background is regenerated at the new phase, so compare the leak dips only)
    c = s.leak_start + 3 * s.leak_spacing
    assert np.argmin(shifted[c - 40 - 10:c - 40 + 10]) == 10 and np.argmin(base[c - 10:c + 10]) == 10
    s.adc.offset = 0
    s.clock.adc_mul = 2
    hw8 = next(x for x in range(256) if HW[SBOX[x]] == 8)
    hw0 = next(x for x in range(256) if HW[SBOX[x]] == 0)
    pt = bytes([hw8 ^ KEY[0]] + [hw0 ^ KEY[b] for b in range(1, 16)])  # byte 0 leaks most, the rest least
    half = s._synth(pt, KEY)
    assert s._leak_pos() == (30, 22)  # x2 instead of x4: half the samples per cycle, every position halves (hardware would too: the leak is at a fixed time)
    assert np.argmin(half[0:60]) == 30
    assert s._leak_width() == 3  # and so does the width of the dip
    # a dip lasts the same number of target clocks at every ADC clock: the x2 dip is the x4 dip squeezed by two
    s.clock.adc_mul = 4
    full = s._synth(pt, KEY)
    assert full[60 - 6] - full[60] == pytest.approx(half[30 - 3] - half[30], abs=0.01)


def test_gain_scales_the_whole_trace():
    lo, hi = scope(seed=5), scope(seed=5)
    lo.noise = hi.noise = 0.0
    lo.gain.gain, hi.gain.gain = 0, 78
    lo.gain.mode = hi.gain.mode = "low"
    a, b = lo._synth(PT, KEY), hi._synth(PT, KEY)
    assert np.allclose(b, a * (gain_scale(hi) / gain_scale(lo)), atol=2e-6)


def test_fast_and_slow_synthesis_paths_agree():
    """A custom leak_spacing that makes byte windows overlap takes the per-byte loop; the vectorised path must give the same samples for any geometry (both are run here on a geometry both can do)."""
    s = scope(seed=9)
    s.noise = 0.0
    fast = s._synth(PT, KEY)
    s._geo_cache = (("never",), None)  # force the slow path
    geo = s._leak_geometry
    s._leak_geometry = lambda n, offset: None
    slow = s._synth(PT, KEY)
    s._leak_geometry = geo
    assert np.allclose(fast, slow, atol=1e-6)


def test_noise_floor_matches_the_setting():
    s = scope(seed=11)
    s.adc.samples = 20000
    s.noise = 0.02
    w = s._synth(PT, KEY)
    quiet = scope(seed=11)
    quiet.adc.samples = 20000
    quiet.noise = 0.0
    resid = w - quiet._synth(PT, KEY)  # what is left is the noise alone, scaled by the gain like the signal
    assert np.std(resid) == pytest.approx(0.02 * gain_scale(s), rel=0.05)
    assert abs(np.mean(resid)) < 0.001


def test_integer_samples_match_the_model_adc_resolution():
    """scope.get_last_trace(as_int=True) on hardware returns raw ADC codes: 10 bits on the Lite and Pro, 8 on the Nano, 12 on the Husky. The simulator must scale the same way for each model."""
    bits = {"lite": 10, "pro": 10, "nano": 8, "husky": 12, "huskyplus": 12}  # chipwhisperer's get_last_trace docstring: 10-bit Lite and Pro, 8-bit Nano, 12-bit Husky
    for model, b in bits.items():
        s = scope(sim_model=model)
        w = s._synth(PT, KEY)
        s._last_trace = w
        ints = s.get_last_trace(as_int=True)
        # hardware: float = code / 2**bits - 0.5 (OpenADC fp_offset, cwnano / 256.0 - 0.5), codes unsigned
        assert ints.min() >= 0 and ints.max() < (1 << b), f"{model}: as_int should give {b}-bit codes"
        assert np.allclose(ints / float(1 << b) - 0.5, w, atol=0.5 / (1 << b) + 1e-6), model


def test_synthesis_speed():
    s = scope()
    s.adc.samples = 5000
    s._synth(PT, KEY)
    t0 = time.perf_counter()
    for _ in range(200):
        s._synth(PT, KEY)
    per = (time.perf_counter() - t0) / 200
    assert per < 2e-3, f"{per * 1e3:.2f} ms per 5000-sample trace"


# ----- emulated firmware traces (codemap/power.py) ---------------------------------------------------
def test_firmware_trace_aligns_at_the_nominal_mapping():
    from cwstudio.codemap import power
    rng = np.random.default_rng(1)
    P = 1.0 + 0.5 * (rng.random(3000) < 0.3) + rng.random(3000) * 0.3
    m = power.Mapping(spc=4.0)
    traces = [power.synth_trace(P, 200, m, 6000, rng, noise=0.02) for _ in range(10)]
    for w in traces:
        assert w.dtype == np.float32 and w.shape == (6000,) and w.min() >= -0.5 and w.max() <= 0.5
    fit = power.align(np.mean(traces, axis=0), P, 200, m)
    assert abs(fit["shift"]) < 0.6 and abs(fit["scale"] - 1.0) < 0.005 and fit["abs_r"] > 0.8


def test_firmware_trace_cycle_power_shows_in_the_samples():
    """Each clock cycle's power is a pulse right after its edge: the mean over a cycle's 4 samples follows P (through a constant), so heavier instructions give deeper dips."""
    from cwstudio.codemap import power
    P = np.array([1.0] * 100 + [2.0] * 100 + [1.0] * 100)
    m = power.Mapping(spc=4.0)
    w = power.synth_trace(P, 0, m, 1200, np.random.default_rng(0), noise=0.0)
    per_cycle = w.reshape(300, 4).mean(axis=1)
    assert per_cycle[:100].std() < 1e-6 and per_cycle[100:200].std() < 1e-6
    assert per_cycle[150] < per_cycle[50]  # more current, lower measured voltage
    assert (per_cycle[50] - per_cycle[150]) == pytest.approx(0.03 * 1.0 * (0.35 + 2.6 * np.exp(-np.arange(4) / 4 * 6.0)).mean(), rel=1e-3)


# ----- protocol lines (logic/synth.py) -------------------------------------------------------------
def bits_from_edges(init, edges, n):
    b = np.full(n, init, np.int8)
    for e in edges:
        b[e:] = 1 - b[e - 1] if e > 0 else 1 - init
    return b


def test_uart_bits_land_on_the_exact_bit_times():
    from cwstudio.logic import synth
    ln = synth.Line(1)
    synth.uart(ln, 10e-6, [0x55], 115200)
    sr = 100e6
    init, edges = synth.render({"tx": ln}, sr, 0.0, 20000)["tx"]
    bits = bits_from_edges(init, edges, 20000)
    bt = sr / 115200
    # start bit at 10 us, then 0x55 LSB first (1,0,1,0,1,0,1,0), then stop
    for k, expect in enumerate([0, 1, 0, 1, 0, 1, 0, 1, 0, 1]):
        i0, i1 = int(np.ceil(1000 + k * bt)), int(np.floor(1000 + (k + 1) * bt)) - 1
        assert bits[i0:i1 + 1].min() == bits[i0:i1 + 1].max() == expect, f"bit {k}"


def test_clock_render_paths_agree():
    """A clock rendered edge by edge (few edges) and by direct sampling (many edges) must give the same bits for the same signal."""
    from cwstudio.logic import synth
    sr, n = 20e6, 4000
    c = synth.Clock(1e6, phase=0.3e-6)
    init, edges = synth._render_clock(c, sr, 0.0, n)  # 200 periods, 400 edges < n/2: the edge path
    a = bits_from_edges(init, edges, n)
    t = np.arange(n) / sr
    # a sample sees every edge at or before its instant; the phase in periods is rounded so an edge exactly on a sample is not lost to floating point
    level = lambda ck: (np.mod(np.round((t - ck.phase) * ck.freq, 9), 1.0) < ck.duty).astype(np.int8)  # noqa: E731
    assert np.array_equal(a, level(c))
    c2 = synth.Clock(6e6, phase=0.3e-6)  # 2400 edges > n/2: the sampling path
    init, edges = synth._render_clock(c2, sr, 0.0, n)
    b = bits_from_edges(init, edges, n)
    assert np.array_equal(b, level(c2))
