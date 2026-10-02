# ChipWhisperer Studio (standalone bundle)

This folder is a self-contained build of ChipWhisperer Studio, a desktop application for ChipWhisperer capture hardware (Nano, Lite, Pro, Husky). It bundles its own Python runtime and the `chipwhisperer` library, so nothing needs to be installed.

## Run

- **Windows:** double-click `ChipWhispererStudio.exe`. Devices need the WinUSB driver; install the NewAE driver package if the scope is not detected (https://chipwhisperer.readthedocs.io/en/latest/windows-install.html). In the window build, use `cw-studio.exe` from a terminal and for AI agents (MCP).
- **macOS:** double-click `ChipWhisperer Studio.app` (move it to Applications if you like). Gatekeeper: right-click and choose Open the first time.
- **Linux:** run `./chipwhisperer-studio.sh`. Install the udev rule once so you can use the device without root; the Connect tab shows the exact command, which uses the bundled `50-newae.rules`. To add Studio with its icon to the applications menu, run `./ChipWhispererStudio --install-desktop` once (`--remove-desktop` takes it out). Studio's own window uses WebKitGTK: if it is missing, Studio prints the package to install (for example `sudo apt install python3-gi gir1.2-webkit2-4.1`) and opens in your browser meanwhile.

The ChipWhisperer Studio build opens in its own window; closing the window quits Studio. The ChipWhisperer Studio Web build opens in your web browser and shows its address (default http://127.0.0.1:8765/) in a console window; closing the console quits Studio.

## Options

```
ChipWhispererStudio [--browser | --app-window | --no-browser] [--port 8765] [--host 127.0.0.1] [--simulate] [--data-dir DIR]
ChipWhispererStudio mcp [--simulate] [--url http://127.0.0.1:8765]
```

- `--simulate` preselects the built-in simulator so you can explore the app without hardware.
- `--host 0.0.0.0` lets other machines on the network open the UI.
- `--data-dir` is where exports, firmware builds and downloaded compilers are stored (default: `~/ChipWhispererStudio`).
- `mcp` runs the Model Context Protocol server for AI agents; see the Help tab for client setup.

Compilers for the Firmware tab are downloaded the first time you build and then work offline.

More information and source code: https://github.com/keyuraghao/chipwhisperer-studio
