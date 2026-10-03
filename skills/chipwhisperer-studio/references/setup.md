# Setting up the ChipWhisperer Studio MCP server

## Install Studio

- pip (Python 3.10 to 3.12, because chipwhisperer 6.0.0 pins numpy<=1.26.4): `pip install chipwhisperer-studio`. This provides `cw-studio` (own window), `cw-studio-web` (browser) and `cw-studio mcp`.
- Standalone bundles (Linux x86_64, Windows x86_64, macOS arm64) from https://github.com/keyuraghao/chipwhisperer-studio/releases. Run the bundle executable with `mcp` as its first argument. In the Windows window build use `cw-studio.exe` next to `ChipWhispererStudio.exe` (the latter has no console, so it cannot speak stdio).
- Not sure which command this installation needs? `GET http://127.0.0.1:8765/api/meta` returns it as `mcp_command`, and Studio's Help tab shows copy-ready snippets.
- Real hardware on Linux needs NewAE's udev rules; on Windows, the ChipWhisperer USB driver. See the wiki page Installation.

## Register it with a client

Claude Code:

```bash
claude mcp add chipwhisperer-studio -- cw-studio mcp
claude mcp add chipwhisperer-studio -- cw-studio mcp --simulate   # no hardware
```

Claude Desktop, Cursor and other clients that read an `mcpServers` block:

```json
{
  "mcpServers": {
    "chipwhisperer-studio": { "command": "cw-studio", "args": ["mcp"] }
  }
}
```

Restart the client after changing its configuration.

Clients that connect to a URL instead of launching a process: run `cw-studio mcp --transport streamable-http --mcp-host 127.0.0.1 --mcp-port 8766` and point the client at `http://127.0.0.1:8766/mcp` (`--transport sse` serves `/sse`).

## How the server finds Studio

`cw-studio mcp` checks whether a Studio answers at `--url` (default `http://127.0.0.1:8765`, or the `CWSTUDIO_URL` environment variable):

1. If one answers, it attaches. The agent and the person at the Studio window share one session: same scope, same traces, live waveform.
2. If none answers, it starts a headless Studio inside the MCP process on `--port` (8765 or the next free port). Its UI URL is printed to stderr and returned by `open_ui` and `studio_status`.
3. With `--no-embed` it exits with an error instead.

Options: `--simulate` (embedded Studio preselects the simulator, and `cw.scope()` in notebooks connects to it), `--data-dir DIR` (default `~/ChipWhispererStudio`: toolchains, firmware, notebooks, notes, exports), `--port N`, `--transport stdio|streamable-http|sse`, `--mcp-host`, `--mcp-port`, `--log-level debug|info|warning|error` (logs go to stderr only).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Client says the server closed immediately | Run `cw-studio mcp` in a terminal to see the error on stderr; check the command path and PATH. |
| Agent sees different traces than the window | The window runs on another port. Pass `--url http://127.0.0.1:PORT`. |
| "Studio is not running" | `--no-embed` was given and nothing answers at `--url`. Start Studio or drop the flag. |
| Tools time out on long jobs | Use `wait=false` and poll `studio_status` / `job_wait`, or raise `timeout_s`. |
| Hardware not found | `list_devices`; on Linux check udev rules; `scope_connect(force=true)` if another program left the device open. |

## Without MCP

Every tool wraps Studio's HTTP API on `http://127.0.0.1:8765` (JSON, no authentication; WebSocket events on `/ws`). An agent with only a shell can call it directly, for example `curl -s localhost:8765/api/status` or `curl -s -X POST localhost:8765/api/scope/connect -H 'Content-Type: application/json' -d '{"kind":"sim"}'`. Endpoints are listed on the wiki page HTTP API. Prefer the MCP tools when they are available: they decimate large data and wait for jobs for you.
