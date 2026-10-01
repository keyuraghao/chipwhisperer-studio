# Performance

Stress tests and benchmarks of Studio with very large trace sets, large trace files, long sessions, many open windows and AI agents, plus memory leak checks. They are produced by `tools/benchmark.py` (see [running them yourself](#running-them-yourself)); every run is kept with its date, version and machine, so results can be compared over time.

## Latest results (2026-10-01, Studio 0.4.4)

| Test | Result |
|---|---|
| Largest trace set held (200,000 x 5,000 samples) | 3.73 GB of traces in 3.79 GB of memory |
| Mean/min/max of all 200,000 traces | 2.6 s the first time, extra memory +65 MB |
| API under load (50,000 traces, 8 clients, capture running) | status 1.5 ms, one trace 1.5 ms, trace block (200) 31.6 ms, mean/min/max 5.9 ms, settings tree 3.9 ms (medians) |
| Simulated capture, 5,000 samples per trace | 5,685 traces/s (108 MB/s) stored |
| CPA on 100,000 x 5,000 traces | 27 s, key recovered: yes |
| Export 100,000 traces (1.9 GB) to .npz | 35 s (54 MB/s), import 9 s |
| Memory leak tests | 7 of 7 workloads without growth |
| Browser during a long live capture | 5,000 samples: 60 fps, heap +0.0 MB; 100,000 samples: 24 fps, heap -0.1 MB; 131,070 samples: 20 fps, heap -0.1 MB |

## Changes since the previous run

Same machine and workload; before is Studio 0.4.3 (`7a3473a`, measured 2026-10-01), after is Studio 0.4.4 (`1f274ab`, measured 2026-10-01).

| Metric | Size | Before | After | Change |
|---|---|---|---|---|
| Mean/min/max of all traces: time | 10,000 x 5,000 | 0.14 s | 0.16 s | 0.9x worse |
| Mean/min/max of all traces: extra memory | 10,000 x 5,000 | 381.85 MB | 63.93 MB | 6.0x better |
| Mean/min/max of all traces: time | 100,000 x 5,000 | 1.25 s | 1.44 s | 0.9x worse |
| Mean/min/max of all traces: extra memory | 100,000 x 5,000 | 3,827.97 MB | 63.45 MB | 60.3x better |
| Mean/min/max of all traces: time | 200,000 x 5,000 | 2.47 s | 2.56 s | 1.0x same |
| Mean/min/max of all traces: extra memory | 200,000 x 5,000 | 7,652.34 MB | 64.98 MB | 117.8x better |
| Mean/min/max of all traces: time | 10,000 x 100,000 | 2.23 s | 2.49 s | 0.9x worse |
| Mean/min/max of all traces: extra memory | 10,000 x 100,000 | 7,631.55 MB | 63.86 MB | 119.5x better |
| Mean/min/max of all traces: time | 1,000 x 1,000,000 | 2.28 s | 4.65 s | 0.5x worse |
| Mean/min/max of all traces: extra memory | 1,000 x 1,000,000 | 7,644.75 MB | 94.92 MB | 80.5x better |
| Export to .npz: speed | 10,000 x 5,000 | 5.59 MB/s | 54.09 MB/s | 9.7x better |
| Export to .npz: file size | 10,000 x 5,000 | 73.66 MB | 83.43 MB | 0.9x worse |
| Export to .npz: speed | 100,000 x 5,000 | 5.50 MB/s | 54.28 MB/s | 9.9x better |
| Export to .npz: file size | 100,000 x 5,000 | 736.57 MB | 834.27 MB | 0.9x worse |
| API under load, status: median | 50,000 traces | 2.69 ms | 1.48 ms | 1.8x better |
| API under load, one trace: median | 50,000 traces | 2.18 ms | 1.49 ms | 1.5x better |
| API under load, trace block (200): median | 50,000 traces | 83.16 ms | 31.60 ms | 2.6x better |
| API under load, mean/min/max: median | 50,000 traces | 6,315.93 ms | 5.85 ms | 1,079.0x better |
| API under load, settings tree: median | 50,000 traces | 12.64 ms | 3.91 ms | 3.2x better |
| Memory after: continuous capture, 2 live windows | 2,026,066 traces | 60.91 MB | 60.84 MB | 1.0x same |
| Memory after: browser windows opening and closing during a capture | 2,000 WebSocket connections | 61.91 MB | 61.88 MB | 1.0x same |
| Memory after: API requests of 7 kinds | 30,000 requests | 69.76 MB | 75.50 MB | 0.9x worse |
| Memory after: capture 5000 traces then clear | 30 cycles | 157.50 MB | 61.43 MB | 2.6x better |
| Memory after: CPA on 2000 traces | 30 attacks | 208.32 MB | 201.09 MB | 1.0x same |
| Memory after: notebook cells with a capture and a figure | 300 cells | 444.92 MB | 294.79 MB | 1.5x better |
| Memory after: MCP tool calls over stdio (5 kinds) | 5,000 tool calls | 40.86 MB | 39.11 MB | 1.0x same |

## Run history

| Date | Studio | Commit | Machine | Highlights | Data |
|---|---|---|---|---|---|
| 2026-10-01 | 0.4.4 | `1f274ab` | AMD Ryzen 9 5900HS with Radeon Graphics, 39 GB | Largest trace set held (200,000 x 5,000 samples): 3.73 GB of traces in 3.79 GB of memory; Mean/min/max of all 200,000 traces: 2.6 s the first time, extra memory +65 MB; API under load (50,000 traces, 8 clients, capture running): status 1.5 ms, one trace 1.5 ms, trace block (200) 31.6 ms, mean/min/max 5.9 ms, settings tree 3.9 ms (medians) | [2026-10-01-v0.4.4.json](https://github.com/keyuraghao/chipwhisperer-studio/blob/main/docs/benchmarks/2026-10-01-v0.4.4.json) |
| 2026-10-01 | 0.4.3 | `7a3473a` | AMD Ryzen 9 5900HS with Radeon Graphics, 39 GB | Largest trace set held (200,000 x 5,000 samples): 3.73 GB of traces in 3.79 GB of memory; Mean/min/max of all 200,000 traces: 2.5 s the first time, extra memory +7.47 GB; API under load (50,000 traces, 8 clients, capture running): status 2.7 ms, one trace 2.2 ms, trace block (200) 83.2 ms, mean/min/max 6315.9 ms, settings tree 12.6 ms (medians) | [2026-10-01-v0.4.3.json](https://github.com/keyuraghao/chipwhisperer-studio/blob/main/docs/benchmarks/2026-10-01-v0.4.3.json) |

## Detailed results

Measured on **2026-10-01** with Studio **0.4.4** (commit `1f274ab`) on AMD Ryzen 9 5900HS with Radeon Graphics (16 threads), 39 GB RAM, Linux 7.1.5+kali-amd64, Python 3.12.12, numpy 2.5.3. All captures use the built-in simulator; with real hardware, capture speed is set by the scope and the target, not by Studio.

### Large trace sets in memory

| Traces x samples | Data | Memory used | Overhead per trace | Append rate | Mean/min/max (time, extra memory) | Trace matrix for CPA (time, extra memory) | One trace to the UI |
|---|---|---|---|---|---|---|---|
| 10,000 x 5,000 | 191 MB | 198 MB | 810 B | 72,856 traces/s | 0.16 s, +64 MB | 0.04 s, +192 MB | 0.01 ms |
| 100,000 x 5,000 | 1.86 GB | 1.90 GB | 368 B | 73,959 traces/s | 1.44 s, +63 MB | 0.46 s, +1.89 GB | 0.01 ms |
| 200,000 x 5,000 | 3.73 GB | 3.79 GB | 329 B | 75,452 traces/s | 2.56 s, +65 MB | 0.87 s, +3.79 GB | 0.01 ms |
| 10,000 x 100,000 | 3.73 GB | 3.73 GB | 719 B | 4,489 traces/s | 2.49 s, +64 MB | 0.65 s, +3.73 GB | 0.05 ms |
| 1,000 x 1,000,000 | 3.73 GB | 3.73 GB | 5,675 B | 686 traces/s | 4.65 s, +95 MB | 0.61 s, +3.73 GB | 0.31 ms |

### Large trace files

| Traces x samples | Format | Data | File size | Export (speed, extra memory) | Import (speed, extra memory) |
|---|---|---|---|---|---|
| 10,000 x 5,000 | .npz | 191 MB | 83 MB | 3.5 s (54 MB/s), +223 MB | 0.9 s (210 MB/s), +192 MB |
| 10,000 x 5,000 | .npy | 191 MB | 191 MB | 0.1 s (1,863 MB/s), +193 MB | 0.0 s (3,978 MB/s), +191 MB |
| 100,000 x 5,000 | .npz | 1.86 GB | 834 MB | 35.1 s (54 MB/s), +1.91 GB | 9.1 s (210 MB/s), +1.90 GB |
| 100,000 x 5,000 | .npy | 1.86 GB | 1.87 GB | 0.9 s (2,227 MB/s), +1.89 GB | 0.4 s (4,474 MB/s), +1.89 GB |

### CPA key recovery

| Traces x samples | Data | Progress every | Time | Throughput | Extra memory | Key recovered |
|---|---|---|---|---|---|---|
| 5,000 x 5,000 | 95 MB | 50 traces | 12.5 s | 400 traces/s | +300 MB | yes |
| 5,000 x 5,000 | 95 MB | 5,000 traces | 1.6 s | 3,072 traces/s | +515 MB | yes |
| 50,000 x 5,000 | 954 MB | 1,000 traces | 20.6 s | 2,425 traces/s | +1.22 GB | yes |
| 100,000 x 5,000 | 1.86 GB | 5,000 traces | 26.6 s | 3,755 traces/s | +2.48 GB | yes |
| 20,000 x 50,000 | 3.73 GB | 5,000 traces | 49.0 s | 408 traces/s | +9.14 GB | yes |

### Capture throughput (simulator, end to end)

| Samples per trace | Traces | Stored | Rate | Data rate | Extra memory |
|---|---|---|---|---|---|
| 5,000 | 20,000 | yes | 5,685 traces/s | 108 MB/s | +389 MB |
| 5,000 | 20,000 | no | 6,720 traces/s | 128 MB/s | +0.5 MB |
| 24,400 | 5,000 | yes | 2,125 traces/s | 198 MB/s | +468 MB |
| 24,400 | 5,000 | no | 2,513 traces/s | 234 MB/s | +0.9 MB |
| 100,000 | 1,000 | yes | 600 traces/s | 229 MB/s | +386 MB |
| 100,000 | 1,000 | no | 691 traces/s | 264 MB/s | +3.2 MB |
| 131,070 | 1,000 | yes | 479 traces/s | 239 MB/s | +505 MB |
| 131,070 | 1,000 | no | 524 traces/s | 262 MB/s | +4.0 MB |

### API response times under load

8 clients polling for 30 s while a continuous capture runs, with 50,000 stored traces of 5,000 samples.

| Endpoint | Requests | Median | 95th percentile | 99th percentile |
|---|---|---|---|---|
| status | 5,129 | 1.5 ms | 3.7 ms | 5.7 ms |
| one trace | 5,129 | 1.5 ms | 3.6 ms | 5.4 ms |
| trace block (200) | 5,130 | 31.6 ms | 39.7 ms | 42.3 ms |
| mean/min/max | 5,129 | 5.9 ms | 12.2 ms | 14.6 ms |
| settings tree | 5,130 | 3.9 ms | 7.9 ms | 10.8 ms |

### Many open windows

| Browser windows (WebSocket clients) | Capture rate | Frames each window received | Studio CPU |
|---|---|---|---|
| 0 | 5,472 traces/s | 0 | 95% |
| 1 | 5,465 traces/s | 35 | 99% |
| 5 | 5,445 traces/s | 36 | 110% |
| 20 | 5,294 traces/s | 37 | 145% |

### Memory leak tests

Studio's resident memory is sampled during each run; growth is measured after a warm-up quarter (allocators keep some memory after the first runs). A leak shows up as steady growth that scales with the number of operations.

| Workload | Operations | Memory at start | Memory at end | Growth after warm-up | Verdict |
|---|---|---|---|---|---|
| continuous capture, 2 live windows | 2,026,066 traces | 60 MB | 61 MB | +0.5 MB | no leak |
| browser windows opening and closing during a capture | 2,000 WebSocket connections (0 subscribers left) | 59 MB | 62 MB | +0.3 MB | no leak |
| API requests of 7 kinds | 30,000 requests | 69 MB | 76 MB | +0.0 MB | no leak |
| capture 5000 traces then clear | 30 cycles | 59 MB | 61 MB | +0.2 MB | no leak |
| CPA on 2000 traces | 30 attacks | 99 MB | 201 MB | +0.0 MB | no leak |
| notebook cells with a capture and a figure | 300 cells | 59 MB | 295 MB | -27.7 MB | no leak |
| MCP tool calls over stdio (5 kinds) | 5,000 tool calls | 38 MB | 39 MB | +0.1 MB | no leak |

### Browser UI during a long live capture

Overlay of 10 traces and the mean on; the JavaScript heap is measured after a forced garbage collection every 10 s.

| Samples per trace | Duration | Frame rate (median, lowest) | JS heap start, end | Heap growth after warm-up | Page errors |
|---|---|---|---|---|---|
| 5,000 | 4 min | 60 fps, 60 fps | 2.6 MB, 2.7 MB | +0.0 MB | 0 |
| 100,000 | 4 min | 24 fps, 23 fps | 3.8 MB, 2.9 MB | -0.1 MB | 0 |
| 131,070 | 4 min | 20 fps, 20 fps | 3.0 MB, 3.0 MB | -0.1 MB | 0 |

## What is tested

| Group | What it does | What it shows |
|---|---|---|
| Large trace sets | Fills the trace store with up to 200,000 traces of 5,000 samples, 10,000 of 100,000 and 1,000 of 1,000,000 (each about 3.7 GB), then asks for the mean/min/max, builds the trace matrix CPA uses and fetches traces for the UI. | Memory per trace, how much extra memory each operation needs at its peak, and how long it takes. |
| Large files | Exports 10,000 and 100,000 traces to `.npz` and `.npy` and imports them back. Trace values sit on a 10-bit ADC grid like a CW-Lite capture, so compression behaves as with real data. | Export and import speed, file size and memory. |
| CPA | Runs the CPA attack the UI runs on up to 100,000 traces of 5,000 samples and 20,000 of 50,000, with a known key. | Time, throughput, memory and that the key is recovered. |
| Capture | Captures with the simulator end to end (scope, target, AES, live events, store) at up to 131,070 samples per trace. | The rate Studio itself can sustain; real hardware is slower. |
| Server under load | A real Studio process with 50,000 stored traces and a continuous capture, polled by 8 clients for 30 s; then captures with 0 to 20 browser windows connected. | Response times (median, 95th and 99th percentile) and the cost of many windows. |
| Memory leaks | Long or repeated workloads against a real Studio process: 5 minutes of continuous capture, 2,000 window connections, 30,000 API requests, 30 capture and clear cycles, 30 CPA runs, 300 notebook cells with figures, 5,000 MCP tool calls. Memory is sampled throughout. | A leak shows up as memory that keeps growing with the number of operations after the warm-up. |
| Browser | Chromium shows a 4 minute live capture with 10 overlaid traces and the mean, at 5,000, 100,000 and 131,070 samples per trace. The JavaScript heap is measured after a forced garbage collection every 10 s. | Frame rate and whether the page leaks memory. |

## Running them yourself

```bash
pip install -e ".[test]" playwright websockets
playwright install chromium
python tools/benchmark.py --quick              # a few minutes
python tools/benchmark.py                      # the full set, about 45 minutes
python tools/benchmark.py --only leaks,server  # some groups
python tools/benchmark.py --publish build/bench/results.json  # add the run to this page and the README
```

The tests read memory from `/proc`, so they run on Linux. They need about 25 GB of free memory for the largest sets (use `--quick` otherwise) and put temporary trace files in `build/bench` (use a real disk, not a RAM disk). Capture numbers use the simulator, so they measure Studio rather than the hardware: with a real scope the capture rate is set by the scope, the USB link and the target.
