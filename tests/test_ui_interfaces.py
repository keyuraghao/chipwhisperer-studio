"""Browser test of the Interfaces tab: with the simulator standing in for a Husky every Husky interface is enabled, as a Nano the SPI, USERIO, bit-banger and debug sections are disabled with their reasons, unsupported pins and programmers are not selectable, and the simulated SPI flash answers from the UI. Screenshots of both themes go to ``$CWSTUDIO_IFACE_SHOTS`` (default ``/tmp/claude-1000/interfaces``) for review.

Skipped when Playwright or its Chromium is not installed (``pip install playwright && playwright install chromium``).
"""
import json
import re
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
                    _api(studio, "POST", "/api/interfaces/spi/disable")
                    page.reload()
                    page.click("#tabs .tab[data-tab=interfaces]")
                    expect(spi.locator(".badge", has_text="off")).to_be_visible(timeout=10000)
                    for name in ("Disable", "Send", "JEDEC ID", "Status", "Read 16 B @0", "Toggle"):
                        btn = spi.locator("button", has_text=name).first
                        expect(btn).to_be_disabled()
                        expect(btn).to_have_attribute("title", "enable the SPI master first")
                    expect(spi.locator("button", has_text="Enable")).to_be_enabled()
                    spi.locator("button", has_text="Enable").click()
                    expect(spi.locator("button", has_text="JEDEC ID")).to_be_enabled(timeout=10000)
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


def _browser(pw):
    from playwright.sync_api import Error
    try:
        return pw.chromium.launch()
    except Error as e:
        pytest.skip(f"Chromium for Playwright is not installed: {e}")


def _leaf_select(page, path):
    return page.locator(f"#panel-scope .leaf:has(.name[title^='{path}']) select")


def test_simulate_as_switch_updates_every_tab(studio):
    """Connecting the simulator as another model while it is connected (no disconnect in between) re-gates the Interfaces tab, the Target programmer list and the Scope settings choices."""
    from playwright.sync_api import expect, sync_playwright
    _api(studio, "POST", "/api/scope/disconnect")
    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(studio)
        conn = page.locator("#panel-connect")
        scope_sel, sim_sel = conn.locator("select").nth(0), conn.locator("select").nth(1)
        scope_sel.select_option("sim")
        assert sim_sel.locator("option").evaluate_all("os => os.map(o => o.value)") == ["husky", "huskyplus", "pro", "lite", "nano"]
        sim_sel.select_option("husky")
        conn.locator("button", has_text="Connect scope").click()
        expect(page.locator("#panel-scope .leaf .name").first).to_be_visible(timeout=10000)
        page.click("#tabs .tab[data-tab=target]")
        expect(page.locator("#panel-target select").first.locator("option[value=AVR]")).to_be_enabled(timeout=10000)
        page.click("#tabs .tab[data-tab=connect]")
        sim_sel.select_option("nano")
        conn.locator("button", has_text="Connect scope").click()
        expect(page.locator("#topbar")).to_contain_text("Nano", timeout=10000)
        page.click("#tabs .tab[data-tab=scope]")
        tio1 = _leaf_select(page, "io.tio1")
        expect(tio1.locator("option[value=serial_tx]")).to_be_disabled(timeout=10000)
        expect(tio1.locator("option[value=serial_rx]")).to_be_enabled()
        page.click("#tabs .tab[data-tab=target]")
        avr = page.locator("#panel-target select").first.locator("option[value=AVR]")
        expect(avr).to_be_disabled(timeout=10000)
        page.click("#tabs .tab[data-tab=interfaces]")
        expect(_card(page, "SPI master")).to_have_class("card iface off", timeout=10000)
        expect(page.locator("#panel-interfaces .help").first).to_contain_text("Nano")
        page.click("#tabs .tab[data-tab=connect]")
        sim_sel.select_option("pro")
        conn.locator("button", has_text="Connect scope").click()
        page.click("#tabs .tab[data-tab=interfaces]")
        kind = _card(page, "Triggers").locator("select").first
        expect(kind.locator("option[value=uart_decode]")).to_be_enabled(timeout=10000)
        expect(kind.locator("option[value=uart_pattern]")).to_be_disabled()
        expect(_card(page, "SPI master")).not_to_have_class("card iface off")
        assert not errors, errors
        browser.close()


def test_trigger_line_and_scope_choices(studio):
    """A trigger changed in the Scope tab shows in the Capture tab's trigger line without visiting the Interfaces tab; the Scope tab offers only the connected model's trigger modules."""
    from playwright.sync_api import expect, sync_playwright
    _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "husky"})
    _api(studio, "POST", "/api/target/connect", {"kind": "sim"})
    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page(viewport={"width": 1400, "height": 950}, color_scheme="light")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(studio)
        page.click("#tabs .tab[data-tab=scope]")
        trig = _leaf_select(page, "trigger.triggers")
        expect(trig).to_be_visible(timeout=10000)
        trig.select_option("tio3")
        page.click("#tabs .tab[data-tab=capture]")
        expect(page.locator("#panel-capture")).to_contain_text("tio3", timeout=10000)
        page.click("#tabs .tab[data-tab=scope]")
        mod = _leaf_select(page, "trigger.module")
        assert mod.locator("option[value=DECODEIO]").is_disabled() and not mod.locator("option[value=UART]").is_disabled()
        tio4 = _leaf_select(page, "io.tio4")
        assert tio4.locator("option[value=serial_rx]").is_disabled() and not tio4.locator("option[value=serial_tx]").is_disabled()
        _api(studio, "PUT", "/api/scope/settings", {"path": "trigger.triggers", "value": "tio4"})
        assert not errors, errors
        browser.close()


def test_terminal_text_hex_line_endings_and_history(studio):
    from playwright.sync_api import expect, sync_playwright
    _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "lite"})
    _api(studio, "POST", "/api/target/connect", {"kind": "sim"})
    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(studio)
        page.evaluate("() => { for (const k of Object.keys(localStorage)) if (k.startsWith('cw.iface.')) localStorage.removeItem(k); }")
        page.reload()
        page.click("#tabs .tab[data-tab=interfaces]")
        uart = _card(page, "UART")
        term = uart.locator(".terminal")
        inp = term.locator("input.mono")
        sels = term.locator("select")
        mode, eol, hist = sels.nth(0), sels.nth(1), sels.nth(2)
        out = term.locator(".console")
        expect(inp).to_be_enabled(timeout=10000)
        eol.select_option("crlf")
        inp.fill("v")
        inp.press("Enter")
        expect(out.locator("div.tx").last).to_have_text(re.compile(r"→ v␍⏎$"), timeout=5000)
        expect(out.locator("div.rx").last).to_contain_text("z01", timeout=5000)
        mode.select_option("hex")
        inp.fill("76 0a")
        inp.press("Enter")
        expect(out.locator("div.tx").last).to_have_text(re.compile(r"→ v⏎$"), timeout=5000)
        term.locator("label", has_text="show hex").locator("input").check()
        expect(out.locator("div.tx").last).to_have_text(re.compile(r"→ 76 0a$"))
        expect(out.locator("div.tx").nth(-2)).to_have_text(re.compile(r"→ 76 0d 0a$"))
        expect(hist.locator("option")).to_have_count(3)  # header + two sends, newest first
        expect(inp).to_have_value("")
        inp.press("ArrowUp")
        expect(inp).to_have_value("76 0a")
        expect(mode).to_have_value("hex")
        inp.press("ArrowUp")
        expect(inp).to_have_value("v")
        expect(mode).to_have_value("text")
        term.locator("label", has_text="timestamps").locator("input").check()
        assert "show-ts" in out.get_attribute("class")
        page.reload()  # options and history persist per terminal
        page.click("#tabs .tab[data-tab=interfaces]")
        term = _card(page, "UART").locator(".terminal")
        expect(term.locator("select").nth(2).locator("option")).to_have_count(3, timeout=10000)
        assert term.locator("select").nth(1).input_value() == "crlf" and term.locator("label", has_text="show hex").locator("input").is_checked()
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize("model", ["huskyplus", "pro", "lite"])
def test_interfaces_gating_more_models(studio, model):
    """Per-model choices in the Interfaces tab: UART pins, trigger types and pins, rule count, bit-banger pins, programmers; no console errors at a narrow width."""
    from playwright.sync_api import expect, sync_playwright
    _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": model})
    _api(studio, "POST", "/api/target/connect", {"kind": "sim"})
    caps = _api(studio, "GET", "/api/capabilities")
    with sync_playwright() as pw:
        browser = _browser(pw)
        for width in (1400, 420):
            page = browser.new_page(viewport={"width": width, "height": 950})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(studio)
            page.click("#tabs .tab[data-tab=interfaces]")
            expect(page.locator("#panel-interfaces .cap-badges .badge").first).to_be_visible(timeout=10000)
            panel_w = page.evaluate("() => { const p = document.querySelector('#panel-interfaces'); return [p.scrollWidth, p.clientWidth]; }")
            assert panel_w[0] <= panel_w[1] + 1, panel_w  # nothing in the panel forces a horizontal scroll
            trig = _card(page, "Triggers")
            kind = trig.locator("select").first
            for k in ("basic", "uart_decode", "uart_pattern", "edge_counter", "adc_level", "sequencer", "sad"):
                assert kind.locator(f"option[value={k}]").is_disabled() == (not caps["triggers"][k]["available"]), k
            for p in ("tio1", "nrst", "sma", "userio_d0"):
                assert trig.locator(f"input[data-pin={p}]").is_disabled() == (p not in caps["triggers"]["basic"]["pins"]), p
            if model == "huskyplus":
                kind.select_option("uart_pattern")
                rules = trig.locator("select").nth(2).locator("option")
                assert rules.count() == 8 and not rules.nth(7).is_disabled()
                bb = _card(page, "Bit-banger and 1-Wire")
                assert not bb.locator("select").first.locator("option[value=TIO1]").is_disabled()
                assert bb.locator("select").nth(1).locator("option[value=nrst]").count() == 0  # nRST cannot be a clock
            elif model == "lite":
                expect(_card(page, "Bit-banger and 1-Wire")).to_have_class("card iface off")
            page.click("#tabs .tab[data-tab=target]")
            progs = page.locator("#panel-target select").first
            for k, v in caps["programmers"].items():
                if progs.locator(f"option[value={k}]").count():
                    assert progs.locator(f"option[value={k}]").is_disabled() == (not v["available"]), k
            assert not errors, errors
            page.close()
        browser.close()


def test_model_switch_refreshes_interfaces_once(studio):
    """Connecting the simulator as another model while it is connected announces scope-connected once (app.js), so the Interfaces tab reloads once per change, not twice."""
    from playwright.sync_api import expect, sync_playwright
    _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "husky"})
    _api(studio, "POST", "/api/target/disconnect")  # no target, so only the scope change reloads the tab
    with sync_playwright() as pw:
        browser = _browser(pw)
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(studio)
        page.click("#tabs .tab[data-tab=connect]")
        expect(page.locator("#topbar")).to_contain_text("Husky", timeout=10000)
        page.wait_for_timeout(1500)
        loads = []
        page.on("request", lambda r: loads.append(r.url) if r.url.split("?")[0].endswith("/api/interfaces") else None)
        _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "pro"})
        expect(page.locator("#topbar")).to_contain_text("Pro", timeout=10000)
        page.wait_for_timeout(2000)
        assert len(loads) == 1, loads
        assert not errors, errors
        browser.close()
