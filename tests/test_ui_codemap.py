"""Browser test of code on the waveform: program a real firmware build into the simulator from the Target tab, capture from the Capture tab, build the code map in the Code tab, ctrl+drag a region over the first S-box round on the waveform and check the side panel lists SubBytes and its source lines, pick a line (its samples are highlighted and its source and disassembly shown), hover the Code band, align, and leave the zoom and cursor behaviour as it was. Screenshots of both themes go to ``$CWSTUDIO_CODEMAP_SHOTS`` (default ``/tmp/claude-1000/codemap``) for review.

Skipped when Playwright or its Chromium is not installed, or when no toolchain can build the firmware (see ``tests/fwbuild.py``).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
SHOTS = os.environ.get("CWSTUDIO_CODEMAP_SHOTS", "/tmp/claude-1000/codemap")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _api(base, method, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    try:
        from tests import fwbuild
    except ImportError:
        import fwbuild
    try:
        elf = fwbuild.build_elf("CWLITEARM", "gcc", "TINYAES128C", "SS_VER_2_1")
    except RuntimeError as e:
        pytest.skip(f"firmware build not available: {str(e).splitlines()[0]}")
    data = tmp_path_factory.mktemp("studio-codemap")
    fw = data / "simpleserial-aes-CWLITEARM.elf"
    shutil.copy(elf, fw)
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": SRC}
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
        yield base, str(fw)
    finally:
        p.terminate()
        p.wait(10)


def test_code_on_the_waveform(studio):
    from playwright.sync_api import Error, expect, sync_playwright
    base, fw = studio
    os.makedirs(SHOTS, exist_ok=True)
    _api(base, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "husky"})
    _api(base, "POST", "/api/target/connect", {"kind": "sim"})
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        for k, theme in enumerate(("dark", "light")):
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base)
            if k == 0:
                # program the build into the simulated target from the Target tab, then capture from the Capture tab
                page.click("#tabs .tab[data-tab=target]")
                page.fill("#panel-target input[placeholder^='or path']", fw)
                page.click("#panel-target button:has-text('Program target')")
                expect(page.locator("#panel-target .help.ok")).to_contain_text("Done", timeout=30000)
                st = _api(base, "GET", "/api/codemap")
                assert st["programmed"]["emulated"] and st["programmed"]["elf"] == fw
                page.click("#tabs .tab[data-tab=capture]")
                page.fill("#panel-capture input[title='0 = continuous']", "40")
                page.click("#panel-capture button.btn.primary:has-text('Run')")
                page.wait_for_function("document.getElementById('trace-count').textContent.startsWith('40 ')", timeout=60000)
                page.wait_for_function("document.getElementById('btn-stop').disabled", timeout=30000)
            page.click("#tabs .tab[data-tab=code]")
            expect(page.locator("#cm-elf option", has_text="(programmed)")).to_have_count(1, timeout=10000)
            page.click("#cm-build")
            expect(page.locator("#cm-build-msg")).to_contain_text("Built", timeout=60000)
            expect(page.locator("#cm-info")).to_contain_text("correct AES")
            expect(page.locator("#cm-info")).to_contain_text("runs this firmware")
            expect(page.locator("#cm-conf-txt")).to_contain_text("high")
            expect(page.locator("#code-band")).to_have_class("on")
            # zoom and cursors still work: plain drag zooms, click sets cursor A
            sb = _api(base, "POST", "/api/codemap/lookup", {"function": "SubBytes"})["ranges"][0]["samples"]
            over = page.locator("#wave-plot .u-over").bounding_box()
            n = _api(base, "GET", "/api/traces")["samples"]
            x = lambda s: over["x"] + over["width"] * s / (n - 1)  # noqa: E731
            y = over["y"] + over["height"] / 2
            page.mouse.click(x(100), y)
            expect(page.locator("#wave-footer")).to_contain_text("A #")
            cursor_a = re.search(r"A #\d+", page.inner_text("#wave-footer")).group(0)
            # ctrl+drag over the first SubBytes: a code region, not a zoom
            page.keyboard.down("Control")
            page.mouse.move(x(sb[0] + 8), y)
            page.mouse.down()
            page.mouse.move(x(sb[1] - 8), y, steps=10)
            page.mouse.up()
            page.keyboard.up("Control")
            expect(page.locator("#cm-region")).to_contain_text("instructions", timeout=10000)
            first_own = page.locator("#cm-funcs .cm-item:not(.caller):not(.inl) .nm").first
            expect(first_own).to_have_text("SubBytes")
            callers = page.locator("#cm-funcs .cm-item.caller .nm").all_inner_texts()
            assert "Cipher" in callers and "get_pt" in callers
            expect(page.locator("#cm-lines")).to_contain_text("getSBoxValue")
            assert cursor_a in page.inner_text("#wave-footer")  # the region drag did not move cursor A
            page.screenshot(path=os.path.join(SHOTS, f"region-{theme}.png"))
            # pick the S-box line: its samples are shaded, the source and disassembly open there
            page.locator("#cm-lines .cm-item", has_text="getSBoxValue").first.click()
            expect(page.locator("#cm-source .ln.cur")).to_contain_text("getSBoxValue")
            expect(page.locator("#cm-files button.on")).to_have_text("aes.c")
            expect(page.locator("#cm-disasm .di").first).to_be_visible()
            page.wait_for_timeout(400)
            page.locator("#cm-source").scroll_into_view_if_needed()
            page.screenshot(path=os.path.join(SHOTS, f"line-{theme}.png"))
            # hover the Code band: function, file:line and cycle
            band = page.locator("#code-band canvas").bounding_box()
            for frac in (0.5, 0.35, 0.65):
                page.mouse.move(band["x"] + band["width"] * frac, band["y"] + 4 + 14 * 4 + 6)
                page.wait_for_timeout(150)
                if page.locator("#code-band .cb-tip").is_visible():
                    break
            expect(page.locator("#code-band .cb-tip")).to_contain_text("cycles")
            page.screenshot(path=os.path.join(SHOTS, f"band-{theme}.png"), clip={"x": over["x"] - 80, "y": over["y"], "width": over["width"] + 120, "height": band["y"] + band["height"] - over["y"]})
            # a block in the band selects that call; the trigger window lists the AES round functions
            page.click("#cm-window")
            expect(page.locator("#cm-funcs")).to_contain_text("ShiftRows")
            expect(page.locator("#cm-funcs")).to_contain_text("AddRoundKey")
            page.click("#cm-align")
            expect(page.locator("#cm-conf-txt")).to_contain_text("high", timeout=20000)
            page.evaluate("document.getElementById('sidebar').scrollTop = 0")
            page.screenshot(path=os.path.join(SHOTS, f"panel-{theme}.png"))
            # double-click still fits and a plain drag still zooms (the middle of the plot is then about sample 1300)
            page.mouse.dblclick(x(2500), y)
            page.wait_for_timeout(200)
            page.mouse.move(x(1000), y)
            page.mouse.down()
            page.mouse.move(x(1600), y, steps=6)
            page.mouse.up()
            page.wait_for_timeout(300)
            page.mouse.move(over["x"] + over["width"] / 2, y)
            page.wait_for_timeout(200)
            hover = int(re.search(r"hover #(\d+)", page.inner_text("#wave-footer")).group(1))
            assert 1200 < hover < 1400, hover
            assert not errors, errors
            page.close()
        browser.close()
