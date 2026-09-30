#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/images.

Starts a Studio with the simulator in a temporary data folder, runs a realistic session through the HTTP API (400 captured traces, a CPA attack, a glitch sweep and a clang firmware build), then photographs every tab with Playwright in dark and light themes.

    pip install -e ".[test]" playwright && playwright install chromium
    python tools/screenshots.py [--data-dir DIR] [--out docs/images]

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


async def shoot(base: str, out: str, width: int, height: int):
    from playwright.async_api import async_playwright

    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for scheme in ("dark", "light"):
            page = await browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(base + "/")
            await page.wait_for_timeout(1500)

            async def tab(name: str, scroll: int = 0):
                await page.click(f'[data-tab="{name}"]')
                await page.wait_for_timeout(900)
                await page.locator("#sidebar").evaluate(f"e => e.scrollTop = {scroll}")
                await page.wait_for_timeout(300)

            async def snap(name: str):
                await page.screenshot(path=os.path.join(out, name))
                print("wrote", name)

            if scheme == "dark":
                await tab("connect")
                await snap("connect.png")
                await tab("scope")
                for group in ("gain", "adc", "clock"):
                    loc = page.locator(".tree .ghead", has_text=group).first
                    if await loc.count():
                        await loc.click()
                await snap("scope.png")
                await tab("target")
                await snap("target.png")
                await tab("firmware")
                await page.wait_for_timeout(600)
                await snap("firmware.png")
                await tab("firmware", 99999)
                await snap("toolchains.png")
                await tab("capture")
                await page.locator("#wave-toolbar label", has_text="mean").locator("input").check()
                await page.click("#btn-run")
                await page.wait_for_timeout(2500)
                await snap("capture.png")
                await tab("analysis", 380)
                await snap("analysis.png")
                await tab("glitch", 700)
                await snap("glitch.png")
                await tab("help", 420)
                await snap("mcp.png")
            else:
                await tab("capture")
                await page.click("#btn-single")
                await page.wait_for_timeout(1500)
                await snap("capture-light.png")
                await tab("firmware")
                await snap("firmware-light.png")
            await page.close()
        await browser.close()
    if errors:
        print("browser console errors:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "build", "screenshot-data"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "images"))
    ap.add_argument("--port", type=int, default=0, help="HTTP port for the temporary Studio (default: any free port)")
    ap.add_argument("--size", default="1600x1000")
    ap.add_argument("--no-firmware", action="store_true", help="skip toolchain downloads and the firmware build")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    width, height = (int(v) for v in args.size.split("x"))
    port = args.port or _free_port()
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.Popen([sys.executable, "-m", "cwstudio", "--simulate", "--no-browser", "--port", str(port), "--data-dir", args.data_dir], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    api = Api(base)
    try:
        _up(base)
        prepare(api, not args.no_firmware)
        return asyncio.run(shoot(base, args.out, width, height))
    finally:
        try:
            api("POST", "/api/shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


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
