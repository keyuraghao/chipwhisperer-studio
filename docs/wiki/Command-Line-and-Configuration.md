# Command Line and Configuration

This page lists every command line option of ChipWhisperer Studio, the environment variables it reads, where Studio keeps its files, and how to run it on a lab machine for remote use.

## Starting Studio

| How you installed it | Command |
|----------------------|---------|
| pip | `cw-studio` (or `python -m cwstudio`) |
| Standalone bundle, Windows | `ChipWhispererStudio.exe` |
| Standalone bundle, macOS and Linux | `./ChipWhispererStudio` or the `chipwhisperer-studio.sh` launcher next to it |

All forms accept the same options. Studio starts a local web server and opens your browser at the printed URL, `http://127.0.0.1:8765/` by default.

## `cw-studio` options

| Option | Default | What it does |
|--------|---------|--------------|
| `--host ADDRESS` | `127.0.0.1` | Address the server listens on. `0.0.0.0` makes Studio reachable from other machines on the network (see Remote use below). |
| `--port N` | `8765` | Preferred port. If it is taken, Studio tries the next 49 ports and uses the first free one, then prints the actual URL. |
| `--no-browser` | off | Do not open a browser window. Useful for remote use and scripting. |
| `--window` | off | Open Studio in a native desktop window instead of the browser. Needs pywebview (`pip install "chipwhisperer-studio[window]"`); if it is missing Studio falls back to the browser. |
| `--simulate` | off | Preselect the built-in simulator on the Connect tab (the URL gets `?simulate=1`), and make `cw.scope()` in notebooks connect to the simulator. See [Simulator](Simulator). |
| `--data-dir DIR` | `~/ChipWhispererStudio` | Folder for exports, firmware, compilers, notebooks and notes. |
| `--log-level LEVEL` | `info` | `debug`, `info`, `warning` or `error` for the console log. |

Examples:

```bash
cw-studio --simulate                       # explore without hardware
cw-studio --no-browser --port 9000         # headless on another port
cw-studio --host 0.0.0.0 --no-browser      # lab machine, open from other computers
cw-studio --data-dir D:\studio-data        # keep everything on another drive (Windows)
```

## `cw-studio mcp` options

`cw-studio mcp` (or `ChipWhispererStudio mcp` for the bundle) runs the Model Context Protocol server for AI agents. Its options are documented in full on [MCP Server](MCP-Server): `--url`, `--no-embed`, `--simulate`, `--data-dir`, `--port`, `--transport` (stdio, streamable-http, sse), `--mcp-host`, `--mcp-port` and `--log-level` (default `warning`).

## Linux applications menu

On Linux, `cw-studio --install-desktop` (or `./ChipWhispererStudio --install-desktop` for the bundle) adds ChipWhisperer Studio with its icon to your desktop's applications menu, by writing `~/.local/share/applications/chipwhisperer-studio.desktop` and the icon. `--remove-desktop` takes the entry out again. On Windows and macOS these options only print a short note.

## Internal modes

`--ccwrap` turns the executable into a compiler wrapper. Firmware builds that use clang run `ChipWhispererStudio --ccwrap ...` (standalone bundle) or `python -m cwstudio.ccwrap` (pip install) as the C compiler; you never need to run it yourself. See [Firmware Builds](Firmware-Builds#gcc-and-clang-builds) and [Architecture](Architecture).

## Environment variables

| Variable | Used for |
|----------|----------|
| `GITHUB_TOKEN` or `GH_TOKEN` | Optional. Sent with GitHub API lookups for firmware sources and tutorials, which lifts GitHub's limit of 60 anonymous requests per hour. A token with no scopes is enough. |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | Optional. If set, Studio verifies HTTPS certificates against this PEM bundle or folder instead of the operating system's trust store. Use it on networks that inspect HTTPS when you cannot install their root certificate in the system store. |
| `CWSTUDIO_URL` | Default `--url` for `cw-studio mcp`. |
| `MPLBACKEND` | If it is not set, Studio sets it to its own inline backend so notebook figures render to images (shell commands run from a notebook get `Agg`). |
| `CWSTUDIO_CC`, `CWSTUDIO_GCC` | Internal. Set by clang firmware builds for the compiler wrapper. |
| `ZIG_GLOBAL_CACHE_DIR`, `ZIG_LOCAL_CACHE_DIR` | Internal. Clang builds point these at `toolchains/.zig-cache` in the data folder. |

HTTPS downloads use the operating system's certificate store by default (through the `truststore` package), fall back to Mozilla's bundle from `certifi`, and use `SSL_CERT_FILE` / `SSL_CERT_DIR` when set.

## The data folder

Everything Studio stores lives in one folder (`~/ChipWhispererStudio` unless you pass `--data-dir`). You can back it up, move it or delete parts of it; Studio recreates what it needs.

```
ChipWhispererStudio/
├── exports/               traces saved with "Download in browser" (and relative export paths)
├── imports/               trace files uploaded through the browser
├── firmware/
│   ├── chipwhisperer/     downloaded ChipWhisperer firmware/mcu sources (plus hal/chipwhisperer-fw-extra)
│   ├── builds/            finished firmware images (.hex and .elf) named project-platform-compiler
│   ├── uploads/           firmware files uploaded in the Target tab
│   └── settings.json      your source channel and custom firmware folder
├── toolchains/
│   ├── arm-gcc/15.2.1-1.1/  one folder per installed toolchain and version
│   ├── custom.json        custom toolchain entries
│   ├── registry.json      a newer toolchain list fetched with "Refresh list" (if any)
│   └── .zig-cache/        clang build cache
├── notebooks/
│   ├── *.ipynb            your notebooks (sub-folders allowed)
│   ├── imported/          notebooks imported through the browser
│   ├── chipwhisperer-jupyter/  NewAE's tutorial notebooks, when downloaded
│   └── firmware/mcu       link to firmware/chipwhisperer so tutorial build cells work
└── notes/                 your notes as .md or .txt files
```

Relative paths you type in Studio (trace export names, glitch CSV names) are saved inside the data folder. Absolute paths are used as given.

Disk usage: the ChipWhisperer firmware sources are about 250 MB. Installed compilers are the biggest items: Arm GCC about 1.1 GB, RISC-V GCC about 1.6 GB, AVR GCC about 215 MB and clang (Zig) about 400 MB unpacked. Remove the ones you do not need in the Firmware tab (see [Toolchains](Toolchains)).

## Remote use

Studio is a web application, so the hardware can live on one machine while you work from another:

1. On the machine with the ChipWhisperer attached, run `cw-studio --host 0.0.0.0 --no-browser` (or the bundle with the same options).
2. From another computer on the same network, open `http://LAB-MACHINE:8765/`.
3. For agents on another machine, run `cw-studio mcp --url http://LAB-MACHINE:8765` there.

Files you choose with the browser's file pickers are uploaded to the lab machine. Paths you type (for example "path on the machine running Studio") refer to the lab machine's file system.

## Security

- The HTTP API and WebSocket have **no authentication**. Anyone who can reach the port can program your target, change scope settings and run Python code through notebooks with your user's rights.
- Keep the default `--host 127.0.0.1` on laptops and shared networks. Use `0.0.0.0` only on a trusted lab network, or put Studio behind an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 lab-machine`) or a reverse proxy with authentication.
- Notebook HTML outputs are shown in a script-free sandbox and Markdown is sanitised, so opening an untrusted `.ipynb` does not run its saved HTML or JavaScript. Running its code cells does run that code, exactly as in Jupyter.

See also: [Installation](Installation), [Troubleshooting](Troubleshooting).
