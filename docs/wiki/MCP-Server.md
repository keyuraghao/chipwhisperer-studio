# MCP Server

ChipWhisperer Studio includes a [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server, so AI agents such as Claude can drive everything the Studio window can do: connect hardware, change settings, build and flash firmware, capture traces, run CPA, sweep glitches, use the target's interfaces (UART, SPI, GPIO, triggers, OpenOCD), capture and decode logic signals, map firmware code onto traces, run notebooks and keep notes. This page explains how it works, how to set it up in each client, and lists all 106 tools.

<picture><source media="(prefers-color-scheme: light)" srcset="images/mcp-setup-light.png"><img alt="MCP setup instructions in the Help tab" src="images/mcp-setup.png"></picture>

![The MCP setup for AI agents (animated)](images/clips/ai-agents-mcp.webp)

## What MCP gives you

MCP is an open standard that lets an AI application call "tools" provided by another program. Studio's MCP server turns each Studio feature into a tool with a description and typed parameters, so an agent can plan and run a whole side-channel experiment from a sentence such as "capture 500 traces from the simulator and recover the key".

Every MCP tool is a thin wrapper around Studio's [HTTP API](HTTP-API), which means:

- The agent has exactly the same features and options as the UI, nothing hidden and nothing extra.
- A person and an agent can share one session. If Studio is already open in your browser, the agent works on the same connected scope and the same stored traces, and you can watch every capture appear live in the waveform view.
- Anything the agent does is visible in Studio's log drawer and status chips.

## How the server finds Studio

When you run `cw-studio mcp`, the server first checks whether a Studio answers at `--url` (default `http://127.0.0.1:8765`, or the `CWSTUDIO_URL` environment variable).

1. If Studio is running there, the MCP server attaches to it. The agent and the browser share one session.
2. If nothing answers, the server starts a headless Studio inside the same process (API and UI, no browser window) on `--port` (default 8765, or the next free port). Its UI URL is printed to stderr and is also returned by the `open_ui` and `studio_status` tools, so you can still open it in a browser to watch.
3. With `--no-embed`, the server refuses to start its own Studio and exits with an error if none is running.

> **Note:** Studio has been tested with the built-in simulator and in CI on Linux, Windows and macOS. It has not yet been verified on physical ChipWhisperer hardware, so treat hardware results from an agent with the same care as your own first experiments.

## Setup

### Claude Code

```bash
claude mcp add chipwhisperer-studio -- cw-studio mcp
```

Add `--simulate` to try it without hardware:

```bash
claude mcp add chipwhisperer-studio -- cw-studio mcp --simulate
```

### Claude Desktop, Cursor and other clients

Most clients read an `mcpServers` JSON block (Claude Desktop: Settings, Developer, Edit Config; Cursor: Settings, MCP). With a pip installation:

```json
{
  "mcpServers": {
    "chipwhisperer-studio": { "command": "cw-studio", "args": ["mcp"] }
  }
}
```

With the standalone bundle, use the full path to the executable and pass `mcp` as the first argument. On Windows, the ChipWhisperer Studio (window) build's `ChipWhispererStudio.exe` has no console, so agents use `cw-studio.exe` next to it; the Web build has only `ChipWhispererStudio.exe`, which works:

```json
{
  "mcpServers": {
    "chipwhisperer-studio": {
      "command": "C:\\Tools\\ChipWhispererStudio\\cw-studio.exe",
      "args": ["mcp"]
    }
  }
}
```

| OS | Example command path |
|----|----------------------|
| Windows | `C:\\Tools\\ChipWhispererStudio\\cw-studio.exe` (window build) or `C:\\Tools\\ChipWhispererStudio-Web\\ChipWhispererStudio.exe` (Web build); escape backslashes in JSON |
| macOS | `/Applications/ChipWhisperer Studio.app/Contents/MacOS/ChipWhispererStudio` |
| Linux | `/home/you/ChipWhispererStudio/ChipWhispererStudio` |

Restart the client after editing its configuration. The Help tab inside Studio shows ready-to-copy versions of these snippets with a Copy button, with the right command for your installation (also in `GET /api/meta` as `mcp_command`): `cw-studio.exe` in the Windows window build, the bundle's own executable otherwise, and `cw-studio` (or `python -m cwstudio mcp`) for a pip install.

### Serving MCP over HTTP

Clients that connect to a URL instead of launching a process can use the streamable HTTP transport:

```bash
cw-studio mcp --transport streamable-http --mcp-host 127.0.0.1 --mcp-port 8766
```

The server then listens on `http://127.0.0.1:8766/mcp`. The older `sse` transport is also available with `--transport sse` (endpoint `/sse`).

## Command line options

| Option | Default | Meaning |
|--------|---------|---------|
| `--url URL` | `http://127.0.0.1:8765` (or `CWSTUDIO_URL`) | Studio to attach to. If it does not answer, a headless Studio is started unless `--no-embed` is given. |
| `--no-embed` | off | Fail instead of starting a headless Studio. |
| `--simulate` | off | Embedded Studio only: preselect the simulator, and `cw.scope()` in notebooks connects to it. |
| `--data-dir DIR` | `~/ChipWhispererStudio` | Embedded Studio only: data folder (toolchains, firmware, notebooks, notes, exports). |
| `--port N` | `8765` | Embedded Studio only: preferred HTTP port (the next free port is used if it is taken). |
| `--transport` | `stdio` | `stdio`, `streamable-http` or `sse`. |
| `--mcp-host HOST` | `127.0.0.1` | Bind address for `streamable-http` and `sse`. |
| `--mcp-port N` | `8766` | Port for `streamable-http` and `sse`. |
| `--log-level` | `warning` | `debug`, `info`, `warning` or `error`. Logs always go to stderr, because stdout carries the MCP protocol on stdio. |

## Tool reference

Every tool carries MCP annotations that clients can use to decide when to ask you for confirmation. In the tables, "Changes" means:

- **read only:** only reads state (annotation `readOnlyHint`).
- **hardware:** changes settings, starts jobs or talks to the device, but is not destructive.
- **destructive:** erases or deletes something (flashing a target, deleting traces or toolchains, clearing notebook variables).
- **network:** downloads from the internet.

### General

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `studio_status` | Connected scope and target, running job and progress, stored trace count, CPA summary, data folder and the Studio URL. | none | read only |
| `studio_options` | Every accepted choice: scope kinds, target kinds, programmers, CPA models, crypto targets, SimpleSerial versions, host platform, version. | none | read only |
| `list_devices` | ChipWhisperer USB devices attached to the Studio machine (name, serial number, in use). | none | read only |
| `get_logs` | Recent Studio and ChipWhisperer log messages, plus capture and glitch events. | `since_seq=0`, `limit=200`, `level=None` (DEBUG, INFO, WARNING, ERROR, CRITICAL) | read only |
| `open_ui` | Returns the web UI URL and the API docs URL, and can open the UI in the default browser. | `open_browser=False` | read only |

### Scope

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `scope_connect` | Connects a capture scope. `kind="sim"` uses the simulator; `sim_model` picks which ChipWhisperer it stands in for, which decides the protocols, triggers and programmers offered. | `kind="auto"` (auto, lite, pro, nano, husky, huskyplus, sim), `sn=None`, `force=False`, `default_setup=True`, `sim_model=None` (husky, huskyplus, pro, lite, nano; default husky) | hardware |
| `scope_disconnect` | Disconnects the scope and releases the USB device. | none | hardware |
| `scope_get_settings` | All scope settings as a flat list of `{path, value, type, writable, choices}` read back from the hardware. | `filter=None` (substring of the path), `include_docs=False` | read only |
| `scope_set_setting` | Sets one setting by dotted path and returns the read-back value, for example `gain.db`, `adc.samples`, `clock.clkgen_freq`, `glitch.width`. | `path`, `value` | hardware |
| `scope_set_settings` | Sets several settings in order from a `{path: value}` object, stopping at the first error. | `settings` | hardware |
| `scope_action` | Runs `default_setup`, `arm_capture` (arm and wait for one trigger, to test triggering) or `reset_fpga`. | `action` | hardware |

### Target

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `target_connect` | Connects the target interface. | `kind="SimpleSerial2"` (SimpleSerial2, SimpleSerial, SimpleSerial2_CDC, CW305, sim), `options=None` (passed to `chipwhisperer.target()`) | hardware |
| `target_disconnect` | Disconnects the target interface. | none | hardware |
| `target_get_settings` | Target interface settings as a flat list. | `filter=None`, `include_docs=False` | read only |
| `target_set_setting` | Sets one target setting, for example `baud`. | `path`, `value` | hardware |
| `target_program` | Erases and programs the target from a `.hex` or `.bin` file on the Studio machine, or loads an FPGA bitstream. Programmers the connected model cannot use are refused with the reason. With the simulator, an ELF runs in the emulator. | `path`, `programmer="STM32F"` (STM32F, XMEGA, AVR, SAM4S, NEORV32, iCE40, XC7A35T) | destructive |
| `serial_write` | Writes text, or hex bytes with `hex=True`, to the target serial port. | `data`, `hex=False`, `newline=True`, `eol=None` (none, lf, cr, crlf) | hardware |
| `serial_read` | Serial traffic in both directions after a timestamp, each `{t, dir, data, hex}`. | `since=0`, `limit=200` | read only |
| `simpleserial` | Sends one SimpleSerial command with a hex payload and reads the reply. `read_len=0` skips the read. | `cmd="p"`, `data=""`, `read_cmd="r"`, `read_len=None` | hardware |

### Capture

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `capture_start` | Captures power traces. `count=0` runs until `capture_stop`. With `wait=True` it returns when the capture ends. | `count=100`, `mode="simpleserial"` or `"trigger_only"`, `key_mode="fixed"`, `text_mode="random"` (random, fixed, counter), `key`, `text` (hex), `length=16`, `seed`, `store=True`, `clear=False`, `ack=True`, `max_timeouts=10`, `max_rate=0`, `wait=True`, `timeout_s=600` | hardware |
| `capture_single` | Captures exactly one trace and returns summary statistics plus a decimated copy of the samples. | `key`, `text`, `mode="simpleserial"`, `store=True`, `max_points=1000` | hardware |
| `capture_stop` | Stops the running hardware job (capture or glitch sweep). | none | hardware |
| `job_wait` | Waits until the running capture or glitch job finishes and returns the final status. | `timeout_s=60` | read only |

### Traces

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `traces_summary` | Number of stored traces, samples per trace and memory used. | none | read only |
| `trace_get` | One trace: plaintext, ciphertext and key in hex, statistics (min, max, mean, std, argmin, argmax) and at most `max_points` samples (min/max decimated for long traces). | `index`, `max_points=1000` | read only |
| `traces_stats` | Per-sample mean, std, min and max over stored traces, each decimated. Useful to find where the target computes. | `start=0`, `end=None`, `max_points=1000` | read only |
| `traces_clear` | Deletes all stored traces from memory (files on disk are kept). | none | destructive |
| `traces_export` | Saves stored traces as `npz`, `cwp` (ChipWhisperer project) or `csv`. Relative paths go into the data folder. | `path="traces"`, `format="npz"` | hardware |
| `traces_import` | Loads traces from an `.npz`, `.npy` or `.cwp` file (or a `.zip` holding a ChipWhisperer project) on the Studio machine; `replace=False` appends. | `path`, `replace=True` | hardware |

### Analysis

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `cpa_start` | Runs a correlation power analysis attack on the stored traces. With `wait=True` it returns the final result. | `model="sbox_hw"` (sbox_hw, ptkey_hw, invsbox_hw, lastround_hd, lastround_hw), `trace_start=0`, `trace_end`, `point_start`, `point_end`, `bytes` (list of key bytes 0 to 15), `known_key`, `use_stored_key=True`, `report_every=50`, `wait=True`, `timeout_s=600` | hardware |
| `cpa_stop` | Stops a running CPA attack; the partial result stays available. | none | hardware |
| `cpa_result` | Latest result: best key guess, per-byte ranking and correlation, PGE per byte when the key is known, progress. | none | read only |
| `cpa_correlation` | Correlation versus sample index for the best guess of one key byte, decimated. Shows where the leakage happens. | `byte`, `max_points=1000` | read only |

### Glitch

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `glitch_start` | Sweeps glitch parameters. Each entry of `parameters` is `{path, start, stop, step}` or `{path, values: [...]}`. | `parameters`, `command="g"`, `data=""`, `expected=None` (hex), `output_len=4`, `repeats=1`, `order="nested"` or `"random"`, `reset="nrst"` (none, nrst, pdic), `reset_on="reset"` (never, reset, always), `reset_delay=0.05`, `glitch_timeout=1000`, `arm_scope=True`, `wait=False`, `timeout_s=1800` | hardware |
| `glitch_results` | Counts per outcome and each point's parameter values, outcome and response. | `only=None` (success, reset, normal), `limit=500` | read only |
| `glitch_export` | Saves the sweep results as CSV. | `path="glitch_results.csv"` | hardware |

### Toolchains

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `toolchains_list` | Known compiler toolchains with version, size, whether installed, bin folder, a system copy on PATH, and any running download. | none | read only |
| `toolchain_install` | Downloads, verifies (SHA-256) and unpacks a toolchain. | `toolchain_id` (arm-gcc, avr-gcc, riscv-gcc, clang, win-build-tools, openocd or a custom id), `wait=True`, `timeout_s=1800` | network |
| `toolchain_cancel` | Cancels a running toolchain download. | `toolchain_id` | hardware |
| `toolchain_remove` | Deletes an installed toolchain from disk. | `toolchain_id` | destructive |
| `toolchain_add_custom` | Registers another toolchain from an archive URL (plus SHA-256) or an existing folder. | `name`, `compiler="gcc"` or `"clang"`, `arch` (list such as arm, avr, riscv, tricore, ppc, rx), `prefix`, `url`, `sha256`, `path`, `version`, `bin="bin"` | hardware |
| `toolchain_remove_custom` | Removes a custom toolchain entry and its download. | `toolchain_id` | destructive |
| `toolchains_refresh` | Fetches the latest toolchain list published in the Studio repository. | none | network |

### Firmware

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `firmware_catalogue` | Source state (folder, repository, channel, installed commit, update check) plus projects, platforms, crypto targets and SimpleSerial versions. | none | read only |
| `firmware_set_channel` | Chooses which newaetech/chipwhisperer version to follow: a branch such as `develop`, `latest-release`, a tag or a commit. | `channel="develop"` | hardware |
| `firmware_check_updates` | Asks GitHub for the newest commit on the channel and reports whether the sources are out of date. | none | network |
| `firmware_fetch_sources` | Downloads or updates `firmware/mcu` and the fw-extra HALs. | `ref=None`, `wait=True`, `timeout_s=900` | network |
| `firmware_set_folder` | Builds from your own `firmware/mcu` folder instead; `root=None` goes back to the downloaded sources. | `root=None` | hardware |
| `firmware_plan` | Dry run: shows the make command, toolchains, output file and programmer without building. | `project="simpleserial-aes"`, `platform="CWLITEARM"`, `compiler="gcc"`, `crypto_target`, `ss_ver`, `cflags`, `make_args` | read only |
| `firmware_build` | Compiles a project for a platform and returns the `.hex` path, sizes, programmer and the end of the log. | `project="simpleserial-aes"`, `platform="CWLITEARM"`, `compiler="gcc"`, `crypto_target="TINYAES128C"`, `ss_ver="SS_VER_2_1"`, `cflags`, `make_args`, `clean=True`, `jobs`, `wait=True`, `timeout_s=900`, `log_tail=40` | hardware |
| `firmware_build_log` | Build output lines after a line number. | `since=0`, `limit=400` | read only |
| `firmware_build_cancel` | Stops a running build. | none | hardware |
| `firmware_builds` | Firmware images built so far, newest first. | none | read only |
| `firmware_program` | Programs the target with the last build (or `path`) using the platform's programmer (or the one given). | `path=None`, `programmer=None` | destructive |

### Notebooks

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `notebook_run_code` | Runs Python and returns the text output, image count and errors. Without `notebook_path` it uses the shared default kernel; with `notebook_path` it runs in that notebook's own kernel (the namespace its tab in the Notebook tab uses) with the notebook's folder as working directory. | `code`, `notebook_path=None`, `timeout_s=600` | hardware |
| `notebook_list` | Stored notebooks and the tutorial download state. | none | read only |
| `notebook_read` | A notebook's cells with an optional text summary of the outputs. | `path`, `include_outputs=True` | read only |
| `notebook_write` | Creates or overwrites a notebook from `[{"type": "code" or "markdown", "source": "..."}]`. | `path`, `cells` | hardware |
| `notebook_run` | Runs every code cell of a stored notebook in order, in that notebook's own kernel, saves the outputs into it and returns a per-cell summary. | `path`, `stop_on_error=True`, `timeout_s=1800` | hardware |
| `kernel_list` | Running kernels: id (notebook path or `default`), busy, queued cells, execution count, number of variables, windows that have the notebook open. | none | read only |
| `kernel_variables` | Variables defined in a kernel (name, type, shape, short repr). | `notebook_path=None` (default kernel) | read only |
| `kernel_interrupt` | Interrupts a kernel's running cell and drops its queued cells; `notebook_path="all"` interrupts every kernel. | `notebook_path=None` (default kernel) | hardware |
| `kernel_restart` | Clears a kernel's variables; other notebooks and the hardware connection are kept. | `notebook_path=None` (default kernel) | destructive |
| `kernel_shutdown` | Stops a notebook's kernel and frees its memory (it starts again, empty, on the next cell). Useful after `notebook_run` on many notebooks. | `notebook_path` | destructive |
| `tutorials_fetch` | Downloads or updates NewAE's chipwhisperer-jupyter tutorials matched to the installed firmware sources. Locally edited or added files are kept; if some would be replaced by a changed upstream version, the job stops in state `confirm` listing them, until called again with `on_modified`. | `wait=True`, `timeout_s=900`, `on_modified=None` (`backup`: install and keep the local copies as `<name>.local-<timestamp>`; `keep`: leave them) | network |

### Notes and calculator

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `notes_list` | Notes in Studio's notes pad. | none | read only |
| `note_read` | Reads one note (use the file name, for example `Lab notes.md`). | `name` | read only |
| `note_write` | Writes a note, creating it if needed; `append=True` adds to the end. | `name`, `text`, `append=True` | hardware |
| `calculate` | Evaluates a calculator expression: arithmetic, bitwise (`^` is XOR), hex and binary, math functions, `mean`, `median`, `std`, `rms`, and `hw()`, `hd()`, `sbox()`. Variables persist and `ans` is the last result. | `expression` | read only |
| `selection_stats` | Count, sum, mean, median, min, max, peak to peak, std, variance and RMS of explicit values, of samples of a stored trace, or of one sample across all traces. | `values`, `trace_index` (-1 is the newest), `start`, `end`, `sample` | read only |

### Interfaces

The [Interfaces](Protocols-and-Interfaces) tab's features. Anything the connected model cannot do returns `{"ok": false, "supported": false, "reason": "..."}` instead of an error, so the agent can explain why.

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `hardware_capabilities` | What the connected ChipWhisperer (or the simulator's model) supports: UART pin modes, SimpleSerial versions, SPI, JTAG/SWD, GPIO, USERIO, bit-banger, 1-Wire, each trigger type and each programmer, as `{available, reason, details}`. | none | read only |
| `interfaces_status` | State of every interface: capabilities, UART configuration and pins, SimpleSerial version, SPI master, the trigger configured last, OpenOCD and MPSSE. | none | read only |
| `uart_configure` | Configures the target UART. Only pin modes valid for the model are accepted. | `baud` (500 to 2,000,000), `parity`, `stop_bits` (1, 1.5, 2), `rx`, `tx` | hardware |
| `simpleserial_connect` | Connects the target with a SimpleSerial version; then use `simpleserial` or `serial_write`. | `version="2.1"` (1.0, 1.1, 2.1, cdc) | hardware |
| `spi_enable` | Turns on the SPI master on the 20-pin header (not on the CW-Nano). On the simulator a W25Q128 SPI flash answers. | `speed=1000000`, `cs="pdid"` (pdid, pdic, tio3, tio4) | hardware |
| `spi_disable` | Turns the SPI master off. | none | hardware |
| `spi_transfer` | Sends hex bytes on MOSI (`9f 00 00 00` reads a flash JEDEC ID) and returns MISO. | `data`, `start=True`, `stop=True` | hardware |
| `gpio_read` | Mode and level of each target pin (the CW-Nano cannot read back). | none | read only |
| `gpio_set` | Drives a pin high or low, or releases it. | `pin`, `state` (high, low, high_z) | hardware |
| `gpio_pulse` | Pulls a pin low for some milliseconds; on nRST this resets the target. | `pin="nrst"` (nrst, pdic), `ms=50` | hardware |
| `userio_set` | Husky USERIO direction and drive bit masks; returns the levels read back. | `direction`, `drive` | hardware |
| `trigger_configure` | Configures the capture trigger; captures use it right away. | `kind="basic"` (basic, uart_decode, uart_pattern, edge_counter, adc_level, sequencer, sad), and per kind `pins`, `op`, `edge`, `pin`, `baud`, `pattern`, `rule`, `data_bits`, `stop_bits`, `parity`, `edges`, `level`, `window_start`, `window_end`, `threshold`, `start`, `enabled` | hardware |
| `bitbang` | Husky bit-banger: sends bits with a clock and returns the recorded ones. | `bits`, `record`, `data_pin="USERIO_D0"`, `clock_pin="USERIO_CK"`, `clk_div` | hardware |
| `onewire` | 1-Wire reset and presence, or Read ROM (family, serial, CRC). | `action="read_rom"` (reset, read_rom), `data_pin="USERIO_D0"`, `clk_div` | hardware |
| `openocd_status` | OpenOCD install, server, ports, MPSSE mode (`mpsse.detected` for a scope found already in MPSSE mode) and recent log lines; `list_targets` adds the target configurations. | `list_targets=False`, `filter` | read only |
| `openocd_mpsse` | Switches the scope into MPSSE mode for JTAG or SWD, or back (`enable=false`, which reconnects the scope; it also restores a scope Studio found already in MPSSE mode, for example after a restart). Not on the simulator. | `enable=True`, `transport="jtag"`, `header="target"` (target, userio) | hardware |
| `openocd_start` | Starts an OpenOCD server with the ChipWhisperer interface and a target configuration. | `target_cfg="target/stm32f3x.cfg"`, `transport`, `gdb_port=3333`, `telnet_port=4444`, `tcl_port=6666` | hardware |
| `openocd_stop` | Stops the OpenOCD server. | none | hardware |
| `openocd_command` | Runs an OpenOCD command (`targets`, `halt`, `reg`, `mdw 0x08000000 4`, `reset run`) and returns its output. | `command` | hardware |
| `openocd_program` | Flashes a `.hex`, `.elf` or `.bin` through OpenOCD, then verifies and resets. | `path`, `verify=True`, `reset=True`, `address` (for .bin) | destructive |

OpenOCD itself is installed with `toolchain_install(toolchain_id="openocd")`.

### Logic analyser

The [Logic](Logic-Analyser) tab. The tools work on the current capture; `la_select` switches between the eight kept in memory.

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `la_sources` | Sources and whether each is available (with the reason): native (Husky), adc (any scope), sim, sigrok, files; plus the Husky LA triggers, clock sources and the decoders with their options. | none | read only |
| `la_capture` | Captures logic data and waits for it; returns the channels, sample rate, trigger index and duration. | `source="sim"` (native, adc, sim, sigrok) and its settings: native `group`, `clk_source`, `oversampling`, `downsample`, `depth`, `trigger`, `fire`, `with_analog`; adc `segments`, `samples`, `level`, `hysteresis`, `sim_signal`; sim `samplerate`, `duration_ms`, `pretrigger`, `channels`, `jitter_ns`, `glitches_per_ms`; sigrok `device`, `samplerate`, `channels`, `samples`, `time_ms`, `triggers`; `timeout` (how long the analyser waits for its trigger: native default 5 s, sigrok 60 s; the tool waits that plus a margin). Defaults match the Logic tab. | hardware |
| `la_import` | Imports a `.vcd`, `.csv` (Saleae or generic) or sigrok `.sr` file on the Studio machine. | `path`, `format`, `samplerate` (for CSV without times) | hardware |
| `la_export` | Writes the current capture as VCD, CSV or `.sr`. | `format="vcd"`, `path`, `channels` | hardware |
| `la_decode` | Decodes the current capture and returns the annotations (time, row, channel, kind, text, value); channels are guessed from their names when left out. | `decoder` (uart, spi, i2c, onewire, jtag, swd, can, simpleserial, sigrok), `channels` (role to name or index), `options`, `limit=200`, `add_to_view=False` | hardware |
| `la_measure` | Edge counts, frequency, period, duty cycle and pulse widths of a channel (or all) over a time or sample range. | `channel`, `t0`, `t1`, `from_sample`, `to_sample` | read only |
| `la_channels` | Lists the channels and renames, hides, shows, reorders or recolours them, or defines buses. | `rename`, `hide`, `show`, `order`, `colors`, `buses` | hardware |
| `la_search` | Finds the next or previous edge, pattern (`1X0`) or decoded value. | `kind="edge"` (edge, pattern, decoded), `channel`, `edge`, `pattern`, `edge_channel`, `text`, `from_sample=-1`, `direction="next"` | read only |
| `la_status` | A running capture, the current capture, the captures in memory, decoders and buses. | none | read only |
| `la_select` | Makes another kept capture the current one. | `capture` | hardware |

### Code map

[Code on the Waveform](Code-on-the-Waveform): which firmware code runs when during a trace.

| Tool | What it does | Key parameters | Changes |
|------|--------------|----------------|---------|
| `code_map_build` | Emulates the firmware ELF (default: the one programmed in this session, else the newest build) for a stored trace's key and plaintext, maps cycles to samples and aligns with the stored traces. Returns whether the emulated ciphertext is correct and matches the trace, the mapping, the alignment and the top functions in the trigger window. | `elf`, `sources`, `trace`, `key`, `text`, `core`, `protocol`, `wait_states`, `align`, `target_freq`, `adc_freq`, `top=25`, `cmd`, `raw`, `max_instructions` | hardware |
| `code_map_region` | The functions (self cycles, share) and source lines (file, line, text, cycles, executions) that ran in a range of samples. | `start`, `end`, `limit=100` | read only |
| `code_map_lookup` | Where a function or a source line ran: cycle and sample ranges. | `function`, or `file` and `line` | read only |
| `code_map_align` | Fits the mapping by cross-correlation and returns shift, scale, correlation and confidence (a low confidence fit is not applied); or sets shift and scale by hand. | `source="mean"`, `trace`, `scale_min=0.9`, `scale_max=1.1`, `max_shift`, `shift`, `scale` | hardware |
| `code_map_disassemble` | Disassembly of a function or source line with how often each instruction ran. | `function`, or `file` and `line` | read only |
| `code_map_pc_trace` | Exact mode: records program counter samples through a Husky's SWO trace port (needs simpleserial-trace firmware) and compares them with the emulation. The interval is rounded to the nearest the Arm DWT supports (64 or 1024 cycles times 1 to 16) and the one used is returned. On the simulator the samples come from the emulated run. | `interval=64` | hardware |

## Prompts and resources

Clients that support MCP prompts can offer these as starting points:

| Prompt | Arguments | What it asks the agent to do |
|--------|-----------|------------------------------|
| `cpa_attack` | `platform="CWLITEARM"`, `traces=50`, `simulate=False` | Connect, build and flash simpleserial-aes (hardware only), capture with a fixed key, check the traces are not clipped, run CPA and report the key. |
| `glitch_search` | `parameter_ranges` | Build and program simpleserial-glitch, configure clock glitching, sweep the given ranges, then summarise successes and propose a narrower second sweep. |

Resources are read-only JSON documents the client can attach to a conversation:

| Resource | Content |
|----------|---------|
| `studio://status` | Live Studio status (same as `studio_status`). |
| `studio://firmware` | Firmware projects, platforms and source state (same as `firmware_catalogue`). |

## Example requests

These are phrased the way you would type them to an agent. The agent chooses the tools.

- "Connect to the simulator, capture 200 traces with a fixed key and recover the key with CPA."
- "Install the Arm compiler if needed, build simpleserial-aes for CWLITEARM with clang and flash it."
- "Show me where in the trace byte 0 leaks, then capture 500 more traces restricted to that window."
- "Configure clock glitching and sweep glitch.width from -40 to 40 in steps of 4 against simpleserial-glitch. Tell me which settings succeed."
- "Download the ChipWhisperer tutorials and run the Lab 3_3 notebook, then summarise the results."
- "Write the recovered key and the glitch window into my Lab notes."
- "What can this ChipWhisperer do? Read the SPI flash's JEDEC ID and set a trigger on a UART pattern."
- "Capture the simulator's demo traffic, decode the I2C bus and tell me which addresses NACKed."
- "Build a code map for the newest trace and tell me which function runs at sample 1500."

## Safety

- Destructive tools are marked with `destructiveHint`: `target_program`, `firmware_program`, `openocd_program`, `traces_clear`, `toolchain_remove`, `toolchain_remove_custom`, `kernel_restart` and `kernel_shutdown`. `openocd_command` can run any OpenOCD command, including flash erase, so review what an agent sends to real hardware. Configure your client to ask before running them if it supports that.
- `notebook_run_code` and `notebook_run` execute arbitrary Python on the Studio machine, with the same rights as the user running Studio. Only connect agents you trust, and review what they plan to run on real hardware.
- The HTTP API behind the MCP server has no authentication. Keep Studio bound to `127.0.0.1` unless you are on a trusted network (see [Command Line and Configuration](Command-Line-and-Configuration)).

## Troubleshooting MCP

| Symptom | Cause and fix |
|---------|---------------|
| Client says the server closed immediately | Run `cw-studio mcp` in a terminal to see the error on stderr. Check the command path (standalone bundle) and that `cw-studio` is on the client's PATH. |
| Agent works on a different session than your browser | The browser Studio runs on another port. Pass `--url http://127.0.0.1:PORT` so the MCP server attaches to it. |
| "Studio is not running" error | You used `--no-embed` and no Studio answers at `--url`. Start Studio first or drop `--no-embed`. |
| Port already in use | The embedded Studio falls back to the next free port automatically. For HTTP transports, pick another `--mcp-port`. |
| Nothing in the client log | Studio only logs warnings by default. Use `--log-level debug`. All logs go to stderr so they never corrupt the stdio protocol. |

See also: [HTTP API](HTTP-API), [Notebooks](Notebooks), [Troubleshooting](Troubleshooting).
