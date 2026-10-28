# ChipWhisperer Studio (standalone bundle)

This folder is a self-contained build of ChipWhisperer Studio, a desktop application for ChipWhisperer capture hardware (Nano, Lite, Pro, Husky, Husky Plus). It bundles its own Python runtime, the `chipwhisperer` library and the CPU emulator used by the code map, so nothing needs to be installed.

## Run

- **Windows:** double-click `ChipWhispererStudio.exe`. Current ChipWhisperer firmware needs no driver on Windows 10 and 11; if the scope is not detected, the Connect tab explains what to check (an old libusb-win32 driver must be removed; see https://chipwhisperer.readthedocs.io/en/latest/drivers.html). In the window build, use `cw-studio.exe` from a terminal and for AI agents (MCP).
- **Log:** every bundle writes its messages to `~/ChipWhispererStudio/studio.log` (also when started from a terminal, and even with `--data-dir`); look there when something goes wrong without a console.
- **macOS:** double-click `ChipWhisperer Studio.app` (or `ChipWhisperer Studio Web.app`; move it to Applications if you like). Gatekeeper: right-click and choose Open the first time.
- **Linux:** run `./chipwhisperer-studio.sh`. Install the udev rule once so you can use the device without root; the Connect tab shows the exact command, which uses the bundled `50-newae.rules`. To add Studio with its icon to the applications menu, run `./ChipWhispererStudio --install-desktop` once (`--remove-desktop` takes it out). Studio's own window uses WebKitGTK: if it is missing, Studio prints the package to install (for example `sudo apt install python3-gi gir1.2-webkit2-4.1`) and opens in your browser meanwhile.

The ChipWhisperer Studio build opens in its own window; closing the window quits Studio. The ChipWhisperer Studio Web build opens in your web browser at its address (default http://127.0.0.1:8765/); on Windows and Linux a console window shows it, and closing the console quits Studio.

## Options

```
ChipWhispererStudio [--browser | --app-window | --no-browser] [--port 8765] [--host 127.0.0.1] [--simulate] [--data-dir DIR] [--log-level debug|info|warning|error]
ChipWhispererStudio mcp [--simulate] [--url http://127.0.0.1:8765] [--no-embed] [--transport stdio|streamable-http|sse] [--mcp-port 8766]
```

- `--simulate` preselects the built-in simulator so you can explore the app without hardware.
- `--host 0.0.0.0` lets other machines on the network open the UI.
- `--data-dir` is where exports, firmware builds and downloaded compilers are stored (default: `~/ChipWhispererStudio`).
- `mcp` runs the Model Context Protocol server for AI agents; see the Help tab for client setup (on Windows, use `cw-studio.exe mcp` in the window build).

Compilers for the Firmware tab, and OpenOCD for JTAG/SWD in the Interfaces tab, are downloaded the first time you need them and then work offline. External logic analysers in the Logic tab need sigrok-cli (for example `sudo apt install sigrok-cli`).

Documentation: https://github.com/keyuraghao/chipwhisperer-studio/wiki

More information and source code: https://github.com/keyuraghao/chipwhisperer-studio
