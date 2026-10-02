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
            if k == 0:
                # before a code map exists, the actions that need one are disabled with the reason (the API would answer 404)
                for sel in ("#cm-pctrace", "#cm-window", "#cm-align", "#cm-shift"):
                    expect(page.locator(sel)).to_be_disabled()
                    expect(page.locator(sel)).to_have_attribute("title", re.compile("build the code map first"))
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


SYNC_JS = """() => {
  const cb = document.getElementById('code-band').codeBand; const u = cb.wave.u; const L = cb.layout();
  if (!L) return null;
  const over = u.over.getBoundingClientRect(), br = cb.el.getBoundingClientRect(), s = u.scales.x;
  let worst = 0;
  for (const f of [0, 0.13, 0.5, 0.77, 1]) { const v = s.min + f * (s.max - s.min); worst = Math.max(worst, Math.abs((L.toX(v) + br.left) - (u.valToPos(v, 'x') + over.left))); }
  return {worst, min: s.min, max: s.max};
}"""


def test_band_sync_long_source_and_switching(studio, tmp_path):
    """The band's x axis matches uPlot's exactly through drag zoom, wheel zoom over the band, the zoom keys, mean/min-max/time axis and resizes; ctrl and alt drags pick a region without zooming; a source file of 30000 lines opens and moves its marks quickly (only the rows in sight are rendered); building the code map of other firmware replaces the file tabs and source view."""
    from playwright.sync_api import Error, expect, sync_playwright
    try:
        from tests import fwbuild
    except ImportError:
        import fwbuild
    base, fw = studio
    try:
        xmega = fwbuild.build_elf("CWLITEXMEGA", "gcc", "TINYAES128C", "SS_VER_2_1")
    except RuntimeError as e:
        pytest.skip(f"firmware build not available: {str(e).splitlines()[0]}")
    _api(base, "POST", "/api/scope/connect", {"kind": "sim", "sim_model": "husky"})
    _api(base, "POST", "/api/target/connect", {"kind": "sim"})
    _api(base, "POST", "/api/target/program", {"programmer": "STM32F", "path": fw})
    _api(base, "POST", "/api/capture/start", {"count": 20, "clear": True})
    for _ in range(300):
        if not _api(base, "GET", "/api/status")["job"]["running"]:
            break
        time.sleep(0.05)
    aes_c = _api(base, "POST", "/api/codemap/build", {})["band"]["files"]
    src = next(f["path"] for f in aes_c if f["name"] == "aes.c")
    long_dir = tmp_path / "long"
    long_dir.mkdir()
    (long_dir / "aes.c").write_text(open(src).read() + "".join(f"/* filler {i} */ int filler_{i} = {i};\n" for i in range(30000)))
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base)
        page.click("#tabs .tab[data-tab=code]")
        page.wait_for_function("document.getElementById('code-band').codeBand && document.getElementById('code-band').codeBand.band", timeout=15000)
        over = page.locator("#wave-plot .u-over").bounding_box()
        y = over["y"] + over["height"] / 2
        band = page.locator("#code-band canvas").bounding_box()

        def sync():
            page.wait_for_timeout(250)
            r = page.evaluate(SYNC_JS)
            assert r["worst"] < 0.5, r
            return r
        sync()
        page.mouse.move(over["x"] + over["width"] * 0.2, y)
        page.mouse.down()
        page.mouse.move(over["x"] + over["width"] * 0.5, y, steps=6)
        page.mouse.up()
        z = sync()
        assert z["max"] - z["min"] < 2000
        page.mouse.move(band["x"] + band["width"] / 2, band["y"] + 20)
        page.mouse.wheel(0, -200)
        sync()
        page.keyboard.press("+")
        before = sync()
        for mod in ("Control", "Alt"):
            page.keyboard.down(mod)
            page.mouse.move(over["x"] + over["width"] * 0.3, y)
            page.mouse.down()
            page.mouse.move(over["x"] + over["width"] * 0.6, y, steps=6)
            page.mouse.up()
            page.keyboard.up(mod)
            expect(page.locator("#cm-region")).to_contain_text("instructions", timeout=10000)
            after = sync()
            assert after["min"] == pytest.approx(before["min"]) and after["max"] == pytest.approx(before["max"])  # a region, not a zoom
        for label in ("mean", "min/max", "time axis"):
            page.locator("#wave-toolbar label", has_text=label).locator("input").check()
            sync()
            page.locator("#wave-toolbar label", has_text=label).locator("input").uncheck()
        page.set_viewport_size({"width": 1100, "height": 900})
        sync()
        # a 30000 line source file
        page.fill("#cm-sources", str(long_dir))
        page.click("#cm-build")
        expect(page.locator("#cm-build-msg")).to_contain_text("Built", timeout=60000)
        page.click("#cm-window")
        expect(page.locator("#cm-funcs")).to_contain_text("SubBytes", timeout=10000)
        t = time.time()
        page.locator("#cm-files button").filter(has_text=re.compile(r"^aes\.c$")).click()
        page.wait_for_function("document.querySelector('#cm-source .cm-src-rows') && parseFloat(document.querySelector('#cm-source .cm-src-rows').style.height) > 30000 * 12", timeout=10000)
        assert time.time() - t < 3 and page.locator("#cm-source .ln").count() < 600
        page.locator("#cm-lines .cm-item", has_text="getSBoxValue").first.click()
        expect(page.locator("#cm-source .ln.cur")).to_contain_text("getSBoxValue")
        page.evaluate("document.getElementById('cm-source').scrollTop = 1e9")
        page.wait_for_timeout(300)
        assert int(page.locator("#cm-source .ln").last.get_attribute("data-ln")) > 30000
        # other firmware: the old file tabs and source go away
        page.fill("#cm-sources", "")
        page.select_option("#cm-elf", "__other")
        page.fill("#cm-elf-path", xmega)
        page.click("#cm-build")
        expect(page.locator("#cm-info")).to_contain_text("AVR XMEGA", timeout=60000)
        # the region is kept and shown in the new firmware's sources: not the long aes.c of the previous build
        page.wait_for_function("!document.querySelector('#cm-source .cm-src-rows') || parseFloat(document.querySelector('#cm-source .cm-src-rows').style.height) < 30000 * 12", timeout=10000)
        names = {f["name"] for f in _api(base, "GET", "/api/codemap/band")["band"]["files"]}
        assert set(page.locator("#cm-files button").all_inner_texts()) <= names
        page.click("#cm-window")
        expect(page.locator("#cm-funcs")).to_contain_text("SubBytes", timeout=10000)
        sync()
        assert not errors, errors
        browser.close()
