"""Browser test of the Notebook tab with several notebooks: tabs, the side by side split, per-notebook kernels and output routing, layout memory across reloads, and screenshots of both themes (and a narrow window) for review.

Skipped when Playwright or its Chromium is not installed (``pip install playwright && playwright install chromium``). Screenshots go to ``$CWSTUDIO_SHOTS`` (default ``/tmp/claude-1000/nbtabs``).
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
SHOTS = os.environ.get("CWSTUDIO_SHOTS", "/tmp/claude-1000/nbtabs")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _api(base, method, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def _notebook(tag, extra=""):
    return {"cells": [
        {"cell_type": "markdown", "source": f"# Notebook {tag}\n\nEach notebook has its **own kernel**: `x` here is `'{tag}'`."},
        {"cell_type": "code", "source": f"x = '{tag}'\nprint('out', x)"},
        {"cell_type": "code", "source": "import numpy as np\nwave = np.sin(np.arange(200) / 9)" + extra},
    ]}


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    data = tmp_path_factory.mktemp("studio-ui")
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
        for tag in ("A", "B", "C"):
            _api(base, "PUT", "/api/notebooks/file", {"path": f"lab/{tag.lower()}.ipynb", "notebook": _notebook(tag)})
        yield base
    finally:
        p.terminate()
        p.wait(10)


def test_two_notebooks_side_by_side(studio):
    from playwright.sync_api import Error, expect, sync_playwright
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        page = browser.new_page(viewport={"width": 1500, "height": 900}, color_scheme="dark")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(studio)
        page.click("#tabs .tab[data-tab=notebook]")
        page.click("details.nb-folder > summary")
        page.click(".nb-file[data-path='lab/a.ipynb']")
        page.click(".nb-file[data-path='lab/b.ipynb']")
        left, right = page.locator(".nb-pane[data-pane='0']"), page.locator(".nb-pane[data-pane='1']")
        expect(left.locator(".nb-tab")).to_have_count(2)
        expect(right).to_be_hidden()

        # split: b (the active tab) moves to a new pane on the right
        left.locator(".nb-split").click()
        expect(right).to_be_visible()
        expect(left.locator(".nb-tab .nb-tab-name")).to_have_text(["a"])
        expect(right.locator(".nb-tab .nb-tab-name")).to_have_text(["b"])
        assert left.bounding_box()["x"] < right.bounding_box()["x"]

        # run the first code cell of each notebook with Shift+Enter; outputs land in their own notebook
        for pane in (left, right):
            pane.locator(".nb-doc:visible .nb-cell.code textarea").first.click()
            page.keyboard.press("Shift+Enter")
        expect(left.locator(".nb-doc:visible .nb-out.stream").first).to_have_text("out A")
        expect(right.locator(".nb-doc:visible .nb-out.stream").first).to_have_text("out B")
        expect(left.locator(".nb-doc:visible .nb-out", has_text="out B")).to_have_count(0)
        expect(right.locator(".nb-doc:visible .nb-out", has_text="out A")).to_have_count(0)
        expect(left.locator(".nb-doc:visible .badge", has_text="Kernel idle")).to_be_visible()
        assert [v["repr"] for v in _api(studio, "GET", "/api/kernel/variables?kernel=lab/a.ipynb") if v["name"] == "x"] == ["'A'"]
        assert [v["repr"] for v in _api(studio, "GET", "/api/kernel/variables?kernel=lab/b.ipynb") if v["name"] == "x"] == ["'B'"]
        assert not any(v["name"] == "x" for v in _api(studio, "GET", "/api/kernel/variables"))
        # the Variables card follows the focused notebook
        right.locator(".nb-doc:visible .nb-cell.code textarea").first.click()
        expect(page.locator(".nb-vars-name")).to_have_text("b")
        expect(page.locator(".nb-vars td", has_text="'B'")).to_be_visible()

        # one queue for all notebooks: while a's cell runs, b's cell waits, and the tabs and badges say so
        _api(studio, "POST", "/api/kernel/execute", {"kernel": "lab/a.ipynb", "cells": [{"id": "slow", "code": "import time\ntime.sleep(2)"}]})
        _api(studio, "POST", "/api/kernel/execute", {"kernel": "lab/b.ipynb", "cells": [{"id": "next", "code": "y = 1"}]})
        expect(left.locator(".nb-tab.busy")).to_have_count(1)
        expect(right.locator(".nb-tab.queued")).to_have_count(1)
        expect(right.locator(".nb-doc:visible .badge", has_text="Queued")).to_be_visible()
        page.screenshot(path=os.path.join(SHOTS, "running-dark.png"))
        expect(right.locator(".nb-doc:visible .badge", has_text="Kernel idle")).to_be_visible(timeout=10000)
        expect(left.locator(".nb-tab.busy")).to_have_count(0)

        # drag a third notebook from the list into the left pane, then a tab across panes
        page.locator(".nb-file[data-path='lab/c.ipynb']").drag_to(left.locator(".nb-tabs"))
        expect(left.locator(".nb-tab .nb-tab-name")).to_have_text(["a", "c"])
        left.locator(".nb-tab", has_text="c").drag_to(right.locator(".nb-tabs"))
        expect(left.locator(".nb-tab .nb-tab-name")).to_have_text(["a"])
        expect(right.locator(".nb-tab .nb-tab-name")).to_have_text(["b", "c"])
        right.locator(".nb-tab", has_text="b").click()
        right.locator(".nb-doc:visible .nb-cell.code textarea").first.click()
        page.keyboard.press("Control+Enter")  # run without moving on
        expect(right.locator(".nb-doc:visible .nb-cell.code .nb-gutter").first).to_have_text("[3]")  # b ran x = ..., then y = 1, now this

        # the layout survives a reload
        time.sleep(0.5)
        page.reload()
        page.wait_for_selector(".nb-pane[data-pane='1'] .nb-tab")
        expect(left.locator(".nb-tab .nb-tab-name")).to_have_text(["a"])
        expect(right.locator(".nb-tab .nb-tab-name")).to_have_text(["b", "c"])
        expect(right.locator(".nb-tab.active .nb-tab-name")).to_have_text("b")
        expect(left.locator(".nb-doc:visible .nb-out.stream").first).to_have_text("out A")
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, "split-dark.png"))
        page.emulate_media(color_scheme="light")
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "split-light.png"))

        # a narrow window stacks the panes
        page.set_viewport_size({"width": 860, "height": 900})
        page.wait_for_timeout(300)
        assert left.bounding_box()["y"] < right.bounding_box()["y"]
        assert abs(left.bounding_box()["x"] - right.bounding_box()["x"]) < 2
        page.screenshot(path=os.path.join(SHOTS, "stacked-light.png"))
        page.emulate_media(color_scheme="dark")
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "stacked-dark.png"))
        page.set_viewport_size({"width": 1500, "height": 900})

        # closing the last tab of a pane collapses the split; closing a notebook releases its kernel
        left.locator(".nb-tab", has_text="a").locator(".nb-tab-close").click()
        expect(right).to_be_hidden()
        expect(left.locator(".nb-tab .nb-tab-name")).to_have_text(["b", "c"])
        held = {k["kernel"]: k["holders"] for k in _api(studio, "GET", "/api/kernels")}
        for _ in range(30):
            if held.get("lab/a.ipynb", 0) == 0:
                break
            time.sleep(0.2)
            held = {k["kernel"]: k["holders"] for k in _api(studio, "GET", "/api/kernels")}
        assert held.get("lab/a.ipynb", 0) == 0 and held.get("lab/b.ipynb") == 1, held
        page.screenshot(path=os.path.join(SHOTS, "tabs-dark.png"))
        assert not errors, errors
        browser.close()
