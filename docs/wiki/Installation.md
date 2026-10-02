# Installation

ChipWhisperer Studio comes as a self-contained download for Windows, macOS and Linux that needs nothing else installed. You can also install it as a Python package if you already work in Python.

## Which option should I choose?

| Option | Best for | Needs Python? |
|--------|----------|---------------|
| [Standalone bundle](#standalone-bundle) | Most users, classrooms, machines without Python | No |
| [Python package](#python-package) | People who already use Python and the `chipwhisperer` library, or want to use Studio's notebook with their own packages | Yes, Python 3.10 to 3.12 |
| [From source](#from-source) | Contributors and people who want the latest `main` | Yes, Python 3.10 to 3.12 |

## Standalone bundle

Every [release](https://github.com/keyuraghao/chipwhisperer-studio/releases/latest) offers two builds for each platform. They are the same Studio; they differ only in how the interface opens:

| Build | Opens in | Choose it when |
|-------|----------|----------------|
| **ChipWhisperer Studio** (`ChipWhispererStudio-<platform>.zip`) | Its own application window, like any desktop program | You want a normal desktop app that does not depend on your browser |
| **ChipWhisperer Studio Web** (`ChipWhispererStudio-Web-<platform>.zip`) | Your web browser, with a console window showing its address | You prefer the browser, use Studio remotely, or your Linux system has no WebKitGTK |

The window uses the web engine that comes with the operating system: Microsoft Edge WebView2 on Windows 10 and 11, WebKit on macOS, and WebKitGTK on Linux (see [Linux](#linux) for the one package it may need). Either build can still do the other: start the window build with `--browser` to use the browser, or the Web build with `--app-window` for the window.

| Platform | Files | Notes |
|----------|-------|-------|
| Windows 10 and 11 (64-bit Intel/AMD) | `ChipWhispererStudio-windows-x86_64.zip`, `ChipWhispererStudio-Web-windows-x86_64.zip` | Needs the WinUSB driver for ChipWhisperer devices |
| macOS on Apple Silicon (M1 and newer) | `ChipWhispererStudio-macos-arm64.zip`, `ChipWhispererStudio-Web-macos-arm64.zip` | Intel Macs are not supported by the bundle yet; use the [Python package](#python-package) |
| Linux (64-bit Intel/AMD) | `ChipWhispererStudio-linux-x86_64.zip`, `ChipWhispererStudio-Web-linux-x86_64.zip` | Built on Ubuntu 22.04, so it needs glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora 36 and later) |

Each zip contains one folder (`ChipWhispererStudio` or `ChipWhispererStudio-Web`) with:

- **Windows:** `ChipWhispererStudio.exe`, the application. The window build also has `cw-studio.exe`, the same program as a console application for the command line, scripts and the [MCP server](MCP-Server). `ChipWhispererStudio-simulator.bat` starts Studio with the simulator preselected.
- **macOS:** `ChipWhisperer Studio.app` (or `ChipWhisperer Studio Web.app`), a normal macOS application with the Studio icon.
- **Linux:** `ChipWhispererStudio`, the application, `chipwhisperer-studio.sh`, a small launcher script, and `ChipWhispererStudio.png`, the icon for desktop shortcuts.
- `50-newae.rules` (Windows and Linux folders): NewAE's Linux udev rule for ChipWhisperer devices.
- `README.md`, `LICENSE.txt` and `NOTICE.txt`.

Everything Studio needs (its own Python runtime, the `chipwhisperer` library, `libusb`, the Unicorn CPU emulator for the [code map](Code-on-the-Waveform) and all other dependencies) is inside the application. Each zip is about 72 MB.

With the window build, closing the window quits Studio. With the Web build, your browser opens Studio's address (normally `http://127.0.0.1:8765/`); on Windows and Linux a console window shows it, and closing the console stops the application (on macOS, quit the app).

### Windows

1. Download the zip of the build you want and extract it (right-click, **Extract All...**) to a folder such as `C:\Tools`. Do not run it from inside the zip.
2. Double-click `ChipWhispererStudio.exe` in the extracted folder.
3. If Windows SmartScreen shows "Windows protected your PC", click **More info** and then **Run anyway**. The bundle is not code-signed yet.
4. If Windows Firewall asks whether Studio may communicate on networks, you can decline: Studio only listens on your own computer (`127.0.0.1`) unless you start it with `--host 0.0.0.0`.
5. Studio opens in its window (or your browser for the Web build). Continue with the [Quick Start](Quick-Start).

The window uses Microsoft Edge WebView2, which is part of Windows 10 and 11. On the rare system without it, Studio opens in the browser instead; installing the [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/) from Microsoft brings the window back.

**USB driver.** ChipWhisperer devices need the WinUSB driver on Windows. If your ChipWhisperer is not detected, install the NewAE driver package as described in [NewAE's Windows driver instructions](https://chipwhisperer.readthedocs.io/en/latest/windows-install.html#windows-drivers), or assign WinUSB with Zadig. The Connect tab on Windows links to the same page.

### macOS

1. Download the zip of the build you want. Safari usually extracts it automatically; otherwise double-click the zip.
2. Move `ChipWhisperer Studio.app` (or `ChipWhisperer Studio Web.app`) to your Applications folder (or anywhere you like).
3. The app is not notarized by Apple yet, so the first time, right-click (or Control-click) it and choose **Open**, then confirm **Open** in the dialog. After that it opens normally with a double-click.
4. If macOS says the app "is damaged" or refuses to open it, remove the download quarantine flag in Terminal and try again: `xattr -dr com.apple.quarantine "/Applications/ChipWhisperer Studio.app"`
5. Studio opens in its window (or your browser for the Web build).

No USB driver is needed on macOS. If a device is not detected, try another cable or USB port. To use command-line options, run the program inside the app, for example `"/Applications/ChipWhisperer Studio.app/Contents/MacOS/ChipWhispererStudio" --simulate`.

### Linux

1. Download the zip of the build you want and extract it, for example: `unzip ChipWhispererStudio-linux-x86_64.zip -d ~/Applications`
2. Start it with `~/Applications/ChipWhispererStudio/chipwhisperer-studio.sh`
3. Studio opens in its window (or your browser for the Web build).
4. Optional: run `~/Applications/ChipWhispererStudio/ChipWhispererStudio --install-desktop` once to add ChipWhisperer Studio with its icon to your applications menu (`--remove-desktop` takes it out again).

**The window on Linux** uses WebKitGTK from your distribution, so Studio does not have to ship a browser engine. Most desktops have the library already; the Python bindings for it may need one package:

```bash
sudo apt install python3-gi gir1.2-webkit2-4.1        # Debian, Ubuntu, Kali, Mint
sudo dnf install python3-gobject webkit2gtk4.1        # Fedora
sudo pacman -S python-gobject webkit2gtk-4.1          # Arch
```

If they are missing, Studio prints this hint and opens in your browser instead, so it always starts.

**udev rule (once per computer).** Linux only lets root open USB devices unless a udev rule grants access. Studio includes NewAE's rule (`50-newae.rules`), and the Connect tab shows the exact command for your installation with a **Copy command** button. It looks like this:

```bash
sudo cp "/path/to/ChipWhispererStudio/_internal/cwstudio/resources/50-newae.rules" /etc/udev/rules.d/50-newae.rules && sudo groupadd -f chipwhisperer && sudo usermod -aG chipwhisperer $USER && sudo udevadm control --reload-rules && sudo udevadm trigger
```

The command copies the rule, creates a `chipwhisperer` group, adds you to it and reloads udev. Afterwards log out and back in (so your new group membership takes effect) and unplug and re-plug the ChipWhisperer.

> **Tip:** Use the command shown in the Connect tab rather than typing the one above: it already contains the correct path to the rule file inside your installation.

## Python package

Use this if you already work with Python. Studio needs **Python 3.10, 3.11 or 3.12**. The reason for the upper limit is that `chipwhisperer` 6.0.0 on PyPI requires numpy 1.26 or older, and numpy 1.26 has no ready-made packages for Python 3.13 and newer.

Install it from [PyPI](https://pypi.org/project/chipwhisperer-studio/), preferably in a virtual environment:

```bash
python -m venv studio-env
source studio-env/bin/activate            # Windows: studio-env\Scripts\activate
pip install chipwhisperer-studio
cw-studio
```

This installs the `chipwhisperer` library from PyPI together with Studio's other dependencies (Starlette, uvicorn, websockets, numpy, matplotlib, pyelftools and Unicorn for the code map, pywebview on Windows and macOS, and a few small ones) and adds the `cw-studio` and `cw-studio-web` commands. Run `cw-studio --simulate` to try it without hardware.

The wheel is also attached to every [GitHub release](https://github.com/keyuraghao/chipwhisperer-studio/releases), and you can install the latest development version straight from the repository: `pip install git+https://github.com/keyuraghao/chipwhisperer-studio@dev`

The USB driver (Windows) and udev rule (Linux) steps above apply to the Python package too.

### Window or browser

`cw-studio` opens Studio in its own window; `cw-studio-web` (or `cw-studio --browser`) opens it in your web browser. On Windows and macOS the window comes with the package (it installs [pywebview](https://pywebview.flowrl.com/), which uses Edge WebView2 or WebKit). On Linux it uses your distribution's WebKitGTK through the system Python, so install the packages listed under [Linux](#linux) above if Studio says they are missing.

## From source

```bash
git clone https://github.com/keyuraghao/chipwhisperer-studio
cd chipwhisperer-studio
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
cw-studio --simulate
```

See [Development and Releases](Development-and-Releases) for running the tests and building the standalone bundle yourself.

## Where Studio keeps its files

Everything Studio creates goes into one data folder, `ChipWhispererStudio` in your home folder (for example `C:\Users\you\ChipWhispererStudio` or `/home/you/ChipWhispererStudio`). You can choose another folder with `--data-dir`. The Connect tab's Status card shows the folder in use.

| Folder | Contents |
|--------|----------|
| `toolchains/` | Compilers downloaded for firmware builds, one folder per toolchain and version, plus your custom toolchain list (`custom.json`) and any refreshed toolchain list (`registry.json`). |
| `firmware/chipwhisperer/` | ChipWhisperer firmware sources (`firmware/mcu`) downloaded from NewAE's GitHub. |
| `firmware/builds/` | Every `.hex` (and `.elf`) you built, named after project, platform and compiler. |
| `firmware/uploads/` | Firmware files you uploaded in the Target tab. |
| `notebooks/` | Your notebooks (`.ipynb`), imported notebooks (`imported/`) and NewAE's tutorials (`chipwhisperer-jupyter/`). |
| `notes/` | Your notes, one Markdown file each. |
| `imports/` | Trace files you uploaded for import. |
| `exports/` and the folder itself | Trace and glitch exports. A relative file name in an export dialog is saved inside the data folder. |
| `logic/` | Logic analyser files: `captures/` (copies kept on disk), `exports/`, `imports/` and `sigrok/`. See [Logic Analyser](Logic-Analyser#files). |
| `codemap/uploads/` | ELF files uploaded in the Code tab. |
| `studio.log` | Studio's messages when it runs without a console (the window build on Windows). It is always in `~/ChipWhispererStudio`, even with `--data-dir`. |

Captured traces live in memory while Studio runs. Export them (see [Capturing Traces](Capturing-Traces)) if you want to keep them.

## Disk space

The download is about 72 MB and the application needs about 150 to 300 MB once extracted. Compilers are only downloaded when you first build firmware for that architecture, and they are large:

| Toolchain | Download | On disk (approximately) |
|-----------|----------|-------------------------|
| GNU Arm GCC | about 300 to 340 MB | about 1.1 GB |
| GNU RISC-V GCC | about 400 to 470 MB | about 1.6 GB |
| GNU AVR GCC | about 35 to 55 MB | about 220 MB |
| LLVM clang (Zig) | about 50 to 100 MB | about 400 MB |
| GNU make + sh (Windows only) | about 3 MB | about 10 MB |
| OpenOCD (only for [JTAG and SWD](Protocols-and-Interfaces#jtag-and-swd-openocd)) | about 3 MB | a few MB |

You can remove any toolchain again from the Firmware tab. See [Toolchains](Toolchains).

## Updating

- **Standalone bundle:** delete the old `ChipWhispererStudio` application folder and extract the new zip in its place (extracting over the old folder can leave stale files behind). Your data folder is separate, so notebooks, notes, compilers, builds and firmware sources are kept.
- **Python package:** `pip install --upgrade chipwhisperer-studio`, or `git pull` in a source checkout.

Firmware sources and the list of available compilers can be updated from inside Studio without a new release. See [Firmware Sources](Firmware-Sources) and [Toolchains](Toolchains).

## Uninstalling

1. Delete the `ChipWhispererStudio` application folder (standalone bundle), or run `pip uninstall chipwhisperer-studio` (Python package).
2. If you also want to remove your notebooks, notes, builds and downloaded compilers, delete the data folder (`~/ChipWhispererStudio` by default). Export anything you want to keep first.
3. On Linux you can remove the udev rule with `sudo rm /etc/udev/rules.d/50-newae.rules`, unless other ChipWhisperer software still needs it.

## Optional extras

- **sigrok-cli** lets the [Logic Analyser](Logic-Analyser#external-analysers-sigrok) capture from external logic analysers (Saleae clones, DSLogic and others). Studio's own decoders and file import work without it.
- **OpenOCD** for [JTAG and SWD](Protocols-and-Interfaces#jtag-and-swd-openocd) installs from inside Studio with one click.

## Next steps

- [Quick Start](Quick-Start): your first capture and key recovery.
- [Troubleshooting](Troubleshooting) if something did not work.
