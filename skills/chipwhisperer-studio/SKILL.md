---
name: chipwhisperer-studio
description: Drive ChipWhisperer Studio (NewAE ChipWhisperer side-channel and fault-injection hardware, or its built-in simulator) through the chipwhisperer-studio MCP server. Use whenever the user asks to connect a ChipWhisperer scope or target, capture power traces, run CPA / recover an AES key, sweep clock or voltage glitches, build or flash ChipWhisperer firmware, install compiler toolchains, run ChipWhisperer notebooks or NewAE tutorials, use target interfaces (UART, SimpleSerial, SPI, GPIO, triggers, OpenOCD/JTAG/SWD), capture or decode logic signals, or map firmware code onto a trace. Also use when setting up the cw-studio MCP server for an AI client.
---

# ChipWhisperer Studio

ChipWhisperer Studio (`cw-studio`, PyPI `chipwhisperer-studio`) is a desktop app for NewAE ChipWhisperer boards (Nano, Lite, Pro, Husky, Husky Plus) with a built-in simulator. Its MCP server, `cw-studio mcp`, exposes 106 tools that each wrap one call of Studio's HTTP API, so an agent has exactly the features of the UI and can share one live session with a person watching the Studio window.

## Before you start

1. Check the tools are available: look for `studio_status` (often namespaced, e.g. `mcp__chipwhisperer-studio__studio_status`). If it is missing, the MCP server is not configured: follow `references/setup.md` and tell the user to restart their client.
2. Call `studio_status` first, every session. It tells you what is connected, whether a job is running, how many traces are stored, the data folder and the Studio UI URL. Never assume a clean state: a person may be using the same Studio.
3. Decide hardware or simulator. If the user did not say, call `list_devices`: no devices means use the simulator (`scope_connect(kind="sim")`, `target_connect(kind="sim")`). Say which one you are using.
4. Offer the UI: `open_ui` returns the URL where the user can watch traces arrive live.

## How the server behaves

- Results arrive as `{"result": ...}`. Errors carry Studio's reason (for example a setting the connected model does not have, or a programmer it cannot use); read it and adjust instead of retrying verbatim.
- Settings are flat dotted paths (`gain.db`, `adc.samples`, `adc.offset`, `clock.clkgen_freq`, `trigger.triggers`, `glitch.width`, `io.hs2`). Discover them with `scope_get_settings(filter="adc")` or `target_get_settings`; add `include_docs=true` to learn what one does. Setters return the value read back from hardware, which may be rounded: report the read-back value, not the requested one.
- Long jobs (capture, glitch sweep, CPA, firmware build, toolchain or source downloads, tutorials) run in the background. Pass `wait=true` (with a sensible `timeout_s`) or poll the matching status tool (`studio_status`, `job_wait`, `firmware_build_log`, `toolchains_list`).
- Only one hardware job runs at a time. `capture_stop` stops a capture or glitch sweep; `cpa_stop`, `firmware_build_cancel`, `toolchain_cancel` and `kernel_interrupt` stop the others.
- Large data never comes back raw: traces and statistics are min/max decimated to `max_points`. Ask for more points only when you need them.
- When something fails without a clear message, read `get_logs(level="ERROR")` or `get_logs(since_seq=N)`.

## Core workflows (short form)

Full step lists with arguments and checks are in `references/workflows.md`. Read it before a workflow you have not done in this session.

- **CPA key recovery:** connect scope and target, (hardware: build and flash `simpleserial-aes`), `capture_start(count=N, key_mode="fixed", text_mode="random", clear=true, wait=true)`, check `traces_stats` for clipping, `cpa_start(model="sbox_hw", wait=true)`, report `best_key`, per-byte correlation and PGE. On the simulator 50 to 60 traces recover the default key `2b7e151628aed2a6abf7158809cf4f3c`.
- **Glitch search:** build and flash `simpleserial-glitch`, `scope_set_settings` for `glitch.clk_src`, `glitch.output`, `glitch.trigger_src`, `io.hs2`, then `glitch_start(parameters=[...], command="g", output_len=4, reset="nrst", wait=true)` and `glitch_results(only="success")`. Propose a narrower second sweep around successes.
- **Autonomous glitch campaign ("here is the source, glitch an instruction"):** when that is the whole instruction, you choose everything. Do not run it as one agent: act as coordinator and spawn subagents with the Agent tool for the distinct roles (source analyst, assembly verifier, sweep executor(s), results analyst), sized to the job. Read the source to pick the vulnerable instruction, then **verify it in the disassembly before glitching** with `code_map_build` + `code_map_disassemble` so you know exactly which instruction runs, where and when, and bound `ext_offset` to its real cycle range. Build and flash, take the un-glitched baseline, run a coarse then a fine sweep with many `repeats`, and report the most reliable and consistent parameters. Full procedure, including the agent split and the one-hardware-job-at-a-time rule, in `references/workflows.md` section 11. Work on the simulator or the user's own board, and follow the glitch safety rules below.
- **Firmware:** `toolchains_list` then `toolchain_install` if needed, `firmware_fetch_sources` if `firmware_catalogue` shows no valid sources, `firmware_plan` to preview, `firmware_build(project, platform, wait=true)`, `firmware_program()`.
- **Custom experiments:** `notebook_run_code` runs Python in Studio's kernel. Inside it, `import chipwhisperer as cw; scope = cw.scope(); target = cw.target(scope)` returns Studio's connected devices (not new USB connections); `cw.capture_trace(...)` stores traces in Studio; `studio.traces`, `studio.add_trace()`, `studio.build_firmware()`, `studio.program()` are available. `scope` is not predefined.
- **Interfaces:** call `hardware_capabilities` before any UART, SPI, GPIO, trigger, bit-banger, 1-Wire or OpenOCD tool; each feature reports `{available, reason}` for the connected model.
- **Logic analyser:** `la_sources`, `la_capture(source=...)`, `la_decode(decoder=...)`, `la_measure`, `la_search`.
- **Code on the waveform:** after capturing with known firmware, `code_map_build`, then `code_map_region(start, end)` or `code_map_lookup(function=...)`.

The full tool list with every argument and default is in `references/tools.md`.

## Safety rules

- **Ask before destructive actions** on real hardware: `target_program`, `firmware_program`, `openocd_program`, any erase or write through `openocd_command`, `traces_clear` (unless the user asked for a fresh capture), `toolchain_remove`, `kernel_restart`, `kernel_shutdown`. On the simulator, programming and clearing are harmless.
- **Ask before downloading** large things the user did not request (toolchains are 50 to 300 MB; firmware sources and tutorials come from GitHub).
- **Glitching and voltage settings can stress real hardware.** Keep crowbar (`glitch.output="glitch_only"` with `io.glitch_lp`/`io.glitch_hp`) sweeps short, start with narrow widths, and turn glitching off when done with `scope_set_settings({"io.glitch_lp": false, "io.glitch_hp": false, "io.hs2": "clkgen"})`.
- `notebook_run_code` executes arbitrary Python on the Studio machine. Show the user code you plan to run against real hardware when it does more than read state.
- `tutorials_fetch` may stop in state `confirm` with conflicts; ask the user whether to `backup` or `keep` their local copies. Never choose for them.
- Do not change `--host` to expose Studio beyond localhost: the HTTP API has no authentication.

## Reporting results

- Give concrete numbers: traces used, samples per trace, recovered key in hex, whether it matches the known key, worst-byte PGE, top correlation, glitch success counts with the exact parameter values.
- Record results the user will want later with `note_write(name, text, append=true)` when they ask you to keep notes, and export data with `traces_export` or `glitch_export` (relative paths go into Studio's data folder, which `studio_status` reports).
- Studio has been verified with the simulator and in CI; behaviour on physical ChipWhisperer hardware is less tested. Mention this if hardware results look wrong, and check `get_logs`.
