# Release notes

All notable changes to ChipWhisperer Studio are listed here, newest first. Versions follow [Semantic Versioning](https://semver.org/). Each release on GitHub uses the matching section below as its description and carries standalone bundles for Linux, Windows and macOS.

## [Unreleased]

## [0.4.2] - 2026-10-01

### Added

- **Zoom buttons in the waveform view:** zoom in and zoom out (magnifier buttons next to **Fit**, or the **+** and **-** keys) halve or double the visible range of samples, centred on cursor A when it is in view and otherwise on the middle of the view. The zoom stops at the ends of the trace and at about 10 samples, keeps across live traces, and zooming out past the whole trace fits it.

## [0.4.1] - 2026-10-01

A full review of 0.4.0 against 0.3.0 (every HTTP route, all 68 MCP tools on three transports, every tab in the browser, the numerics and the published packages) found the issues below. They are fixed here, together with several older bugs it uncovered.

### Fixed

- **macOS bundle without libusb:** the 0.3.0 and 0.4.0 macOS bundles did not contain libusb, so they could only reach real hardware when Homebrew's libusb was installed. The bundle build now includes it on every OS and fails if it cannot.
- **MCP arguments:** `null` for a parameter that is not optional is rejected again, as in 0.3.0. In 0.4.0 `capture_start(count=null)` reported an error but left an endless capture running, and `scope_connect(kind=null)` disconnected the scope. List items are validated and converted again (`bytes=["0"]`), and numbers, booleans and strings convert as leniently as before.
- **MCP results:** structured results are wrapped as `{"result": ...}` with an output schema again, exactly as 0.3.0 sent them, and lists come as one text block per item. NaN and infinity are sent as `null` instead of invalid JSON.
- **MCP concurrency:** every request runs on its own thread, so `ping`, `capture_stop` and status calls answer at once even while many long calls wait (0.4.0 had a pool of 8).
- **MCP over HTTP:** the streamable HTTP transport checks the Host header (DNS rebinding), Accept, Content-Type, session ids and the protocol version header again, `DELETE` ends a session, and request bodies are bounded. A lone surrogate character in a request no longer stops the stdio server, and on Windows child processes can no longer write into the protocol stream.
- **Uploads:** a request without a file is rejected with 422 as in 0.3.0 (0.4.0 programmed an empty firmware file), filenames containing `;` or escaped quotes are kept intact, file content that happens to contain the boundary text is no longer cut short, and multipart uploads need about half the memory.
- **API errors:** invalid or missing parameters return FastAPI's 422 format again (`{"detail": [{"type", "loc", "msg", "input"}]}`), whole numbers like `3.0` are accepted for integer parameters, `HEAD` requests are refused as before instead of running an export, and `/openapi.json` is back (a compact description of every route). Values that JSON cannot represent (infinity, NaN, numpy keys, dates, enums) no longer cause a 500 (`/api/calc` with `1e308*10`, trace meta of a trace containing NaN).
- **CPA results are bit-identical to 0.3.0 again:** 0.4.0 rearranged the correlation formula, which changed the last bit of some values and, with ties, the reported sample. The formula is restored inside the faster blocked computation.
- **Live view:** the WebSocket loop sends queued events without per-event task overhead (0.4.0 was slower than 0.3.0 when events backed up) and cleans up its pending read when a window closes.
- **Header Stop button:** a capture or glitch sweep started from the panel, a notebook, a script or an agent now shows in the header right away with live progress, and the header Stop button works during it. Before, the header said Idle and Stop stayed disabled until the job ended.
- **`plt.show()` in the first cell** that imports pyplot now shows the figure at that point too, without a warning (Studio has its own matplotlib backend for notebooks). Figures can also be saved as SVG, PDF and PS in the bundles, and common standard modules (`zoneinfo`, `tomllib`, `sqlite3` and others) import there again.
- **ChipWhisperer project download:** **Download in browser** for `.cwp` now gives a zip with the project and its data folder (it was a 264 byte file that could not be opened), and importing such a zip works.
- **Notebook import** never overwrites an earlier import with the same name, `notebook_read` over MCP works, relative paths for trace import resolve inside the data folder like export, `python -m cwstudio mcp --no-embed` exits with status 1 when Studio is not running, and **reset FPGA** on a scope without an FPGA says so.
- **Markdown:** tabs in code blocks are kept, SVG images given as data URLs show again, and inline styles can no longer pin content over the whole window.
- **Packaging:** README images that went missing with the wiki move are back, the Python badge says 3.10 to 3.12, the sdist includes the test helpers, the package metadata uses the SPDX licence expression, and CI checks README links and the plain (MCP SDK free) install.

### Performance

CPA with live progress (5000 traces of 5000 samples, progress every 50 traces) takes 13 to 15 s against 24 to 26 s in 0.3.0, about 1.8 times faster, with bit-identical results. The 0.4.0 table below listed 3.0 times from one run; independent runs of 0.4.0 measured 2.1 times.

## [0.4.0] - 2026-10-01

This release makes Studio smaller and faster. Heavy libraries that Studio used only a small part of are replaced by compact built-in code, so a pip install pulls in about 25 fewer packages and the standalone bundles shrink by a third, while CPA, the simulator and live streaming get several times faster. The HTTP API, the MCP tools and the file formats are unchanged.

### Added

- **Wiki:** a full user and developer guide in `docs/wiki`, published to the GitHub wiki by CI, with new screenshots of every tab and a diagram of how notebook cells share the hardware.

- **Built-in MCP server** (`mcplite.py`): Studio now speaks the Model Context Protocol itself (stdio, streamable HTTP and SSE) instead of using the MCP SDK. The 68 tools, their schemas, the prompts and the resources are identical, and agents see no difference; it was checked against the official MCP client on all three transports.
- **Built-in router** (`web.py`): the HTTP API runs on Starlette directly with a small routing layer instead of FastAPI, so pydantic is no longer needed. `/api/docs` now lists every endpoint with its parameters (the wiki's HTTP API page has the details).
- Uploads also accept the file as the raw request body with `?filename=NAME`, besides `multipart/form-data`.

### Changed

- **Fewer dependencies:** `fastapi`, `mcp`, `python-multipart` and the `uvicorn[standard]` extras are gone, and with them pydantic, pydantic-core, jsonschema, rpds, httpx2, opentelemetry, pyjwt, cryptography, uvloop, httptools, watchfiles and PyYAML. Studio now needs Starlette, uvicorn, websockets, numpy, matplotlib and a few small packages.
- **Smaller bundles:** the Linux bundle zip shrinks from 108 MB to 64 MB (macOS 80 MB to 61 MB, Windows 67 MB to 49 MB). Besides the dependencies above, the bundles leave out Cython, setuptools, matplotlib's GUI backends and sample data, and Pillow's AVIF, WebP and colour management codecs, and strip debug symbols on Linux.
- **Smaller frontend:** the notebook and notes Markdown renderer and HTML sanitizer are now one 14 KB module (`markdown.js`) instead of marked and DOMPurify (76 KB). It renders NewAE's tutorial notebooks like before, leaves LaTeX math untouched, and keeps notebook output safe from scripts.
- **Faster CPA:** about 2 to 3 times faster with live progress (5000 traces of 5000 samples) and up to 1.6 times faster without; progress reports no longer allocate hundreds of MB.
- **Faster capture and simulator:** the simulator synthesises traces 3 times faster and AES runs 7 times faster, so simulated captures run about 2.7 times faster. The same seed still gives the same traces.
- **Faster live view:** each WebSocket event is encoded once for all open windows instead of once per window, the WebSocket loop sleeps until there is an event instead of waking every second, the trace store keeps running counters for its summary, and the waveform view reuses buffers instead of allocating them for every frame.
- The Connect tab now preselects SimpleSerial v2, which current ChipWhisperer firmware uses, instead of the legacy v1 protocol.
- In notebooks, `cw.plot()` returns a plot object that combines with `*` and `+` like ChipWhisperer's holoviews version (`cw.plot(a) * cw.plot(b)`, `fig = cw.plot()`), and `plt.show()` shows figures immediately.
- `studio.build_firmware()` defaults to SimpleSerial v2.1, like the Firmware tab.

### Performance

Measured before and after on the same machine (about 15% noise); results are identical. On real hardware, captures are mostly limited by USB and the target, so the gains show mainly in CPA and the live view.

| Operation | Before | After | Speedup |
|---|---|---|---|
| Live update encoding (500 events, 8 open windows) | 30.6 ms | 3.8 ms | 8.1x |
| AES encryption (5000 blocks) | 650 ms | 93 ms | 7.0x |
| CPA, 5000 traces of 5000 samples, progress every 50 traces | 26.6 s | 8.8 to 14 s | 2.1x to 3.0x |
| Simulator trace generation (5000 traces) | 1340 ms | 441 ms | 3.0x |
| Simulated capture loop (1000 traces) | 460 ms | 168 ms | 2.7x |
| Trace export as arrays | 33 ms | 20 ms | 1.65x |
| CPA, progress every 1000 traces | 3.1 s | 1.9 s | 1.6x |
| Trace block fetch for the waveform view | 11.4 ms | 7.8 ms | 1.5x |
| Mean, min and max statistics | 81 ms | 64 ms | 1.3x |
| Trace store summary | 3.3 ms | about 0 | effectively instant |

### Fixed

- `cw.trace` (TraceWhisperer) failed with "No module named 'pkg_resources'" in the bundles and in new Python environments, because setuptools 81 removed that module. Studio now provides the small part of it that ChipWhisperer uses, and the bundles include ChipWhisperer's trace modules again.
- SVG figures in notebooks show their text, tick labels and titles again (the old sanitizer removed the `<use>` elements matplotlib draws text with).
- **Download in browser** for the NumPy `.npy` set now downloads one zip with all four files instead of failing.
- The serial console shows traffic from before the page was opened.
- The build log prints the `make clean` command before its output, and `clean` receives the same SimpleSerial version as the build, so the log no longer shows a misleading "SS_VER set to SS_VER_1_1".

## [0.3.0] - 2026-09-30

### Added

- **Notebook tab:** Jupyter-style notebooks that run inside Studio. Write Python cell by cell (Shift+Enter to run, Run all, Stop, Restart), with text (Markdown) cells, inline matplotlib figures, rich outputs and a variables panel. Notebooks are standard `.ipynb` files: create them in Studio, import your own, export them back to Jupyter.
  - Cells share Studio's hardware connection: inside a notebook `cw.scope()` and `cw.target()` return the devices connected in the Connect tab (or connect them), and traces captured with `cw.capture_trace()` or a manual `scope.arm()` / `scope.capture()` / `scope.get_last_trace()` loop appear live in the waveform view and the Capture tab, with their plaintext, ciphertext and key.
  - IPython features used by ChipWhisperer's tutorials work: `%run other.ipynb`, `!make ...` and `%%bash -s` with Studio's downloaded compilers on `PATH` and `{var}` / `$var` expansion, `%cd`, `%env`, `%time`, `%%writefile`, `display()`, `IPython.display`, `tqdm.notebook` progress bars.
  - **ChipWhisperer tutorials:** one click downloads NewAE's chipwhisperer-jupyter notebooks (SCA101, Fault101 and more) at the version matching your firmware sources, with the firmware folder linked so their build cells work. NewAE's Lab 3_3 (DPA on firmware AES) runs unmodified: setup, firmware build, programming and a 2500 trace capture into the Capture tab.
  - A `studio` helper in every notebook: `studio.traces`, `studio.add_trace()`, `studio.show()`, `studio.build_firmware()`, `studio.program()`.
- **Notes tab:** a text pad with multiple notes, Markdown preview and autosave, plus buttons to insert the latest CPA key or selection statistics.
- **Calc tab:** a calculator for quick maths and side-channel work (bitwise XOR, hex and binary, `hw()`, `hd()`, AES `sbox()`, `mean()`, `std()`, variables) and statistics (count, sum, mean, median, min, max, peak to peak, standard deviation, variance, RMS) of the current selection: selected text anywhere in Studio, the waveform between cursors or in the zoomed range, the whole trace, one sample across all stored traces, or typed numbers.
- **Live selection statistics:** select numbers anywhere in Studio and the log bar shows count, sum, mean, min and max instantly, like a spreadsheet status bar.
- MCP tools for the new features (68 tools in total): `notebook_run_code`, `notebook_run`, `notebook_read`, `notebook_write`, `notebook_list`, `kernel_variables`, `kernel_interrupt`, `kernel_restart`, `tutorials_fetch`, `note_read`, `note_write`, `notes_list`, `calculate`, `selection_stats`.

### Fixed

- HTTPS downloads failed with "certificate verify failed: unable to get local issuer certificate" when Python could not find CA certificates, which affected Refresh list, toolchain installs, firmware source downloads and update checks. This happens in the standalone bundles, with python.org Python on macOS and Windows, and on networks that inspect HTTPS. Studio now verifies certificates against the operating system's trust store (through `truststore`, including roots your IT department installed), falls back to Mozilla's CA bundle (`certifi`), and honours `SSL_CERT_FILE` and `SSL_CERT_DIR`. If verification still fails, the error explains how to fix it.
- Installing a toolchain that is already installed no longer downloads it again.
- Firmware source lookups use `GITHUB_TOKEN` when it is set, avoiding GitHub's limit of 60 anonymous API requests per hour.

### Changed

- matplotlib and tqdm are now dependencies, and matplotlib is included in the standalone bundles.
- Bundles `marked` (MIT) and `DOMPurify` (Apache 2.0 or MPL 2.0) for safe Markdown rendering; HTML outputs of imported notebooks are shown in a script-free sandbox.

## [0.2.0] - 2026-09-30

First release as a standalone project. Studio now lives in its own repository and installs the official `chipwhisperer` package from PyPI instead of shipping inside a ChipWhisperer fork.

### Added

- **Firmware tab:** build any ChipWhisperer firmware project (simpleserial-aes, simpleserial-glitch and the rest) for any platform with GCC or clang, then program the target in one click with "Build & program". Build output streams live, and the result shows the code, data and bss sizes plus a download link for the .hex.
- **On-demand compilers:** Studio downloads official toolchain releases the first time you need them, verifies them against pinned SHA-256 checksums and works offline afterwards:
  - GNU Arm GCC 15.2.1 (xPack) for Arm Cortex-M targets.
  - GNU AVR GCC 7.3.0 with avr-libc (Arduino build) for XMEGA and ATmega.
  - GNU RISC-V GCC 15.2.0 (xPack) for NEORV32 and Ibex.
  - LLVM clang 21 (from the Zig 0.16.0 toolchain) for all three architectures.
  - GNU make and sh on Windows.
- **Custom toolchains:** register any other compiler from an archive URL or an existing folder, for example TriCore, PowerPC or RX, or a specific GCC version.
- **Firmware sources from NewAE's GitHub:** firmware is not bundled. Studio downloads `firmware/mcu` and the matching `chipwhisperer-fw-extra` HALs straight from newaetech/chipwhisperer and can follow `develop`, the latest release, or any tag or commit. "Check for updates" and "Update now" pull new upstream examples and fixes without a new Studio release. A known-good commit is used when GitHub cannot be reached, and you can point Studio at your own checkout instead.
- **Toolchain list updates:** the pinned toolchain list can refresh itself from this repository, so new compiler versions also arrive without a new Studio release.
- **MCP server:** `cw-studio mcp` exposes every Studio feature to AI agents over the Model Context Protocol:
  - 54 tools covering connection, every scope and target setting, programming, serial and SimpleSerial, capture with all options, trace access and export, CPA, glitch sweeps, toolchains, firmware sources and builds.
  - Two guided prompts (CPA attack, glitch search) and live status resources.
  - It attaches to a running Studio, so you can watch the agent work in the browser, or starts a headless one. It supports stdio and streamable HTTP transports.
- **Redesigned interface:** a new design system with light and dark themes (follows the OS, switchable in the header), SVG icons, clearer cards and forms, a panel header explaining each tab, and theme-aware charts.
- The waveform view shows the newest stored trace after a page reload instead of an empty plot.
- `tools/screenshots.py` regenerates every README screenshot from a scripted simulator session.
- Tests for the toolchain manager (checksum verification, path traversal protection, aliases, custom toolchains), the clang compiler wrapper, platform parsing, and an end-to-end MCP test that recovers an AES key through the protocol.

### Changed

- Licensed under Apache 2.0, matching ChipWhisperer. See NOTICE for bundled third-party files.
- Requires Python 3.10 or newer (needed by the MCP SDK). With `chipwhisperer` 6.0.0 from PyPI, which pins numpy 1.26, use Python 3.10 to 3.12 for pip installs; the standalone bundles are not affected.
- `pip install chipwhisperer-studio` replaces `pip install "chipwhisperer[studio]"`.
- Uploaded firmware files are stored in `<data dir>/firmware/uploads`.

### Fixed

- Builds of ChipWhisperer's older HALs with current compilers: Studio adds the compatibility flags they need (`-fcommon`, relaxed implicit declaration errors, RISC-V `-misa-spec=2.2`).

### Known limitations

- Not yet verified on physical ChipWhisperer hardware. Everything is tested against the built-in simulator.
- Windows and macOS bundles are built and smoke tested in CI only.
- A few platforms fail to build because of bugs in the upstream HAL sources, not Studio: CW308_EFM32GG11 (the HAL contains `#error "Unfinished HAL"`), CW308_PSOC62 (a syntax error) and CW308_NRF52 (a missing linker script). With clang, CW308_CC2538, CW308_LPC55S6X, CW308_IMXRT1062 and CW308_FE310 also fail because their sources use GCC-only constructs; use GCC for those.

## [0.1.0] - 2026-09-23

Initial version, written as `software/cwstudio` inside a fork of ChipWhisperer and proposed upstream. NewAE preferred to keep a GUI out of the main project, so development continues here as a standalone application.

### Added

- FastAPI and WebSocket backend with a single hardware worker thread; long jobs (capture, glitch sweep) are interleaved so the UI stays responsive.
- Build-free web frontend (ES modules and uPlot): a live waveform with overlay, mean and envelope, browsing, zoom, cursors, a time axis and PNG export.
- A settings tree for every scope and target setting, with documentation and hardware read-back (Nano, Lite, Pro, Husky).
- Firmware programming, a serial console and a SimpleSerial helper.
- Capture modes (single, N traces, continuous, trigger only) with export to npz, cwp and csv.
- Progressive CPA with five AES leakage models and PGE convergence plots.
- Glitch parameter sweeps with target reset handling and a result scatter plot.
- A built-in simulator (AES leakage and glitch behaviour) for use without hardware.
- PyInstaller packaging that bundles Python and libusb, and CI that builds Linux, Windows and macOS archives.

[Unreleased]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.2...HEAD
[0.4.2]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.2.0
[0.1.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.1.0
