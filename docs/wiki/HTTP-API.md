# HTTP API

Everything the Studio window does goes through a local HTTP API and one WebSocket, so you can drive Studio from scripts, CI jobs or other programs without the browser. This page describes the conventions, the binary trace format, every WebSocket event, every endpoint, and worked examples.

## Basics

- **Base URL:** `http://127.0.0.1:8765` by default (the host and port you started Studio with; see [Command Line and Configuration](Command-Line-and-Configuration)).
- **Endpoint list:** open `http://127.0.0.1:8765/api/docs` for a list of every route the running server offers, with its method, parameters and notes. `http://127.0.0.1:8765/openapi.json` has the same list as an OpenAPI 3 document for tools.
- **Requests:** `POST` and `PUT` bodies are JSON (`Content-Type: application/json`). Uploads use `multipart/form-data` with a `file` field, or send the file itself as the request body with `?filename=NAME` (for example `curl --data-binary @fw.hex "http://127.0.0.1:8765/api/target/program/upload?programmer=STM32F&filename=fw.hex"`).
- **Responses:** JSON, except trace data, which uses the binary frame format below, and file downloads.
- **Errors:** a non-2xx status with `{"detail": "ExceptionType: message"}`. Most failures (no scope connected, build failed to start) return status 400. A missing or malformed query, path or file parameter returns 422 with a list instead: `{"detail": [{"type": "missing", "loc": ["query", "path"], "msg": "Field required", "input": null}]}`.
- **Long jobs:** captures, glitch sweeps, CPA, builds and downloads start in the background and return immediately. Poll the matching status endpoint or listen on the WebSocket.
- **One hardware job at a time:** a capture, a glitch sweep and a logic analyser capture cannot run together. Starting a second one returns an error.
- **Not found:** an unknown notebook, logic channel, decoder or code map item returns 404 with the same `{"detail": ...}` form.

> **Note:** The API has no authentication. Anyone who can reach the port can control your hardware and run notebook code. Keep the default bind address `127.0.0.1` unless you are on a trusted network.

## Binary trace frames

Trace data (`GET /api/traces/{index}`, `/api/traces/stats`, `/api/traces/block`, `/api/analysis/cpa/corr/{b}`, `/api/codemap/model` and `trace` WebSocket messages) uses one compact format:

| Bytes | Content |
|-------|---------|
| 0 to 3 | Header length `N` as an unsigned 32-bit little-endian integer |
| 4 to 4+N | JSON header, UTF-8 (for example `{"type":"trace","index":12,"n":5000,"dtype":"f32","textin":"...","key":"..."}`) |
| 4+N to end | Samples as little-endian 32-bit floats |

For `/api/traces/stats` the samples are the fields listed in `header.fields` (mean, std, min, max) one after another, each `header.samples` long. For `/api/traces/block` they are the requested traces one after another.

```python
import json, struct
import numpy as np
import requests

def decode(frame: bytes):
    (n,) = struct.unpack_from("<I", frame, 0)
    header = json.loads(frame[4:4 + n])
    samples = np.frombuffer(frame[4 + n:], dtype="<f4")
    return header, samples

header, wave = decode(requests.get("http://127.0.0.1:8765/api/traces/0").content)
print(header["textin"], wave.shape)
```

## WebSocket events

Connect to `ws://127.0.0.1:8765/ws`. Studio sends a `hello` message with the version and full status, then one message per event. Text frames are JSON with a `type` field (plus `seq` and `ts`); binary frames use the trace frame format with `type` in the header. Messages you send are ignored.

| Type | Frame | When and what |
|------|-------|---------------|
| `hello` | text | On connect: `version` and `status`. |
| `status` | text | Every 2 seconds and after changes: the same object as `GET /api/status`. |
| `log` | text | A log record: `level`, `logger`, `msg`. |
| `capture` | text | Capture progress: `state` (running, done, stopped, error), `done`, `target`, `timeouts`, `rate`, `stored`, `mode`, `elapsed`. |
| `trace` | binary | A captured or displayed trace (rate limited to about 25 per second): `index`, `stored`, plaintext, ciphertext and key when known, `source` (`notebook` for notebook traces). |
| `traces` | text | Trace store summary after a clear or import: `count`, `samples`, `memory_bytes`. |
| `serial` | text | Serial traffic: `t`, `dir` (tx or rx), `data`, `hex`. |
| `setting` | text | A setting changed: `target` (scope or target), `path`, `value`. |
| `cpa` | text | CPA progress or result (same object as `GET /api/analysis/cpa`). |
| `glitch` | text | Sweep progress: `state`, `point`, `points`, `repeats`, `counts`, `parameters`. |
| `glitch_result` | text | One sweep point: `i`, `rep`, `values`, `result`, `response`, `t`. |
| `toolchain` | text | Toolchain status changed (download progress, installed, error). |
| `firmware_sources` | text | Firmware source status changed (download progress, installed, update check). |
| `build` | text | Firmware build state: `state` (running, ok, failed, cancelled), `hex`, `size`, `error`. |
| `build_log` | text | New build output: `start` (line number) and `lines`. |
| `nb` | text | Notebook kernels: `kind` (queued, running, output, done, cancelled, restarted, shutdown, renamed), `kernel` (the kernel id: the notebook's path, or `default`), `path` (the notebook path, or null for the default kernel), `cell`, `exec`, and `output`, or `ok`, `execution_count` and `outputs` when done. `restarted` carries the kernel status; `renamed` carries the new id in `kernel` and the previous one in `old`. Each event belongs to exactly one kernel, so a client showing several notebooks routes it by `kernel`. `kind: "file"` (with `action` `saved` or `deleted`, `path`, `mtime` and the saving window's `client`) announces changes to notebook files. |
| `tutorials` | text | Tutorial notebook download status. |
| `programmed` | text | A target was programmed: the file, and with the simulator whether its firmware runs in the emulator. |
| `spi` | text | One SPI transfer from the Interfaces tab or the API: MOSI and MISO bytes. |
| `openocd` | text | OpenOCD: `kind` `log` (a log line), `state` (`running`, `exit_code`) or `mpsse` (MPSSE mode on or off). |
| `la` | text | Logic analyser: `kind` `job` (capture progress: `state`, `source`, `phase`), `capture` (a new capture), `select` (the current capture changed), `channels`, `decoders`, `decoded` (a background decode finished). |
| `codemap` | text | The code map changed (same object as `GET /api/codemap`). |

## Endpoints

### Status and system

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/status` | Scope and target info, running job, trace summary, CPA summary, uptime, data folder. |
| `GET /api/meta` | Version, scope kinds, target kinds, programmers, CPA models, crypto targets, SimpleSerial versions, simulator models (`sim_models`), the command an MCP client should run for this installation (`mcp_command`: `{command, args}`), host platform, platform help (drivers, udev). |
| `GET /api/capabilities` | What the connected scope supports: UART, SimpleSerial, SPI, JTAG, SWD, trace, GPIO, USERIO, bit-banger, 1-Wire, every trigger type, every programmer and the logic analyser sources, each as `{available, reason, ...details}`. For a scope type the Interfaces tab does not support, a top-level `reason` says so. See [Protocols and Interfaces](Protocols-and-Interfaces). |
| `GET /api/devices` | Attached ChipWhisperer USB devices. |
| `GET /api/logs?since=SEQ` | Log, capture and glitch events after a sequence number. |
| `POST /api/shutdown` | Stops the Studio server. |

### Scope

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/scope/connect` | Body: `kind` (auto, lite, pro, nano, husky, huskyplus, sim), `sn`, `force`, `default_setup`, and for the simulator `sim_model` (husky, huskyplus, pro, lite, nano; default husky). Connecting again switches the model. |
| `POST /api/scope/disconnect` | Disconnect the scope. |
| `GET /api/scope/settings` | Settings tree (groups with `children`, leaves with `path`, `value`, `type`, `writable`, `doc`, `choices`, and `disabled_choices` with the reason for choices the connected model does not have). |
| `PUT /api/scope/settings` | Body: `path`, `value`. Returns the read-back value. Values the connected model does not support are refused with the reason. |
| `POST /api/scope/action/{action}` | `default_setup`, `arm_capture`, `reset_fpga` or `glitch_disable`. |

### Target

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/target/connect` | Body: `kind` (SimpleSerial2, SimpleSerial, SimpleSerial2_CDC, CW305, sim) plus extra options for `chipwhisperer.target()`. |
| `POST /api/target/disconnect` | Disconnect the target interface. |
| `GET /api/target/settings` / `PUT /api/target/settings` | Target settings tree, set one setting. |
| `POST /api/target/program` | Body: `programmer` (STM32F, XMEGA, AVR, SAM4S, NEORV32, iCE40, XC7A35T; refused with the reason when the model cannot use it), `path` (a file on the Studio machine). With the simulator the reply has `emulation`: whether the firmware runs in the emulator. |
| `POST /api/target/program/upload?programmer=STM32F` | Multipart `file`: upload and program a firmware file. |
| `POST /api/target/serial/write` | Body: `data`, `hex`, and the line ending for text: `eol` (none, lf, cr, crlf) or the older `newline` (true adds LF). |
| `GET /api/target/serial?since=T` | Serial traffic after timestamp `T`. |
| `POST /api/target/simpleserial` | Body: `cmd`, `data` (hex), `read_cmd`, `read_len`. Returns `response`. |

### Capture and traces

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/capture/start` | Body: `count` (0 = continuous), `mode` (simpleserial, trigger_only), `key_mode` (fixed, random), `text_mode` (random, fixed, counter), `key`, `text`, `length`, `seed`, `store`, `clear`, `ack`, `max_timeouts`, `display_rate`, `max_rate`. |
| `POST /api/capture/single` | Same body, captures one trace. |
| `POST /api/capture/stop` | Stop the running capture or glitch sweep. |
| `GET /api/traces` | Trace store summary. |
| `DELETE /api/traces` | Clear all stored traces. |
| `GET /api/traces/{index}` | One trace (binary frame). |
| `GET /api/traces/{index}/meta` | One trace's plaintext, ciphertext, key, length, min and max as JSON. |
| `GET /api/traces/stats?start=&end=` | Per-sample mean, std, min, max (binary frame). |
| `GET /api/traces/block?start=&end=&step=` | Several traces in one binary frame. |
| `POST /api/traces/export` | Body: `path`, `format` (npz, npy, csv, cwp). Relative paths go into the data folder. |
| `GET /api/traces/download/{fmt}` | Export and download in one step. `npy` and `cwp` come as one zip (`cwp` holds the project file and its `_data` folder). |
| `POST /api/traces/import` | Body: `path` (an `.npz`, `.npy` or `.cwp` file on the Studio machine, or a `.zip` holding a ChipWhisperer project), `replace`. |
| `POST /api/traces/import/upload?replace=true` | Multipart `file`: upload and import a `.npz`, `.npy`, `.cwp`, or a `.zip` holding a ChipWhisperer project (as downloaded above). |

### Analysis and glitching

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/analysis/cpa/start` | Body: `model`, `trace_start`, `trace_end`, `point_range` ([start, end]), `bytes`, `known_key`, `use_stored_key`, `report_every`. |
| `POST /api/analysis/cpa/stop` | Stop the attack. |
| `GET /api/analysis/cpa` | Latest result: `best_key`, `bytes` (ranking per byte), `history`, `done`, `traces_used`, `known_key`. |
| `GET /api/analysis/cpa/corr/{b}` | Correlation trace for key byte `b` (binary frame). |
| `POST /api/glitch/start` | Body: `parameters` ([{path, start, stop, step}] or [{path, values}]), `command`, `data`, `expected`, `output_len`, `repeats`, `order`, `reset`, `reset_on`, `reset_delay`, `glitch_timeout`, `arm_scope`. |
| `GET /api/glitch/results` | Results, counts and progress. |
| `POST /api/glitch/export` | Body: `path` (CSV). |

### Toolchains and firmware

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/toolchains` | Host, registry revision and every toolchain's status. |
| `POST /api/toolchains/{id}/install?force=false` | Start a download (no-op if installed unless `force=true`). |
| `POST /api/toolchains/{id}/cancel` | Cancel a download. |
| `DELETE /api/toolchains/{id}` | Remove an installed toolchain. |
| `POST /api/toolchains/custom` | Body: `name`, `compiler`, `arch`, `prefix`, `url` and `sha256`, or `path`, `version`, `bin`. |
| `DELETE /api/toolchains/custom/{id}` | Remove a custom toolchain. |
| `POST /api/toolchains/refresh` | Fetch the latest registry from the Studio repository. |
| `GET /api/firmware` | Sources status, projects, platforms, crypto targets, SimpleSerial versions. |
| `PUT /api/firmware/channel` | Body: `channel`. |
| `POST /api/firmware/sources/check` | Compare installed sources with GitHub. |
| `POST /api/firmware/sources/fetch` | Body: `ref` (optional). Download or update sources. |
| `POST /api/firmware/sources/cancel` | Cancel the download. |
| `PUT /api/firmware/sources` | Body: `root` (your own firmware folder, or null for the downloaded sources). |
| `POST /api/firmware/plan` | Dry run of a build (same body as build). |
| `POST /api/firmware/build` | Body: `project`, `platform`, `compiler` (gcc, clang), `crypto_target`, `ss_ver`, `cflags`, `make_args`, `clean`, `jobs`. |
| `GET /api/firmware/build` | Build status. |
| `GET /api/firmware/build/log?since=N` | Build output lines. |
| `POST /api/firmware/build/cancel` | Cancel the build. |
| `GET /api/firmware/builds` | Built images. |
| `GET /api/firmware/builds/{name}` | Download a built `.hex`. |
| `POST /api/firmware/program` | Body: `path`, `programmer` (both optional: defaults to the last build and its platform's programmer). |

### Notebooks and kernel

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/notebooks` | Notebook list and tutorial status. |
| `GET /api/notebooks/file?path=` | Load a notebook (cells with string sources and ids) and its `mtime`. |
| `PUT /api/notebooks/file` | Body: `path`, `notebook`, and optionally `base_mtime` (the `mtime` the editor loaded) and `client` (a window id, echoed in the `nb` `file` event). With `base_mtime`, the save is refused with 409 if the file changed since, and 410 if it was deleted. |
| `DELETE /api/notebooks/file?path=` | Delete; its kernel is shut down. |
| `POST /api/notebooks/new` | Body: `name`. Create from the starter template. |
| `POST /api/notebooks/import` | Multipart `file`: import an `.ipynb` into `imported/`. |
| `GET /api/notebooks/download?path=` | Download as `.ipynb`. |
| `GET /api/notebooks/asset?path=` | An image referenced by notebook Markdown. |
| `POST /api/notebooks/tutorials/fetch` | Download or update NewAE's tutorial notebooks. Files you edited or added are kept; if some would be replaced by a changed upstream version, the job stops in state `confirm` with `conflicts` until you call again with `{"on_modified": "backup"}` (install, keep your copies as `<name>.local-<timestamp>`) or `{"on_modified": "keep"}`. The finished job reports what was `kept` and the `backups`. |
| `POST /api/notebooks/run` | Body: `path`, `timeout`, `stop_on_error`, `kernel`. Run all cells in the notebook's own kernel (or `kernel`), save outputs, wait. |
| `GET /api/kernel?kernel=` | Kernel status: `kernel`, `path`, `started` (false until the notebook first runs a cell), `busy` (a cell of this kernel is running), `cell`, `queued` (this kernel's queued cells), `execution_count`, `store_traces`, `running_kernel` (whose cell runs now, in any kernel), `queued_total`. |
| `GET /api/kernel/variables?kernel=` | Variables defined in the kernel. |
| `POST /api/kernel/execute` | Body: `cells` ([{id, code}]), `path` (working directory, by default the notebook's folder), `kernel`. Queue cells; results arrive as `nb` events. |
| `POST /api/kernel/run` | Body: `code`, `path`, `kernel`, `timeout`. Run one cell and wait for its outputs (the reply includes `kernel`). |
| `POST /api/kernel/interrupt` | Body: `kernel`. Interrupt that kernel's running cell and drop its queued cells; other kernels are untouched. `"kernel": "all"` interrupts every kernel. |
| `POST /api/kernel/restart` | Body: `kernel`. Clear that kernel's namespace and execution count. |
| `GET /api/kernels` | Every running kernel: the status above plus `holders` (windows that have the notebook open) and `variables` (how many). |
| `POST /api/kernels/shutdown` | Body: `kernel`. Stop the kernel and free its namespace; it starts again, empty, on the next cell. For the default kernel this is a restart. |
| `POST /api/kernels/rename` | Body: `kernel`, `to`. Re-key a kernel, keeping its variables (for example a temporary id that becomes a path when a notebook is saved). |
| `POST /api/kernels/attach` | Body: `client` (a window id), `kernels` (the ids that window has open, all of them each time; `[]` when it closes). Studio's own windows call it so that a notebook's kernel is shut down about 20 seconds after it is closed in the last window. |
| `POST /api/notebooks/rename` | Body: `path`, `to`. Rename or move a notebook; its kernel moves with it. 409 if `to` exists. |

Each notebook has its own kernel. The optional `kernel` parameter (query parameter for `GET`, body field or query parameter for `POST`) is a notebook path relative to the notebooks folder (the id the Notebook tab uses for that notebook), any other string as a temporary id, or empty for the shared **default** kernel. Requests without it behave as before, against the default kernel. Kernels are created by the first `execute` or `run` that names them; status and variables of a kernel that has not started report it as idle and empty without creating it. `POST /api/notebooks/run` runs in the notebook's own kernel unless its body names another `kernel`, and deleting a notebook shuts its kernel down. Cells of all kernels share one queue and run one at a time on the hardware thread.

### Interfaces

Everything in the [Interfaces](Protocols-and-Interfaces) tab. Requests for something the connected model does not support return 400 with the reason (`Unsupported: ...`). Booleans accept `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off`.

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/interfaces` | Capabilities plus the state of every interface: UART pins and settings, SimpleSerial version, SPI master, the trigger last applied, OpenOCD and MPSSE. |
| `GET` / `PUT /api/interfaces/uart` | Body: `baud` (500 to 2,000,000), `parity` (none, odd, even, mark, space), `stop_bits` (1, 1.5, 2), `rx` and `tx` (tio1 to tio4, only modes valid for the model), `data_bits` (always 8). |
| `POST /api/interfaces/simpleserial/connect` | Body: `version` (1.0, 1.1, 2.1 or cdc). Connects the matching SimpleSerial target. |
| `POST /api/interfaces/simpleserial/send` | Body: `cmd`, `data` (hex), `read_cmd` (r), `read_len`, `timeout` (ms). Returns `response` (hex or null). Version 1.0 does not wait for an ack. |
| `POST /api/interfaces/spi/enable` | Body: `speed` (Hz, 1 kHz to 20 MHz, default 1 MHz), `cs` (pdid, pdic, tio3, tio4). |
| `POST /api/interfaces/spi/disable` | Turn the SPI master off. |
| `POST /api/interfaces/spi/transfer` | Body: `data` (hex bytes for MOSI), `start`, `stop` (chip select framing), `writeonly`. Returns `miso`. |
| `POST /api/interfaces/spi/toggle_sck` | Body: `cycles` (1 to 255). |
| `GET` / `PUT /api/interfaces/gpio` | Pin modes and levels; PUT body: `pin`, `state` (high, low, high_z). |
| `POST /api/interfaces/gpio/pulse` | Body: `pin` (nrst or pdic), `ms` (1 to 5000). |
| `GET` / `PUT /api/interfaces/userio` | Husky USERIO header; PUT body: `direction` (bit mask, 1 = driven by the Husky), `drive` (bit mask), `mode` (normal). |
| `GET` / `PUT /api/interfaces/trigger` | PUT body: `kind` (basic, uart_decode, uart_pattern, edge_counter, adc_level, sequencer, sad) plus its parameters (the same as the MCP tool `trigger_configure`). |
| `POST /api/interfaces/bitbang` | Body: `bits` (`'0110...'`), `record` (same length, 1 = release and record that slot), `data_pin`, `clock_pin`, `clk_div`. Returns the recorded bits. |
| `POST /api/interfaces/onewire` | Body: `action` (reset, read_rom), `data_pin`, `clk_div`. Returns the presence and the ROM (family, serial, CRC check). |
| `GET /api/interfaces/openocd` | OpenOCD binary, scripts folder, server state, ports, MPSSE state and the newest log lines. `mpsse` carries `detected: true` (with `connected`, `sn` and `kind`) for a scope Studio found already in MPSSE mode, for example after a restart. |
| `GET /api/interfaces/openocd/targets` | Target configurations in OpenOCD's scripts folder. |
| `GET /api/interfaces/openocd/log?since=` | OpenOCD log lines. |
| `POST /api/interfaces/openocd/mpsse` | Body: `enable`, `transport` (jtag, swd), `header` (target, userio). Enabling releases the scope; disabling restores normal mode and reconnects it, also for a scope that was found already in MPSSE mode. |
| `POST /api/interfaces/openocd/start` | Body: `target_cfg` (for example `target/stm32f3x.cfg`), `transport`, `ports` (`{gdb, telnet, tcl}`), `extra` (a list of `-c` commands). |
| `POST /api/interfaces/openocd/stop` | Stop the OpenOCD server. |
| `POST /api/interfaces/openocd/command` | Body: `command`. Runs it on OpenOCD's TCL port; returns `ok` and the output. |
| `POST /api/interfaces/openocd/program` | Body: `path` (.hex, .elf or .bin on the Studio machine), `verify`, `reset`, `address` (for .bin). |

### Logic analyser

The [Logic](Logic-Analyser) tab. Sample indices count from the start of the capture; times are in seconds from the trigger. Channels can be given by index or by name.

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/la` | State: the running job, the current capture, the captures in memory, decoders and buses. |
| `GET /api/la/sources` | Capture sources with availability and reason (native, adc, sim, sigrok, files), the Husky LA triggers and clock sources, the decoders with their options, the default settings of each source (`defaults`, the same as the Logic tab and the MCP tools), and the scope's ADC and LA clock rates. |
| `POST /api/la/capture` | Body: `source` (native, adc, sim, sigrok), `settings` (per source, as in the tab: native `group`, `clk_source`, `oversampling`, `downsample`, `depth`, `trigger`, `fire`, `with_analog`; adc `segments`, `samples`, `level`, `hysteresis`, `fire`, `invert`; sim `samplerate`, `duration_ms` or `samples`, `pretrigger`, `channels`, `jitter_ns`, `glitches_per_ms`; sigrok `device`, `samplerate`, `channels`, `samples` or `time_ms`, `triggers`, `config`; any source `persist` to keep a `.sr` copy), `wait`, `timeout`. |
| `POST /api/la/stop` | Stop the running capture. |
| `GET /api/la/captures`, `PUT /api/la/current`, `DELETE /api/la/captures/{cid}` | The captures in memory; choose the current one (body `id`); delete one. |
| `GET /api/la/capture?capture=` | A capture's summary: channels, sample rate, trigger, duration, analog rows. |
| `POST /api/la/view` | Body: `a`, `b` (sample range), `px` (width in pixels), `channels`, `buses`, `decoders`, `analog`. Returns the edges (or per-pixel summaries) of each channel and the decoded annotations in range, with `notices`. A bus whose channels are missing is returned `disabled` with the `missing` names; a decoder that fails on this capture returns `{id, type, name, error, rows: []}` while the rest of the view is drawn. This is what the tab draws from. |
| `GET` / `PUT /api/la/channels` | Channels, analog rows, order and buses; PUT body: `channels` (`[{channel, name, color, hidden}]`: `channel` picks the channel by index or current name, `index` is an alias, `name` renames it), `order`, `buses` (`[{name, channels, format}]`; buses refer to channels by name and follow them into later captures), `analog` (`[{index, hidden, remove}]`). Responses list the channels with their `index`, and buses with `missing`, `disabled` and a `notice` when their channels are not in this capture. |
| `POST /api/la/measure` | Body: `channel` (or `channels`; all when left out) and a range (`from`/`to` samples or `t0`/`t1` seconds). Returns edges, frequency, period, duty cycle and pulse widths. |
| `POST /api/la/search` | Body: `kind` (edge, pattern, decoded), `channel`, `edge`, `pattern`, `edge_channel`, `text`, `decoder`, `from`, `direction` (next, prev). Returns `found`, `index` and `t`. |
| `GET` / `POST /api/la/decoders` | The decoders shown in the tab; POST body: `type` (uart, spi, i2c, onewire, jtag, swd, can, simpleserial, sigrok), `channels` (`{role: index or name}`, guessed from the channel names when left out), `options`, `name`. |
| `PUT` / `DELETE /api/la/decoders/{did}` | Change or remove a decoder. Adding or changing a decoder with invalid options returns 400 with the reason. |
| `POST /api/la/decode` | Run a decoder once without adding it. Body: `type`, `channels`, `options`, `limit` (500). Invalid options return 400. |
| `GET /api/la/annotations` | Decoded results: `decoder`, `q` (filter), `offset`, `limit`, `after`, `before`, `row`. |
| `GET /api/la/annotations.csv` | The same as CSV. |
| `POST /api/la/import` | Body: `path` (a .vcd, .csv or .sr file on the Studio machine), `format`, `samplerate` (for CSV files without a time column). |
| `POST /api/la/import/upload` | Multipart `file` (query `format`, `samplerate`). |
| `POST /api/la/export` | Body: `format` (vcd, csv, sr), `path` (default: the `logic/exports` folder), `channels`. Returns the path. |
| `GET /api/la/download/{fmt}` | Export the current capture and download it. |
| `GET /api/la/files` | Logic files in the data folder (captures, exports, imports, sigrok). |
| `POST /api/la/analog/from_trace` | Body: `index` (stored trace, default the newest). Adds it as an analog row on the capture's time base. |
| `POST /api/la/analog/to_waveform` | Body: `index`. Adds an analog row to the trace store, so it shows in the waveform view. |
| `GET /api/la/sigrok?refresh=`, `GET /api/la/sigrok/scan`, `GET /api/la/sigrok/decoders` | sigrok-cli status and install help, the analysers it finds, its protocol decoders. |

### Code map

[Code on the Waveform](Code-on-the-Waveform). Sample indices are those of the stored traces.

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/codemap` | State: firmware (ELF, architecture, core, SimpleSerial version), the inputs it ran with, whether the emulated response is the correct AES and matches the stored trace, the cycle to sample mapping and the last alignment, and which emulators are available, and `pathsep` (the separator for several source folders). |
| `GET /api/codemap/elfs` | ELF files to choose from: the one programmed in this session, then Studio's firmware builds. |
| `POST /api/codemap/build` | Body: `elf` (path; default the programmed firmware or the newest build), `sources` (folder, or several separated by `pathsep`; `""` clears a folder set before and goes back to the paths in the ELF), `trace` (index; default the newest), `key`/`text` (hex, instead of a trace; `text: ""` sends the command without data), `cmd` (`p`), `raw` (hex serial bytes fed as they are, for firmware that is not SimpleSerial; `raw_text: true` for text), `core`, `protocol`, `wait_states`, `mapping` (`{adc_freq, target_freq, adc_offset, presamples, decimate, scale, shift}`), `align` (true, false or `auto`), `options` (`{skip, getch, putch, trigger_high, trigger_low, max_instructions}`). |
| `POST /api/codemap/upload` | Multipart `file`: upload an ELF; returns its path for `build`. |
| `GET /api/codemap/band` | The timeline the code band draws: function spans and source line runs in cycles from the trigger, and the mapping. |
| `POST /api/codemap/region` | Body: `start`, `end` (samples), `limit`. The functions and source lines that ran there; code without line information is one line entry with `no_line_info: true`. |
| `POST /api/codemap/lookup` | Body: `function`, or `file` and `line`. Where it ran: cycle and sample ranges. |
| `POST /api/codemap/align` | Body: `source` (mean, trace), `trace`, `scale_min`, `scale_max`, `max_shift`, `apply` (true, false or `auto`: apply unless the confidence is low). |
| `PUT /api/codemap/mapping` | Body: `shift`, `scale` (absolute), `dshift` (samples) or `dscale` (fraction) to nudge, `adc_offset`, `presamples`, `decimate`, `adc_freq`, `target_freq`, `reset`. |
| `GET /api/codemap/source?file=` | A source file: text, lines with code, executed lines with their cycles. |
| `GET /api/codemap/disasm` | Disassembly of a line (`file`, `line`), a `function` or an address range (`lo`, `hi`), with how often each instruction ran. |
| `GET /api/codemap/at?sample=` | The instruction (address, function, file and line, cycle) at a sample. |
| `GET /api/codemap/model?n=` | The emulated power model resampled to the trace's samples (binary frame). |
| `POST /api/codemap/pctrace` | Exact mode: record program counter samples through the Husky's Arm trace port (SWO) and compare them with the emulation. Body: `interval` (rounded to the nearest the Arm DWT supports, 64 or 1024 cycles times 1 to 16; the reply has `interval` and `interval_requested`), `swo_div`. When no samples arrive the reply has a `message` with the likely cause. |

### Notes and calculator

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/notes` / `POST /api/notes` | List notes, create one (body: `name`). |
| `GET /api/notes/{name}` | Read a note. |
| `PUT /api/notes/{name}` | Body: `text`, optional `rename`. |
| `DELETE /api/notes/{name}` | Delete a note. |
| `POST /api/calc` | Body: `expr`. Returns `value`, `text`, and `hex`, `bin`, `dec` for integers. |
| `GET /api/calc/variables` | Calculator variables. |
| `POST /api/calc/stats` | Body: `values`, or `source: "trace"` with `index`, `start`, `end`, or `source: "sample"` with `sample`, `sample_end`, `trace_start`, `trace_end`. |

## Examples

### curl: simulator capture and CPA

```bash
B=http://127.0.0.1:8765
curl -s -X POST $B/api/scope/connect -H 'Content-Type: application/json' -d '{"kind":"sim"}'
curl -s -X POST $B/api/target/connect -H 'Content-Type: application/json' -d '{"kind":"sim"}'
curl -s -X POST $B/api/capture/start -H 'Content-Type: application/json' -d '{"count":100,"clear":true,"key":"2b7e151628aed2a6abf7158809cf4f3c"}'
sleep 3
curl -s $B/api/traces
curl -s -o traces.npz $B/api/traces/download/npz
curl -s -X POST $B/api/analysis/cpa/start -H 'Content-Type: application/json' -d '{"model":"sbox_hw"}'
sleep 2
curl -s $B/api/analysis/cpa | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['best_key'], r['done'])"
```

### Python: capture, wait and recover the key

```python
import time
import requests

B = "http://127.0.0.1:8765"
s = requests.Session()

def wait_idle():
    while True:
        job = s.get(f"{B}/api/status").json().get("job") or {}
        if not job.get("running"):
            return
        time.sleep(0.2)

s.post(f"{B}/api/scope/connect", json={"kind": "sim"}).raise_for_status()
s.post(f"{B}/api/target/connect", json={"kind": "sim"}).raise_for_status()
s.post(f"{B}/api/capture/start", json={"count": 100, "clear": True, "key": "2b7e151628aed2a6abf7158809cf4f3c"}).raise_for_status()
wait_idle()

s.post(f"{B}/api/analysis/cpa/start", json={"model": "sbox_hw"}).raise_for_status()
while not (r := s.get(f"{B}/api/analysis/cpa").json()).get("done"):
    time.sleep(0.2)
print("key:", r["best_key"])
```

### Python: build firmware and program it

```python
r = s.post(f"{B}/api/firmware/build", json={"project": "simpleserial-aes", "platform": "CWLITEARM", "compiler": "gcc"}).json()
while r["state"] == "running":
    time.sleep(0.5)
    r = s.get(f"{B}/api/firmware/build").json()
print(r["state"], r.get("hex"), r.get("size"))
if r["state"] == "ok":
    print(s.post(f"{B}/api/firmware/program", json={}).json())
```

The compiler and firmware sources must be installed first (see [Toolchains](Toolchains) and [Firmware Sources](Firmware-Sources)), otherwise the build call returns an error that says what is missing.

### Python: run a notebook cell

```python
r = s.post(f"{B}/api/kernel/run", json={"code": "import chipwhisperer as cw\nscope = cw.scope()\nlen(studio.traces)"}).json()
print(r["ok"], [o.get("data", {}).get("text/plain") or o.get("text") for o in r["outputs"]])
# the same in the kernel of a notebook (sees the variables its tab in the Notebook tab defined)
r = s.post(f"{B}/api/kernel/run", json={"code": "sorted(k for k in globals() if not k.startswith('_'))", "kernel": "lab/attack.ipynb"}).json()
print(r["kernel"], r["outputs"][-1]["data"]["text/plain"])
```

### Python: follow live events

```python
import asyncio, json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        async for msg in ws:
            if isinstance(msg, bytes):
                continue  # binary trace frame, decode with decode() above
            ev = json.loads(msg)
            if ev["type"] in ("capture", "log"):
                print(ev)

asyncio.run(main())
```

See also: [MCP Server](MCP-Server), [Command Line and Configuration](Command-Line-and-Configuration).
