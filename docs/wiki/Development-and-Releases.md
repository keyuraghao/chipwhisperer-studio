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

The editable install puts the `cw-studio` command on your PATH and uses the code in `src/cwstudio` directly.

## Running from source

```bash
cw-studio --simulate --log-level debug
```

`--simulate` preselects the simulator so you can work on every feature without hardware (see [Simulator](Simulator)). Use `--data-dir ./build/dev-data` to keep development files away from your normal data folder.

## Repository layout

| Path | Contents |
|------|----------|
| `src/cwstudio/` | The Python package (see the module table on [Architecture](Architecture)). |
| `src/cwstudio/static/` | The web UI: `index.html`, `css/app.css`, `js/*.js` (including `markdown.js`, the Markdown renderer and sanitiser), `vendor/` (uPlot). |
| `src/cwstudio/resources/` | `toolchains.json` (pinned compilers and firmware source settings) and `50-newae.rules`. |
| `tests/` | pytest suite. |
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

The suite runs against the simulator and needs no hardware or network access. It covers:

| File | What it tests |
|------|---------------|
| `test_api.py` | The HTTP API end to end: metadata, connecting, settings, serial, capture modes, traces, export and import (including the `.npy` and `.cwp` zip downloads), CPA, glitch sweeps, WebSocket frames, programming upload, notebook import, and the routing layer's parameter handling and errors. |
| `test_units.py` | Settings introspection on the simulated scope, value conversion, the hardware worker's short and long jobs, the trace store and its running statistics, `.npz` export round trips, key and text generation, simulated glitch outcomes, and the event history. |
| `test_toolchains.py` | Toolchain registry pinning, install with checksum verification, mirror fallback, path traversal protection, aliases, custom toolchains, the clang wrapper's flag translation, platform parsing, firmware folder checks. |
| `test_net.py` | HTTPS certificate source selection and the error shown when verification fails. |
| `test_notebook.py` | Notebook kernel: results and errors, tutorial-style capture loops reaching the trace store, shell and cell magics, `%run`, simulated programming, matplotlib figures, notebook files API, interrupt and restart, notes, calculator and statistics. |
| `test_mcp.py` | Unit tests of the MCP protocol layer, a raw stdio session and the streamable HTTP transport, then (when the `mcp` package is installed, as with `pip install -e ".[test-mcp]"`) starts `cw-studio mcp` over stdio with the official MCP client and runs a full session: connect, settings, capture, CPA key recovery, glitch sweep, notebook code, calculator, notes and notebook runs. |

Real compiler downloads and firmware builds are exercised in CI rather than in the unit tests.

## Working on the frontend

There is no build step: edit files under `src/cwstudio/static/` and reload the browser. The UI is plain ES modules. Colours are CSS custom properties defined for light and dark themes at the top of `app.css`; use the existing tokens rather than hard-coded colours so both themes keep working.

## Screenshots

`tools/screenshots.py` regenerates every image used in the wiki and README from a scripted simulator session: it starts a temporary Studio, captures traces, runs CPA, sweeps glitches, builds firmware with clang, creates a sample notebook and notes, then photographs each view with Playwright twice: `name.png` in the dark theme and `name-light.png` in the light theme. `tools/theme_images.py` then turns every screenshot reference in the README and the wiki into a `<picture>` that shows the variant matching the reader's GitHub theme (run it after adding a new screenshot; `--check` reports anything left to convert).

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

`build.py` runs PyInstaller with `packaging/cwstudio.spec`, copies the udev rule, a README, LICENSE, NOTICE and the application icon next to the executable and zips `dist/ChipWhispererStudio-<os>-<arch>.zip`. Without `--no-venv` it first creates an isolated virtual environment; `--out` changes the output folder and `--skip-zip` leaves the unzipped folder only. `packaging/launcher.py` is the bundle's entry point: it handles the `--ccwrap` compiler wrapper mode quickly and points python-libusb1 at the bundled libusb before starting Studio.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main`, on pull requests and on version tags:

| Job | What it does |
|-----|--------------|
| Tests | pytest on Ubuntu (Python 3.10 and 3.12), Windows and macOS (Python 3.12). The Python 3.10 job installs only the `test` extra; the others install `test-mcp` and also run the official MCP client test. |
| Docs | Checks that links and images in README, DESIGN and CHANGELOG resolve, that no em dashes appear anywhere, and that every screenshot uses the light and dark `<picture>` format (`tools/theme_images.py --check`). |
| Firmware build | On all three operating systems: installs Arm GCC, AVR GCC, clang (and Windows make) through Studio, downloads the firmware sources from GitHub (failing if it had to use the fallback commit), and builds simpleserial-aes for CWLITEARM and CWLITEXMEGA with GCC and clang, and for CWHUSKY with GCC. |
| Bundle | Builds the standalone bundle on each operating system (Linux on Ubuntu 22.04) and smoke tests it: `mcp --help`, the compiler wrapper, a simulator capture over the API, the toolchain list, and a notebook cell that plots with matplotlib. |
| GitHub release | Only for tags `v*`, after all other jobs pass: extracts the release notes from `CHANGELOG.md`, builds the wheel and source package, and publishes a GitHub release with the three bundles attached. |

## Releasing a version

1. Move the entries under `## [Unreleased]` in `CHANGELOG.md` into a new `## [X.Y.Z] - YYYY-MM-DD` section, grouped under Added, Changed and Fixed, and update the comparison links at the bottom.
2. Set `__version__ = "X.Y.Z"` in `src/cwstudio/__init__.py`.
3. Check that the notes and version agree: `python tools/release_notes.py --check X.Y.Z`.
4. Commit, push to `main` and wait for CI to pass.
5. Tag and push: `git tag -a vX.Y.Z -m "ChipWhisperer Studio X.Y.Z"` then `git push origin vX.Y.Z`. CI publishes the release when every job passes.

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
