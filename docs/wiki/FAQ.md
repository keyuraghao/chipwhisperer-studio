# FAQ

Short answers to the questions people ask most. Each links to the page with the details.

### Is ChipWhisperer Studio official NewAE software?

No. It is an independent open source project and is not affiliated with or endorsed by NewAE Technology Inc. It uses NewAE's open source `chipwhisperer` Python library for all hardware access and downloads NewAE's firmware and tutorial notebooks from their public GitHub repositories. "ChipWhisperer" is a trademark of NewAE Technology Inc.

### Has it been tested on real hardware?

Not yet. Studio has been tested with its built-in simulator and in automated builds on Linux, Windows and macOS, which download real compilers and build real ChipWhisperer firmware. Because every hardware call goes through the unmodified `chipwhisperer` library it is expected to work, but please report anything that does not ([Troubleshooting](Troubleshooting)).

### Which hardware is supported?

The scopes supported by the `chipwhisperer` library: ChipWhisperer-Nano, Lite, Pro (CW1200), Husky and Husky Plus, with SimpleSerial v1 and v2 targets, the CW305 FPGA board, and the programmers for STM32F, XMEGA, AVR, SAM4S and NEORV32 targets. Firmware builds cover the 38 platforms in ChipWhisperer's firmware tree (see [Firmware Builds](Firmware-Builds)).

### Does it work with the ChipWhisperer-Husky?

Yes, Husky and Husky Plus are among the supported scope types, their extra settings appear in the settings tree, and the Husky's built-in target is built with platform `CWHUSKY` and programmed with the SAM4S programmer.

### Do I need Python installed?

No, if you use the standalone bundle from the releases page: it contains its own Python, the `chipwhisperer` library and libusb. With the pip package you need Python 3.10 to 3.12 (see [Installation](Installation)).

### Does it replace Jupyter?

For most everyday work, yes: connecting, configuring, programming, capturing, CPA and glitch sweeps need no code at all. When you want code, the Notebook tab runs Python cell by cell against the same hardware, and it runs NewAE's tutorial notebooks unmodified. Notebooks are standard `.ipynb` files, so you can keep using Jupyter for anything Studio does not do. See [Notebooks](Notebooks).

### Can I use my own firmware?

Yes. Program any `.hex` or `.bin` from the Target tab, build your own projects by pointing Studio at your ChipWhisperer checkout (**Use my own firmware folder**), or run `make` from a notebook. See [Target and Programming](Target-and-Programming) and [Firmware Sources](Firmware-Sources).

### Can I build firmware without installing compilers?

Studio installs them for you. The first time a build needs a compiler, the Toolchains card downloads the official release for your operating system, checks its SHA-256 and uses it offline from then on. See [Toolchains](Toolchains).

### Does it work offline?

Yes, once the compilers, firmware sources and (if you want them) tutorial notebooks are downloaded. Capturing, analysis, glitching, notebooks, notes and the calculator never need the internet.

### Where are my files?

In the data folder, `~/ChipWhispererStudio` by default (or the folder you pass with `--data-dir`): exports, firmware builds, compilers, notebooks and notes. See [Command Line and Configuration](Command-Line-and-Configuration).

### How do I get NewAE's latest firmware and examples?

Open the Firmware tab and press **Check for updates**, then **Update now**. Studio follows NewAE's `develop` branch by default; you can follow the latest release or a specific tag instead. No new Studio release is needed. See [Firmware Sources](Firmware-Sources).

### Can I try it without hardware?

Yes. Set **Device** to **Simulator (no hardware)** on the Connect tab or start with `--simulate`. The simulator behaves like a CW-Lite with an unprotected AES target, so capture, CPA and glitching all work. See [Simulator](Simulator).

### Can two people use it at the same time?

Several browser windows can open the same Studio and see the same live session, which is handy for demonstrations. They share one scope and one set of traces, so coordinate who starts captures. Only one hardware job (capture or glitch sweep) runs at a time.

### Can I use it from another computer?

Yes: start it with `--host 0.0.0.0` on the machine with the hardware and open `http://that-machine:8765/`. Read the security notes first. See [Command Line and Configuration](Command-Line-and-Configuration).

### Is it safe to expose Studio to the network?

Only on a network you trust. The API has no authentication, and anyone who can reach it can control your hardware and run Python code on the Studio machine through notebooks. Use an SSH tunnel or an authenticating reverse proxy for anything else.

### Can I script it or use it from CI?

Yes. Every feature is available through the [HTTP API](HTTP-API), and AI agents can use the [MCP Server](MCP-Server). Studio can run headless with `--no-browser`.

### Can AI assistants control it?

Yes, through the MCP server (`cw-studio mcp`), which offers 68 tools covering every feature. The agent can share your browser session so you watch what it does. See [MCP Server](MCP-Server).

### Why are the downloads so large?

The standalone bundles contain a complete Python runtime, numpy, matplotlib, the `chipwhisperer` library (with its FPGA bitstreams) and libusb, so they run without anything installed. Compilers are not included; they are downloaded only when you need them.

### Why does clang fail for some platforms?

A few HALs in ChipWhisperer's firmware tree use GCC-only constructs (for example naked functions containing C code). Use GCC for those platforms. See [Firmware Builds](Firmware-Builds).

### Does it run on Intel Macs?

The macOS bundle is built for Apple Silicon. On Intel Macs, install the pip package in Python 3.10 to 3.12.

### What license is it under?

Apache License 2.0, the same as ChipWhisperer. Downloaded compilers and bundled third-party components keep their own licences (see the NOTICE file).

### How do I report a problem or contribute?

Open an issue on [GitHub](https://github.com/keyuraghao/chipwhisperer-studio/issues) with your version, operating system and the log. Pull requests are welcome; see [Development and Releases](Development-and-Releases).
