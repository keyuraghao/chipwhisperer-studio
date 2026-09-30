# Release notes

All notable changes to ChipWhisperer Studio are listed here, newest first. Versions follow [Semantic Versioning](https://semver.org/). Each release on GitHub uses the matching section below as its description and carries standalone bundles for Linux, Windows and macOS.

## [Unreleased]

Nothing yet.

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

[Unreleased]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.2.0
[0.1.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.1.0
