# Simulator

Studio includes a simulated ChipWhisperer scope and a simulated AES target, so you can learn, demonstrate and test every feature without any hardware. The simulated target leaks power like an unprotected software AES implementation and can be glitched, so CPA attacks and glitch sweeps behave realistically.

## How to use it

1. In the **Connect** tab, set **Device** to **Simulator (no hardware)**. The target protocol switches to **Simulated AES target**.
2. Click **Connect scope**. Studio connects both the simulated scope and the simulated target.

To have the simulator preselected every time, start Studio with `--simulate` (`cw-studio --simulate`, or `ChipWhispererStudio-simulator.bat` in the Windows bundle). The MCP server has the same option (`cw-studio mcp --simulate`), and scope connections use `kind: "sim"` in the [HTTP API](HTTP-API) and [MCP Server](MCP-Server).

In the top bar the scope appears as **ChipWhisperer-Simulator · SIM000001** and the target as **SimTarget**.

## What it is good for

- **Learning.** Follow the [Quick Start](Quick-Start), try the leakage models in CPA Analysis or find a glitch window in Glitching before touching real hardware.
- **Teaching and demos.** Every student can work on their own laptop even if there are only a few ChipWhisperers in the room.
- **Developing notebooks and scripts.** Write and debug a notebook, script or AI agent workflow against the simulator, then switch to real hardware.
- **Automated testing.** Studio's own test suite and CI use the simulator on Windows, macOS and Linux.

## What the simulated scope provides

The simulated scope implements the parts of the `chipwhisperer` scope API that Studio and typical notebooks use: `arm()`, `capture()`, `get_last_trace()`, `default_setup()`, `con()`, `dis()` and the settings objects below. Its serial number is `SIM000001` and it reports firmware version 0.1.0. Every setting appears in the [Scope tab](Scope-Settings) with a short description.

| Group | Settings (default) |
|-------|--------------------|
| gain | `mode` (high), `gain` (30, range 0 to 78), `db` (derived from gain and mode, read only in effect) |
| adc | `state` (read only), `basic_mode` (rising_edge; also falling_edge, low, high), `timeout` (2.0 s), `offset` (0), `presamples` (0), `samples` (5000), `decimate` (1), `trig_count` (read only) |
| clock | `adc_src` (clkgen_x4), `adc_freq` (4 x clkgen_freq, about 29.5 MHz), `adc_locked`, `clkgen_src` (system), `clkgen_freq` (7.37 MHz), `clkgen_locked`, `freq_ctr` |
| trigger | `triggers` (tio4), `module` (basic) |
| io | `tio1` (serial_rx), `tio2` (serial_tx), `tio3`, `tio4`, `pdid`, `pdic`, `nrst` (all high_z), `hs2` (clkgen), `target_pwr` (on), `glitch_hp`, `glitch_lp` (off) |
| glitch | `enabled` (off), `clk_src` (clkgen), `width` (0), `width_fine`, `offset` (0), `offset_fine`, `trigger_src` (manual), `arm_timing` (after_scope), `ext_offset` (0), `repeat` (1), `output` (clock_xor) |

`default_setup()` sets gain 30, 5000 samples, offset 0, rising edge trigger on TIO4, a 7.37 MHz clock with the ADC at 4x, serial on TIO1 and TIO2, and HS2 as clock output.

## How the traces are made

Each capture produces `adc.samples` samples (5000 by default) built from:

- A background of clock ripple, a slow envelope and random noise (standard deviation 0.02).
- **Key-dependent leakage for the first AES round.** For each of the 16 key bytes there is a short dip in the trace, starting at sample 60 and spaced 45 samples apart. Its depth depends on the Hamming weight of the S-box output `SBOX[plaintext[i] XOR key[i]]`. This is exactly what the **HW of SBox output** CPA model looks for, so CPA recovers the key from a few hundred traces.
- Nine more bumps for the remaining AES rounds, which look like activity but do not depend on the key.
- **Gain.** Traces scale with `gain.gain` and `gain.mode`, and are clipped to the range -0.5 to 0.5 like a real ADC. If you raise the gain too far the peaks clip, just as on real hardware.
- **Offset.** `adc.offset` shifts the trace window, so the leaks move left as you increase it.

The simulated target really computes AES-128 in software, so the ciphertext in each trace is correct and can be checked against the key.

## The simulated target

The simulated AES target understands the SimpleSerial commands of NewAE's example firmware:

| Command | What it does |
|---------|--------------|
| `k` + 16 bytes | Sets the AES key (default key `000102030405060708090a0b0c0d0e0f` until one is sent) |
| `p` + 16 bytes | Encrypts the plaintext, triggers a capture and replies with the 16 byte ciphertext |
| `g` | Runs a "glitch loop" like simpleserial-glitch and replies with a 4 byte loop counter (normally 2500, which is `c4090000` in little endian hex) |
| `x` | Counts as a reset |

Its settings (visible in the Target tab) are `output_len` (16), `baud` (38400), the last command sent and read, and `protver` (2.1). Programming firmware in the simulator only pretends: it waits half a second and reports the file size as written.

## Simulated glitches

The simulated target reacts to the scope's glitch settings when the glitch module is armed, that is when `glitch.trigger_src` is `ext_single` or `ext_continuous` and `glitch.repeat` is at least 1. Each glitch attempt then has one of three outcomes, depending on `glitch.width` (its absolute value) and `glitch.ext_offset`:

| Condition | Typical outcome |
|-----------|-----------------|
| Width below 1 | Normal (no effect) |
| Width above 45, or repeat above 20 | Reset (no response) |
| Width 5 to 40 and ext_offset 20 to 60 | Success about 75% of the time (a wrong but valid response), otherwise reset or normal |
| Width 35 to 45 outside the window | Reset about 30% of the time, otherwise normal |
| Anything else | Normal |

A success on the `g` command returns a loop counter slightly below 2500; a success on the `p` command returns a corrupted ciphertext. A sweep of `glitch.ext_offset` from 0 to 90 and `glitch.width` from -45 to 45 shows a clear success region. See Glitching for a walkthrough.

## Limitations

- The simulator models what Studio needs, not every register of a real scope. Settings that exist only on a Husky or Pro (streaming, the extra glitch and trigger modules, logic analyser) are not simulated.
- The leakage is idealised: one clear S-box leak per byte with Gaussian noise. Real devices have more complicated leakage, misalignment and noise, and usually need more traces.
- Only SimpleSerial AES and the glitch loop are simulated. Other firmware (ECC, RSA, password check, bootloaders) is not; programming any `.hex` "succeeds" without changing the simulated behaviour.
- The simulated target ignores `set_key` timing, baud rate and serial framing details.
- Notebook code that calls `cw.program_target()` on the simulator only prints a message; nothing is flashed.
