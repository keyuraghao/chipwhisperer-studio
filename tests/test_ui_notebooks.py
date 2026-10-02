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


def test_notebook_ui_regressions(studio):
    """Ctrl+S saves only the focused notebook, long outputs stay fast, cells are compact, and a finished tutorial download does not reload the list forever."""
    from playwright.sync_api import Error, expect, sync_playwright
    _api(studio, "PUT", "/api/notebooks/file", {"path": "reg/long.ipynb", "notebook": {"cells": [{"cell_type": "code", "source": "for i in range(150000):\n    print('line', i)"}, {"cell_type": "code", "source": "x = 1"}]}})
    _api(studio, "PUT", "/api/notebooks/file", {"path": "reg/other.ipynb", "notebook": {"cells": [{"cell_type": "code", "source": "y = 2"}]}})
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # the tutorials job keeps saying "installed" after a download; the list used to reload itself in a loop from then on
        lists = []

        def tutorials_installed(route):
            lists.append(1)
            r = route.fetch()
            body = r.json()
            body["tutorials"]["job"] = {"state": "installed", "done": 1, "total": 1, "error": None}
            route.fulfill(response=r, json=body)
        page.route("**/api/notebooks", tutorials_installed)
        page.goto(studio)
        page.evaluate("localStorage.clear()")
        page.reload()
        page.click("#tabs .tab[data-tab=notebook]")
        page.wait_for_timeout(1500)
        assert len(lists) <= 4, f"/api/notebooks was requested {len(lists)} times"
        page.unroute("**/api/notebooks")

        page.locator("details.nb-folder > summary", has_text="reg").click()
        page.click(".nb-file[data-path='reg/long.ipynb']")
        page.click(".nb-file[data-path='reg/other.ipynb']")
        left, right = page.locator(".nb-pane[data-pane='0']"), page.locator(".nb-pane[data-pane='1']")
        left.locator(".nb-split").click()
        expect(right.locator(".nb-tab .nb-tab-name")).to_have_text(["other"])
        # a one-line cell is not as tall as the cell buttons stacked up
        assert right.locator(".nb-doc:visible .nb-cell.code").first.bounding_box()["height"] < 80

        # Ctrl+S saves the focused notebook only
        left.locator(".nb-doc:visible .nb-cell.code textarea").nth(1).click()
        page.keyboard.type("  # left")
        right.locator(".nb-doc:visible .nb-cell.code textarea").first.click()
        page.keyboard.type("  # right")
        page.keyboard.press("Control+s")
        expect(right.locator(".nb-doc:visible .nb-dirty")).to_have_text("saved")
        expect(left.locator(".nb-doc:visible .nb-dirty")).to_have_text("unsaved")
        assert _api(studio, "GET", "/api/notebooks/file?path=reg/other.ipynb")["cells"][0]["source"] == "y = 2  # right"
        assert _api(studio, "GET", "/api/notebooks/file?path=reg/long.ipynb")["cells"][1]["source"] == "x = 1"

        # the waveform's single-key capture shortcuts do not fire in the Notebook tab (Esc leaves a cell, then r used to start a 1000 trace capture)
        _api(studio, "POST", "/api/kernel/run", {"code": "import chipwhisperer as cw\nscope = cw.scope()\ntarget = cw.target(scope)"})
        before = _api(studio, "GET", "/api/status")["traces"]["count"]
        right.locator(".nb-doc:visible .nb-cell.code textarea").first.click()
        page.keyboard.press("Escape")
        assert page.evaluate("document.activeElement.id") == "nb-view"  # the cell is left, the notebook keeps the focus
        page.keyboard.press("r")
        right.locator(".nb-doc:visible button", has_text="Clear outputs").focus()
        page.keyboard.press("s")
        page.wait_for_timeout(800)
        st = _api(studio, "GET", "/api/status")
        assert st["traces"]["count"] == before and not (st.get("job") or {}).get("running"), st.get("job")

        # a cell printing 150k lines: the page shows the end of it and stays responsive (it used to re-render the whole text for every chunk)
        left.locator(".nb-doc:visible .nb-cell.code textarea").first.click()
        t0 = time.time()
        page.keyboard.press("Control+Enter")
        expect(left.locator(".nb-doc:visible .badge", has_text="Kernel idle")).to_be_visible(timeout=60000)
        out = left.locator(".nb-doc:visible .nb-out.stream").first
        expect(out).to_contain_text("line 149999")
        assert time.time() - t0 < 20
        text = out.inner_text()
        assert len(text) < 120000 and text.startswith("[") and "earlier lines not shown" in text.splitlines()[0]
        assert not errors, errors
        browser.close()


def test_tabs_follow_changes_from_other_windows_and_runs(studio):
    """A notebook run from the API refreshes the open tab; a change made elsewhere while a tab has unsaved edits shows a conflict notice instead of being overwritten; deleting or renaming in one window closes or renames the tab in the others, and autosave never recreates a deleted notebook."""
    from playwright.sync_api import Error, expect, sync_playwright
    for name in ("run", "edit", "gone", "move"):
        _api(studio, "PUT", "/api/notebooks/file", {"path": f"sync/{name}.ipynb", "notebook": {"cells": [{"cell_type": "code", "source": f"print('{name}')"}]}})
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        errors = []
        windows = []
        for _ in range(2):
            page = browser.new_context(viewport={"width": 1300, "height": 850}).new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(studio)
            page.click("#tabs .tab[data-tab=notebook]")
            page.locator("details.nb-folder > summary", has_text="sync").click()
            for name in ("run", "edit", "gone", "move"):
                page.click(f".nb-file[data-path='sync/{name}.ipynb']")
            expect(page.locator(".nb-pane[data-pane='0'] .nb-tab")).to_have_count(4)
            windows.append(page)
        a, b = windows
        tab = lambda page, name: page.locator(".nb-pane[data-pane='0'] .nb-tab", has_text=name)  # noqa: E731
        doc = lambda page: page.locator(".nb-pane[data-pane='0'] .nb-doc:visible")  # noqa: E731

        # a run from the API (notebook_run in MCP) shows its outputs in the open tab of every window
        tab(a, "run").click()
        _api(studio, "POST", "/api/notebooks/run", {"path": "sync/run.ipynb"})
        expect(doc(a).locator(".nb-out.stream")).to_have_text("run")
        expect(doc(a).locator(".nb-gutter").first).to_have_text("[1]")

        # unsaved edits plus a change from elsewhere: a notice, and the agent's version stays on disk
        tab(b, "edit").click()
        doc(b).locator("textarea").first.click()
        b.keyboard.type("  # mine")
        _api(studio, "PUT", "/api/notebooks/file", {"path": "sync/edit.ipynb", "notebook": {"cells": [{"cell_type": "code", "source": "print('agent')"}]}})
        notice = doc(b).locator(".nb-notice")
        expect(notice).to_be_visible()
        b.wait_for_timeout(3000)  # longer than the autosave delay
        assert _api(studio, "GET", "/api/notebooks/file?path=sync/edit.ipynb")["cells"][0]["source"] == "print('agent')"
        notice.locator("button", has_text="Reload from disk").click()
        expect(doc(b).locator("textarea").first).to_have_value("print('agent')")
        expect(notice).to_be_hidden()
        # the other window had no edits: it just reloaded
        tab(a, "edit").click()
        expect(doc(a).locator("textarea").first).to_have_value("print('agent')")

        # delete in a, the tab closes in b; a tab with unsaved edits keeps them but never recreates the file
        tab(b, "gone").click()
        doc(b).locator("textarea").first.click()
        b.keyboard.type("  # unsaved")
        a.once("dialog", lambda d: d.accept())
        f = a.locator(".nb-file[data-path='sync/gone.ipynb']")
        f.hover()
        f.locator("button").click()
        expect(tab(a, "gone")).to_have_count(0)
        expect(doc(b).locator(".nb-notice")).to_contain_text("deleted")
        b.wait_for_timeout(3000)
        assert not any(n["path"] == "sync/gone.ipynb" for n in _api(studio, "GET", "/api/notebooks")["notebooks"])
        doc(b).locator(".nb-notice button", has_text="Close").click()
        expect(tab(b, "gone")).to_have_count(0)

        # rename in a, the tab follows in b
        a.once("dialog", lambda d: d.accept("sync/moved"))
        tab(a, "move").dblclick()
        expect(tab(a, "moved")).to_have_count(1)
        expect(tab(b, "moved")).to_have_count(1)
        expect(b.locator(".nb-pane[data-pane='0'] .nb-tab .nb-tab-name")).to_have_text(["run", "edit", "moved"])
        # a deleted notebook without unsaved edits simply closes in the other window
        _api(studio, "DELETE", "/api/notebooks/file?path=sync/run.ipynb")
        expect(b.locator(".nb-pane[data-pane='0'] .nb-tab .nb-tab-name")).to_have_text(["edit", "moved"])
        expect(a.locator(".nb-pane[data-pane='0'] .nb-tab .nb-tab-name")).to_have_text(["edit", "moved"])
        assert not errors, errors
        browser.close()


def test_tutorial_download_asks_before_replacing_edits(studio):
    """Download tutorials asks before replacing tutorial files the user changed, and sends the chosen answer back."""
    from playwright.sync_api import Error, expect, sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Error as e:
            pytest.skip(f"Chromium for Playwright is not installed: {e}")
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        bodies = []
        base = {"installed": {"repo": "newaetech/chipwhisperer-jupyter", "commit": "a" * 40}, "folder": "chipwhisperer-jupyter", "firmware_linked": True}

        def fetch(route):
            body = json.loads(route.request.post_data or "{}")
            bodies.append(body)
            if body.get("on_modified"):
                job = {"state": "installed", "done": 1, "total": 1, "error": None, "kept": [], "backups": ["courses/lab1.local-20261002-120000.ipynb"]}
            else:
                job = {"state": "confirm", "done": 1, "total": 1, "error": None, "conflicts": ["courses/lab1.ipynb", "courses/lab2.ipynb"]}
            route.fulfill(json=dict(base, job=job))
        page.route("**/api/notebooks/tutorials/fetch", fetch)
        page.goto(studio)
        page.click("#tabs .tab[data-tab=notebook]")
        ask = page.locator(".nb-tut-ask")
        expect(ask).to_be_hidden()
        page.get_by_role("button", name="Download tutorials").click()
        expect(ask).to_be_visible()
        expect(ask).to_contain_text("You changed 2 tutorial files")
        expect(ask.locator("li")).to_have_text(["courses/lab1.ipynb", "courses/lab2.ipynb"])
        ask.get_by_role("button", name="Cancel").click()
        expect(ask).to_be_hidden()
        page.get_by_role("button", name="Download tutorials").click()
        ask.get_by_role("button", name="Update, back up mine").click()
        expect(ask).to_be_hidden()
        expect(page.locator(".toast", has_text="lab1.local-20261002-120000.ipynb")).to_be_visible()
        assert bodies == [{}, {}, {"on_modified": "backup"}]
        assert not errors, errors
        browser.close()
