# Installation

ChipWhisperer Studio comes as a self-contained download for Windows, macOS and Linux that needs nothing else installed. You can also install it as a Python package if you already work in Python.

## Which option should I choose?

| Option | Best for | Needs Python? |
|--------|----------|---------------|
| [Standalone bundle](#standalone-bundle) | Most users, classrooms, machines without Python | No |
| [Python package](#python-package) | People who already use Python and the `chipwhisperer` library, or want to use Studio's notebook with their own packages | Yes, Python 3.10 to 3.12 |
| [From source](#from-source) | Contributors and people who want the latest `main` | Yes, Python 3.10 to 3.12 |

## Standalone bundle

Every [release](https://github.com/keyuraghao/chipwhisperer-studio/releases/latest) has one zip file per platform:

| Platform | File | Notes |
|----------|------|-------|
| Windows 10 and 11 (64-bit Intel/AMD) | `ChipWhispererStudio-windows-x86_64.zip` | Needs the WinUSB driver for ChipWhisperer devices |
| macOS on Apple Silicon (M1 and newer) | `ChipWhispererStudio-macos-arm64.zip` | Intel Macs are not supported by the bundle yet; use the [Python package](#python-package) |
| Linux (64-bit Intel/AMD) | `ChipWhispererStudio-linux-x86_64.zip` | Built on Ubuntu 22.04, so it needs glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora 36 and later) |

Each zip contains a single `ChipWhispererStudio` folder with:

- `ChipWhispererStudio` (or `ChipWhispererStudio.exe` on Windows): the application, with its own Python runtime, the `chipwhisperer` library, `libusb` and all other dependencies inside the `_internal` folder.
- `chipwhisperer-studio.sh` (macOS and Linux): a small launcher script.
- `ChipWhispererStudio-simulator.bat` (Windows): starts Studio with the simulator preselected.
- `50-newae.rules`: the Linux udev rule for ChipWhisperer devices.
- `README.md`, `LICENSE.txt` and `NOTICE.txt`.

When Studio starts, a console window shows its address (normally `http://127.0.0.1:8765/`) and your web browser opens it automatically. Keep the console window open while you use Studio; closing it stops the application.

### Windows

1. Download `ChipWhispererStudio-windows-x86_64.zip` and extract it (right-click, **Extract All...**) to a folder such as `C:\Tools`. Do not run it from inside the zip.
2. Double-click `ChipWhispererStudio.exe` in the extracted `ChipWhispererStudio` folder.
3. If Windows SmartScreen shows "Windows protected your PC", click **More info** and then **Run anyway**. The bundle is not code-signed yet.
4. If Windows Firewall asks whether Studio may communicate on networks, you can decline: Studio only listens on your own computer (`127.0.0.1`) unless you start it with `--host 0.0.0.0`.
5. Your browser opens Studio. Continue with the [Quick Start](Quick-Start).

**USB driver.** ChipWhisperer devices need the WinUSB driver on Windows. If your ChipWhisperer is not detected, install the NewAE driver package as described in [NewAE's Windows driver instructions](https://chipwhisperer.readthedocs.io/en/latest/windows-install.html#windows-drivers), or assign WinUSB with Zadig. The Connect tab on Windows links to the same page.

### macOS

1. Download `ChipWhispererStudio-macos-arm64.zip`. Safari usually extracts it automatically; otherwise double-click the zip.
2. Move the `ChipWhispererStudio` folder somewhere permanent, for example your Applications or Documents folder.
3. The bundle is not notarized by Apple yet, so the first time you open it, right-click (or Control-click) `ChipWhispererStudio` and choose **Open**, then confirm **Open** in the dialog. After that it opens normally with a double-click. You can also run `./chipwhisperer-studio.sh` from Terminal.
4. If macOS says the app "is damaged" or refuses to open it, remove the download quarantine flag in Terminal and try again: `xattr -dr com.apple.quarantine /path/to/ChipWhispererStudio`
5. Your browser opens Studio.

No USB driver is needed on macOS. If a device is not detected, try another cable or USB port.

### Linux

1. Download `ChipWhispererStudio-linux-x86_64.zip` and extract it, for example: `unzip ChipWhispererStudio-linux-x86_64.zip -d ~/Applications`
2. Start it with `~/Applications/ChipWhispererStudio/chipwhisperer-studio.sh`
3. Your browser opens Studio.

**udev rule (once per computer).** Linux only lets root open USB devices unless a udev rule grants access. Studio includes NewAE's rule (`50-newae.rules`), and the Connect tab shows the exact command for your installation with a **Copy command** button. It looks like this:

```bash
sudo cp "/path/to/ChipWhispererStudio/_internal/cwstudio/resources/50-newae.rules" /etc/udev/rules.d/50-newae.rules && sudo groupadd -f chipwhisperer && sudo usermod -aG chipwhisperer $USER && sudo udevadm control --reload-rules && sudo udevadm trigger
```

The command copies the rule, creates a `chipwhisperer` group, adds you to it and reloads udev. Afterwards log out and back in (so your new group membership takes effect) and unplug and re-plug the ChipWhisperer.

> **Tip:** Use the command shown in the Connect tab rather than typing the one above: it already contains the correct path to the rule file inside your installation.

## Python package

Use this if you already work with Python. Studio needs **Python 3.10, 3.11 or 3.12**. The reason for the upper limit is that `chipwhisperer` 6.0.0 on PyPI requires numpy 1.26 or older, and numpy 1.26 has no ready-made packages for Python 3.13 and newer.

Studio is not on PyPI yet. Install the wheel from the latest GitHub release instead:

```bash
python -m venv studio-env
source studio-env/bin/activate            # Windows: studio-env\Scripts\activate
pip install https://github.com/keyuraghao/chipwhisperer-studio/releases/download/v0.4.4/chipwhisperer_studio-0.4.4-py3-none-any.whl
cw-studio
```

This installs the `chipwhisperer` library from PyPI together with Studio's other dependencies (Starlette, uvicorn, websockets, numpy, matplotlib and a few small ones) and adds the `cw-studio` command. Run `cw-studio --simulate` to try it without hardware.

You can also install straight from the repository: `pip install git+https://github.com/keyuraghao/chipwhisperer-studio`

The USB driver (Windows) and udev rule (Linux) steps above apply to the Python package too.

### Optional native window

By default Studio opens in your web browser. To open it in its own desktop window instead, install the `window` extra and start Studio with `--window`:

```bash
pip install "chipwhisperer-studio[window] @ https://github.com/keyuraghao/chipwhisperer-studio/releases/download/v0.4.4/chipwhisperer_studio-0.4.4-py3-none-any.whl"
cw-studio --window
```

This uses [pywebview](https://pywebview.flowrl.com/). If pywebview is missing, Studio prints a message and falls back to the browser.

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

Captured traces live in memory while Studio runs. Export them (see [Capturing Traces](Capturing-Traces)) if you want to keep them.

## Disk space

The application itself needs about 150 to 300 MB once extracted. Compilers are only downloaded when you first build firmware for that architecture, and they are large:

| Toolchain | Download | On disk (approximately) |
|-----------|----------|-------------------------|
| GNU Arm GCC | about 300 MB | about 1.1 GB |
| GNU RISC-V GCC | about 400 to 470 MB | about 1.6 GB |
| GNU AVR GCC | about 40 to 55 MB | about 220 MB |
| LLVM clang (Zig) | about 50 to 100 MB | about 400 MB |
| GNU make + sh (Windows only) | about 3 MB | about 10 MB |

You can remove any toolchain again from the Firmware tab. See [Toolchains](Toolchains).

## Updating

- **Standalone bundle:** download the new zip and extract it next to (or over) the old folder. Your data folder is separate, so notebooks, notes, compilers, builds and firmware sources are kept.
- **Python package:** `pip install --upgrade` with the new release's wheel URL, or `git pull` in a source checkout.

Firmware sources and the list of available compilers can be updated from inside Studio without a new release. See [Firmware Sources](Firmware-Sources) and [Toolchains](Toolchains).

## Uninstalling

1. Delete the `ChipWhispererStudio` application folder (standalone bundle), or run `pip uninstall chipwhisperer-studio` (Python package).
2. If you also want to remove your notebooks, notes, builds and downloaded compilers, delete the data folder (`~/ChipWhispererStudio` by default). Export anything you want to keep first.
3. On Linux you can remove the udev rule with `sudo rm /etc/udev/rules.d/50-newae.rules`, unless other ChipWhisperer software still needs it.

## Next steps

- [Quick Start](Quick-Start): your first capture and key recovery.
- [Troubleshooting](Troubleshooting) if something did not work.
