# Development and Releases

How to run Studio from source, run the tests, change the UI, build the standalone bundles, publish a release and keep this wiki up to date. Read [Architecture](Architecture) first for how the pieces fit together.

## Setting up a development environment

You need Python 3.10 to 3.12 (the `chipwhisperer` 6.0.0 package on PyPI pins numpy 1.26, which has no wheels for newer Pythons) and git.

```bash
git clone https://github.com/keyuraghao/chipwhisperer-studio
cd chipwhisperer-studio
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[test]"
```

The editable install puts the `cw-studio` and `cw-studio-web` commands on your PATH and uses the code in `src/cwstudio` directly. For the UI tests and the screenshot tools also install Playwright: `pip install playwright && playwright install chromium`.

## Running from source

```bash
cw-studio --simulate --log-level debug
```

`--simulate` preselects the simulator so you can work on every feature without hardware (see [Simulator](Simulator)). Use `--data-dir ./build/dev-data` to keep development files away from your normal data folder.

## Repository layout

| Path | Contents |
|------|----------|
| `src/cwstudio/` | The Python package (see the module table on [Architecture](Architecture)), with the `logic/` (logic analyser) and `codemap/` (code map) sub-packages. |
| `src/cwstudio/static/` | The web UI: `index.html`, `css/app.css`, `js/*.js` (including `markdown.js`, the Markdown renderer and sanitiser), `vendor/` (uPlot). |
| `src/cwstudio/resources/` | `toolchains.json` (pinned compilers, OpenOCD and firmware source settings), `50-newae.rules`, `cw_openocd.cfg` (the ChipWhisperer interface for OpenOCD), `gtk_window.py` (the Linux window) and the window icons. |
| `tests/` | pytest suite, with small firmware projects for the code map tests in `tests/fw_projects`. |
| `tools/` | `screenshots.py` (wiki and README screenshots), `theme_images.py` (light and dark `<picture>` references), `demo_video.py` (the video tour and feature clips), `benchmark.py` (stress tests and the [Performance](Performance) page), `release_notes.py` (release notes from `CHANGELOG.md`) and `make_icon.py` (the application icon). |
| `packaging/` | PyInstaller spec, `build.py`, `launcher.py`, the application icons and the README placed inside bundles. |
| `docs/wiki/` | Source of this wiki. |
| `docs/DESIGN.md` | Design notes. |
| `docs/benchmarks/` | Stored benchmark runs (JSON), one per published run. |
| `.github/workflows/` | `ci.yml` (tests, firmware builds, bundles, releases) and `wiki.yml` (publishes this wiki). |

## Tests

```bash
python -m pytest
```

The suite runs against the simulator and needs no hardware or network access (the code map tests that build firmware use the toolchains when they are installed and skip otherwise). It covers:

| File | What it tests |
|------|---------------|
| `test_api.py` | The HTTP API end to end: metadata, connecting, settings, serial, capture modes, traces, export and import (including the `.npy` and `.cwp` zip downloads), CPA, glitch sweeps, WebSocket frames, programming upload, notebook import, and the routing layer's parameter handling and errors. |
| `test_units.py` | Settings introspection on the simulated scope, value conversion, the hardware worker's short and long jobs, the trace store and its running statistics, `.npz` export round trips, key and text generation, simulated glitch outcomes, and the event history. |
| `test_toolchains.py` | Toolchain registry pinning, install with checksum verification, mirror fallback, path traversal protection, aliases, custom toolchains, the clang wrapper's flag translation, platform parsing, firmware folder checks. |
| `test_net.py` | HTTPS certificate source selection and the error shown when verification fails. |
| `test_notebook.py` | Notebook kernels: results and errors, one kernel per notebook, tutorial-style capture loops reaching the trace store, shell and cell magics, `%run`, simulated programming, matplotlib figures, notebook files API with conflict checks, interrupt, cancel and restart, notes, calculator and statistics. |
| `test_interfaces.py`, `test_interfaces_hw.py` | Capabilities per model (every entry of the capability matrix for each simulated model), the Interfaces API and MCP tools against the simulator, and the library calls Studio makes against recorded fakes of every scope model (triggers, SAD, decode IO, USERIO, SPI, bit-banger, MPSSE), plus OpenOCD with its dummy adapter. |
| `test_logic_sources.py`, `test_logic_decoders.py`, `test_logic_realworld.py`, `test_logic_perf.py` | Logic analyser sources, every decoder (including clock error, glitches, clock stretching, overdrive, CAN FD), PulseView, Saleae and sigrok files, exports that open back identically, worker-process decoding, and view speed on 10 million samples. |
| `test_codemap.py`, `test_codemap_robust.py` | ELF and DWARF parsing, Arm, RISC-V and AVR emulation and cycle counts, the mapping and alignment, simulator firmware with CPA and instruction-skip glitches, and firmware that halts, sleeps or misbehaves. |
| `test_window.py` | Choosing window or browser, the pywebview and GTK windows (with fakes), shutdown closing the window, desktop entries, ports and locales. |
| `test_ui_*.py` | Browser tests with Playwright (skipped when it is not installed): interfaces, the logic view, the code band and Code tab, multiple notebooks and panes, shortcuts, narrow windows and digits in other locales. |
| `test_mcp.py` | Unit tests of the MCP protocol layer, a raw stdio session and the streamable HTTP transport, then (when the `mcp` package is installed, as with `pip install -e ".[test-mcp]"`) starts `cw-studio mcp` over stdio with the official MCP client and runs a full session: connect, settings, capture, CPA key recovery, glitch sweep, notebook code, calculator, notes and notebook runs. |

Real compiler downloads and firmware builds are exercised in CI rather than in the unit tests.

## Working on the frontend

There is no build step: edit files under `src/cwstudio/static/` and reload the browser. The UI is plain ES modules. Colours are CSS custom properties defined for light and dark themes at the top of `app.css`; use the existing tokens rather than hard-coded colours so both themes keep working.

## Screenshots

`tools/screenshots.py` regenerates every image used in the wiki and README from a scripted simulator session: it starts a temporary Studio, captures traces, runs CPA, sweeps glitches, builds firmware with clang, creates sample notebooks and notes, then photographs each view with Playwright (including the Interfaces tab for a simulated Husky and Nano, the UART terminal, the Logic tab with decoded UART, SPI and I2C, the Code tab with a selected region running the clang build in the simulator, two notebooks side by side and the Simulate as choice) twice: `name.png` in the dark theme and `name-light.png` in the light theme. `tools/theme_images.py` then turns every screenshot reference in the README and the wiki into a `<picture>` that shows the variant matching the reader's GitHub theme (run it after adding a new screenshot; `--check` reports anything left to convert).

```bash
pip install playwright
playwright install chromium
python tools/screenshots.py
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--out DIR` | `docs/wiki/images` | Where the PNG files are written. |
| `--data-dir DIR` | `build/screenshot-data` | Data folder for the temporary Studio. Reuse it: the first run downloads the Arm GCC and clang toolchains (about 360 MB). |
| `--size WxH` | `1600x1000` | Browser viewport. |
| `--port N` | any free port | Port for the temporary Studio. |
| `--no-firmware` | off | Skip toolchain downloads and the firmware build. |

The script exits with an error if the browser console reported any JavaScript errors, which makes it a useful UI smoke test.

## Benchmarks and stress tests

`tools/benchmark.py` measures large trace sets, large files, CPA, capture throughput, the server under load and many windows, runs memory leak checks against a real Studio process, and watches the browser during long live captures. `--quick` takes a few minutes, the full set about 45 minutes, and `--only` picks groups (store, files, cpa, capture, server, leaks, ui). `--publish results.json` keeps the run in `docs/benchmarks` and rebuilds the [Performance](Performance) page and the README section; `--compare before.json after.json` prints what changed. Run it before a release that touches capture, storage, analysis or the server.

## Demo video

`tools/demo_video.py` records the [Video Tour](Video-Tour): it starts a Studio with the simulator and drives the real UI through every feature with Playwright, with a caption bar explaining each step and a visible pointer. It writes `chipwhisperer-studio-demo.mp4` (H.264), one short looping animated WebP per chapter in `clips/` (GitHub plays these inline in the README and the wiki, which it cannot do for MP4 files), `demo-poster.png` and `chapters.json` with the chapter timestamps. `--clips-only` cuts the clips again from an existing recording.

```bash
pip install playwright imageio-ffmpeg
playwright install chromium
python tools/demo_video.py --data-dir build/screenshot-data
```

Use the same data folder as the screenshots, so the firmware chapter finds the compilers and sources. ffmpeg comes from `imageio-ffmpeg` when it is not installed system-wide. The full video is attached to the GitHub release rather than committed; copy `clips/*.webp` into `docs/wiki/images/clips` and the poster into `docs/wiki/images`.

## Standalone bundles

```bash
pip install -e . "pyinstaller>=6.0" libusb-package
python packaging/build.py --no-venv
```

`build.py` runs PyInstaller with `packaging/cwstudio.spec`, copies the udev rule, a README, LICENSE, NOTICE and the application icon next to the executable and zips the result. `--variant app` (the default) builds ChipWhisperer Studio, which opens in its own window, as `dist/ChipWhispererStudio-<os>-<arch>.zip`; `--variant web` builds ChipWhisperer Studio Web, which opens in the browser, as `dist/ChipWhispererStudio-Web-<os>-<arch>.zip`. On macOS each is a `.app`; on Windows the window build has a windowed `ChipWhispererStudio.exe` plus a console `cw-studio.exe`. The build fails if the Unicorn library is missing. Without `--no-venv` it first creates an isolated virtual environment; `--out` changes the output folder and `--skip-zip` leaves the unzipped folder only. `packaging/launcher.py` is the bundle's entry point: it handles the `--ccwrap` compiler wrapper mode quickly, gives child processes (compilers, OpenOCD, the Linux window) the user's own library path rather than the bundle's, points python-libusb1 at the bundled libusb on Windows, and supports the logic decoder's worker process.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main`, on pull requests and on version tags:

| Job | What it does |
|-----|--------------|
| Tests | pytest on Ubuntu (Python 3.10 and 3.12), Windows and macOS (Python 3.12). The Python 3.10 job installs only the `test` extra; the others install `test-mcp` and also run the official MCP client test. The Playwright UI tests run locally (they are skipped without Playwright). |
| Docs | Checks that links and images in README, DESIGN, CHANGELOG and the bundle README resolve, that no em dashes appear anywhere, that every screenshot uses the light and dark `<picture>` format (`tools/theme_images.py --check`) and that the README can be converted for PyPI (`tools/pypi_readme.py --check`). |
| Firmware build | On all three operating systems: installs Arm GCC, AVR GCC, clang (and Windows make) through Studio, downloads the firmware sources from GitHub (failing if it had to use the fallback commit), and builds simpleserial-aes for CWLITEARM and CWLITEXMEGA with GCC and clang, and for CWHUSKY with GCC. |
| Bundle | Builds both bundles (window and Web) on each operating system (Linux on Ubuntu 22.04) and smoke tests them: the contents (the Linux window helper, Unicorn, pywebview only in the window build, the Windows icon and WebView2 library, the macOS `.app` and its signature), `mcp --help`, the compiler wrapper, `--install-desktop` on Linux, a simulator capture over the API, the toolchain list, a notebook cell that plots with matplotlib and `import unicorn`. The Linux window build opens its window under Xvfb and must close it on `/api/shutdown`; on Windows and macOS the window build must start and stop cleanly. |
| GitHub release | Only for tags `v*`, after all other jobs pass: extracts the release notes from `CHANGELOG.md`, points the README's images and links at the tag (`tools/pypi_readme.py`, since PyPI has no copy of the repository), builds and checks the wheel and source package, and publishes a GitHub release with them and the six bundles attached. |
| Publish to PyPI | After the GitHub release: uploads the wheel and source package to [PyPI](https://pypi.org/project/chipwhisperer-studio/) with trusted publishing, so no token is stored. PyPI trusts the `ci.yml` workflow of this repository in the `pypi` environment (set once under Publishing in the PyPI project settings). |

## Releasing a version

1. Move the entries under `## [Unreleased]` in `CHANGELOG.md` into a new `## [X.Y.Z] - YYYY-MM-DD` section, grouped under Added, Changed and Fixed, and update the comparison links at the bottom.
2. Set `__version__ = "X.Y.Z"` in `src/cwstudio/__init__.py`.
3. Check that the notes and version agree: `python tools/release_notes.py --check X.Y.Z`.
4. Commit, push to `main` and wait for CI to pass.
5. Tag and push: `git tag -a vX.Y.Z -m "ChipWhisperer Studio X.Y.Z"` then `git push origin vX.Y.Z`. CI publishes the GitHub release and the PyPI package when every job passes. PyPI never accepts the same version twice, so a fix after that needs a new version.

## Updating the toolchain registry

`src/cwstudio/resources/toolchains.json` pins every downloadable compiler. To publish a new compiler version:

1. Update the entry's `version` and, for each host (`linux-x64`, `linux-arm64`, `darwin-x64`, `darwin-arm64`, `win32-x64` and so on), the `url`, `sha256` (from the project's published checksum files) and `size`. Mirrors go in an optional `mirrors` list and must be `https`.
2. Increase the top-level `revision`.
3. Run the tests (`test_toolchains.py` checks that every download is pinned).

Once merged to `main`, running Studios pick up the new list with **Refresh list** on the Toolchains card, because the registry's `update_url` points at the file on `main`. Studio only accepts a fetched registry with the same `schema`, a higher `revision`, and an `https` URL and SHA-256 for every download.

## This wiki

The wiki source lives in `docs/wiki/` in the main repository, so documentation changes are reviewed with code changes and match each release. `.github/workflows/wiki.yml` runs on every push to `main` that touches `docs/wiki/`: it checks that there are no em dashes, that every page link and image reference resolves, and then copies the folder to the GitHub wiki repository. Page names are file names without `.md`; `_Sidebar.md` and `_Footer.md` are the navigation and footer. Regenerate the images with `tools/screenshots.py`.

## Style

- Do not hard-wrap prose: every paragraph, comment, docstring paragraph, list item and table row stays on one line.
- Do not use em dashes; use commas, colons, parentheses or separate sentences.
- Keep hardware access inside the public `chipwhisperer` API and on the hardware worker thread.
- Keep the frontend build-free.
- Add or update tests for new behaviour, and update `CHANGELOG.md` under Unreleased.

## Contributing

- **Bugs and ideas:** open an issue at [github.com/keyuraghao/chipwhisperer-studio/issues](https://github.com/keyuraghao/chipwhisperer-studio/issues). For bugs, include the version, operating system, hardware and log (see [Troubleshooting](Troubleshooting)).
- **Pull requests:** fork the repository, create a branch, make your change with tests and a CHANGELOG entry, run `python -m pytest`, and open a pull request against `main`. CI must pass.
- **Hardware reports are especially welcome:** Studio has so far been verified with the simulator and in CI, so reports of what works (or does not) on real Nano, Lite, Pro and Husky setups help everyone.

Studio is licensed under the Apache License 2.0; contributions are accepted under the same license.
