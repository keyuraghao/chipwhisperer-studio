# ChipWhisperer Studio workflows

Each recipe is a tested order of MCP tool calls. Tool names are shown without the client's namespace prefix. Start every session with `studio_status`.

## 1. Connect

| Situation | Calls |
|-----------|-------|
| Simulator | `scope_connect(kind="sim")`, then `target_connect(kind="sim")`. `sim_model` (husky, huskyplus, pro, lite, nano; default husky) decides which interfaces, triggers and programmers are offered. |
| One board attached | `list_devices`, `scope_connect(kind="auto")` (applies `default_setup`), `target_connect(kind="SimpleSerial2")`. |
| Several boards | `scope_connect(sn="...")` with the serial number from `list_devices`. |
| "Device in use" | Another program left it open: `scope_connect(force=true)`. |
| Legacy firmware | `target_connect(kind="SimpleSerial")` (v1), or `simpleserial_connect(version="1.1")`. |
| CW305 FPGA target | `target_connect(kind="CW305", options={"bitfile": "/path/file.bit"})`. |

Check the target answers before capturing: `simpleserial(cmd="p", data="00"*16, read_len=16)` should return 16 bytes. On real hardware an empty reply usually means wrong firmware, wrong SimpleSerial version or wrong baud (`target_get_settings(filter="baud")`).

## 2. Build and flash firmware (real hardware)

1. `firmware_catalogue`: if `sources.valid` is false, run `firmware_fetch_sources(wait=true)` (network, about 100 MB; ask first). Pick `platform` from the catalogue (for example CWLITEARM for the CW-Lite Arm, CWNANO for the CW-Nano, CWLITEXMEGA for XMEGA, CW308_SAM4S for SAM4S targets). Ask the user which target board they have when it is not obvious.
2. `toolchains_list`: the GCC toolchain for the platform's architecture must be installed (`arm-gcc`, `avr-gcc` or `riscv-gcc`). If not, ask, then `toolchain_install(toolchain_id, wait=true)`. Clang builds (`compiler="clang"`) need the `clang` toolchain **and** the GCC one (GCC links). Windows also needs `win-build-tools` for make.
3. `firmware_plan(project, platform)` to show what will run (optional, no side effects).
4. `firmware_build(project="simpleserial-aes", platform="CWLITEARM", crypto_target="TINYAES128C", ss_ver="SS_VER_2_1", wait=true)`. On failure read `log_tail` in the reply or `firmware_build_log`. `TINYAES128C` builds on every platform.
5. Ask before flashing, then `firmware_program()` (uses the last build and the platform's programmer). For a file of your own, `target_program(path, programmer)`.

With the simulator, programming an ELF runs it in the emulator (the reply says `emulation`); the simulated AES target works without any firmware.

## 3. Capture traces

1. Check the capture window: `scope_get_settings(filter="adc")` (`adc.samples`, `adc.offset`, `adc.presamples`), `gain.db`, `clock.adc_src` or `clock.adc_mul`.
2. `capture_start(count=N, key_mode="fixed", text_mode="random", clear=true, wait=true)`. Use `key="..."`/`text="..."` for fixed hex values, `seed` for repeatable random data, `mode="trigger_only"` when the target triggers on its own (no SimpleSerial), `count=0` for continuous capture until `capture_stop`.
3. Check the reply: `job.done` equals the target count, `job.timeouts` is small, `job.error` is null. Many timeouts mean the trigger never fired (check `trigger.triggers`, firmware, `scope_action("arm_capture")` to test the trigger alone).
4. Check signal quality with `traces_stats(max_points=200)`: the mean trace's min/max must stay inside about -0.45..0.45 (clipping at ±0.5). If clipped, lower `gain.db`; if the signal is tiny, raise it. Recapture with `clear=true`.
5. `trace_get(index)` shows one trace with its plaintext, ciphertext and key. `capture_single` is a quick look that returns the samples directly.

Typical counts: simulator 50 to 100 traces; CW-Lite software AES 50 to 200; hardware AES or noisy targets thousands (run with `wait=false` and poll `studio_status`).

## 4. CPA key recovery

1. Traces captured with a fixed key and random plaintexts (section 3).
2. `cpa_start(model="sbox_hw", wait=true)`. Models: `sbox_hw` (first-round S-box output, software AES such as TINYAES128C), `lastround_hd` (hardware AES, uses ciphertexts), `ptkey_hw`, `invsbox_hw`, `lastround_hw`. Narrow with `point_start`/`point_end` once you know where leakage is, `trace_end` to test with fewer traces, `bytes=[0,1]` to attack only some bytes.
3. Read `best_key` and, per byte, `top` (guess, corr, sample), `best`, `known` and `pge`. PGE 0 for every byte means the full key is recovered. Studio uses the stored key as the known key unless `use_stored_key=false` or `known_key` is given.
4. `cpa_correlation(byte=0)` shows where in the trace the byte leaks; good for narrowing the window.
5. If some bytes have PGE above 0: capture more traces, narrow the sample window, or check the model matches the implementation.

The `cpa_attack` MCP prompt contains the same steps.

## 5. Clock glitch search

1. Build and flash `simpleserial-glitch` (section 2; not needed on the simulator).
2. Configure glitching:
   `scope_set_settings({"glitch.clk_src": "clkgen", "glitch.output": "clock_xor", "glitch.trigger_src": "ext_single", "io.hs2": "glitch"})`
   On Husky, also check `glitch.enabled` and the `glitch.*` paths from `scope_get_settings(filter="glitch", include_docs=true)`; ranges and units differ by model (Lite/Pro use percentages, Husky uses phase steps).
3. Sweep: `glitch_start(parameters=[{"path": "glitch.width", "start": -40, "stop": 40, "step": 5}, {"path": "glitch.offset", "start": -40, "stop": 40, "step": 5}], command="g", output_len=4, reset="nrst", repeats=1, wait=true)`. Use `{"path": ..., "values": [...]}` for explicit lists, `order="random"` to spread drift, `glitch.ext_offset` and `glitch.repeat` for timing and strength.
4. `glitch_results(only="success")` gives successful points with their `values` (same order as `parameters`) and `response`; the reply also has `counts` for normal, success and reset. Many resets mean the glitch is too strong.
5. Propose a second, finer sweep around the successes, and save with `glitch_export`.
6. Turn glitching off when done: `scope_set_settings({"io.hs2": "clkgen"})` (and `io.glitch_lp`/`io.glitch_hp` false after crowbar glitching).

Voltage (crowbar) glitching uses `glitch.output="glitch_only"` with `io.glitch_lp=true` or `io.glitch_hp=true`. Ask before running it on real hardware and keep `repeat` small at first.

## 6. Notebooks and custom Python

- `notebook_run_code(code)` runs in the shared default kernel; variables persist between calls (`kernel_variables` lists them).
- Inside: `import chipwhisperer as cw`, `scope = cw.scope()`, `target = cw.target(scope)` give stand-ins bound to Studio's connected devices (connect them with the MCP tools first, or let `cw.scope()` connect). `cw.capture_trace(scope, target, text, key)` stores traces in Studio, so CPA and the waveform view see them. `studio.traces` (`.waves`, `.textins`, `.textouts`, `.keys`), `studio.add_trace(wave, textin, textout, key)`, `studio.build_firmware(project, platform, compiler)`, `studio.program(hex_path)`. IPython magics, `!shell` and `plt.show()` work.
- Stored notebooks: `notebook_list`, `notebook_read(path)`, `notebook_write(path, cells)` (the user sees it in the Notebook tab), `notebook_run(path)` runs all cells in that notebook's own kernel; continue in it with `notebook_run_code(code, notebook_path=path)`. Free memory afterwards with `kernel_shutdown(path)`.
- NewAE tutorials: `tutorials_fetch(wait=true)` downloads them into the notebooks folder with firmware paths linked. If it stops in state `confirm`, ask the user whether to `on_modified="backup"` or `"keep"`.
- A stuck cell: `kernel_interrupt(notebook_path)`; `kernel_restart` clears variables (ask first).

## 7. Target interfaces

Always call `hardware_capabilities` first; every feature reports `{available, reason}` for the connected model (the simulator imitates `sim_model`).

- UART: `uart_configure(baud, parity, stop_bits, rx, tx)`, then `serial_write(data, hex=false, eol="lf")` and `serial_read(since=t)`.
- SimpleSerial: `simpleserial(cmd, data, read_len)`; `simpleserial_connect(version)` to switch protocol (1.0, 1.1, 2.1, cdc).
- SPI: `spi_enable(speed, cs)`, `spi_transfer("9f 00 00 00")` (JEDEC ID; the simulator's flash answers EF 40 18), `spi_disable()`. Needs TARGET_SPI firmware; not on the Nano.
- GPIO: `gpio_read`, `gpio_set(pin, "high"|"low"|"high_z")`, `gpio_pulse(pin="nrst", ms=10)` to reset the target.
- Triggers: `trigger_configure(kind=...)` with basic, uart_decode (Pro), uart_pattern, edge_counter, adc_level, sequencer (Husky), sad (Pro, Husky). Captures use the new trigger right away.
- Husky extras: `userio_set`, `bitbang`, `onewire(action="read_rom")`.
- OpenOCD (real hardware only): install with `toolchain_install("openocd")`, `openocd_mpsse(enable=true, transport="swd")` (this disconnects the scope), `openocd_start(target_cfg=...)` (find configs with `openocd_status(list_targets=true, filter="stm32")`), `openocd_command("halt")`, `openocd_program(path)`, then `openocd_stop` and `openocd_mpsse(enable=false)` to get the scope back. Review any erase or write command with the user.

## 8. Logic analyser

1. `la_sources` lists what is available: native (Husky), adc (any ChipWhisperer, thresholded analog input), sim (demo UART, SPI, I2C, 1-Wire, CAN, JTAG, SWD traffic), sigrok (external analysers), files.
2. `la_capture(source="sim")` (or native/adc/sigrok with their options); `la_import(path)` for VCD, CSV or .sr files.
3. `la_decode(decoder="i2c"|"uart"|"spi"|"onewire"|"jtag"|"swd"|"can"|"simpleserial", channels={...}, options={...})`. The reply's `meta` summarises (for example I2C transactions and NACKs); `annotations` lists each item with time and text.
4. `la_measure(channel)` for frequency, duty cycle and pulse widths; `la_search` for edges, patterns or decoded values; `la_channels` to rename, hide or group buses; `la_export(format="vcd")`.

## 9. Code on the waveform

Needs stored traces from firmware Studio knows (programmed in this session, or the newest build with its ELF).

1. `code_map_build()` (defaults: newest trace, programmed or newest ELF). Check the reply: emulated ciphertext correct and matching the stored one, alignment confidence, and `top` functions with sample ranges. For simpleserial-glitch use `cmd="g", text=""`.
2. `code_map_region(start, end)` answers "what runs at these samples"; `code_map_lookup(function="AddRoundKey")` or `code_map_lookup(file="aes.c", line=120)` answers "where does this run"; `code_map_disassemble(function=...)` shows instructions with execution counts.
3. Low alignment confidence: `code_map_align` refits, or set `shift`/`scale` by hand.
4. Combine with CPA: the `sample` of the best guess in `cpa_result` can be passed to `code_map_region` to name the leaking function.

## 10. Notes, calculator and exports

- `note_write(name, text, append=true)` to record keys, settings and findings in Studio's notes pad (`notes_list`, `note_read`).
- `calculate("hw(sbox(0x2b ^ 0x00))")`: arithmetic, hex, XOR, `hw`, `hd`, `sbox`; variables persist.
- `selection_stats(trace_index=-1, start, end)` for statistics over part of a trace.
- `traces_export(path, format="npz"|"cwp"|"csv")`, `traces_import(path)`, `glitch_export(path)`, `la_export`. Relative paths go into the data folder.
