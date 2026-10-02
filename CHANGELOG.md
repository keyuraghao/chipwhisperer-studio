# Release notes

All notable changes to ChipWhisperer Studio are listed here, newest first. Versions follow [Semantic Versioning](https://semver.org/). Each release on GitHub uses the matching section below as its description and carries standalone bundles for Linux, Windows and macOS.

## [Unreleased]

## [0.5.1] - 2026-10-02

Studio is now on PyPI: `pip install chipwhisperer-studio`. No changes to the application itself.

### Added

- **PyPI package.** Every release is now also published to [PyPI](https://pypi.org/project/chipwhisperer-studio/) by CI, with trusted publishing (no stored token). The package description is the README with its screenshots, clips and links pointing at the release tag (`tools/pypi_readme.py`), and the package lists keywords, classifiers for the supported systems and Python versions, and links to the documentation, issues, changelog and video tour.

### Changed

- The README and the Installation wiki page install with `pip install chipwhisperer-studio` and update with `pip install --upgrade chipwhisperer-studio` (the Installation page still pointed at the 0.4.5 wheel).

## [0.5.0] - 2026-10-02

This release adds five large features: Studio's own application window, several notebooks at once, an Interfaces tab for everything that talks to the target, a logic analyser for every ChipWhisperer, and firmware code mapped onto the waveform. The MCP server grows from 68 to 106 tools to cover them.

### Added

- **Studio's own application window.** Studio now opens in a window of its own instead of a browser tab, using the operating system's web engine: Microsoft Edge WebView2 on Windows and WebKit on macOS (through pywebview, now a dependency on those systems), and WebKitGTK on Linux through a small helper run by the system Python, so the bundle does not ship GTK (`sudo apt install python3-gi gir1.2-webkit2-4.1`, `dnf install python3-gobject webkit2gtk4.1` or `pacman -S python-gobject webkit2gtk-4.1` if it is missing). Downloads, file pickers and links to other sites work, settings persist between runs, closing the window quits Studio and stopping Studio closes the window. If the window cannot open, Studio says why and uses the browser; over SSH it prints the address instead of starting a text browser.
  - `--browser` opens the browser, `--app-window` forces the window, `--no-browser` runs only the server; `CWSTUDIO_DEFAULT_UI` sets the default, `CWSTUDIO_GTK_PYTHON` picks the Python for the Linux window and `CWSTUDIO_DEVTOOLS=1` enables its web inspector. The new `cw-studio-web` command opens the browser by default.
  - **Two builds per OS:** ChipWhisperer Studio (`ChipWhispererStudio-<os>-<arch>.zip`, own window) and ChipWhisperer Studio Web (`ChipWhispererStudio-Web-<os>-<arch>.zip`, browser), about 72 MB each. On macOS each is a real `.app` (`ChipWhisperer Studio.app`, `ChipWhisperer Studio Web.app`); on Windows the window build's `ChipWhispererStudio.exe` has no console window and `cw-studio.exe` next to it is the console version for scripts and AI agents.
- **Several notebooks at once.** Notebooks open as tabs, and two can be shown side by side (drag a tab to the right half or press the split button; drag the divider to resize). Every notebook has its own kernel, so variables, execution counts, Stop and Restart are per notebook, while all kernels share Studio's hardware connection and run their cells one at a time on the hardware thread. A kernel is shut down shortly after its notebook is closed in every window. Notebooks open in several windows, or rewritten by the API or an agent, stay in sync: a tab without edits reloads, a tab with unsaved edits shows a conflict notice instead of being overwritten.
  - API: `GET /api/kernels`, `POST /api/kernels/shutdown`, `/api/kernels/rename`, `/api/kernels/attach`, `POST /api/notebooks/rename`, and a `kernel` parameter on the kernel routes; saves take `base_mtime` and are refused with 409 or 410 when the file changed or was deleted. MCP: `kernel_list` and `kernel_shutdown`, and `notebook_path` on the kernel tools.
  - `%config InlineBackend.figure_format = 'svg'` shows figures as SVG; `%cd` lasts until the kernel restarts.
- **Interfaces tab** with everything that talks to the target, gated by the connected model: the target UART (baud, parity, stop bits and pins) with a terminal (text or hex, line endings, history, timestamps), SimpleSerial 1.0, 1.1, 2.1 and v2 over the scope's USB-CDC port, an SPI master with SPI flash shortcuts, GPIO on the target pins with read-back and a reset pulse, the Husky's USERIO header, triggers (edge and level on any pin combination, the Pro's UART I/O decode trigger, the Husky's UART pattern rules, edge counter, ADC level and trigger sequencer, SAD), the Husky bit-banger and 1-Wire, and **JTAG/SWD through OpenOCD**: switch the scope into MPSSE mode, start an OpenOCD server for a target configuration, run commands, flash and connect GDB. OpenOCD (xPack 0.12.0) installs on demand like the compilers.
  - A new capabilities module knows what every model supports, from the `chipwhisperer` library's source: the Interfaces tab, the Target tab's programmers and the Scope tab's choices offer only that, and everything else shows the reason (for example *the CW-Nano has no SPI pins*). The [capability matrix](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Protocols-and-Interfaces) is on the new Protocols and Interfaces wiki page.
  - **Simulate as:** the simulator can pose as a Husky (default), Husky Plus, Pro, Lite or Nano, with exactly that model's interfaces, triggers, programmers and settings, a simulated W25Q128 SPI flash, pins, bit-banger and 1-Wire device.
  - iCE40 and XC7A35T FPGA bitstreams can be programmed from the Target tab.
  - API: `GET /api/capabilities` and `/api/interfaces/*`. MCP: 20 tools, `hardware_capabilities`, `interfaces_status`, `uart_configure`, `simpleserial_connect`, `spi_*`, `gpio_*`, `userio_set`, `trigger_configure`, `bitbang`, `onewire` and `openocd_*`.
- **Logic tab**, a logic analyser for every ChipWhisperer. Sources: the Husky's built-in logic analyser (9 signals from the 20-pin header, the USERIO header or the glitch internals, up to 16,376 or 65,535 samples), one digital line through the analog input of any scope, external analysers through sigrok-cli, VCD, CSV (Saleae Logic 1 and 2, sigrok, generic) and sigrok `.sr` files, and simulated traffic. Decoders for UART (with automatic baud rate), SPI, I2C, 1-Wire (with overdrive), JTAG, SWD, CAN (CAN FD frames are marked, not misdecoded) and SimpleSerial, plus sigrok's decoders when sigrok-cli is installed, with a glitch filter. The viewer has zoom and pan, cursors with edge snapping, measurements (frequency, period, duty cycle, pulse widths), buses, edge, pattern and decoded-value search, a results table with CSV export, repeat capture that keeps your channel setup, the last eight captures in memory, exports to VCD, CSV and `.sr`, and analog rows (the ADC input, or a stored power trace on the logic time base). It stays fast at 10 million samples, and big captures decode in a worker process. API: `/api/la/*`. MCP: 10 `la_*` tools.
- **Code on the waveform (Code tab).** Studio emulates the firmware's ELF for the key and plaintext of a captured trace (Unicorn for Arm Cortex-M and RISC-V RV32, Studio's own cycle-accurate emulator for AVR and XMEGA), counts clock cycles per core, maps them to samples from the scope's clocks and aligns the result with the stored traces by correlation, with a confidence score. A **code band** under the waveform shows the functions (as a flame chart, inlined functions included) and source lines; Ctrl, Cmd or Alt+drag picks a region and the Code tab lists its functions and source lines with highlighted source and disassembly; clicking a function or line shades every sample where it ran. Peripherals, interrupts and UART timing are not emulated; the HAL's `getch`, `putch` and trigger functions are hooked instead. On a Husky with an Arm target, **exact mode** records program counter samples over SWO and compares them with the emulation. API: `/api/codemap/*`. MCP: 6 `code_map_*` tools.
- **The simulator evaluates the special triggers** on what its target really does (its serial traffic on TIO1 and TIO2 and its trigger pin, timed like on the wire): the Husky's UART pattern rules, the Pro's UART decode trigger, the edge counter, the ADC level, SAD and the trigger sequencer start the capture where they would fire on hardware, and a trigger that never fires times out with a log message saying why. Only the Arm trace and bit-banger triggers fall back to the TIO4 edge, with a warning.
- **A scope left in MPSSE mode is found and restored:** after a restart (or replug), Studio recognises a ChipWhisperer still in JTAG/SWD mode by its USB configuration, shows it in the Interfaces tab and restores it with **Restore normal mode** (`openocd_mpsse(enable=false)`).
- **Updating the tutorials keeps your work:** **Download tutorials** keeps files you edited or added, and asks before replacing an edited file that also changed upstream (**Update, back up mine** keeps yours as `<name>.local-YYYYMMDD-HHMMSS.ipynb`, **Keep mine**, **Cancel**). API and MCP: `on_modified` and the job state `confirm`.
- The Help tab's MCP setup shows the right command for the installation (`cw-studio.exe` in the Windows window build), also as `mcp_command` in `GET /api/meta`.
- **The simulator runs your firmware.** Programming an ELF (or a `.hex` Studio built, which keeps its `.elf`) into the simulator runs it in the emulator: responses and traces come from your code, CPA recovers the key from them, and a glitch skips the instructions where it lands or crashes the core.
- `eol` (none, lf, cr, crlf) for `POST /api/target/serial/write` and the MCP tool `serial_write`; `sim_model` for scope connections.
- New wiki pages: [Protocols and Interfaces](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Protocols-and-Interfaces), [Logic Analyser](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Logic-Analyser) and [Code on the Waveform](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Code-on-the-Waveform). New screenshots, video chapters and clips show the new features.

### Changed

- `cw-studio` opens Studio's window instead of the browser. Use `cw-studio-web`, `--browser` or the Web build for the old behaviour. `--window` still works as an alias of `--app-window`; the `window` extra is no longer needed.
- The simulator poses as a ChipWhisperer-Husky by default (it used to behave like a CW-Lite), and its target drives only TIO4, like ChipWhisperer firmware: a trigger on another pin now times out as on hardware, and the special triggers are evaluated (see Added). Its ADC clock follows `clock.adc_src` (CW-Lite and Pro models) or `clock.adc_mul` (Husky models), and the traces stretch or squeeze with it.
- The Target tab's serial console is the same terminal as in the Interfaces tab: line endings, history and timestamps; UART settings live in the Interfaces tab.
- The Capture tab shows the trigger the scope is set to, wherever it was changed.
- Choices the connected model does not support are marked *(not available)* with the reason in the Scope tab and the Target tab's programmer list, and refused by the API and MCP.
- The single-key shortcuts follow the active tab: S and R capture from every tab except while a notebook has the focus, the waveform keys act only where the waveform is shown, and the Logic tab has its own keys.
- The bundles include the Unicorn engine; each zip is about 72 MB.
- The Help tab and the documentation cover the new tabs; the wiki, README and architecture diagram were updated throughout.

### Fixed

Most of these were found while verifying the new features on Linux, Windows and macOS before release; the first group also affected 0.4.5.

- **Wrong choices offered in 0.4.5:** the Scope tab offered `serial_rx` for TIO4 (it can only transmit) and Pro and Husky trigger modules on every scope; the Firmware tab offered `SS_VER_2_0`, which NewAE's firmware refuses to compile; the AVR and NEORV32 programmers were offered on the CW-Nano, which cannot run them. The Husky Plus now shows its own name.
- **Window and bundles:** the Linux window helper and the Unicorn library were missing from the bundles (the Linux window build always fell back to the browser); the Windows window had no icon (it needs an `.ico`); settings saved in the window (theme, tab, notebook layout) were lost on restart; stopping Studio from the UI, Ctrl+C or SIGTERM left the window open; restarting right after closing could move Studio to another port and lose its saved settings; the bundle's library path leaked into compilers and other programs Studio starts; an invalid `LANG` broke the Linux window. CI now checks the bundle contents and opens the Linux window under Xvfb.
- **Notebooks:** a finished tutorial download reloaded the notebook list forever; pressing a key after leaving a cell could start a capture; `%` and `!` inside brackets or strings were treated as magics; an API or MCP call waiting for a cell that was cancelled (by Stop, Restart or an error earlier in Run all) hung instead of returning; output of quiet cells appeared late; very long outputs slowed the page down; Stop did not end a running shell command; Ctrl+S saved every pane instead of the focused one; autosave could re-create a deleted notebook, and `notebook_run` could overwrite edits saved meanwhile.
- **Interfaces:** switching the simulated model or reconnecting kept the previous SPI master, trigger and SimpleSerial version; the Husky trigger sequencer and SWD on Husky firmware older than 1.4 were not handled; OpenOCD replies and file paths with spaces, `$` or brackets were mangled; invalid requests gave unclear errors; buttons that need an enabled SPI master, a connected target or a stored trace are disabled with a hint until then.
- **Logic analyser:** VCD files from PulseView and HDL simulators (vectors, nested scopes, `x` and `z`), sigrok `.sr` files with disabled probes, 16-bit samples or several chunks, and Saleae and sigrok CSV variants did not import correctly; decoding a big capture stalled the whole API (it now runs in a worker process); the Husky logic analyser read its FIFO before the capture had finished and lost the oversampling setting after a clock change; repeat captures forgot channel names, colours and order; exports did not always open back identically; CAN FD frames were misdecoded as classic frames; 1-Wire overdrive was not recognised.
- **Code map:** Arm IT blocks (conditional instructions) were emulated wrongly; firmware that halts, sleeps or waits for a peripheral ran into the instruction limit without a useful message; alignment could lock onto a neighbouring AES round (the search window is now 200 target cycles and the confidence considers rival matches); caches were unbounded; a 30,000-line source file opened slowly.
- **Found while writing these notes:**
  - **Exact mode always sampled every 64 cycles on real hardware:** Studio did not set the Arm DWT's POSTPRESET. Intervals are now 64 or 1024 cycles times 1 to 16, the request is rounded to the nearest and the interval used is shown (the Code tab has an **Every** field); a capture without PC samples says so instead of failing.
  - Logic analyser: SPI `cpol`/`cpha` options were ignored; buses referred to channel positions and broke the view on a capture with fewer channels (they now follow channel names, and a bus with missing channels is disabled with a notice); a decoder with invalid options was kept and stopped the whole view from drawing (options are now checked, and a decoder that fails on a capture shows its own error); `auto` hysteresis was 0 with a numeric threshold; Esc did not stop the first capture; the tab, the API and MCP used different defaults (downsample, segments, trigger timeout), and `la_capture` did not pass its timeout to the analyser.
  - Code map: the Sources field could not be cleared; code without line information showed as `aes.c:0 (no source)` (now one *(no line info)* entry).
  - Interfaces: the *firmware too old* reason pointed to a firmware update in the Connect tab that does not exist (it now names `scope.upgrade_firmware()`); scope types the tab does not know said *connect a scope first*; "v2 over CDC" was disabled in the simulator although the simulated target answers it.
  - The Help tab told Windows users of the window build to use `ChipWhispererStudio.exe` for MCP; the browser fallback message mentioned a console window even when there was none; S and R were ignored in the whole Notebook tab while a notebook was open, against what the Help tab said (now only while a notebook has the focus).
- **General:** the simulator ignored the trigger pins and `adc_src`; `clock.adc_mul` appeared on simulated models that do not have it; the layout broke at small window widths (the panel, top bar, waveform height and logic view now adapt and nothing scrolls sideways); numbers, axes and times appeared in Arabic-Indic and other non-Latin digits in some locales; the sample rate under the waveform did not follow clock changes made from notebooks or agents; the CPA plots did not fit after results arrived in the background.

## [0.4.5] - 2026-10-01

### Added

- **Application icon** on every platform, drawn from Studio's logo (`tools/make_icon.py`):
  - **Windows:** the executable has the Studio icon instead of PyInstaller's default, plus version information, so Explorer, the taskbar and Task Manager show "ChipWhisperer Studio" and its version.
  - **macOS:** the bundle includes `ChipWhisperer Studio.app`, which shows the icon in Finder and the Dock and opens Studio in Terminal (a plain executable cannot have an icon on macOS).
  - **Linux:** `cw-studio --install-desktop` (or `./ChipWhispererStudio --install-desktop` in the bundle) adds Studio with its icon to the applications menu; `--remove-desktop` takes it out. The bundle also includes the icon as `ChipWhispererStudio.png`.
- The logo is shown at the top of the README and the wiki.

### Changed

- The documentation was reviewed against the code for production: wrong sizes, flags, labels and links fixed, references to pages that do not exist and to replaced libraries removed, and the architecture diagram updated (`web.py`, `mcplite.py`, the simulator).
- All screenshots, the video tour and its clips were regenerated for this version from a neutral data folder. The settings-tree screenshots and the tour now show their groups expanded (the scripts used to click open groups closed).
- The programmer list names the CW-Nano under STM32F and the CW304 under AVR (it wrongly listed a "Nano ATmega").

## [0.4.4] - 2026-10-01

Stress tests with very large trace sets, large files, long sessions, many windows and leak checks (`tools/benchmark.py`, results on the new [Performance](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Performance) wiki page and in the README) found the problems below. No memory leaks were found in any workload.

### Changed

- **Mean, standard deviation and min/max of all traces** (the waveform's mean and envelope, `/api/traces/stats`, the MCP `traces_stats` tool) come from running sums that only take in the traces added since the last request. With 50,000 stored traces a request under load takes about 6 ms instead of 6.3 s, and with 200,000 traces the extra memory drops from 7.6 GB to about 60 MB. The values are also more accurate (float64 sums: within 4e-9 of exact, against 3e-6 before).
- **Exporting to .npz is about 10 times faster** (54 instead of 6 MB/s on realistic ADC data; 1.9 GB now takes 35 s instead of almost 6 minutes). Files are about 13 percent larger and load exactly as before.
- **Clearing traces gives the memory back to the operating system** on Linux, so Studio's memory drops back to where it was (61 instead of 157 MB after 30 capture and clear cycles).
- Studio keeps only log, capture and glitch events for `/api/logs`; it used to also keep the last 2000 notebook outputs (with their figures), CPA results and other events, which could hold hundreds of MB in figure-heavy sessions.

## [0.4.3] - 2026-10-01

### Added

- **Video tour:** a walkthrough of every feature in under three minutes (`chipwhisperer-studio-demo.mp4`, attached to this release), with a chapter list on the new [Video Tour](https://github.com/keyuraghao/chipwhisperer-studio/wiki/Video-Tour) wiki page. `tools/demo_video.py` records it from the real UI with captions, so it can be regenerated after UI changes.
- **Screenshots in both themes:** every screenshot in the README and the wiki now exists in the dark and the light theme, and GitHub shows the one matching your theme. New screenshots show the zoom buttons and the glitch sweep, and an animated GIF shows zooming. `tools/theme_images.py` keeps the docs in sync, and CI checks it.

### Fixed

- With **time axis** on, the waveform's time labels no longer run into each other when the whole trace is shown.
- The README's pip instructions install the wheel from the release (Studio is not on PyPI yet), and the README images that referred to old screenshots use the current ones.

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

[Unreleased]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.5...v0.5.0
[0.4.5]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/keyuraghao/chipwhisperer-studio/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/keyuraghao/chipwhisperer-studio/releases/tag/v0.2.0
