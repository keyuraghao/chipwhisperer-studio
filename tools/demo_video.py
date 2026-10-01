#!/usr/bin/env python3
"""Record a captioned walkthrough video of every ChipWhisperer Studio feature.

Starts a Studio with the simulator, then drives the real UI with Playwright: connecting, scope settings, target I/O, capture, the waveform view (overlay, mean, cursors, zoom), CPA, a glitch sweep, a firmware build, notebooks, notes, the calculator, the MCP setup and both themes. A caption bar explains each step and a pointer shows where the mouse goes. The recording is converted to MP4 (H.264), and every chapter is also cut into a short looping animated WebP clip (``clips/<chapter>.webp``) that plays by itself in the README and the wiki, which cannot play MP4 files inline. A chapter list with timestamps is written next to them.

    pip install -e ".[test]" playwright imageio-ffmpeg && playwright install chromium
    python tools/demo_video.py --data-dir DIR [--out build/demo]

Use the same --data-dir as tools/screenshots.py: the firmware chapter needs the Arm GCC and clang toolchains and the firmware sources, which that script downloads.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screenshots import ROOT, Api, _free_port, _up  # noqa: E402

KEY = "2b7e151628aed2a6abf7158809cf4f3c"

# Caption bar, title cards and a visible pointer (Playwright's video does not draw the mouse).
OVERLAY_JS = r"""
(() => {
  const css = `
  #demo-cap{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:99999;max-width:78%;padding:12px 22px;border-radius:12px;
    background:rgba(12,18,28,.86);color:#fff;font:600 21px/1.35 Inter,system-ui,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.35);text-align:center;transition:opacity .25s;pointer-events:none}
  #demo-cap small{display:block;font-weight:400;font-size:16px;opacity:.85;margin-top:3px}
  #demo-card{position:fixed;inset:0;z-index:100000;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;
    background:linear-gradient(135deg,#0f1a24,#123a4a 55%,#1d5f7a);color:#fff;font-family:Inter,system-ui,sans-serif;transition:opacity .4s;pointer-events:none}
  #demo-card h1{font-size:54px;margin:0;font-weight:700} #demo-card p{font-size:24px;margin:0;opacity:.9;text-align:center;max-width:70%}
  #demo-ptr{position:fixed;z-index:100001;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;border:3px solid #ffb020;background:rgba(255,176,32,.25);pointer-events:none;transition:transform .12s}
  #demo-ptr.down{transform:scale(.7);background:rgba(255,176,32,.6)}`;
  // The script runs before the document exists: attach the style and pointer once there is a body.
  const st = document.createElement('style'); st.textContent = css;
  const ptr = document.createElement('div'); ptr.id = 'demo-ptr';
  const add = () => { if (!document.body) return; if (!st.isConnected) document.head.appendChild(st); if (!ptr.isConnected) document.body.appendChild(ptr); };
  document.addEventListener('DOMContentLoaded', add); add();
  addEventListener('mousemove', (e) => { ptr.style.left = e.clientX + 'px'; ptr.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => ptr.classList.add('down'), true);
  addEventListener('mouseup', () => ptr.classList.remove('down'), true);
  window.__demo = {
    caption(title, sub) {
      add();
      let c = document.getElementById('demo-cap');
      if (!c) { c = document.createElement('div'); c.id = 'demo-cap'; document.body.appendChild(c); }
      c.innerHTML = ''; c.append(title); if (sub) { const s = document.createElement('small'); s.textContent = sub; c.append(s); }
      c.style.opacity = title ? 1 : 0;
    },
    card(title, lines) {
      add();
      let c = document.getElementById('demo-card');
      if (!c) { c = document.createElement('div'); c.id = 'demo-card'; document.body.appendChild(c); }
      c.innerHTML = ''; const h = document.createElement('h1'); h.textContent = title; c.append(h);
      for (const l of lines || []) { const p = document.createElement('p'); p.textContent = l; c.append(p); }
      c.style.opacity = 1; c.style.display = 'flex';
    },
    hideCard() { const c = document.getElementById('demo-card'); if (c) { c.style.opacity = 0; setTimeout(() => { c.style.display = 'none'; }, 400); } },
  };
})();
"""


class Demo:
    def __init__(self, page, api: Api):
        self.page, self.api = page, api
        self.t0 = time.monotonic()
        self.chapters = []

    def at(self) -> float:
        return time.monotonic() - self.t0

    def chapter(self, name: str):
        self.chapters.append((self.at(), name))
        print(f"{self.at():6.1f}s  {name}", flush=True)

    def wait(self, s: float):
        self.page.wait_for_timeout(int(s * 1000))

    def caption(self, title: str, sub: str = "", hold: float = 0):
        self.page.evaluate("([t, s]) => window.__demo.caption(t, s)", [title, sub])
        if hold:
            self.wait(hold)

    def card(self, title: str, lines, hold: float):
        self.page.evaluate("([t, l]) => window.__demo.card(t, l)", [title, lines])
        self.wait(hold)
        self.page.evaluate("() => window.__demo.hideCard()")
        self.wait(0.5)

    def move(self, x: float, y: float, steps: int = 18):
        self.page.mouse.move(x, y, steps=steps)

    def point(self, locator, dx: float = 0.5, dy: float = 0.5):
        locator.scroll_into_view_if_needed()
        b = locator.bounding_box()
        self.move(b["x"] + b["width"] * dx, b["y"] + b["height"] * dy)
        self.wait(0.25)
        return b

    def click(self, locator, pause: float = 0.6, **kw):
        self.point(locator)
        locator.click(**kw)
        self.wait(pause)

    def tab(self, name: str):
        self.click(self.page.locator(f'[data-tab="{name}"]'), 0.9)

    def type(self, locator, text: str, delay: int = 45):
        self.click(locator, 0.2)
        locator.fill("")
        locator.type(text, delay=delay)

    def plot_box(self):
        return self.page.locator("#wave-plot").bounding_box()

    def plot_click(self, fx: float, shift: bool = False):
        b = self.plot_box()
        self.move(b["x"] + b["width"] * fx, b["y"] + b["height"] * 0.5)
        self.wait(0.2)
        if shift:
            self.page.keyboard.down("Shift")
        self.page.mouse.click(b["x"] + b["width"] * fx, b["y"] + b["height"] * 0.5)
        if shift:
            self.page.keyboard.up("Shift")
        self.wait(0.5)

    def idle(self, timeout: float = 120):
        self.api.wait("/api/status", lambda st: not (st.get("job") or {}).get("running"), timeout=timeout)


def prepare(api: Api):
    """State the video builds on: a tour notebook, lab notes and glitch settings. Hardware is connected on camera."""
    tour = [
        {"cell_type": "markdown", "source": "# Studio tour\n\nCells run **inside Studio**: `cw.scope()` and `cw.target()` use the devices connected in the Connect tab, and every trace captured here also appears in the **Capture** tab."},
        {"cell_type": "code", "source": "import chipwhisperer as cw\nimport numpy as np\n\nscope = cw.scope()\ntarget = cw.target(scope)\nkey = bytearray.fromhex('" + KEY + "')\nprint(type(scope).__name__, 'connected through Studio')"},
        {"cell_type": "code", "source": "from tqdm.notebook import trange\nfor i in trange(100, desc='Capturing'):\n    cw.capture_trace(scope, target, bytearray(np.random.bytes(16)), key)\nprint(len(studio.traces), 'traces stored in Studio')"},
        {"cell_type": "code", "source": "import matplotlib.pyplot as plt\nwaves = studio.traces.waves\nplt.figure(figsize=(10, 3))\nplt.plot(waves.mean(axis=0), lw=0.8)\nplt.title(f'Mean of {len(waves)} traces')\nplt.xlabel('sample'); plt.ylabel('power')\nplt.show()"},
    ]
    api("PUT", "/api/notebooks/file", {"path": "Studio tour.ipynb", "notebook": {"cells": tour}})
    api("PUT", "/api/notes/Lab%20notes.md", {"text": "# Lab notes\n\nTarget: CWLITEARM, simpleserial-aes\n\n- [x] capture 500 traces\n- [ ] glitch the password check next\n"})


def record(base: str, api: Api, out: str, width: int, height: int) -> Demo:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark", record_video_dir=out, record_video_size={"width": width, "height": height})
        ctx.add_init_script(OVERLAY_JS)
        page = ctx.new_page()
        d = Demo(page, api)  # timestamps start with the recording, so chapters line up with the video
        page.goto(base + "/")
        page.wait_for_timeout(1200)
        pg = page

        d.chapter("Introduction")
        d.card("ChipWhisperer Studio", ["A desktop app for NewAE ChipWhisperer hardware", "Live waveforms, capture, CPA, glitching, firmware builds, notebooks and an MCP server for AI agents", "This walkthrough uses the built-in simulator, so no hardware is needed"], 6)

        d.chapter("Connect")
        d.tab("connect")
        d.caption("Connect a scope and a target", "Nano, Lite, Pro and Husky are detected over USB; here we pick the built-in simulator", 1.5)
        sel = pg.locator("#panel-connect select").first
        d.point(sel)
        sel.select_option("sim")
        d.wait(0.6)
        d.click(pg.locator("#panel-connect button", has_text="Connect scope"), 1.5)
        d.point(pg.locator("#chip-target"))
        d.caption("With the simulator the target connects too; both show in the top bar", "With real hardware, pick the target and press Connect target", 2.5)

        d.chapter("Scope settings")
        d.tab("scope")
        d.caption("Every scope setting in one searchable tree", "Gain, ADC, clock, triggers and glitch settings, with live values and documentation", 0.5)
        for group in ("gain", "adc"):
            loc = pg.locator(".tree .ghead", has_text=group).first
            if loc.count():
                d.click(loc, 0.7)
        d.wait(1.5)
        search = pg.locator("#panel-scope input.flex").first
        d.type(search, "glitch")
        loc = pg.locator(".tree .ghead", has_text="glitch").first
        if loc.count():
            d.click(loc, 0.8)
        d.wait(2)
        search.fill("")

        d.chapter("Target I/O")
        d.tab("target")
        d.caption("Talk to the target", "Send a key and plaintexts with SimpleSerial, watch the serial console, program firmware", 0.8)
        pg.locator("#sidebar").evaluate("e => e.scrollTop = 99999")
        d.wait(0.5)
        d.click(pg.locator("#panel-target button", has_text="Send key"), 0.8)
        d.click(pg.locator("#panel-target .row:has-text('Command') button", has_text="Send").first, 1.2)
        d.wait(1.5)

        d.chapter("Capture")
        d.tab("capture")
        d.caption("Capture traces", "Set how many traces to take and press Run; each one streams into the waveform view", 0.5)
        count = pg.locator("#panel-capture input[type=number]").first
        d.type(count, "500")
        d.click(pg.locator("#btn-run"), 0.5)
        d.wait(3)
        d.idle()

        d.chapter("Waveform view")
        d.caption("Overlay the last traces and show the mean and min/max envelope", "", 0.5)
        sel = pg.locator("#wave-toolbar select").nth(1)
        d.point(sel)
        sel.select_option("10")
        d.wait(0.8)
        d.click(pg.locator("#wave-toolbar label", has_text="mean").locator("input"), 0.5)
        d.click(pg.locator("#wave-toolbar label", has_text="min/max").locator("input"), 0.5)
        d.click(pg.locator("#btn-run"), 0.5)
        d.wait(3)
        d.idle()
        sel.select_option("0")
        pg.locator("#wave-toolbar label", has_text="min/max").locator("input").uncheck()
        d.wait(0.5)

        d.chapter("Cursors and zoom")
        d.caption("Click to place cursor A, Shift+click for cursor B", "The footer shows the values at both cursors and the difference between them", 0.3)
        d.plot_click(0.30)
        d.plot_click(0.62, shift=True)
        d.wait(1.5)
        d.caption("Zoom in and out with the magnifier buttons", "Each step halves or doubles the visible range, centred on cursor A", 0.3)
        zin = pg.locator("#wave-toolbar button[title='Zoom in (+)']")
        zout = pg.locator("#wave-toolbar button[title='Zoom out (-)']")
        for _ in range(3):
            d.click(zin, 0.9)
        d.wait(0.8)
        d.click(zout, 0.9)
        d.caption("The + and - keys do the same, and dragging zooms into a range", "", 0.3)
        d.move(10, 300)
        pg.keyboard.press("-")
        d.wait(0.8)
        b = d.plot_box()
        d.move(b["x"] + b["width"] * 0.15, b["y"] + b["height"] * 0.5)
        pg.mouse.down()
        d.move(b["x"] + b["width"] * 0.45, b["y"] + b["height"] * 0.5, steps=25)
        pg.mouse.up()
        d.wait(1.5)
        d.caption("Fit (or double-click) shows the whole trace again", "", 0.3)
        d.click(pg.locator("#wave-toolbar button[title='Reset zoom (double-click plot)']"), 1.5)

        d.chapter("CPA key recovery")
        d.tab("analysis")
        d.caption("Recover the AES key with correlation power analysis", "Pick a leakage model and run; every byte converges live", 0.5)
        d.click(pg.locator("#panel-analysis button", has_text="Run CPA"), 0.5)
        api.wait("/api/analysis/cpa", lambda r: bool(r) and r.get("done"), timeout=120)
        d.wait(1)
        pg.locator("#sidebar").evaluate("e => e.scrollTop = 380")
        d.caption("Key recovered: " + KEY, "Click a byte for its correlation plot; overlay it on the waveform to see where the leak is", 2.5)
        chk = pg.locator("#panel-analysis label", has_text="overlay corr").locator("input")
        if chk.count():
            d.click(chk, 2)

        d.chapter("Glitch sweep")
        for path, value in (("glitch.repeat", 1), ("glitch.trigger_src", "ext_single")):
            api("PUT", "/api/scope/settings", {"path": path, "value": value})
        d.tab("glitch")
        d.caption("Fault injection: sweep glitch parameters", "Each point sends a command, checks the response and colours the result", 0.5)
        d.point(pg.locator("#panel-glitch button", has_text="Start sweep"))
        sweep = [{"path": "glitch.ext_offset", "start": 0, "stop": 90, "step": 3}, {"path": "glitch.width", "start": -45, "stop": 45, "step": 3}]
        api("POST", "/api/glitch/start", {"parameters": sweep, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "nrst"})
        d.wait(2)
        d.idle(300)
        pg.locator("#sidebar").evaluate("e => e.scrollTop = 99999")
        d.caption("Green points made the target misbehave the way we wanted", "", 3)

        d.chapter("Firmware build")
        d.tab("firmware")
        d.caption("Build any ChipWhisperer firmware with GCC or clang", "Compilers and NewAE's firmware sources download on demand; no toolchain setup", 0.5)
        for sel_value in ("simpleserial-aes", "CWLITEARM"):
            s = pg.locator(f"#panel-firmware select:has(option[value='{sel_value}'])").first
            if s.count():
                d.point(s)
                s.select_option(sel_value)
                d.wait(0.5)
        d.click(pg.locator("#panel-firmware .seg button[data-v='clang']").first, 0.6)
        out_sum = pg.locator("#panel-firmware summary", has_text="Build output")
        if out_sum.count():
            out_sum.first.click()
        d.click(pg.locator("#panel-firmware button", has_text="Build").first, 0.5)
        api.wait("/api/firmware/build", lambda r: r.get("state") != "running", timeout=600, every=1)
        d.wait(1)
        d.caption("Build succeeded: Build & program flashes it to the connected target", "", 3)

        d.chapter("Notebooks")
        d.tab("notebook")
        d.caption("Jupyter-style notebooks inside Studio", "cw.scope() and cw.target() use Studio's connection; NewAE's tutorials run unmodified", 0.5)
        d.click(pg.locator(".nb-file", has_text="Studio tour").first, 1)
        d.click(pg.locator("#panel-notebook button, .nb-toolbar button", has_text="Run all").first, 0.5)
        d.wait(1.5)
        api.wait("/api/kernel", lambda r: not r.get("busy") and not r.get("queued"), timeout=120, every=0.5)
        d.wait(1.5)
        pg.locator(".nb-scroll").evaluate("e => e.scrollTo({top: e.scrollHeight, behavior: 'smooth'})")
        d.caption("Traces captured in a notebook land in the Capture tab too", "", 3)

        d.chapter("Notes")
        d.tab("notes")
        d.caption("A notes pad with Markdown preview and autosave", "Insert the recovered CPA key or selection statistics with one click", 0.5)
        pg.evaluate("() => { const s = document.querySelector('#panel-notes select'); s.value = 'Lab notes.md'; s.dispatchEvent(new Event('change')); }")
        d.wait(0.8)
        ins = pg.locator("#panel-notes button", has_text="Insert CPA key")
        if ins.count():
            d.click(ins.first, 1)
        d.click(pg.locator("#panel-notes button", has_text="Preview"), 2.5)

        d.chapter("Calculator")
        d.tab("calc")
        d.caption("A calculator for side-channel work", "XOR, hex and binary, Hamming weight and distance, the AES S-box, statistics", 0.3)
        for expr in ("0x2b ^ 0x7e", "hw(0xff) + sbox(0x53)", "3.3 / 4096 * 1000"):
            d.type(pg.locator(".calc-in"), expr)
            pg.keyboard.press("Enter")
            d.wait(0.8)
        d.plot_click(0.25)
        d.plot_click(0.6, shift=True)
        pg.select_option("#panel-calc select", "cursors")
        d.caption("Statistics of the waveform between the cursors", "", 2.5)

        d.chapter("AI agents (MCP)")
        d.tab("help")
        pg.locator("#sidebar").evaluate("e => e.scrollTo({top: 420, behavior: 'smooth'})")
        d.caption("An MCP server lets AI agents drive everything", "Copy the configuration for Claude or another agent: 68 tools from connect to build and flash", 3.5)

        d.chapter("Themes")
        d.tab("capture")
        d.caption("Light and dark themes", "", 0.5)
        d.click(pg.locator("#theme-toggle"), 2.5)
        d.click(pg.locator("#theme-toggle"), 1.5)

        d.chapter("Get it")
        d.caption("", "")
        d.card("ChipWhisperer Studio", ["Windows, macOS and Linux bundles, or pip install", "github.com/keyuraghao/chipwhisperer-studio", "Documentation in the wiki; feedback and issues welcome"], 6)
        video = page.video
        ctx.close()
        browser.close()
        d.video_path = video.path()
    return d


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def slug(name: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in name.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def duration(ff: str, path: str) -> float:
    err = subprocess.run([ff, "-i", path], capture_output=True, text=True).stderr
    h, m, sec = err.split("Duration: ")[1].split(",")[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def make_clips(ff: str, mp4: str, chapters, folder: str, skip=("Introduction", "Get it")):
    """Cut every chapter into a looping animated WebP (10 fps, 960 px wide); GitHub plays these inline, unlike MP4."""
    os.makedirs(folder, exist_ok=True)
    total = duration(ff, mp4)
    for i, (start, name) in enumerate(chapters):
        if name in skip:
            continue
        end = chapters[i + 1][0] if i + 1 < len(chapters) else total
        path = os.path.join(folder, slug(name) + ".webp")
        subprocess.check_call([ff, "-y", "-loglevel", "error", "-ss", f"{start + 0.2:.2f}", "-t", f"{end - start - 0.3:.2f}", "-i", mp4, "-vf", "fps=10,scale=960:-1:flags=lanczos", "-c:v", "libwebp_anim", "-quality", "60", "-compression_level", "3", "-loop", "0", path])
        print(f"clip {name}: {path} ({os.path.getsize(path) / 1e6:.1f} MB)", flush=True)


def convert(webm: str, out: str, chapters, width: int):
    ff = ffmpeg_exe()
    mp4 = os.path.join(out, "chipwhisperer-studio-demo.mp4")
    subprocess.check_call([ff, "-y", "-loglevel", "error", "-i", webm, "-vf", f"scale={width}:-2:flags=lanczos,fps=30", "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4])
    make_clips(ff, mp4, chapters, os.path.join(out, "clips"))
    names = [n for _t, n in chapters]
    poster = os.path.join(out, "demo-poster.png")
    t = chapters[names.index("Waveform view")][0] + 4
    subprocess.check_call([ff, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", mp4, "-frames:v", "1", poster])
    _play_button(poster)
    return mp4, poster


def _play_button(path: str):
    """Draw a play button in the middle of the poster so it reads as a video in the README."""
    from PIL import Image, ImageDraw
    im = Image.open(path).convert("RGBA")
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, r = im.width // 2, im.height // 2, min(im.size) // 9
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(12, 18, 28, 190), outline=(255, 255, 255, 230), width=max(3, r // 14))
    t = r * 0.42
    d.polygon([(cx - t * 0.8, cy - t), (cx - t * 0.8, cy + t), (cx + t * 1.1, cy)], fill=(255, 255, 255, 240))
    Image.alpha_composite(im, layer).convert("RGB").save(path, optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "build", "screenshot-data"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "demo"))
    ap.add_argument("--size", default="1600x1000", help="browser size while recording")
    ap.add_argument("--width", type=int, default=1440, help="width of the MP4")
    ap.add_argument("--clips-only", action="store_true", help="only cut the clips again from the MP4 and chapters.json already in --out")
    args = ap.parse_args()
    if args.clips_only:
        with open(os.path.join(args.out, "chapters.json")) as f:
            chapters = [(c["t"], c["name"]) for c in json.load(f)]
        make_clips(ffmpeg_exe(), os.path.join(args.out, "chipwhisperer-studio-demo.mp4"), chapters, os.path.join(args.out, "clips"))
        return 0
    os.makedirs(args.out, exist_ok=True)
    width, height = (int(v) for v in args.size.split("x"))
    port = _free_port()
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-m", "cwstudio", "--no-browser", "--port", str(port), "--data-dir", args.data_dir], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    api = Api(base)
    try:
        _up(base)
        prepare(api)
        rec = os.path.join(args.out, "raw")
        shutil.rmtree(rec, ignore_errors=True)
        d = record(base, api, rec, width, height)
    finally:
        try:
            api("POST", "/api/shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    mp4, poster = convert(d.video_path, args.out, d.chapters, args.width)
    with open(os.path.join(args.out, "chapters.json"), "w") as f:
        json.dump([{"t": round(t, 1), "name": n} for t, n in d.chapters], f, indent=1)
    for path in (mp4, poster):
        print(f"wrote {path} ({os.path.getsize(path) / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
