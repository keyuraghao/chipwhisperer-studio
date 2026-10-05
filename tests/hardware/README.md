# Real-hardware test run (hw/husky)

This branch carries the staged real-hardware session in `test_session.py` plus the notes from the actual run on a physical rig. The session is skipped unless `CWSTUDIO_HW` is set, so a normal `pytest` ignores it.

## The rig that was tested

- Scope: ChipWhisperer-Husky, serial `...17003`.
- Target: CW308 UFO board with a Microchip SAM4S (`CW308_SAM4S`).
- Target firmware: SimpleSerial v1 at 38400 baud (not v2). A v2 connection to this firmware just times out with "10 consecutive timeouts"; ping `v` at 38400 to confirm the version before blaming the trigger.
- Host: Linux (Kali), NewAE udev rules installed, user in the `plugdev` and `chipwhisperer` groups, driven through Studio's HTTP API on `127.0.0.1:8765`.

## One-time firmware upgrade

The Husky shipped with scope firmware 1.5.0, which is too old for `chipwhisperer` 6.0.0. It must be upgraded once before it will connect:

```python
import chipwhisperer as cw
cw.scope().upgrade_firmware()   # flashes the SAM3U, reports "Upgrade successful"
```

Then unplug and replug the scope. After this the Husky reports firmware 1.7.0 and connects normally.

## How to run the staged session

This exact rig (SAM4S running SimpleSerial v1) runs with:

```bash
source ../../.venv/bin/activate            # the project venv
CWSTUDIO_HW=husky \
CWSTUDIO_HW_PLATFORM=CW308_SAM4S \
CWSTUDIO_HW_TARGET=SimpleSerial \
pytest tests/hardware -v -s
```

Set `CWSTUDIO_HW=sim` to run the same stages against the simulator with no hardware. Other knobs (see `conftest.py`): `CWSTUDIO_HW_SN`, `CWSTUDIO_HW_PROGRAMMER`, `CWSTUDIO_HW_SKIP_PROGRAM=1`, `CWSTUDIO_HW_MANUAL=1`, `CWSTUDIO_HW_DATA`.

Stages run in order and a failed stage skips the ones that depend on it: environment, discovery, scope, firmware, target, capture, analysis, glitch, husky. The last run's per-stage results are written to `last_run.json`.

### SimpleSerial version

`CWSTUDIO_HW_TARGET` selects the target kind (default `SimpleSerial2`). The session derives everything else from it: the target connect kind, the `ss_ver` the AES firmware is built with (`SimpleSerial` builds `SS_VER_1_1`, `SimpleSerial2` builds `SS_VER_2_1`), and the protocol the codemap emulator frames. So `CWSTUDIO_HW_TARGET=SimpleSerial` is all that is needed to drive a v1 board end to end. A v2 connection to v1 firmware just times out with "10 consecutive timeouts"; if that happens, check the firmware's SimpleSerial version (ping `v` at 38400) and set this knob to match.

## Results from the actual run (2026-10-05)

Captured by driving the HTTP API directly, cross-checked against the staged stages.

Scope setup: `clock.clkgen_freq=7.37 MHz`, `clock.adc_mul=4` (ADC 29.45 MHz, MMCM locked), `adc.samples=5000`, `gain.db=25`, trigger on `tio4`.

- Target sanity: AES-128 of `6bc1bee22e409f96e93d7e117393172a` under the test key returned `3ad77bb40d7a3660a89ecaf32466ef97`, the NIST ECB vector. Correct.
- CPA key recovery (`simpleserial-aes`, model `sbox_hw`): 60 traces at about 30 traces/s recovered the full AES-128 key `2b7e151628aed2a6abf7158809cf4f3c`. All 16 bytes at PGE 0, best-guess correlation 0.61 to 0.78.
- Clock glitching (`simpleserial-glitch`, `g` = 50x50 loop, baseline count 2500): ineffective on the SAM4S because its core runs from an internal PLL that filters a glitched input clock. 945 shots gave 939 normal, 6 reset, 0 success.
- Voltage (crowbar) glitching (`glitch.output=glitch_only`, `io.glitch_lp=True`): works. The reliable point is `ext_offset=1700, offset=2800, width=1550, repeat=2`, which corrupts the loop counter from 2500 to 2425 about 80 to 90 percent of the time (9/10 on the sweep, 8/12 on a fresh repro). A slightly wider `width=1580` at the same point faults harder (count 1325). Full sweep data is in the Studio data folder as `husky_sam4s_glitch_results.csv`.

## Safety note left for the next run

Glitching was disabled at the end: `glitch.enabled=False`, both crowbar MOSFETs off (`io.glitch_lp=False`, `io.glitch_hp=False`), and `io.hs2` restored to `clkgen`. The target currently holds the glitch firmware, so a fresh CPA run needs `simpleserial-aes` reflashed first. Crowbar sweeps short the target rail, so keep them short and start from narrow widths.
