"""Browser test of the Interfaces tab: with the simulator standing in for a Husky every Husky interface is enabled, as a Nano the SPI, USERIO, bit-banger and debug sections are disabled with their reasons, unsupported pins and programmers are not selectable, and the simulated SPI flash answers from the UI. Screenshots of both themes go to ``$CWSTUDIO_IFACE_SHOTS`` (default ``/tmp/claude-1000/interfaces``) for review.

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
SHOTS = os.environ.get("CWSTUDIO_IFACE_SHOTS", "/tmp/claude-1000/interfaces")


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
    data = tmp_path_factory.mktemp("studio-iface")
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": SRC}
    flags = ["--no-browser"]
    if "--browser" in subprocess.run([sys.executable, "-m", "cwstudio", "--help"], capture_output=True, text=True, env=env).stdout:
        flags.insert(0, "--browser")
    p = subprocess.Popen([sys.executable, "-m", "cwstudio", *flags, "--simulate", "--port", str(port), "--data-dir", str(data)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def _card(page, title):
    return page.locator(".card.iface", has=page.locator(f".card-head .title:text-is('{title}')"))


@pytest.mark.parametrize("model", ["husky", "nano"])
def test_interfaces_gating(studio, model):
    from playwright.sync_api import Error, expect, sync_playwright
    os.makedirs(SHOTS, exist_ok=True)
    _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": model})
    _api(studio, "POST", "/api/target/connect", {"kind": "sim"})
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        for theme in ("dark", "light"):
            page = browser.new_page(viewport={"width": 1500, "height": 1000}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(studio)
            page.click("#tabs .tab[data-tab=interfaces]")
            expect(page.locator("#panel-interfaces .cap-badges .badge").first).to_be_visible(timeout=10000)
            uart, spi, uio, bb, ocd = (_card(page, t) for t in ("UART", "SPI master", "Husky USERIO", "Bit-banger and 1-Wire", "JTAG and SWD"))
            expect(uart).not_to_have_class("card iface off")
            expect(ocd).to_have_class("card iface off")
            expect(ocd.locator(".iface-reason")).to_contain_text("needs real hardware" if model == "husky" else "Nano")
            if model == "husky":
                for c in (spi, uio, bb):
                    expect(c).not_to_have_class("card iface off")
                assert not uart.locator("select").nth(2).locator("option[value=tio3]").is_disabled()
                assert uart.locator("select").nth(2).locator("option[value=tio4]").is_disabled()  # TIO4 cannot receive
                if theme == "dark":
                    spi.locator("button", has_text="Enable").click()
                    expect(spi.locator(".badge", has_text="on,")).to_be_visible()
                    spi.locator("button", has_text="JEDEC ID").click()
                    expect(spi.locator("table")).to_contain_text("ef 40 18")
                    bb.locator("button", has_text="Read ROM").click()
                    expect(bb.locator(".kv")).to_contain_text("0x28")
            else:
                for c, why in ((spi, "no SPI pins"), (uio, "Husky only"), (bb, "Husky only")):
                    expect(c).to_have_class("card iface off")
                    expect(c.locator(".iface-reason")).to_contain_text(why)
                    assert c.locator("button").first.is_disabled()
                rx = uart.locator("select").nth(2)
                assert rx.locator("option[value=tio3]").is_disabled() and not rx.locator("option[value=tio1]").is_disabled()
                trig = _card(page, "Triggers")
                assert trig.locator("select").first.locator("option[value=uart_pattern]").is_disabled()
                assert trig.locator("input[data-pin=tio1]").is_disabled() and not trig.locator("input[data-pin=tio4]").is_disabled()
            page.wait_for_timeout(300)
            page.screenshot(path=os.path.join(SHOTS, f"interfaces-{model}-{theme}.png"))
            ocd.scroll_into_view_if_needed()
            page.screenshot(path=os.path.join(SHOTS, f"interfaces-{model}-{theme}-debug.png"))
            page.click("#tabs .tab[data-tab=target]")
            avr = page.locator("#panel-target select").first.locator("option[value=AVR]")
            if model == "nano":
                expect(avr).to_be_disabled()
                expect(avr).to_have_attribute("title", "AVR ISP needs the SPI pins, which the CW-Nano does not have")
            else:
                expect(avr).to_be_enabled()
            page.screenshot(path=os.path.join(SHOTS, f"target-{model}-{theme}.png"))
            assert not errors, errors
            page.close()
        browser.close()
