"""Browser test of the Logic tab: capture the simulator's demo traffic, add UART and SPI decoders from the sidebar, zoom by dragging on the time axis, find a decoded value, hover it, place both cursors and measure between them. Screenshots of both themes go to ``$CWSTUDIO_LOGIC_SHOTS`` (default ``/tmp/claude-1000/logic``) for review.

Skipped when Playwright or its Chromium is not installed (``pip install playwright && playwright install chromium``).
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
SHOTS = os.environ.get("CWSTUDIO_LOGIC_SHOTS", "/tmp/claude-1000/logic")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _api(base, method, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    data = tmp_path_factory.mktemp("studio-logic")
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": SRC}
    env.pop("CWSTUDIO_SIGROK_CLI", None)
    p = subprocess.Popen([sys.executable, "-m", "cwstudio", "--no-browser", "--simulate", "--port", str(port), "--data-dir", str(data)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                _api(base, "GET", "/api/status")
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("Studio did not start")
        yield base
    finally:
        p.terminate()
        p.wait(10)


def _add_decoder(page, kind):
    page.select_option("#la-dec-type", kind)
    form = page.locator(".la-decform")
    form.locator("button", has_text="Add decoder").click()
    page.wait_for_function("k => document.querySelector('#la-decoders').textContent.includes(k)", arg={"uart": "UART", "spi": "SPI"}[kind], timeout=10000)


def test_logic_tab(studio):
    from playwright.sync_api import Error, expect, sync_playwright
    os.makedirs(SHOTS, exist_ok=True)
    _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "husky"})
    _api(studio, "POST", "/api/target/connect", {"kind": "sim"})
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        for theme in ("dark", "light"):
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(studio)
            page.click("#tabs .tab[data-tab=logic]")
            expect(page.locator("#la-source option[value=sim]")).to_have_count(1, timeout=10000)
            # every source is listed; sigrok is not installed here, so it says why and how to install it
            page.select_option("#la-source", "sigrok")
            expect(page.locator("#panel-logic .iface-reason")).to_contain_text("sigrok-cli is not installed")
            expect(page.locator("#panel-logic .la-install")).to_contain_text("apt install sigrok-cli")
            page.select_option("#la-source", "sim")
            page.click("#la-capture")
            expect(page.locator("#la-footer")).to_contain_text("100,000", timeout=15000)
            expect(page.locator(".la-label.ch").first).to_contain_text("UART TX")
            if theme == "dark":
                _add_decoder(page, "uart")
                _add_decoder(page, "spi")
            expect(page.locator(".la-label.dec", has_text="UART TX")).to_have_count(1, timeout=10000)
            expect(page.locator(".la-label.dec", has_text="MOSI")).to_have_count(1)
            # the results table lists decoded values from both decoders
            body = page.locator("#la-results-body")
            expect(body).to_contain_text("MOSI 9F 00 00 00 / MISO FF EF 40 18", timeout=10000)
            expect(body).to_contain_text("UART")
            # zoom by dragging on the time axis: 0 .. 1 ms of the -10 .. 15 ms capture
            ax = page.locator(".la-axis-canvas").bounding_box()
            x0 = ax["x"] + ax["width"] * 10 / 25
            x1 = ax["x"] + ax["width"] * 11 / 25
            page.mouse.move(x0, ax["y"] + 15)
            page.mouse.down()
            page.mouse.move((x0 + x1) / 2, ax["y"] + 15, steps=4)
            page.mouse.move(x1, ax["y"] + 15, steps=4)
            page.mouse.up()
            expect(page.locator("#la-footer")).not_to_contain_text("view -10.000 ms", timeout=5000)
            # find the JEDEC ID in the decoded results (the view centres on the SPI transfer), then hover the transfer and the MISO byte under it
            page.select_option(".la-search select >> nth=0", "decoded")
            page.fill(".la-search input >> nth=1", "EF")
            page.locator(".la-search button").nth(1).click()
            expect(page.locator(".la-search")).to_contain_text("at ", timeout=5000)
            plot = page.locator("#la-plot").bounding_box()
            xfer = page.locator(".la-label.dec", has_text="SPI").bounding_box()
            page.mouse.move(plot["x"] + plot["width"] / 2, xfer["y"] + xfer["height"] / 2)
            expect(page.locator(".la-tip")).to_contain_text("MISO FF EF 40 18", timeout=5000)
            miso = page.locator(".la-label.dec", has_text="MISO").bounding_box()
            page.mouse.move(plot["x"] + plot["width"] / 2, miso["y"] + miso["height"] / 2)
            expect(page.locator(".la-tip")).to_contain_text("SPI MISO: 40", timeout=5000)
            # cursors: A by click and B by right click on the SPI SCK row, then the measurement between them
            sck = page.locator(".la-label.ch", has_text="SPI SCK").bounding_box()
            y = sck["y"] + sck["height"] / 2
            page.mouse.click(plot["x"] + plot["width"] * 0.15, y)
            page.mouse.click(plot["x"] + plot["width"] * 0.85, y, button="right")
            expect(page.locator("#la-footer")).to_contain_text("Δ", timeout=5000)
            page.select_option("#la-meas-ch", label="SPI SCK")
            expect(page.locator("#la-measure")).to_contain_text("Edges", timeout=5000)
            expect(page.locator("#la-measure")).to_contain_text("Frequency")
            page.wait_for_timeout(400)
            page.mouse.move(plot["x"] + plot["width"] / 2, miso["y"] + miso["height"] / 2)
            page.wait_for_timeout(200)
            page.screenshot(path=os.path.join(SHOTS, f"logic_{theme}.png"))
            # zoomed far out the annotations aggregate instead of overlapping
            page.click(".la-toolbar button:has-text('Fit')")
            page.wait_for_timeout(600)
            page.screenshot(path=os.path.join(SHOTS, f"logic_fit_{theme}.png"))
            assert not errors, errors
            page.close()
        browser.close()
