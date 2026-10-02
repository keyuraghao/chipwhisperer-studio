"""Browser tests of how the tabs live together: keyboard shortcuts act only where they belong, the top bar works from every tab, every tab opens without page errors and fits its sidebar, the page never scrolls sideways (down to phone widths), the CPA plots follow their box, a scope switched to another model is announced to every tab, and the Help tab covers the newer tabs.

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _api(base, method, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


def _idle(base, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if not (_api(base, "GET", "/api/status").get("job") or {}).get("running"):
            return
        time.sleep(0.1)
    raise AssertionError("job still running")


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    data = tmp_path_factory.mktemp("studio-integration")
    port = _free_port()
    p = subprocess.Popen([sys.executable, "-m", "cwstudio", "--no-browser", "--simulate", "--port", str(port), "--data-dir", str(data)], env={**os.environ, "PYTHONPATH": SRC}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(300):
            try:
                _api(base, "GET", "/api/status")
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("Studio did not start")
        _api(base, "POST", "/api/scope/connect", {"kind": "sim"})
        _api(base, "POST", "/api/target/connect", {"kind": "sim"})
        yield base
    finally:
        p.terminate()
        p.wait(10)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        yield b
        b.close()


def _page(browser, base, width=1600, height=1000, theme="dark", tab="capture"):
    ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=theme)
    ctx.add_init_script(f"try {{ localStorage.setItem('cw.tab', '{tab}'); localStorage.removeItem('cw.theme'); }} catch (e) {{}}")
    page = ctx.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(base)
    page.wait_for_selector("#tabs .tab.active")
    page.wait_for_function("() => document.getElementById('ws-state').classList.contains('on')")
    return page


def _count(base):
    return _api(base, "GET", "/api/traces")["count"]


def _wait_count(base, n, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if _count(base) >= n:
            return True
        time.sleep(0.1)
    return False


def test_shortcuts_follow_the_active_tab(studio, browser):
    page = _page(browser, studio)
    pause = page.locator("#wave-toolbar button.active")
    # Space pauses the waveform where it is shown
    page.click("#tabs .tab[data-tab=capture]")
    page.locator("body").press("Space")
    assert pause.count() == 1
    page.locator("body").press("Space")
    assert pause.count() == 0
    # ... but not in the Notebook or Logic tabs, which hide it
    for tab in ("notebook", "logic"):
        page.click(f"#tabs .tab[data-tab={tab}]")
        page.locator("body").press("Space")
        assert pause.count() == 0, tab
    # S captures from every tab, the Logic tab included, like the Single button
    for tab in ("logic", "notebook", "interfaces", "code", "help"):
        page.click(f"#tabs .tab[data-tab={tab}]")
        n = _count(studio)
        page.locator("body").press("s")
        assert _wait_count(studio, n + 1), tab
        _idle(studio)
    # Ctrl+S saves a notebook; it must not also capture a trace
    page.click("#tabs .tab[data-tab=notebook]")
    n = _count(studio)
    page.locator("body").press("Control+s")
    time.sleep(0.8)
    assert _count(studio) == n
    assert not page.errors
    page.context.close()


def test_top_bar_and_every_tab(studio, browser):
    for theme in ("dark", "light"):
        page = _page(browser, studio, 1100, 800, theme)
        tabs = page.eval_on_selector_all("#tabs .tab", "els => els.map((e) => e.dataset.tab)")
        assert {"interfaces", "code", "logic", "notebook", "capture", "analysis", "glitch", "help"} <= set(tabs)
        for tab in tabs:
            page.click(f"#tabs .tab[data-tab={tab}]")
            page.wait_for_timeout(250)
            assert page.locator(f"#panel-{tab}").is_visible(), tab
            geo = page.evaluate("""() => { const sb = document.getElementById('sidebar'), d = document.documentElement;
                return { side: sb.scrollWidth - sb.clientWidth, page: d.scrollWidth - d.clientWidth, main: document.getElementById('main').getBoundingClientRect().width,
                         clipped: [...document.querySelectorAll('#tabs .tab')].filter((t) => t.scrollWidth > t.clientWidth + 1).map((t) => t.dataset.tab) }; }""")
            assert geo["side"] <= 1 and geo["page"] <= 0 and geo["main"] > 300 and not geo["clipped"], (tab, geo)
            # the top bar Single button captures whatever tab is open
            if theme == "dark":
                n = _count(studio)
                page.click("#btn-single")
                assert _wait_count(studio, n + 1), tab
                _idle(studio)
        assert not page.errors, page.errors
        page.context.close()


def test_narrow_windows_never_scroll_sideways(studio, browser):
    for width, height in ((800, 900), (700, 800), (390, 844)):
        page = _page(browser, studio, width, height)
        for tab in ("capture", "notebook", "logic", "interfaces", "code", "analysis"):
            page.click(f"#tabs .tab[data-tab={tab}]")
            page.wait_for_timeout(200)
            geo = page.evaluate("""() => { const d = document.documentElement, r = (id) => document.getElementById(id).getBoundingClientRect();
                return { over: d.scrollWidth - d.clientWidth, down: d.scrollHeight - d.clientHeight, top: r('topbar').right, run: r('btn-run').right, theme: r('theme-toggle').right, main: r('main').width }; }""")
            assert geo["over"] <= 0 and geo["down"] <= 0, (width, tab, geo)
            assert max(geo["top"], geo["run"], geo["theme"]) <= width and geo["main"] > 250, (width, tab, geo)
        assert not page.errors, page.errors
        page.context.close()


def test_cpa_plots_fit_after_results_arrive_in_the_background(studio, browser):
    page = _page(browser, studio, 1100, 800, tab="capture")
    _api(studio, "POST", "/api/capture/start", {"count": 200, "clear": True})
    _idle(studio)
    _api(studio, "POST", "/api/analysis/cpa/start", {"model": "sbox_hw"})
    end = time.time() + 60
    while not _api(studio, "GET", "/api/analysis/cpa").get("done") and time.time() < end:
        time.sleep(0.2)
    page.wait_for_timeout(800)  # the result is drawn while the Analysis tab is hidden
    page.click("#tabs .tab[data-tab=analysis]")
    page.wait_for_timeout(500)
    over = page.evaluate("() => { const sb = document.getElementById('sidebar'); return [sb.scrollWidth - sb.clientWidth, [...document.querySelectorAll('#panel-analysis .miniplot')].map((m) => m.querySelector('canvas') ? m.querySelector('canvas').getBoundingClientRect().width - m.getBoundingClientRect().width : 0)]; }")
    assert over[0] <= 1 and all(d <= 1 for d in over[1]), over
    page.context.close()


def test_scope_switched_to_another_model_is_announced(studio, browser):
    page = _page(browser, studio, tab="scope")
    try:
        page.wait_for_selector("#panel-scope .leaf")
        chip = page.locator("#chip-scope .txt")
        page.wait_for_function("() => document.querySelector('#chip-scope .txt').textContent.includes('Husky')")
        _api(studio, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "nano"})  # replaces the connected scope: the connected flag never drops
        page.wait_for_function("() => document.querySelector('#chip-scope .txt').textContent.includes('Nano')", timeout=10000)
        # the Scope tab reloaded its tree for the new model: the CW-Nano triggers on TIO4 only
        sel = page.locator("#panel-scope .leaf:has(.name[title^='trigger.triggers']) select")
        page.wait_for_function("() => { const s = document.querySelector(\"#panel-scope .leaf:has(.name[title^='trigger.triggers']) select\"); return s && s.querySelector('option[value=tio1]') && s.querySelector('option[value=tio1]').disabled; }", timeout=10000)
        assert sel.locator("option[value=tio4]").is_enabled()
        assert "Nano" in chip.text_content()
    finally:
        _api(studio, "POST", "/api/scope/connect", {"kind": "sim"})
        _api(studio, "POST", "/api/target/connect", {"kind": "sim"})
        page.context.close()


def test_help_covers_the_newer_tabs(studio, browser):
    page = _page(browser, studio, tab="help")
    text = page.locator("#panel-help").inner_text()
    for word in ("Interfaces", "Logic", "Code", "own variables", "Simulate as", "Ctrl+drag", "Shift+Enter", "logic analyser", "code map"):
        assert word in text, word
    page.context.close()


def test_latin_digits_in_arabic_locale(studio, browser):
    """Axis labels, measurements and log times use the digits 0-9 even where the browser locale writes numbers in its own digits."""
    ctx = browser.new_context(locale="ar-EG", viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    page.goto(studio)
    page.wait_for_function("() => document.getElementById('ws-state').classList.contains('on')")
    native = page.evaluate("() => (12345.5).toLocaleString()")
    assert not native.isascii()  # the locale really uses other digits here
    r = page.evaluate("""async () => { const m = await import('/static/js/api.js'); const ax = m.axisStyle();
        return { num: m.fmtNum(12345.5), clock: m.fmtClock(new Date(2026, 0, 2, 13, 45, 7)), ticks: ax.values(null, [0, 1500, -2.5, null]) }; }""")
    assert all(not ch.isdigit() or ch.isascii() for ch in r["num"] + "".join(r["ticks"])), r
    assert "12" in r["num"] and r["ticks"][1].replace(",", "").replace(".", "") == "1500", r
    assert all(not ch.isdigit() or ch.isascii() for ch in r["clock"]) and "45" in r["clock"], r
    page.wait_for_selector("#log-body .log-line .t", state="attached", timeout=10000)  # the log history is replayed once the page has booted
    log_times = page.eval_on_selector_all("#log-body .log-line .t", "els => els.map((e) => e.textContent)")
    assert log_times and all(not ch.isdigit() or ch.isascii() for t in log_times for ch in t), log_times
    ctx.close()


def test_waveform_keeps_its_height_in_small_windows(studio, browser):
    """With the code band on, a wrapped toolbar and footer must not squeeze the plot away: it keeps a minimum height and the main area scrolls instead."""
    for width, height in ((1000, 600), (700, 700), (390, 844), (800, 480)):
        ctx = browser.new_context(viewport={"width": width, "height": height})
        ctx.add_init_script("try { localStorage.setItem('cw.codeband', '1'); localStorage.setItem('cw.tab', 'capture'); } catch (e) {}")
        page = ctx.new_page()
        page.goto(studio)
        page.wait_for_function("() => document.querySelector('#wave-plot canvas')")
        page.wait_for_timeout(300)
        geo = page.evaluate("() => ({ plot: document.getElementById('wave-plot').getBoundingClientRect().height, canvas: document.querySelector('#wave-plot canvas').getBoundingClientRect().height, band: document.getElementById('code-band').getBoundingClientRect().height, over: document.documentElement.scrollWidth - document.documentElement.clientWidth })")
        assert geo["plot"] >= 150 and geo["canvas"] >= 140 and geo["band"] > 0 and geo["over"] <= 0, (width, height, geo)
        ctx.close()


def test_sample_rate_follows_clock_changes_from_anywhere(studio, browser):
    """A clock change made through the API (a notebook, an agent, another window) updates the sample rate under the waveform."""
    _api(studio, "POST", "/api/capture/single", {})
    _idle(studio)
    page = _page(browser, studio, tab="capture")
    try:
        page.wait_for_function("() => document.getElementById('wave-footer').textContent.includes('29.480 MS/s')", timeout=10000)
        _api(studio, "PUT", "/api/scope/settings", {"path": "clock.adc_mul", "value": 1})
        page.wait_for_function("() => document.getElementById('wave-footer').textContent.includes('7.370 MS/s')", timeout=10000)
        _api(studio, "PUT", "/api/scope/settings", {"path": "clock.adc_src", "value": "clkgen_x4"})
        page.wait_for_function("() => document.getElementById('wave-footer').textContent.includes('29.480 MS/s')", timeout=10000)
    finally:
        _api(studio, "PUT", "/api/scope/settings", {"path": "clock.adc_mul", "value": 4})
        page.context.close()
