# Troubleshooting

Problems are grouped by symptom. Each entry explains the likely cause and the fix. If nothing here helps, see [Reporting a bug](#reporting-a-bug) at the end.

> **Note:** Studio has been tested with the built-in simulator and in automated builds on Linux, Windows and macOS. It has not yet been verified on physical ChipWhisperer hardware, so hardware-specific problems may exist that are not listed here. Please report them.

## Where to find logs

- **Log drawer:** the bottom panel of the Studio window shows Studio and ChipWhisperer messages. Type in the filter box to search. Errors also pop up as red notifications.
- **Console:** the terminal (or the console window of the standalone bundle) shows server messages. Start Studio with `--log-level debug` for more detail.
- **API:** `GET /api/logs` returns recent log records as JSON (see [HTTP API](HTTP-API)).
- **Firmware builds:** the full `make` output is under **Build output** on the Firmware tab.

## Hardware and connection

### "No ChipWhisperer found" or the device is not listed

1. Press **Scan USB** on the Connect tab. If the list is empty, the operating system does not see a NewAE device.
2. Try another USB cable (many cables are charge-only) and another port, preferably directly on the computer rather than a hub.
3. **Windows:** ChipWhisperer devices need the WinUSB driver. Install NewAE's driver package (the Connect tab links to it) or use Zadig to assign WinUSB, then reconnect.
4. **Linux:** install the udev rule (next section).
5. **macOS:** no driver is needed. Try another cable or port.

### Permission denied or "access denied" on Linux

Your user may not access the USB device without a udev rule. The Connect tab shows the exact command for your installation: it copies the bundled `50-newae.rules` into `/etc/udev/rules.d/`, creates the `chipwhisperer` group, adds your user to it and reloads udev. After running it, log out and back in (so the new group applies) and replug the device.

### "Device is in use" or connecting fails after another program used it

Another program (a Jupyter notebook, a second Studio) still holds the device. Close it and connect again. If the scope was left in a bad state, tick **force FPGA reprogram** on the Connect tab, which reloads the scope's FPGA while connecting.

### The target does not answer

- Check that the target is programmed with SimpleSerial firmware matching the protocol you chose: **SimpleSerial v2** for current ChipWhisperer firmware (`SS_VER_2_1`), **SimpleSerial v1** for older builds (`SS_VER_1_1`).
- Use the serial console on the Target tab to see whether anything comes back.
- Reprogram the target (see [Target and Programming](Target-and-Programming)) and make sure the clock is running (`default_setup()` on the Scope tab configures the standard clock for CW-Lite and CW-Husky targets).

## Starting Studio

### Port already in use

Studio automatically tries the next 49 ports if 8765 is taken and prints the URL it actually used. To choose a port yourself, pass `--port 9000`. If you want to find the process holding the port: `lsof -i :8765` (macOS, Linux) or `netstat -ano | findstr 8765` (Windows).

### Studio opened in the browser instead of its own window

Studio prints the reason in its console (or in `~/ChipWhispererStudio/studio.log` when there is no console) and uses the browser so you can keep working. To quit Studio afterwards, close its console window if it has one; without a console, end it from Task Manager, Activity Monitor or your system monitor, or send `POST /api/shutdown` (for example `curl -X POST http://127.0.0.1:8765/api/shutdown`).

- **Linux:** the window needs WebKitGTK for the system Python. Install `python3-gi` and `gir1.2-webkit2-4.1` (Debian, Ubuntu, Kali, Mint), `python3-gobject webkit2gtk4.1` (Fedora) or `python-gobject webkit2gtk-4.1` (Arch). Over SSH without a display there is no window; use `--no-browser` and open the address from another machine.
- **Windows:** the window needs Microsoft Edge WebView2, which Windows 10 and 11 include; on a system without it, install the [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **Python package on Windows or macOS:** the window needs pywebview, which `pip` installs with Studio; reinstall Studio if it is missing.

You can also choose the browser on purpose with `--browser`, `cw-studio-web` or the ChipWhisperer Studio Web build.

### The browser did not open

Open the URL printed in the console (by default `http://127.0.0.1:8765/`) manually. On a machine without a graphical display (over SSH), Studio does not try to open anything and prints the address with a hint: forward the port (`ssh -L 8765:127.0.0.1:8765 lab-machine`) and open it on your computer, or use `--no-browser`.

### macOS says the app cannot be opened or is damaged

The standalone bundle is not notarized by Apple. Right-click `ChipWhisperer Studio.app` and choose **Open**, then confirm. If macOS still blocks it, run `xattr -dr "com.apple.quarantine" "/Applications/ChipWhisperer Studio.app"` once. The macOS bundle is built for Apple Silicon (arm64); on Intel Macs install with pip instead.

### Windows SmartScreen warns about the app

The executable is not code-signed. Choose **More info** and **Run anyway** if you downloaded it from the official GitHub release page.

## Interfaces

### A feature is greyed out

The Interfaces tab, the Target tab's programmer list and the Scope tab's choices show only what the connected ChipWhisperer supports; hover the disabled item for the reason. The full list per model is on [Protocols and Interfaces](Protocols-and-Interfaces#capability-matrix). With the simulator, check **Simulate as** on the Connect tab: a simulated Nano has far fewer features than a simulated Husky.

### "the scope firmware is too old for ..."

The SPI master, JTAG/SWD and the USB-CDC serial port need newer scope firmware than some older ChipWhisperers have. Update it with the `chipwhisperer` library's `scope.upgrade_firmware()`, for example in a cell in the Notebook tab, as described on [NewAE's firmware page](https://chipwhisperer.readthedocs.io/en/latest/firmware.html), then reconnect.

### The bit-banger or 1-Wire says it needs a newer chipwhisperer library

They exist only in ChipWhisperer's `develop` branch, not in the 6.0.0 release that the standalone bundles use. Install the library from NewAE's repository into a pip installation of Studio.

### JTAG or SWD (OpenOCD)

| Symptom | Cause and fix |
|---------|---------------|
| *OpenOCD is not installed* | Press **Install OpenOCD** in the JTAG and SWD section (or install it on the Toolchains card), or put `openocd` on `PATH`. |
| *enable MPSSE (JTAG/SWD) mode on the scope first* | Press **Enable MPSSE** before **Start server**. |
| The scope disappeared after **Enable MPSSE** | Expected: the scope works as a debug adapter until you press **Restore normal mode**, which reconnects it. Capture, USB-CDC serial and the native programmers are unavailable meanwhile. |
| Studio was closed while the scope was in MPSSE mode | Studio finds a scope left in MPSSE mode when it starts and shows a banner in the Interfaces tab; press **Restore normal mode**. Unplugging and replugging the ChipWhisperer also returns it to normal mode. |
| OpenOCD cannot find the target | Check the wiring (SCK to TCK/SWCLK, PDID to TMS/SWDIO, MISO to TDO, MOSI to TDI), the target configuration and that the target is powered. The OpenOCD log in the section shows its messages. |
| SWD does not work on a Husky | SWD needs Husky SAM firmware 1.4 or newer. |

## Logic analyser

| Symptom | Cause and fix |
|---------|---------------|
| *sigrok-cli is not installed* | Install it (`sudo apt install sigrok-cli`, `brew install sigrok-cli` or the Windows installer), restart Studio or set `CWSTUDIO_SIGROK_CLI`. On Linux, replug the analyser after installing so sigrok's udev rules apply. See [Logic Analyser](Logic-Analyser#external-analysers-sigrok). |
| *no logic analyser trigger within ... s* | The Husky's trigger did not fire. Try the `manual` trigger to check the setup, use `capture` with **Fire target** to make the target raise its trigger, or raise the timeout. |
| *the logic analyser clock is not locked* | The Husky cannot make that sampling clock. Change the clock source or the oversampling factor. |
| The analog input source shows nothing useful | The signal must reach the measure input and fit the ADC range; adjust the gain, or set the threshold and hysteresis by hand. Every segment needs its own scope trigger. |
| A decoder finds nothing or only errors | Check its channels (they are guessed from channel names) and options: the UART baud rate (`auto` needs a few short pulses), the SPI mode, the CAN bit rate. Raise the sample rate: UART needs at least twice the baud rate, 1-Wire at least 1 MHz. Try the glitch filter on noisy captures. |
| A CSV import has the wrong time scale | A CSV without a time column needs its sample rate; Studio asks for it, and the API takes `samplerate`. |
| *decoding...* for a long time | Decoders run in a background process on captures with many edges; the rest of Studio keeps working meanwhile. |

## Code map

| Symptom | Cause and fix |
|---------|---------------|
| *no firmware ELF yet* | Build firmware in the Firmware tab (Studio keeps the `.elf`), program one, or choose an `.elf` under **Other ELF file**. A `.hex` alone has no symbols. |
| *the firmware has no getch function ...* | The emulator feeds SimpleSerial input through `getch`. If link time optimisation inlined it, build without `-flto`. |
| The run stops *in a loop of a few instructions* | The firmware waits for a peripheral, an interrupt or a timer, which the emulator does not provide. Skip that function (`options.skip` over the [HTTP API](HTTP-API#code-map)). |
| *source file not found: choose the source folder* | The firmware was built elsewhere or the sources moved: give the folder in **Sources**. |
| Only functions, no source lines | The ELF has no debug information; build with `-g` (ChipWhisperer's makefiles do by default). |
| Alignment confidence is low and the fit is not applied | The traces probably do not come from this firmware or these inputs, or the scope settings changed after the map was built. Check **Response** and **Stored trace** in the Firmware card, build again, or set the shift by hand. |
| The band is off by a constant amount | Check the target clock and `adc.offset`, build again after changing scope settings, or nudge the shift with **-cyc** and **+cyc**. |

## Downloads (compilers, firmware sources, tutorials)

### `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`

Python could not verify an HTTPS certificate. Studio uses the operating system's certificate store (with Mozilla's bundle from `certifi` as a fallback), which works for standard setups. If it still happens you are probably on a network that inspects HTTPS with its own root certificate:

1. Install that root certificate in the operating system's trust store (your IT department can provide it), then restart Studio, or
2. Save the certificate (PEM) and start Studio with `SSL_CERT_FILE=/path/to/bundle.pem`.

The error message names the server that failed, which helps your IT department.

### GitHub rate limit (HTTP 403) when checking for updates or downloading sources

GitHub allows 60 anonymous API requests per hour per IP address, which shared networks can exhaust. Create a GitHub token (no scopes needed) and start Studio with `GITHUB_TOKEN=...`. When GitHub cannot be reached at all, firmware downloads fall back to a known good commit.

### Downloads are very slow

Compilers are large (hundreds of MB). Downloads of entries that list mirrors (the clang toolchain) switch to the next mirror automatically if a server is slower than about 256 KB/s. For the others, retry later or install the compiler yourself and register it as a custom toolchain or put it on `PATH` (Studio detects compilers on `PATH`).

### "checksum mismatch" when installing a toolchain

The downloaded file does not match the SHA-256 pinned in Studio's registry. The file is deleted and nothing is installed. Usually this is a truncated download or a proxy that altered it; retry. If it persists, press **Refresh list** in case the published toolchain list changed, and report it.

## Firmware builds

### "no GCC toolchain for arm/avr/riscv"

Install the matching compiler on the **Toolchains** card (the **Build firmware** card offers an **Install now** link). Clang builds need the GCC toolchain too, because GCC provides the C library and links the firmware.

### "make not found"

- **Windows:** install **GNU make + sh** on the **Toolchains** card.
- **macOS:** run `xcode-select --install`.
- **Linux:** install make with your package manager, for example `sudo apt install make`.

### "no firmware sources yet"

Download the sources on the **Firmware sources** card of the Firmware tab, or point Studio at your own checkout with **Use my own firmware folder** (see [Firmware Sources](Firmware-Sources)).

### Some platforms always fail

A few platforms fail because of bugs in NewAE's HAL sources, with any compiler: CW308_EFM32GG11 (the HAL contains `#error "Unfinished HAL"`), CW308_PSOC62 (syntax error) and CW308_NRF52 (missing linker script). With clang, CW308_CC2538, CW308_LPC55S6X, CW308_IMXRT1062 and CW308_FE310 fail because their sources use GCC-only constructs: use GCC for those. AURIX, RX65N and MPC5676R need a custom toolchain. See [Firmware Builds](Firmware-Builds) for the full coverage list.

### The build fails after switching compilers

Keep **Clean before building** ticked (the default). Object files from one compiler cannot be linked with another.

## Programming

- Make sure the scope is connected: programming goes through the scope.
- Choose the programmer for your target family (STM32F for CW-Lite ARM, CW-Nano and STM32 targets, XMEGA for CW-Lite XMEGA, AVR for ATmega, SAM4S for the Husky target, NEORV32 for the soft core). Build & program picks it automatically.
- Some targets need the clock running before programming; run `default_setup()` on the Scope tab first.
- With the simulator, programming is simulated and only checks that the file exists.

## Capturing

### Every capture times out

- The target is not running the expected firmware, or it is not answering on SimpleSerial (see "The target does not answer").
- The trigger setting does not match the firmware (`trigger.triggers` is normally `tio4` for ChipWhisperer targets).
- For firmware without SimpleSerial, use **Trigger only** mode.
- Untick **expect ack** for firmware that does not send an acknowledgement.
- Capture stops after `max_timeouts` consecutive timeouts (10 by default) to avoid looping forever.

### The waveform is flat or clipped

Adjust `gain.db` (lower if the trace hits the top or bottom of the range, higher if it is flat) and `adc.samples` / `adc.offset` so the interesting part of the operation is inside the window.

## CPA does not recover the key

- Use a **fixed key** and **random plaintext** when capturing (the defaults).
- Pick the leakage model that matches the implementation: `sbox_hw` for software AES such as TINYAES128C, `lastround_hd` for hardware AES.
- Capture more traces (tens for the simulator, hundreds to thousands for real targets) and limit the sample range to the first round with **use zoom** on the Analysis tab.
- Check the traces are aligned and not clipped.

## Glitch sweep finds no successes

- Configure the glitch module on the Scope tab first: for clock glitching, `glitch.clk_src`, `glitch.output` (for example `clock_xor`), `glitch.trigger_src` (for example `ext_single`) and the IO routing such as `io.hs2 = "glitch"`.
- Set **Expected** to the normal response so wrong answers count as successes.
- Widen the ranges or use **repeats**. Many results marked **reset** mean the glitch is too strong; narrow the width.

## Notebooks

| Symptom | Cause and fix |
|---------|---------------|
| A cell keeps running | Press **Stop** (interrupts the cell and clears queued cells). Code blocked inside a USB call stops as soon as that call returns. |
| Other tabs do not respond to hardware buttons | A notebook cell is using the hardware thread. Wait for it or press Stop. |
| `No module named 'holoviews'` or `bokeh` | Those plotting libraries are not included. Use matplotlib, which is included and shows figures inline. With a pip install you can `pip install holoviews`, but its interactive plots are not displayed. |
| `%pip install` says it is not possible | The standalone bundle cannot install packages. Use a pip installation of Studio for that. |
| `input()` raises an error | Interactive input is not supported; set values in code. |
| Download tutorials asks about files you changed | You edited tutorial files that also changed upstream. **Update, back up mine** keeps your version as `<name>.local-<date>-<time>.ipynb`; **Keep mine** leaves them untouched. |
| Tutorial build cells fail with "No such file" | Download the firmware sources on the Firmware tab, then download the tutorials again so the firmware folder link is created. |
| `type(scope).__name__` prints `StudioScope` | In notebooks, `scope` and `target` are stand-ins that forward to Studio's connection. `isinstance(scope, OpenADC)` and every attribute work as usual. |
| A notice says the notebook *was changed outside this tab* | A run from the API or an agent, or another window, saved the notebook while this tab had unsaved edits. Choose **Reload from disk** or **Keep my version**; see [Notebooks](Notebooks#changes-from-other-windows-and-agents). |
| Pressing S or R in the Notebook tab does nothing | The single-key capture shortcuts are off while a notebook has the focus, so they cannot fire while you edit; use the **Single** and **Run** buttons in the top bar. |

See [Notebooks](Notebooks).

## MCP clients

- Run `cw-studio mcp` in a terminal to see startup errors (all MCP logs go to stderr).
- For the standalone bundle, the client's `command` must be the full path to the executable with `mcp` as the first argument. On Windows, use `cw-studio.exe` from the window build (its `ChipWhispererStudio.exe` has no console for the MCP protocol), or `ChipWhispererStudio.exe` from the Web build.
- If the agent does not see your browser session, pass `--url` with the port your Studio uses.

See [MCP Server](MCP-Server).

## Reporting a bug

Open an issue at [github.com/keyuraghao/chipwhisperer-studio/issues](https://github.com/keyuraghao/chipwhisperer-studio/issues) and include:

1. The Studio version (shown next to the title in the top bar) and how you installed it (bundle or pip).
2. Your operating system and version, and the ChipWhisperer scope and target you use.
3. What you did, what you expected and what happened.
4. The relevant log lines from the log drawer or console (start with `--log-level debug` if you can reproduce it), or the build output for build problems.

See also: [FAQ](FAQ).
