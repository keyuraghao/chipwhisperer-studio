# ChipWhisperer Studio design

ChipWhisperer Studio (`cwstudio`) is a standalone, cross-platform desktop application for ChipWhisperer capture hardware. It replaces the "open a Jupyter notebook and type Python" workflow with a point-and-click tool built around a live waveform viewer, while still driving the hardware through the unmodified `chipwhisperer` Python package.

## Goals

1. **Zero-setup install.** A user downloads one archive for their OS, unzips it and double-clicks. No Python, no pip, no Jupyter. The bundle carries its own Python runtime, the `chipwhisperer` package and `libusb`.
2. **See the waveform.** Every captured trace is streamed to the screen as it arrives, with overlay, averaging, zoom, cursors and per-trace inspection of plaintext, ciphertext and key.
3. **Do the whole job.** Build firmware, connect, configure the scope, program the target, talk to it over serial, capture thousands of traces, run a CPA attack, run a glitch sweep and export, all from one window.
4. **Do not fork the library.** All hardware access goes through the public `chipwhisperer` API (`cw.scope()`, `cw.target()`, `cw.capture_trace()`, `cw.program_target()` and settings properties). Studio is a thin, well-behaved client, so it keeps working as the library evolves.
5. **Hardware optional.** A built-in simulator (`--simulate`) provides a scope and target pair that leaks like a real unprotected AES implementation. Every feature can be demonstrated and tested without hardware.
6. **Agents are first-class users.** Everything the UI can do is available through a documented HTTP API and an MCP server.

## Architecture

![ChipWhisperer Studio architecture](images/architecture.svg)

Everything runs in one Python process. Clients (the browser UI, AI agents through the MCP server, and scripts) all use the same HTTP API and WebSocket. A single worker thread owns the hardware; CPA analysis, toolchain downloads and firmware builds run on their own threads and only reach the hardware through the worker.

### Why a local web UI instead of Qt

- One code path for Windows, macOS and Linux with no GUI toolkit system dependencies (no WebKitGTK, no Qt platform plugins).
- The bundle stays small and the build needs only Python and PyInstaller, no Node toolchain: the frontend is plain ES modules served as is.
- Remote use for free: run Studio on the lab machine or a Raspberry Pi next to the target and open it from a laptop (`--host 0.0.0.0`).
- uPlot renders traces with 100k+ samples at 60 fps on a canvas.

### Concurrency model

`chipwhisperer` objects are not thread-safe and the USB transport is stateful, so exactly one thread (`HardwareWorker`) touches the scope and target. The asyncio server hands every hardware request to that thread and awaits a future. Long-running operations (a 10 000 trace capture, a glitch sweep) are long jobs: objects with a `step()` method that the worker calls repeatedly, servicing queued short jobs between steps. That keeps the UI responsive (you can read settings, poll the serial console and stop the capture) without ever running two hardware calls at once.

CPA analysis is pure numpy over an in-memory trace array and runs on its own thread with progressive reporting. Toolchain downloads and firmware builds also run on their own threads because they never touch the hardware; only the final "program target" step goes through the hardware worker.

### Event streaming

A single WebSocket (`/ws`) carries server to client events. JSON text frames carry status, log lines, capture progress, serial data, analysis updates, toolchain download progress, firmware source updates and build output. Trace waveforms are sent as binary frames:

```
u32 little-endian: JSON header length
JSON header      : {"type":"trace","index":123,"n":5000,"dtype":"f32", ...}
f32[n]           : samples
```

Trace publication is rate-limited (25 frames/s by default) so a fast capture does not flood the socket; the in-memory `TraceStore` keeps every trace regardless.

### Settings introspection

Every scope and target sub-object in `chipwhisperer` implements `_dict_repr()`. `settings.py` walks that tree, looks up the matching `property` on the class to learn whether it is writable and to pull the docstring, and emits a JSON tree the UI renders generically. This works for Nano, Lite, Pro and Husky without per-device UI code and picks up new settings automatically. A small table of known enumerations (for example `gain.mode` in `{low, high}`) improves the widgets where the docstring cannot be parsed.

### Firmware builds

`toolchains.py` manages compilers. `resources/toolchains.json` pins each toolchain by version, per-host URL (plus mirrors) and SHA-256. An install downloads the archive (switching to the next mirror if a server fails or is too slow), verifies the checksum, unpacks it with path traversal checks, creates tool name aliases where ChipWhisperer's makefiles expect different prefixes (for example `riscv32-unknown-elf-gcc` for xPack's `riscv-none-elf-gcc`) and records a marker file. Custom toolchains can be registered from an archive URL or an existing folder, and compilers on `PATH` are used as a fallback. The registry can refresh itself from the Studio repository, so new compiler versions do not require a Studio release.

![How Studio builds and flashes firmware](images/firmware-flow.svg)

`firmware.py` manages sources and builds. Sources are not bundled: the manager asks GitHub which commit the chosen channel (`develop`, `latest-release`, a tag or a commit) points to, downloads `firmware/mcu` from that commit plus the `chipwhisperer-fw-extra` submodule commit it pins, and records what it installed so "check for updates" can compare against upstream. A known-good commit is used when GitHub is unreachable. Projects are discovered from makefiles that include `Makefile.inc`, and platforms, HALs and MCUs are parsed from `hal/Makefile.hal`.

A build runs ChipWhisperer's own makefiles. `plan()` computes the whole build (make command, `PATH`, environment, output file, programmer) without side effects, which the UI and MCP expose as a dry run. GCC builds pass the tool names explicitly and add compatibility flags that ChipWhisperer's older HALs need with current compilers (`-fcommon`, relaxed implicit declaration errors, RISC-V `-misa-spec=2.2`). Clang builds set `CC` to `ccwrap.py`, which compiles C with clang (using GCC's newlib or avr-libc headers), strips GCC-only options, spells out RISC-V extensions, and hands hand-written assembly to GCC; GCC links (`LINK_COMPILER`), so the firmware uses the same C library and linker scripts as a GCC build. In the frozen bundle the Studio executable itself acts as the wrapper (`ChipWhispererStudio --ccwrap`).

### MCP server

`mcp_server.py` is a Model Context Protocol server built on the official Python SDK. Each tool is a thin wrapper around an HTTP API route, so the agent sees exactly the features and options of the UI, and one session can be shared between a person in the browser and an agent. If no Studio answers at `--url`, the server starts a headless one in the same process. Long operations accept `wait=true` so an agent can run "capture 500 traces" or "build and flash" as a single call.

### Modules

| File | Responsibility |
|------|----------------|
| `cli.py` | Entry point: argument parsing, uvicorn, browser or native window, and the `mcp` / `--ccwrap` subcommands. |
| `app.py` | FastAPI app: REST endpoints, WebSocket, static files. |
| `session.py` | The single application state: scope, target, jobs, trace store, toolchain and firmware managers. |
| `worker.py` | The hardware thread, futures and long job scheduling. |
| `hardware.py` | Connect, disconnect, detect and program on real hardware. |
| `simulator.py` | `SimScope` and `SimTarget`: AES leakage and glitch behaviour without hardware. |
| `settings.py` | Generic settings tree introspection and typed assignment. |
| `capture.py` | `CaptureJob`: key and text generation, capture loop, publication. |
| `glitch.py` | `GlitchJob`: parameter sweep, target reset, result classification. |
| `traces.py` | `TraceStore`: in-memory traces, statistics, import and export (npz, cwp, csv). |
| `analysis.py` | Progressive CPA with several AES leakage models. |
| `toolchains.py` | Pinned, checksummed, on-demand compiler downloads and custom toolchains. |
| `firmware.py` | Firmware sources from GitHub, project and platform catalogue, builds. |
| `ccwrap.py` | Compiler wrapper that lets GCC-oriented makefiles build with clang. |
| `mcp_server.py` | MCP server exposing every feature to AI agents. |
| `events.py` | Thread-safe event bus fanning out to WebSocket clients. |
| `static/` | Frontend (vanilla ES modules and uPlot) with light and dark themes. |

### Packaging

`packaging/` holds a PyInstaller spec and a `build.py` driver that creates an isolated venv, installs the project, runs PyInstaller and zips a `ChipWhispererStudio-<os>-<arch>` folder. `.github/workflows/ci.yml` runs the tests on all three operating systems, builds real firmware with downloaded toolchains on each of them, builds and smoke tests the bundles, and publishes a GitHub release with notes from `CHANGELOG.md` when a version tag is pushed.

USB access on a machine without Python:

- **Windows:** the `libusb1` wheel ships `libusb-1.0.dll`; the WinUSB driver is installed by the NewAE driver package (linked from the Connect tab).
- **macOS:** `libusb-1.0.dylib` from `libusb-package` is copied next to `usb1`.
- **Linux:** the same `.so` copy plus the bundled `50-newae.rules`; the Connect tab shows the command that installs the udev rule.

## Non-goals (for now)

- Replacing the analysis and notebook ecosystem for research: Studio exports ChipWhisperer projects so advanced analysis can continue in Python.
- A dedicated UI for FPGA targets (CW305, CW310); they work through the generic settings tree.
