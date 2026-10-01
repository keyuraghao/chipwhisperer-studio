# ChipWhisperer Studio (standalone bundle)

This folder is a self-contained build of ChipWhisperer Studio, a desktop application for ChipWhisperer capture hardware (Nano, Lite, Pro, Husky). It bundles its own Python runtime and the `chipwhisperer` library, so nothing needs to be installed.

## Run

- **Windows:** double-click `ChipWhispererStudio.exe`. Devices need the WinUSB driver; install the NewAE driver package if the scope is not detected (https://chipwhisperer.readthedocs.io/en/latest/windows-install.html).
- **macOS:** double-click `ChipWhisperer Studio.app` (it opens Studio in Terminal), or run `./chipwhisperer-studio.sh`. Gatekeeper: right-click and choose Open the first time.
- **Linux:** run `./chipwhisperer-studio.sh`. Install the udev rule once so you can use the device without root; the Connect tab shows the exact command, which uses the bundled `50-newae.rules`. To add Studio with its icon to the applications menu, run `./ChipWhispererStudio --install-desktop` once (`--remove-desktop` takes it out).

A console window shows the URL (default http://127.0.0.1:8765/) and your browser opens automatically.

## Options

```
ChipWhispererStudio [--port 8765] [--host 127.0.0.1] [--no-browser] [--simulate] [--data-dir DIR]
ChipWhispererStudio mcp [--simulate] [--url http://127.0.0.1:8765]
```

- `--simulate` preselects the built-in simulator so you can explore the app without hardware.
- `--host 0.0.0.0` lets other machines on the network open the UI.
- `--data-dir` is where exports, firmware builds and downloaded compilers are stored (default: `~/ChipWhispererStudio`).
- `mcp` runs the Model Context Protocol server for AI agents; see the Help tab for client setup.

Compilers for the Firmware tab are downloaded the first time you build and then work offline.

More information and source code: https://github.com/keyuraghao/chipwhisperer-studio
