#!/usr/bin/env python3
"""Regenerate the screenshots used by the README and the wiki (docs/wiki/images).

Starts a Studio with the simulator in a temporary data folder, runs a realistic session through the HTTP API (400 captured traces, a CPA attack, a glitch sweep and a clang firmware build), then photographs every scene with Playwright twice: ``name.png`` in the dark theme and ``name-light.png`` in the light theme. The README and the wiki show whichever matches the reader's GitHub theme. Besides the classic tabs it shows the Interfaces tab (a simulated Husky, then a Nano with its gating), the UART terminal, the Logic tab with decoded UART, SPI and I2C traffic, the Code tab running the clang build in the simulator with a selected region, two notebooks side by side and the Simulate as choice.

    pip install -e ".[test]" playwright && playwright install chromium
    python tools/screenshots.py [--data-dir DIR] [--out docs/wiki/images]

Firmware screenshots need the Arm GCC and clang toolchains; they are downloaded into --data-dir on the first run (about 360 MB), so reuse the same folder to make later runs fast.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Api:
    def __init__(self, base: str):
        self.base = base

    def __call__(self, method: str, path: str, body=None, timeout: float = 1800):
        data = json.dumps(body or {}).encode() if method != "GET" else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def wait(self, path: str, done, timeout: float = 1800, every: float = 0.5):
        end = time.time() + timeout
        while time.time() < end:
            r = self("GET", path)
            if done(r):
                return r
            time.sleep(every)
        raise TimeoutError(path)


def prepare(api: Api, with_firmware: bool):
    api("POST", "/api/scope/connect", {"kind": "sim"})
    api("POST", "/api/target/connect", {"kind": "sim"})
    idle = lambda st: not (st.get("job") or {}).get("running")  # noqa: E731
    api("POST", "/api/capture/start", {"count": 400, "clear": True, "key_mode": "fixed", "key": "2b7e151628aed2a6abf7158809cf4f3c"})
    api.wait("/api/status", idle)
    api("POST", "/api/analysis/cpa/start", {"model": "sbox_hw", "report_every": 25})
    api.wait("/api/analysis/cpa", lambda r: bool(r) and r.get("done"))
    for path, value in (("glitch.repeat", 1), ("glitch.trigger_src", "ext_single")):
        api("PUT", "/api/scope/settings", {"path": path, "value": value})
    sweep = [{"path": "glitch.ext_offset", "start": 0, "stop": 90, "step": 3}, {"path": "glitch.width", "start": -45, "stop": 45, "step": 3}]
    api("POST", "/api/glitch/start", {"parameters": sweep, "command": "g", "expected": "c4090000", "output_len": 4, "reset": "nrst"})
    api.wait("/api/status", idle)
    tour = [
        {"cell_type": "markdown", "source": "# Studio tour\n\nCells run **inside Studio**: `cw.scope()` and `cw.target()` use the devices connected in the Connect tab, and every trace captured here also appears in the **Capture** tab."},
        {"cell_type": "code", "source": "import chipwhisperer as cw\nimport numpy as np\n\nscope = cw.scope()\ntarget = cw.target(scope)\nkey = bytearray.fromhex('2b7e151628aed2a6abf7158809cf4f3c')\nprint(type(scope).__name__, 'connected through Studio')"},
        {"cell_type": "code", "source": "from tqdm.notebook import trange\nfor i in trange(200, desc='Capturing'):\n    cw.capture_trace(scope, target, bytearray(np.random.bytes(16)), key)\nprint(len(studio.traces), 'traces stored in Studio')"},
        {"cell_type": "code", "source": "import matplotlib.pyplot as plt\nwaves = studio.traces.waves\nplt.figure(figsize=(10, 3))\nplt.plot(waves.mean(axis=0), lw=0.8)\nplt.title(f'Mean of {len(waves)} traces')\nplt.xlabel('sample'); plt.ylabel('power')"},
        {"cell_type": "code", "source": "waves.shape, float(waves.std())"},
    ]
    api("PUT", "/api/notebooks/file", {"path": "Studio tour.ipynb", "notebook": {"cells": tour}})
    api("POST", "/api/notebooks/run", {"path": "Studio tour.ipynb"})
    # A second notebook, shown next to the first one: each notebook has its own kernel
    api("PUT", "/api/notebooks/file", {"path": "Key check.ipynb", "notebook": {"cells": KEY_CHECK}})
    api("POST", "/api/notebooks/run", {"path": "Key check.ipynb"})
    api("PUT", "/api/notes/Lab%20notes.md", {"text": "# Lab notes\n\nCPA key (sbox_hw, 400 traces): 2b7e151628aed2a6abf7158809cf4f3c\n\nGlitch window: ext_offset 21 24 27 30 33 36 39, width 6 9 12 15\n\n- [x] simpleserial-aes built with clang for CWLITEARM\n- [ ] try CW308_STM32F4 next\n"})
    if not with_firmware:
        return
    for tc in ("arm-gcc", "clang"):
        api("POST", f"/api/toolchains/{tc}/install")
    api.wait("/api/toolchains", lambda r: all(t["installed"] for t in r["toolchains"] if t["id"] in ("arm-gcc", "clang")), every=2)
    if not api("GET", "/api/firmware")["sources"]["valid"]:
        api("POST", "/api/firmware/sources/fetch", {})
        api.wait("/api/firmware", lambda r: (r["sources"].get("job") or {}).get("state") in ("installed", "error"), every=2)
    api("POST", "/api/firmware/build", {"project": "simpleserial-aes", "platform": "CWLITEARM", "compiler": "clang", "crypto_target": "TINYAES128C", "ss_ver": "SS_VER_2_1"})
    api.wait("/api/firmware/build", lambda r: r.get("state") != "running")
    api("POST", "/api/firmware/sources/check")


TUTORIAL = "chipwhisperer-jupyter/courses/sca101/Lab 3_3 - DPA on Firmware Implementation of AES (HARDWARE).ipynb"
KEY = "2b7e151628aed2a6abf7158809cf4f3c"
KEY_CHECK = [
    {"cell_type": "markdown", "source": "# Key check\n\nThis notebook has **its own kernel**: its variables are separate from *Studio tour*, but both use the same scope and target."},
    {"cell_type": "code", "source": "key = bytes.fromhex('" + KEY + "')\nwave, textin, textout, k = studio.traces[len(studio.traces) - 1]\nprint('plaintext ', bytes(textin).hex())\nprint('ciphertext', bytes(textout).hex())\nprint('key matches:', bytes(k) == key)"},
    {"cell_type": "code", "source": "waves = studio.traces.waves\nsnr = waves.mean(axis=0).std() / waves.std(axis=0).mean()\nprint(f'{len(waves)} traces, signal to noise {snr:.2f}')"},
]
# The Logic tab: the simulator's demo traffic with these channels hidden, so UART, SPI and I2C fill the view
LOGIC_HIDDEN = ["CLK", "1-Wire", "CAN", "JTAG TCK", "JTAG TMS", "JTAG TDI", "JTAG TDO", "SWCLK", "SWDIO"]


def prepare_logic(api: Api):
    """A simulated logic capture with the UART, SPI and I2C decoders (idempotent, so each theme starts from the same state)."""
    for d in api("GET", "/api/la/decoders")["decoders"]:
        api("DELETE", f"/api/la/decoders/{d['id']}")
    api("POST", "/api/la/capture", {"source": "sim", "wait": True})
    for kind in ("uart", "spi", "i2c"):
        api("POST", "/api/la/decoders", {"type": kind})
    api("PUT", "/api/la/channels", {"channels": [{"channel": n, "hidden": True} for n in LOGIC_HIDDEN]})
    return api("GET", "/api/la/capture")


def newest_build(api: Api):
    """Path of the newest firmware build (its .elf is next to it), or None."""
    builds = api("GET", "/api/firmware/builds")
    builds = builds.get("builds", builds) if isinstance(builds, dict) else builds
    return (builds[0].get("path") or builds[0].get("hex")) if builds else None


def connect_sim(api: Api, model: str = "husky"):
    """(Re)connect the simulator as a ChipWhisperer model, with its target. A fresh simulated scope also drops programmed firmware, so the built-in AES model is back."""
    api("POST", "/api/scope/connect", {"kind": "sim", "sim_model": model})
    api("POST", "/api/target/connect", {"kind": "sim"})


def prepare_extras(api: Api, with_firmware: bool):
    """State for the more detailed wiki scenes: serial traffic, a run NewAE tutorial."""
    api("POST", "/api/target/simpleserial", {"cmd": "k", "data": "2b7e151628aed2a6abf7158809cf4f3c", "read_len": 0})
    for pt in ("00112233445566778899aabbccddeeff", "3243f6a8885a308d313198a2e0370734"):
        api("POST", "/api/target/simpleserial", {"cmd": "p", "data": pt, "read_len": 16})
    if with_firmware:
        tut = api("GET", "/api/notebooks")["tutorials"]
        if not tut.get("installed"):
            api("POST", "/api/notebooks/tutorials/fetch")
            api.wait("/api/notebooks", lambda r: (r["tutorials"].get("job") or {}).get("state") in ("installed", "error"), every=2)
        setup = {"cells": [{"cell_type": "code", "source": "SCOPETYPE = 'OPENADC'\nPLATFORM = 'CWLITEARM'\nCRYPTO_TARGET = 'TINYAES128C'\nSS_VER = 'SS_VER_2_1'"}]}
        nb = api("GET", "/api/notebooks/file?path=" + urllib.request.quote(TUTORIAL))
        if not any(c["source"].startswith("SCOPETYPE = 'OPENADC'") for c in nb["cells"]):
            nb["cells"].insert(0, {"cell_type": "code", "source": setup["cells"][0]["source"], "outputs": [], "execution_count": None})
        api("PUT", "/api/notebooks/file", {"path": TUTORIAL, "notebook": nb})
        api("POST", "/api/notebooks/run", {"path": TUTORIAL, "stop_on_error": False})


async def shoot(base: str, out: str, width: int, height: int, with_firmware: bool = True):
    from playwright.async_api import async_playwright

    errors = []
    api = Api(base)  # some scenes change the session (the simulated model, logic captures, programmed firmware)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scheme in ("dark", "light"):
            page = await browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
            # The code band switches itself on once a code map exists; keep it off except in the Code scenes
            await page.add_init_script("try { localStorage.setItem('cw.codeband', '0') } catch (e) {}")
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(base + "/")
            await page.wait_for_timeout(1500)

            async def tab(name: str, scroll: int = 0):
                await page.click(f'[data-tab="{name}"]')
                await page.wait_for_timeout(900)
                await page.locator("#sidebar").evaluate(f"e => e.scrollTop = {scroll}")
                await page.wait_for_timeout(300)

            async def snap(name: str, element: str = None, keep_focus: bool = False):
                if not keep_focus:  # no focus ring left on the last control clicked
                    await page.evaluate("() => document.activeElement && document.activeElement.blur && document.activeElement.blur()")
                    await page.wait_for_timeout(100)
                if scheme == "light":
                    name = name[:-4] + "-light.png"
                path = os.path.join(out, name)
                if element:
                    await page.locator(element).first.screenshot(path=path)
                else:
                    await page.screenshot(path=path)
                print("wrote", name)

            async def expand(group: str):
                """Open a settings-tree group or a <details> section only if it is closed (clicking an open one would collapse it)."""
                loc = page.locator(".tree .ghead", has_text=group).first
                if await loc.count() and not await loc.evaluate("e => e.parentElement.classList.contains('open')"):
                    await loc.click()

            async def open_details(scope: str, text: str):
                loc = page.locator(f"{scope} summary", has_text=text).first
                if await loc.count() and not await loc.evaluate("e => e.parentElement.open"):
                    await loc.click()

            async def clear_toasts():
                await page.evaluate("() => { const t = document.getElementById('toasts'); if (t) t.innerHTML = ''; }")

            async def run_capture(count=300):
                await page.fill("#panel-capture input[type=number] >> nth=0", str(count))
                await page.click("#btn-run")
                await page.wait_for_timeout(2500)

            # Connect
            await tab("connect")
            await snap("connect.png")
            scan = page.locator("#panel-connect button", has_text="Scan USB")
            if await scan.count():
                await scan.first.click()
                await page.wait_for_timeout(1200)
            await snap("connect-panel.png", "#sidebar")
            await snap("simulate-as.png", "#panel-connect .card >> nth=0")
            # Scope
            await tab("scope")
            for group in ("gain", "adc", "clock"):
                await expand(group)
            await page.wait_for_timeout(400)
            await snap("scope.png")
            await page.fill("#panel-scope input.flex >> nth=0", "glitch")
            await page.wait_for_timeout(400)
            await expand("glitch")
            await page.wait_for_timeout(400)
            await snap("scope-glitch.png")
            await page.fill("#panel-scope input.flex >> nth=0", "")
            # Target
            await tab("target")
            await snap("target.png")
            await tab("target", 99999)
            await page.locator("#panel-target button", has_text="Send key").click()
            await page.wait_for_timeout(400)
            await page.locator("#panel-target .row:has-text('Command') button", has_text="Send").first.click()
            await page.wait_for_timeout(800)
            await page.locator("#panel-target h2", has_text="Serial console").scroll_into_view_if_needed()
            await page.wait_for_timeout(300)
            await snap("target-serial.png", "#sidebar")
            # Interfaces: a simulated Husky (everything available), its UART terminal, then a Nano with what it lacks
            await tab("interfaces")
            await page.wait_for_timeout(600)
            await snap("interfaces.png")
            await snap("interfaces-terminal.png", "#panel-interfaces .card.iface >> nth=0")
            connect_sim(api, "nano")
            await page.wait_for_timeout(1500)
            await page.locator("#panel-interfaces .card.iface", has=page.locator(".title", has_text="SPI")).first.scroll_into_view_if_needed()
            await page.wait_for_timeout(400)
            await snap("interfaces-nano.png", "#sidebar")
            connect_sim(api, "husky")
            await page.wait_for_timeout(1500)
            await clear_toasts()
            # Firmware
            await tab("firmware")
            await page.wait_for_timeout(600)
            await page.locator("#panel-firmware .seg button[data-v='clang']").first.click()  # the result box shows a clang build
            await page.wait_for_timeout(400)
            await snap("firmware.png")
            for summ in ("Advanced options", "Build output"):
                await open_details("#panel-firmware", summ)
            await page.locator("#panel-firmware .console.build").evaluate("e => e.scrollTop = e.scrollHeight")
            await page.wait_for_timeout(400)
            await snap("firmware-build-output.png", "#panel-firmware .card")
            await snap("firmware-sources.png", "#panel-firmware .card:has(.card-head .title:text-is('Firmware sources'))")
            await snap("toolchains.png", "#panel-firmware .card:has(.card-head .title:text-is('Toolchains'))")
            await open_details("#panel-firmware", "Add a custom toolchain")
            fields = {"Name, e.g. TriCore GCC 11": "TriCore GCC 11", "arch, e.g. tricore or arm,riscv": "tricore", "tool prefix, e.g. tricore-elf-": "tricore-elf-", "or an existing install folder": "/opt/tricore-gcc"}
            for ph, val in fields.items():
                await page.fill(f'#panel-firmware input[placeholder="{ph}"]', val)
            await page.wait_for_timeout(300)
            await snap("toolchains-custom.png", "#panel-firmware .card:has(.card-head .title:text-is('Toolchains'))")
            # Capture and waveform
            await tab("capture")
            await page.locator("#wave-toolbar label", has_text="mean").locator("input").check()
            await page.fill("#panel-capture input[type=number] >> nth=0", "0")  # continuous, so the photo shows a capture in progress
            await page.click("#btn-run")
            await page.wait_for_timeout(2500)
            await snap("capture.png")
            await snap("capture-panel.png", "#sidebar")
            await page.click("#btn-stop")
            await page.wait_for_timeout(1500)
            await page.fill("#panel-capture input[type=number] >> nth=0", "300")
            await page.locator("#wave-toolbar label", has_text="mean").locator("input").uncheck()
            await page.select_option("#wave-toolbar select >> nth=1", value="10")
            await run_capture()
            await snap("waveform-overlay.png")
            await page.select_option("#wave-toolbar select >> nth=1", value="0")
            await page.locator("#wave-toolbar label", has_text="mean").locator("input").check()
            await page.locator("#wave-toolbar label", has_text="time axis").locator("input").check()
            await page.click("#btn-single")
            await page.wait_for_timeout(1200)
            box = await page.locator("#wave-plot").bounding_box()
            await page.mouse.move(box["x"] + box["width"] * 0.08, box["y"] + box["height"] * 0.5)
            await page.mouse.down()
            await page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.5, steps=8)
            await page.mouse.up()
            await page.wait_for_timeout(600)
            await page.mouse.click(box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.5)
            await page.keyboard.down("Shift")
            await page.mouse.click(box["x"] + box["width"] * 0.72, box["y"] + box["height"] * 0.5)
            await page.keyboard.up("Shift")
            await page.wait_for_timeout(600)
            await snap("waveform-cursors.png")
            # Zoom buttons: two steps in around cursor A, so the toolbar's magnifier buttons and the zoomed view show together
            await page.locator("#wave-toolbar button[title='Reset zoom (double-click plot)']").click()
            await page.locator("#wave-toolbar button[title='Clear cursors']").click()
            await page.wait_for_timeout(300)
            await page.mouse.click(box["x"] + box["width"] * 0.42, box["y"] + box["height"] * 0.5)
            for _ in range(2):
                await page.locator("#wave-toolbar button[title='Zoom in (+)']").click()
                await page.wait_for_timeout(250)
            await page.wait_for_timeout(400)
            await snap("waveform-zoom.png")
            await page.locator("#wave-toolbar button[title='Reset zoom (double-click plot)']").click()
            # Analysis
            await tab("analysis", 380)
            await snap("analysis.png")
            # Glitch sweep results from prepare()
            await tab("glitch", 99999)
            await page.wait_for_timeout(600)
            await snap("glitch.png")
            # Notebook
            await tab("notebook")
            await page.locator(".nb-file", has_text="Studio tour").first.click()
            await page.wait_for_timeout(1500)
            await page.locator(".nb-scroll").evaluate("e => e.scrollTop = e.scrollHeight")  # the mean-trace figure is in the last cells
            await page.wait_for_timeout(500)
            await snap("notebook.png")
            if with_firmware:
                folder = page.locator(".nb-folder > summary", has_text="courses/sca101")
                if await folder.count():
                    if not await folder.first.evaluate("e => e.parentElement.open"):
                        await folder.first.click()
                    await page.locator(".nb-file", has_text="Lab 3_3 - DPA on Firmware Implementation of AES (HARDWARE)").first.click()
                    await page.wait_for_timeout(2000)
                    target = page.locator(".nb-cell", has_text="Capturing traces").first
                    if await target.count():
                        await target.scroll_into_view_if_needed()
                        await page.locator(".nb-scroll:visible").first.evaluate("e => e.scrollTop = Math.max(0, e.scrollTop - 250)")
                    await page.wait_for_timeout(500)
                    await snap("notebook-tutorial.png")
                    await page.locator(".nb-tab", has_text="Lab 3_3").locator(".nb-tab-close").first.click()  # back to the tour alone for the next scene
                    await page.wait_for_timeout(800)
            # A second notebook in a pane on the right (the split button moves the current notebook there)
            await page.locator(".nb-file", has_text="Key check").first.click()
            await page.wait_for_timeout(1500)
            await page.locator(".nb-split").first.click()
            await page.wait_for_timeout(1500)
            await snap("notebook-split.png")
            # Notes, selection statistics, calculator
            await tab("notes")
            await page.evaluate("() => { const s = document.querySelector('#panel-notes select'); s.value = 'Lab notes.md'; s.dispatchEvent(new Event('change')); }")
            await page.wait_for_timeout(800)
            await page.evaluate("() => { const t = document.querySelector('.notes-text'); t.focus(); const i = t.value.indexOf('21 24'); t.setSelectionRange(i, i + 20); document.dispatchEvent(new Event('selectionchange')); }")
            await page.wait_for_timeout(600)
            await snap("selection-stats.png", keep_focus=True)
            await page.locator("#panel-notes button", has_text="Preview").click()
            await page.wait_for_timeout(500)
            await snap("notes.png")
            await tab("calc")
            for expr in ("0x2b ^ 0x7e", "hw(0xff) + sbox(0x53)", "3.3 / 4096 * 1000"):
                await page.fill(".calc-in", expr)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(300)
            await page.mouse.click(box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.5)
            await page.keyboard.down("Shift")
            await page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.5)
            await page.keyboard.up("Shift")
            await page.select_option("#panel-calc select", "cursors")
            await page.wait_for_timeout(600)
            await snap("calc.png")
            # Logic analyser: demo traffic decoded, zoomed to the trigger, where SPI, UART and I2C overlap
            cap = prepare_logic(api)
            await tab("logic")
            await page.select_option("#la-source", "sim")
            await page.wait_for_timeout(800)
            for _ in range(20):  # the capture finished over the API; let the badge catch up
                if (await page.locator("#la-run-badge").inner_text()).strip() == "idle":
                    break
                await page.wait_for_timeout(250)
            res = page.locator("#main button", has_text="Results")
            if await res.count() and "active" in (await res.first.get_attribute("class") or ""):
                await res.first.click()  # the results table would push the I2C rows out of view
            ax = await page.locator(".la-axis-canvas").bounding_box()
            t0, t1 = cap["t_start"], cap["t_end"]
            xt = lambda t: ax["x"] + (t - t0) / (t1 - t0) * ax["width"]  # noqa: E731
            await page.mouse.move(xt(-0.0002), ax["y"] + ax["height"] / 2)
            await page.mouse.down()
            await page.mouse.move(xt(0.003), ax["y"] + ax["height"] / 2, steps=10)
            await page.mouse.up()
            await page.mouse.move(10, height - 10)
            await page.wait_for_timeout(1200)
            await snap("logic.png")
            await page.locator("#panel-logic .card", has=page.locator(".title", has_text="Decoders")).first.scroll_into_view_if_needed()
            await page.wait_for_timeout(400)
            await snap("logic-panel.png", "#sidebar")
            if with_firmware and newest_build(api):
                # Code on the waveform: the clang build runs in the simulator, so its traces and the code map match
                api("POST", "/api/target/program", {"programmer": "STM32F", "path": newest_build(api)})
                api("POST", "/api/capture/start", {"count": 100, "clear": True, "key_mode": "fixed", "key": KEY})
                api.wait("/api/status", lambda st: not (st.get("job") or {}).get("running"))
                await tab("code")
                await page.click("#cm-build")
                api.wait("/api/codemap", lambda r: bool(r.get("built")), timeout=300)
                await page.wait_for_timeout(1500)
                if not await page.locator("#wave-code-toggle").is_checked():
                    await page.check("#wave-code-toggle")
                await page.wait_for_timeout(600)
                box = await page.locator("#wave-plot").bounding_box()
                await page.keyboard.down("Control")
                await page.mouse.move(box["x"] + box["width"] * 0.30, box["y"] + box["height"] * 0.5)
                await page.mouse.down()
                await page.mouse.move(box["x"] + box["width"] * 0.38, box["y"] + box["height"] * 0.5, steps=8)
                await page.mouse.up()
                await page.keyboard.up("Control")
                await page.wait_for_timeout(1500)
                await page.locator("#cm-lines > *").first.click()  # the busiest source line: shades every sample where it ran
                await page.wait_for_timeout(1500)
                await page.locator("#panel-code h2", has_text="Selection").scroll_into_view_if_needed()
                await page.mouse.move(10, height - 10)
                await page.wait_for_timeout(500)
                await clear_toasts()
                await snap("code.png")
                await page.locator("#panel-code h2", has_text="Source").last.evaluate("e => e.scrollIntoView({block: 'start'})")
                await page.locator("#sidebar").evaluate("e => e.scrollTop -= 330")
                await page.wait_for_timeout(500)
                await snap("code-region.png", "#sidebar")
                await page.locator("#panel-code button", has_text="Clear").first.click()  # no region or shaded lines left on the waveform for later scenes
                await page.uncheck("#wave-code-toggle")
                await page.dblclick("#wave-plot")
                connect_sim(api, "husky")  # back to the built-in AES model for the scenes after this one
                await page.wait_for_timeout(1500)
                await clear_toasts()
            # Help
            await tab("help")
            await snap("help.png")
            await tab("help", 420)
            await snap("mcp-setup.png")
            # Overview last so the waveform is busy
            await tab("capture")
            await page.locator("#wave-toolbar label", has_text="time axis").locator("input").uncheck()
            await page.dblclick("#wave-plot")
            await run_capture()
            await snap("overview.png")
            await page.close()
        await browser.close()
    if errors:
        print("browser console errors:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "build", "screenshot-data"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "wiki", "images"))
    ap.add_argument("--port", type=int, default=0, help="HTTP port for the temporary Studio (default: any free port)")
    ap.add_argument("--size", default="1600x1000")
    ap.add_argument("--no-firmware", action="store_true", help="skip toolchain downloads and the firmware build")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    width, height = (int(v) for v in args.size.split("x"))
    port = args.port or _free_port()
    env = dict(os.environ, PYTHONPATH=neutral_pythonpath() + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-m", "cwstudio", "--simulate", "--no-browser", "--port", str(port), "--data-dir", args.data_dir], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    api = Api(base)
    try:
        _up(base)
        prepare(api, not args.no_firmware)
        prepare_extras(api, not args.no_firmware)
        return asyncio.run(shoot(base, args.out, width, height, not args.no_firmware))
    finally:
        try:
            api("POST", "/api/shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def neutral_pythonpath(folder: str = "/tmp/ChipWhispererStudio-app/_internal") -> str:
    """Copy the package to a neutral folder and return it for PYTHONPATH, so paths Studio shows (such as the udev rule in the Connect tab) look like an installed bundle instead of the developer's checkout."""
    import shutil
    dest = os.path.join(folder, "cwstudio")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "src", "cwstudio"), dest, ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _up(base: str, timeout: float = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/api/status", timeout=2)
            return True
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("Studio did not start")


if __name__ == "__main__":
    sys.exit(main())
