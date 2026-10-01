# Release notes

All notable changes to ChipWhisperer Studio are listed here, newest first. Versions follow [Semantic Versioning](https://semver.org/). Each release on GitHub uses the matching section below as its description and carries standalone bundles for Linux, Windows and macOS.

## [Unreleased]

## [0.4.0] - 2026-10-01

This release makes Studio smaller and faster. Heavy libraries that Studio used only a small part of are replaced by compact built-in code, so a pip install pulls in about 25 fewer packages and the standalone bundles shrink by a third, while CPA, the simulator and live streaming get several times faster. The HTTP API, the MCP tools and the file formats are unchanged.

### Added

- **Wiki:** a full user and developer guide in `docs/wiki`, published to the GitHub wiki by CI, with new screenshots of every tab and a diagram of how notebook cells share the hardware.

- **Built-in MCP server** (`mcplite.py`): Studio now speaks the Model Context Protocol itself (stdio, streamable HTTP and SSE) instead of using the MCP SDK. The 68 tools, their schemas, the prompts and the resources are identical, and agents see no difference; it was checked against the official MCP client on all three transports.
- **Built-in router** (`web.py`): the HTTP API runs on Starlette directly with a small routing layer instead of FastAPI, so pydantic is no longer needed. `/api/docs` now lists every endpoint with its parameters (the wiki's HTTP API page has the details).
- Uploads also accept the file as the raw request body with `?filename=NAME`, besides `multipart/form-data`.

### Changed

- **Fewer dependencies:** `fastapi`, `mcp`, `python-multipart` and the `uvicorn[standard]` extras are gone, and with them pydantic, pydantic-core, jsonschema, rpds, httpx2, opentelemetry, pyjwt, cryptography, uvloop, httptools, watchfiles and PyYAML. Studio now needs Starlette, uvicorn, websockets, numpy, matplotlib and a few small packages.
- **Smaller bundles:** the Linux bundle zip shrinks from 108 MB to 65 MB. Besides the dependencies above, the bundles leave out Cython, setuptools, matplotlib's GUI backends and sample data, and Pillow's AVIF, WebP and colour management codecs, and strip debug symbols on Linux.
- **Smaller frontend:** the notebook and notes Markdown renderer and HTML sanitizer are now one 14 KB module (`markdown.js`) instead of marked and DOMPurify (76 KB). It renders NewAE's tutorial notebooks like before, leaves LaTeX math untouched, and keeps notebook output safe from scripts.
- **Faster CPA:** about 3 times faster with live progress (5000 traces of 5000 samples: 27 s to 9 s) and 1.6 times faster without, with identical results; progress reports no longer allocate hundreds of MB.
- **Faster capture and simulator:** the simulator synthesises traces 3 times faster and AES runs 7 times faster, so simulated captures run about 2.7 times faster. The same seed still gives the same traces.
- **Faster live view:** each WebSocket event is encoded once for all open windows instead of once per window, the WebSocket loop sleeps until there is an event instead of waking every second, the trace store keeps running counters for its summary, and the waveform view reuses buffers instead of allocating them for every frame.
- The Connect tab now preselects SimpleSerial v2, which current ChipWhisperer firmware uses, instead of the legacy v1 protocol.
- In notebooks, `cw.plot()` returns a plot object that combines with `*` and `+` like ChipWhisperer's holoviews version (`cw.plot(a) * cw.plot(b)`, `fig = cw.plot()`), and `plt.show()` shows figures immediately.
- `studio.build_firmware()` defaults to SimpleSerial v2.1, like the Firmware tab.

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

[Unreleased]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.2.0
[0.1.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.1.0
