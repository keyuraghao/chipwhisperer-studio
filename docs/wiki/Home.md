<p align="center"><img src="images/logo.png" alt="ChipWhisperer Studio" width="120"></p>

# ChipWhisperer Studio

ChipWhisperer Studio is a desktop application for [NewAE ChipWhisperer](https://github.com/newaetech/chipwhisperer) side-channel and fault-injection hardware. It lets you connect a scope, build and flash target firmware, capture power traces while the waveform updates live, recover AES keys with correlation power analysis (CPA) and sweep glitch parameters, all from one window and without setting up Python or Jupyter. It also talks to the target over UART, SPI, GPIO and JTAG/SWD, captures and decodes logic signals, and shows which firmware code runs at each point of a trace.

<picture><source media="(prefers-color-scheme: light)" srcset="images/overview-light.png"><img alt="ChipWhisperer Studio with the waveform view and the Capture tab" src="images/overview.png"></picture>
*The main window: navigation on the left, the panel for the current tab in the middle, and the live waveform on the right.*

> **Note:** ChipWhisperer Studio is an independent community project. It is not affiliated with or endorsed by NewAE Technology Inc. It uses NewAE's open source `chipwhisperer` Python library for all hardware access. "ChipWhisperer" is a trademark of NewAE Technology Inc.

## Who it is for

- **Students and newcomers** who want to see power analysis and glitching work without first learning the Python API and notebook setup.
- **Lab and workshop instructors** who need something that installs in one step on Windows, macOS and Linux, and that works with a built-in simulator when there is not enough hardware for everyone.
- **Researchers and engineers** who want a fast interactive tool for tuning a capture setup, plus notebooks, an HTTP API and an MCP server for automation.

> **Note:** Studio has been tested extensively with the built-in [simulator](Simulator) and in continuous integration on Windows, macOS and Linux, but not yet on physical ChipWhisperer hardware. Please [report](https://github.com/keyuraghao/chipwhisperer-studio/issues) anything that behaves differently on your device.

## What you can do

**Connect hardware.** Auto-detect a ChipWhisperer Nano, Lite, Pro or Husky, pick one by serial number, or use the simulator, which can pose as any of them. See [Connecting Hardware](Connecting-Hardware) and [Simulator](Simulator).

**Configure the scope.** Every setting of the connected scope (gain, ADC, clock, trigger, IO, glitch module and the Husky extras) appears in an editable tree with its documentation, and every change is read back from the hardware. See [Scope Settings](Scope-Settings).

**Program and talk to the target.** Flash a `.hex` file with the right programmer, watch the serial terminal and send SimpleSerial commands by hand. See [Target and Programming](Target-and-Programming).

**Use the target's interfaces.** UART with a terminal, SimpleSerial, an SPI master for flash chips, GPIO, the Husky's USERIO header, every trigger type, the bit-banger and 1-Wire, and JTAG/SWD debugging through OpenOCD. Studio enables exactly what the connected model supports and says why the rest is unavailable. See [Protocols and Interfaces](Protocols-and-Interfaces).

**Build firmware without installing compilers.** Build any ChipWhisperer firmware project for any supported platform with GCC or clang. Studio downloads the compilers and the firmware sources for you the first time. See [Firmware Builds](Firmware-Builds), [Toolchains](Toolchains) and [Firmware Sources](Firmware-Sources).

**Capture traces and watch them live.** Capture single traces, a fixed number or continuously, with fixed, random or counter keys and plaintexts, and export to NumPy, CSV or a ChipWhisperer project. See [Capturing Traces](Capturing-Traces) and [Waveform Viewer](Waveform-Viewer).

**Recover keys with CPA.** In the **Analysis** tab, run a progressive correlation power analysis attack with five AES leakage models and watch every key byte converge. The [Quick Start](Quick-Start) walks through a full attack.

**Find glitches.** In the **Glitch** tab, sweep any glitch parameters, reset the target when it crashes and map successes on a live scatter plot.

**See the code on the waveform.** The **Code** tab emulates your firmware for a captured trace and draws its functions and source lines under the waveform; select part of the trace to see the code that made it. See [Code on the Waveform](Code-on-the-Waveform).

**Capture and decode logic signals.** The **Logic** tab records digital signals from the Husky's logic analyser, any scope's analog input, sigrok analysers or files, and decodes UART, SPI, I2C, 1-Wire, JTAG, SWD, CAN and SimpleSerial. See [Logic Analyser](Logic-Analyser).

**Write code when you want to.** Jupyter-style notebooks run Python cell by cell against the same connected hardware, as tabs or side by side with a kernel each, and run NewAE's own tutorial notebooks unmodified. See [Notebooks](Notebooks).

**Take notes and do the maths.** A text pad, a calculator with side-channel helpers, and live statistics of anything you select. See [Notes and Calculator](Notes-and-Calculator).

**Automate everything.** Drive Studio from AI agents through the built-in MCP server, or from scripts through the HTTP API. See [MCP Server](MCP-Server) and [HTTP API](HTTP-API).

## Start here

1. [Install Studio](Installation) (a download for your OS, nothing else to install; it opens in its own window or, with the Web build, in your browser).
2. Follow the [Quick Start](Quick-Start) to capture traces and recover a key in a few minutes, with the simulator or with real hardware.
3. Watch the [Video Tour](Video-Tour) (every feature in a few minutes), or take the [Interface Tour](Interface-Tour) to learn where everything is.
4. Stuck? Check [Troubleshooting](Troubleshooting) and the [FAQ](FAQ).

## All pages

**Getting started**

- [Installation](Installation)
- [Quick Start](Quick-Start)
- [Video Tour](Video-Tour)
- [Interface Tour](Interface-Tour)
- [Connecting Hardware](Connecting-Hardware)
- [Simulator](Simulator)

**Using Studio**

- [Scope Settings](Scope-Settings)
- [Target and Programming](Target-and-Programming)
- [Capturing Traces](Capturing-Traces)
- [Waveform Viewer](Waveform-Viewer)
- [Protocols and Interfaces](Protocols-and-Interfaces)
- [Logic Analyser](Logic-Analyser)
- [Code on the Waveform](Code-on-the-Waveform)

**Building and coding**

- [Firmware Builds](Firmware-Builds)
- [Toolchains](Toolchains)
- [Firmware Sources](Firmware-Sources)
- [Notebooks](Notebooks)
- [Notes and Calculator](Notes-and-Calculator)

**Automation**

- [MCP Server](MCP-Server)
- [HTTP API](HTTP-API)
- [Command Line and Configuration](Command-Line-and-Configuration)

**Help**

- [Troubleshooting](Troubleshooting)
- [FAQ](FAQ)

**Developers**

- [Performance](Performance)
- [Architecture](Architecture)
- [Development and Releases](Development-and-Releases)

## Downloads, releases and license

- Downloads for Windows, macOS and Linux are on the [releases page](https://github.com/keyuraghao/chipwhisperer-studio/releases/latest).
- What changed in each version is listed in the [CHANGELOG](https://github.com/keyuraghao/chipwhisperer-studio/blob/main/CHANGELOG.md).
- The source code is at [github.com/keyuraghao/chipwhisperer-studio](https://github.com/keyuraghao/chipwhisperer-studio).
- ChipWhisperer Studio is licensed under the [Apache License 2.0](https://github.com/keyuraghao/chipwhisperer-studio/blob/main/LICENSE), the same license as ChipWhisperer. Third-party components are listed in [NOTICE](https://github.com/keyuraghao/chipwhisperer-studio/blob/main/NOTICE). Compilers that Studio downloads for you are covered by their own licenses.
