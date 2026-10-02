# Simulator

Studio includes a simulated ChipWhisperer scope and a simulated target, so you can learn, demonstrate and test every feature without any hardware. The simulated target leaks power like an unprotected software AES implementation and can be glitched, so CPA attacks and glitch sweeps behave realistically. The simulator can pose as any ChipWhisperer model, and it can run real firmware: program an ELF and the simulated target executes it in an emulator.

## How to use it

1. In the **Connect** tab, set **Device** to **Simulator (no hardware)**. The target protocol switches to **Simulated AES target**.
2. Under **Simulate as**, choose the ChipWhisperer the simulator stands in for (Husky by default; see [below](#simulate-as)).
3. Click **Connect scope**. Studio connects both the simulated scope and the simulated target.

To have the simulator preselected every time, start Studio with `--simulate` (`cw-studio --simulate`, or `ChipWhispererStudio-simulator.bat` in the Windows bundle). The MCP server has the same option (`cw-studio mcp --simulate`), and scope connections use `kind: "sim"` (with an optional `sim_model`) in the [HTTP API](HTTP-API) and [MCP Server](MCP-Server).

In the top bar the scope appears as **ChipWhisperer-Simulator (Husky) · SIM000001** (with the model you chose) and the target as **SimTarget**.

## What it is good for

- **Learning.** Follow the [Quick Start](Quick-Start), try the leakage models in the **Analysis** tab or find a glitch window in the **Glitch** tab before touching real hardware.
- **Choosing hardware.** See what a Nano, Lite, Pro, Husky or Husky Plus can do in the [Interfaces](Protocols-and-Interfaces) and [Logic](Logic-Analyser) tabs before you buy or borrow one.
- **Teaching and demos.** Every student can work on their own laptop even if there are only a few ChipWhisperers in the room.
- **Developing firmware, notebooks and scripts.** Run your own firmware in the emulator, write and debug a notebook, script or AI agent workflow against the simulator, then switch to real hardware.
- **Automated testing.** Studio's own test suite and CI use the simulator on Windows, macOS and Linux.

## Simulate as

**Simulate as** makes the simulator behave like a ChipWhisperer-Husky, Husky Plus, Pro, Lite or Nano. Studio then offers exactly what that model supports: the UART pins, triggers, programmers, SPI, GPIO, USERIO and bit-banger sections of the [Interfaces](Protocols-and-Interfaces) tab, the logic analyser sources, the trigger modules and pin modes in the Scope tab, and the programmers in the Target tab. Model-specific settings follow too: only the Husky models have `clock.adc_mul` and a built-in logic analyser. The choice is remembered, and connecting again with another model switches without a disconnect.

What the simulated interfaces do (an SPI flash, GPIO levels, USERIO, the bit-banger and a 1-Wire device) is described on [Protocols and Interfaces](Protocols-and-Interfaces#simulate-as). JTAG and SWD through OpenOCD need real hardware.

## What the simulated scope provides

The simulated scope implements the parts of the `chipwhisperer` scope API that Studio and typical notebooks use: `arm()`, `capture()`, `get_last_trace()`, `default_setup()`, `con()`, `dis()` and the settings objects below. Its serial number is `SIM000001` and it reports firmware version 0.1.0. Every setting appears in the [Scope tab](Scope-Settings) with a short description.

| Group | Settings (default) |
|-------|--------------------|
| gain | `mode` (high), `gain` (30, range 0 to 78), `db` (derived from gain and mode, read only in effect) |
| adc | `state` (read only), `basic_mode` (rising_edge; also falling_edge, low, high), `timeout` (2.0 s), `offset` (0), `presamples` (0), `samples` (5000), `decimate` (1), `trig_count` (read only) |
| clock | `adc_src` (clkgen_x4; also clkgen_x1, extclk_x4, extclk_x1, extclk_dir), `adc_mul` (4, 1 to 16, Husky models only), `adc_freq` (about 29.5 MHz), `adc_locked`, `clkgen_src` (system), `clkgen_freq` (7.37 MHz), `clkgen_locked`, `freq_ctr` |
| trigger | `triggers` (tio4), `module` (basic) |
| io | `tio1` (serial_rx), `tio2` (serial_tx), `tio3`, `tio4`, `pdid`, `pdic`, `nrst` (all high_z), `hs2` (clkgen), `target_pwr` (on), `glitch_hp`, `glitch_lp` (off) |
| glitch | `enabled` (off), `clk_src` (clkgen), `width` (0), `width_fine`, `offset` (0), `offset_fine`, `trigger_src` (manual), `arm_timing` (after_scope), `ext_offset` (0), `repeat` (1), `output` (clock_xor) |

`default_setup()` sets gain 30, 5000 samples, offset 0, rising edge trigger on TIO4, a 7.37 MHz clock with the ADC at 4x, serial on TIO1 and TIO2, and HS2 as clock output.

**Clocks.** The ADC samples at the target clock times a multiplier: `adc_src` sets it on the CW-Lite and Pro models (x4 or x1), `adc_mul` on the Husky models. Traces follow it like on hardware: at 1 sample per cycle the leaks move closer together, at 8 they spread out. The default stays 4 samples per target clock.

**Trigger pins.** The simulated target raises its trigger on TIO4, as ChipWhisperer firmware does. A capture fires only when `trigger.triggers` includes `tio4` (combined with OR, or with AND together with TIO1 or TIO2, which idle high); a trigger on any other pin times out exactly as it would on real hardware. The special triggers (the Husky's UART pattern rules, the Pro's UART decode trigger, the edge counter, the ADC level, SAD and the trigger sequencer) are evaluated on what the simulated target really does: its serial traffic (it receives on TIO2 and sends on TIO1 at its baud rate, parity and stop bits) and its trigger pin, laid out in time like on the wire, so the capture starts where the trigger would fire on hardware. The default UART trigger (TIO1, pattern `'r'`) fires on the target's response. A trigger that never fires times out, with a log message saying why. SAD needs a captured trace first (its reference is cut from it), and with the default threshold of 10 it usually times out in the simulator because of the noise; raise the threshold. Only the Arm trace and bit-banger triggers are not simulated: they fall back to the TIO4 edge, with a warning in the log.

## How the traces are made

With the built-in AES model, each capture produces `adc.samples` samples (5000 by default) built from:

- A background of clock ripple, a slow envelope and random noise (standard deviation 0.02).
- **Key-dependent leakage for the first AES round.** For each of the 16 key bytes there is a short dip in the trace, starting at sample 60 and spaced 45 samples apart (at 4 samples per cycle). Its depth depends on the Hamming weight of the S-box output `SBOX[plaintext[i] XOR key[i]]`. This is exactly what the **HW of SBox output** CPA model looks for, so CPA recovers the key from a few hundred traces.
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

Its settings (visible in the Target tab) are `output_len` (16), `baud` (38400), the last command sent and read, and `protver` (2.1).

## Running your own firmware

Programming firmware into the simulator runs it. Program an ELF (or a `.hex` that Studio built, which keeps its `.elf` next to it) from the Target tab, the Firmware tab's **Build & program**, a notebook or an agent, and from then on the simulated target executes that firmware in an emulator:

- **Processors:** Arm Cortex-M and RISC-V RV32 (through the Unicorn engine) and AVR/XMEGA (Studio's own cycle-accurate emulator). This covers simpleserial firmware built for CWLITEARM, CWNANO, CWHUSKY, the STM32 and SAM4S boards, CWLITEXMEGA, CW304, NEORV32 and others.
- **Responses** come from the firmware itself: the AES implementation you built, a password check, simpleserial-glitch's loop counter, or anything else that talks SimpleSerial. Raw serial input from the terminal goes straight to the firmware.
- **Traces** are made from the emulated execution: each instruction's power depends on the data it loads, stores or writes, placed at the scope's ADC rate within the firmware's own trigger window (`trigger_high` to `trigger_low`), with the scope's gain, offset, decimation and noise. CPA recovers the key from them, so you can attack your own build of a cipher.
- **Glitches** make the emulated core skip instructions (see below).
- The [code map](Code-on-the-Waveform) built from the same ELF lines up with these traces exactly.

The emulator does not model peripherals, interrupts or UART timing; Studio hooks the HAL's `getch`, `putch` and trigger functions instead (see [Code on the Waveform](Code-on-the-Waveform#how-the-emulation-works)). The Target tab's toast and the log say which firmware the simulator runs, for example *Simulator runs simpleserial-aes-CWLITEARM-clang.elf (arm, Arm Cortex-M4, SimpleSerial 2.1) in the emulator*. Programming a file without an ELF (or one the emulator cannot run) switches back to the built-in AES model, and the reason is shown.

## Simulated glitches

The simulated target reacts to the scope's glitch settings when the glitch module is armed, that is when `glitch.trigger_src` is `ext_single` or `ext_continuous` and `glitch.repeat` is at least 1. Each glitch attempt then has one of three outcomes, depending on `glitch.width` (its absolute value) and `glitch.ext_offset`:

| Condition | Typical outcome |
|-----------|-----------------|
| Width below 1 | Normal (no effect) |
| Width above 45, or repeat above 20 | Reset (no response) |
| Width 5 to 40 and ext_offset 20 to 60 | Success about 75% of the time (a wrong but valid response), otherwise reset or normal |
| Width 35 to 45, when not a success case above | Reset about 30% of the time, otherwise normal |
| Anything else | Normal |

With the built-in model, a success on the `g` command returns a loop counter slightly below 2500 and a success on the `p` command returns a corrupted ciphertext. A sweep of `glitch.ext_offset` from 0 to 90 and `glitch.width` from -45 to 45 shows a clear success region in the **Glitch** tab. On the `p` command the reset outcome is not modelled: the target still answers with the ciphertext.

**With emulated firmware**, a glitch is an instruction skip, the most common effect of clock and voltage glitches on real microcontrollers. The width decides the outcome as above (between 5 and 40: a skip about 75% of the time, a crash 10%, no effect 15%; too wide: a crash), and `glitch.ext_offset` is the number of target clock cycles after the trigger where it lands: the instruction executing at that cycle is skipped (more with a larger `repeat`). Whether that changes the answer depends on the firmware, so a sweep finds the real vulnerable instructions of simpleserial-glitch or a password check. A skip that makes the firmware fault or hang counts as a reset.

## Logic analyser

A simulated Husky or Husky Plus has a simulated built-in logic analyser, and every model offers the **Simulator demo traffic** source with UART, SPI, I2C, 1-Wire, CAN, JTAG and SWD signals. The analog-input source can also show any demo line on the simulated measure input. See [Logic Analyser](Logic-Analyser).

## Limitations

- The simulator models what Studio needs, not every register of a real scope. Settings that exist only on a Husky or Pro (streaming, the extra glitch modules) are not simulated, and the Arm trace and bit-banger triggers fall back to the TIO4 edge (see [Trigger pins](#what-the-simulated-scope-provides)).
- The built-in leakage is idealised: one clear S-box leak per byte with Gaussian noise. Real devices have more complicated leakage, misalignment and noise, and usually need more traces.
- The built-in model only simulates SimpleSerial AES and the glitch loop. Other firmware (ECC, RSA, password check, bootloaders) needs its ELF programmed so the emulator runs it.
- Emulated firmware runs each SimpleSerial command from the state right after start-up (with the key applied), so firmware that keeps state between commands behaves as if freshly reset; raw serial input keeps its state. The emulator settings are automatic (core, protocol, no flash wait states).
- The simulated target ignores baud rate and serial framing details.
- `cw.program_target()` in a notebook programs the simulated target like the Target tab does; without an ELF it only checks that the file exists.
- OpenOCD (JTAG and SWD) needs real hardware. SimpleSerial "v2 over CDC" works in the simulator, answered by the simulated target.
