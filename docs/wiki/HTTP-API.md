# HTTP API

Everything the Studio window does goes through a local HTTP API and one WebSocket, so you can drive Studio from scripts, CI jobs or other programs without the browser. This page describes the conventions, the binary trace format, every WebSocket event, every endpoint, and worked examples.

## Basics

- **Base URL:** `http://127.0.0.1:8765` by default (the host and port you started Studio with; see [Command Line and Configuration](Command-Line-and-Configuration)).
- **Interactive documentation:** open `http://127.0.0.1:8765/api/docs` for a browsable Swagger UI generated from the running server. You can try every call there.
- **Requests:** `POST` and `PUT` bodies are JSON (`Content-Type: application/json`). Uploads use `multipart/form-data`.
- **Responses:** JSON, except trace data, which uses the binary frame format below, and file downloads.
- **Errors:** a non-2xx status with `{"detail": "ExceptionType: message"}`. Most failures (wrong parameter, no scope connected, build failed to start) return status 400.
- **Long jobs:** captures, glitch sweeps, CPA, builds and downloads start in the background and return immediately. Poll the matching status endpoint or listen on the WebSocket.
- **One hardware job at a time:** a capture and a glitch sweep cannot run together. Starting a second one returns an error.

> **Note:** The API has no authentication. Anyone who can reach the port can control your hardware and run notebook code. Keep the default bind address `127.0.0.1` unless you are on a trusted network.

## Binary trace frames

Trace data (`GET /api/traces/{index}`, `/api/traces/stats`, `/api/traces/block`, `/api/analysis/cpa/corr/{b}` and `trace` WebSocket messages) uses one compact format:

| Bytes | Content |
|-------|---------|
| 0 to 3 | Header length `N` as an unsigned 32-bit little-endian integer |
| 4 to 4+N | JSON header, UTF-8 (for example `{"type":"trace","index":12,"n":5000,"dtype":"f32","textin":"...","key":"..."}`) |
| 4+N to end | Samples as little-endian 32-bit floats |

For `/api/traces/stats` the samples are the fields listed in `header.fields` (mean, std, min, max) one after another, each `header.samples` long. For `/api/traces/block` they are the requested traces one after another.

```python
import json, struct
import numpy as np
import requests

def decode(frame: bytes):
    (n,) = struct.unpack_from("<I", frame, 0)
    header = json.loads(frame[4:4 + n])
    samples = np.frombuffer(frame[4 + n:], dtype="<f4")
    return header, samples

header, wave = decode(requests.get("http://127.0.0.1:8765/api/traces/0").content)
print(header["textin"], wave.shape)
```

## WebSocket events

Connect to `ws://127.0.0.1:8765/ws`. Studio sends a `hello` message with the version and full status, then one message per event. Text frames are JSON with a `type` field (plus `seq` and `ts`); binary frames use the trace frame format with `type` in the header. Messages you send are ignored.

| Type | Frame | When and what |
|------|-------|---------------|
| `hello` | text | On connect: `version` and `status`. |
| `status` | text | Every 2 seconds and after changes: the same object as `GET /api/status`. |
| `log` | text | A log record: `level`, `logger`, `msg`. |
| `capture` | text | Capture progress: `state` (running, done, stopped, error), `done`, `target`, `timeouts`, `rate`, `stored`, `mode`, `elapsed`. |
| `trace` | binary | A captured or displayed trace (rate limited to about 25 per second): `index`, `stored`, plaintext, ciphertext and key when known, `source` (`notebook` for notebook traces). |
| `traces` | text | Trace store summary after a clear or import: `count`, `samples`, `memory_bytes`. |
| `serial` | text | Serial traffic: `t`, `dir` (tx or rx), `data`, `hex`. |
| `setting` | text | A setting changed: `target` (scope or target), `path`, `value`. |
| `cpa` | text | CPA progress or result (same object as `GET /api/analysis/cpa`). |
| `glitch` | text | Sweep progress: `state`, `point`, `points`, `repeats`, `counts`, `parameters`. |
| `glitch_result` | text | One sweep point: `i`, `rep`, `values`, `result`, `response`, `t`. |
| `toolchain` | text | Toolchain status changed (download progress, installed, error). |
| `firmware_sources` | text | Firmware source status changed (download progress, installed, update check). |
| `build` | text | Firmware build state: `state` (running, ok, failed, cancelled), `hex`, `size`, `error`. |
| `build_log` | text | New build output: `start` (line number) and `lines`. |
| `nb` | text | Notebook kernel: `kind` (queued, running, output, done, cancelled, restarted), `cell`, `exec`, and `output`, or `ok`, `execution_count` and `outputs` when done. |
| `tutorials` | text | Tutorial notebook download status. |

## Endpoints

### Status and system

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/status` | Scope and target info, running job, trace summary, CPA summary, uptime, data folder. |
| `GET /api/meta` | Version, scope kinds, target kinds, programmers, CPA models, crypto targets, SimpleSerial versions, host platform, platform help (drivers, udev). |
| `GET /api/devices` | Attached ChipWhisperer USB devices. |
| `GET /api/logs?since=SEQ` | Log, capture and glitch events after a sequence number. |
| `POST /api/shutdown` | Stops the Studio server. |

### Scope

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/scope/connect` | Body: `kind` (auto, lite, pro, nano, husky, huskyplus, sim), `sn`, `force`, `default_setup`. |
| `POST /api/scope/disconnect` | Disconnect the scope. |
| `GET /api/scope/settings` | Settings tree (groups with `children`, leaves with `path`, `value`, `type`, `writable`, `doc`, `choices`). |
| `PUT /api/scope/settings` | Body: `path`, `value`. Returns the read-back value. |
| `POST /api/scope/action/{action}` | `default_setup`, `arm_capture`, `reset_fpga` or `glitch_disable`. |

### Target

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/target/connect` | Body: `kind` (SimpleSerial2, SimpleSerial, SimpleSerial2_CDC, CW305, sim) plus extra options for `chipwhisperer.target()`. |
| `POST /api/target/disconnect` | Disconnect the target interface. |
| `GET /api/target/settings` / `PUT /api/target/settings` | Target settings tree, set one setting. |
| `POST /api/target/program` | Body: `programmer`, `path` (a file on the Studio machine). |
| `POST /api/target/program/upload?programmer=STM32F` | Multipart `file`: upload and program a firmware file. |
| `POST /api/target/serial/write` | Body: `data`, `hex`, `newline`. |
| `GET /api/target/serial?since=T` | Serial traffic after timestamp `T`. |
| `POST /api/target/simpleserial` | Body: `cmd`, `data` (hex), `read_cmd`, `read_len`. Returns `response`. |

### Capture and traces

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/capture/start` | Body: `count` (0 = continuous), `mode` (simpleserial, trigger_only), `key_mode` (fixed, random), `text_mode` (random, fixed, counter), `key`, `text`, `length`, `seed`, `store`, `clear`, `ack`, `max_timeouts`, `display_rate`, `max_rate`. |
| `POST /api/capture/single` | Same body, captures one trace. |
| `POST /api/capture/stop` | Stop the running capture or glitch sweep. |
| `GET /api/traces` | Trace store summary. |
| `DELETE /api/traces` | Clear all stored traces. |
| `GET /api/traces/{index}` | One trace (binary frame). |
| `GET /api/traces/{index}/meta` | One trace's plaintext, ciphertext, key, length, min and max as JSON. |
| `GET /api/traces/stats?start=&end=` | Per-sample mean, std, min, max (binary frame). |
| `GET /api/traces/block?start=&end=&step=` | Several traces in one binary frame. |
| `POST /api/traces/export` | Body: `path`, `format` (npz, npy, csv, cwp). Relative paths go into the data folder. |
| `GET /api/traces/download/{fmt}` | Export and download in one step. |
| `POST /api/traces/import` | Body: `path` (.npz or .cwp on the Studio machine), `replace`. |
| `POST /api/traces/import/upload?replace=true` | Multipart `file`: upload and import. |

### Analysis and glitching

| Method and path | Purpose |
|-----------------|---------|
| `POST /api/analysis/cpa/start` | Body: `model`, `trace_start`, `trace_end`, `point_range` ([start, end]), `bytes`, `known_key`, `use_stored_key`, `report_every`. |
| `POST /api/analysis/cpa/stop` | Stop the attack. |
| `GET /api/analysis/cpa` | Latest result: `best_key`, `bytes` (ranking per byte), `history`, `done`, `traces_used`, `known_key`. |
| `GET /api/analysis/cpa/corr/{b}` | Correlation trace for key byte `b` (binary frame). |
| `POST /api/glitch/start` | Body: `parameters` ([{path, start, stop, step}] or [{path, values}]), `command`, `data`, `expected`, `output_len`, `repeats`, `order`, `reset`, `reset_on`, `reset_delay`, `glitch_timeout`, `arm_scope`. |
| `GET /api/glitch/results` | Results, counts and progress. |
| `POST /api/glitch/export` | Body: `path` (CSV). |

### Toolchains and firmware

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/toolchains` | Host, registry revision and every toolchain's status. |
| `POST /api/toolchains/{id}/install?force=false` | Start a download (no-op if installed unless `force=true`). |
| `POST /api/toolchains/{id}/cancel` | Cancel a download. |
| `DELETE /api/toolchains/{id}` | Remove an installed toolchain. |
| `POST /api/toolchains/custom` | Body: `name`, `compiler`, `arch`, `prefix`, `url` and `sha256`, or `path`, `version`, `bin`. |
| `DELETE /api/toolchains/custom/{id}` | Remove a custom toolchain. |
| `POST /api/toolchains/refresh` | Fetch the latest registry from the Studio repository. |
| `GET /api/firmware` | Sources status, projects, platforms, crypto targets, SimpleSerial versions. |
| `PUT /api/firmware/channel` | Body: `channel`. |
| `POST /api/firmware/sources/check` | Compare installed sources with GitHub. |
| `POST /api/firmware/sources/fetch` | Body: `ref` (optional). Download or update sources. |
| `POST /api/firmware/sources/cancel` | Cancel the download. |
| `PUT /api/firmware/sources` | Body: `root` (your own firmware folder, or null for the downloaded sources). |
| `POST /api/firmware/plan` | Dry run of a build (same body as build). |
| `POST /api/firmware/build` | Body: `project`, `platform`, `compiler` (gcc, clang), `crypto_target`, `ss_ver`, `cflags`, `make_args`, `clean`, `jobs`. |
| `GET /api/firmware/build` | Build status. |
| `GET /api/firmware/build/log?since=N` | Build output lines. |
| `POST /api/firmware/build/cancel` | Cancel the build. |
| `GET /api/firmware/builds` | Built images. |
| `GET /api/firmware/builds/{name}` | Download a built `.hex`. |
| `POST /api/firmware/program` | Body: `path`, `programmer` (both optional: defaults to the last build and its platform's programmer). |

### Notebooks and kernel

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/notebooks` | Notebook list and tutorial status. |
| `GET /api/notebooks/file?path=` | Load a notebook (cells with string sources and ids). |
| `PUT /api/notebooks/file` | Body: `path`, `notebook`. Save. |
| `DELETE /api/notebooks/file?path=` | Delete. |
| `POST /api/notebooks/new` | Body: `name`. Create from the starter template. |
| `POST /api/notebooks/import` | Multipart `file`: import an `.ipynb` into `imported/`. |
| `GET /api/notebooks/download?path=` | Download as `.ipynb`. |
| `GET /api/notebooks/asset?path=` | An image referenced by notebook Markdown. |
| `POST /api/notebooks/tutorials/fetch` | Download NewAE's tutorial notebooks. |
| `POST /api/notebooks/run` | Body: `path`, `timeout`, `stop_on_error`. Run all cells, save outputs, wait. |
| `GET /api/kernel` | Kernel status: `busy`, `cell`, `queued`, `execution_count`. |
| `GET /api/kernel/variables` | Defined variables. |
| `POST /api/kernel/execute` | Body: `cells` ([{id, code}]), `path`. Queue cells; results arrive as `nb` events. |
| `POST /api/kernel/run` | Body: `code`, `path`, `timeout`. Run one cell and wait for its outputs. |
| `POST /api/kernel/interrupt` | Interrupt the running cell and drop queued cells. |
| `POST /api/kernel/restart` | Clear the namespace. |

### Notes and calculator

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/notes` / `POST /api/notes` | List notes, create one (body: `name`). |
| `GET /api/notes/{name}` | Read a note. |
| `PUT /api/notes/{name}` | Body: `text`, optional `rename`. |
| `DELETE /api/notes/{name}` | Delete a note. |
| `POST /api/calc` | Body: `expr`. Returns `value`, `text`, and `hex`, `bin`, `dec` for integers. |
| `GET /api/calc/variables` | Calculator variables. |
| `POST /api/calc/stats` | Body: `values`, or `source: "trace"` with `index`, `start`, `end`, or `source: "sample"` with `sample`, `sample_end`, `trace_start`, `trace_end`. |

## Examples

### curl: simulator capture and CPA

```bash
B=http://127.0.0.1:8765
curl -s -X POST $B/api/scope/connect -H 'Content-Type: application/json' -d '{"kind":"sim"}'
curl -s -X POST $B/api/target/connect -H 'Content-Type: application/json' -d '{"kind":"sim"}'
curl -s -X POST $B/api/capture/start -H 'Content-Type: application/json' -d '{"count":100,"clear":true,"key":"2b7e151628aed2a6abf7158809cf4f3c"}'
sleep 3
curl -s $B/api/traces
curl -s -o traces.npz $B/api/traces/download/npz
curl -s -X POST $B/api/analysis/cpa/start -H 'Content-Type: application/json' -d '{"model":"sbox_hw"}'
sleep 2
curl -s $B/api/analysis/cpa | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['best_key'], r['done'])"
```

### Python: capture, wait and recover the key

```python
import time
import requests

B = "http://127.0.0.1:8765"
s = requests.Session()

def wait_idle():
    while True:
        job = s.get(f"{B}/api/status").json().get("job") or {}
        if not job.get("running"):
            return
        time.sleep(0.2)

s.post(f"{B}/api/scope/connect", json={"kind": "sim"}).raise_for_status()
s.post(f"{B}/api/target/connect", json={"kind": "sim"}).raise_for_status()
s.post(f"{B}/api/capture/start", json={"count": 100, "clear": True, "key": "2b7e151628aed2a6abf7158809cf4f3c"}).raise_for_status()
wait_idle()

s.post(f"{B}/api/analysis/cpa/start", json={"model": "sbox_hw"}).raise_for_status()
while not (r := s.get(f"{B}/api/analysis/cpa").json()).get("done"):
    time.sleep(0.2)
print("key:", r["best_key"])
```

### Python: build firmware and program it

```python
r = s.post(f"{B}/api/firmware/build", json={"project": "simpleserial-aes", "platform": "CWLITEARM", "compiler": "gcc"}).json()
while r["state"] == "running":
    time.sleep(0.5)
    r = s.get(f"{B}/api/firmware/build").json()
print(r["state"], r.get("hex"), r.get("size"))
if r["state"] == "ok":
    print(s.post(f"{B}/api/firmware/program", json={}).json())
```

The compiler and firmware sources must be installed first (see [Toolchains](Toolchains) and [Firmware Sources](Firmware-Sources)), otherwise the build call returns an error that says what is missing.

### Python: run a notebook cell

```python
r = s.post(f"{B}/api/kernel/run", json={"code": "import chipwhisperer as cw\nscope = cw.scope()\nlen(studio.traces)"}).json()
print(r["ok"], [o.get("data", {}).get("text/plain") or o.get("text") for o in r["outputs"]])
```

### Python: follow live events

```python
import asyncio, json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        async for msg in ws:
            if isinstance(msg, bytes):
                continue  # binary trace frame, decode with decode() above
            ev = json.loads(msg)
            if ev["type"] in ("capture", "log"):
                print(ev)

asyncio.run(main())
```

See also: [MCP Server](MCP-Server), [Command Line and Configuration](Command-Line-and-Configuration).
