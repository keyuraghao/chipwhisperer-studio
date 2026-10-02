"""Notebook kernel, IPython syntax, ChipWhisperer stand-ins, notebook files, notes and calculator (simulator only)."""
import json
import os
import tempfile

import pytest
from starlette.testclient import TestClient

from cwstudio import notebook, tools
from cwstudio.app import create_app
from cwstudio.session import Session

KEY = "2b7e151628aed2a6abf7158809cf4f3c"


@pytest.fixture(scope="module")
def client():
    session = Session(simulate=True, data_dir=tempfile.mkdtemp())
    app = create_app(session)
    with TestClient(app) as c:
        c.session = session
        yield c


def run(client, code, path=None):
    r = client.post("/api/kernel/run", json={"code": code, "path": path, "timeout": 60})
    assert r.status_code == 200, r.text
    return r.json()


def text(res, name="stdout"):
    return "".join(o.get("text", "") for o in res["outputs"] if o["output_type"] == "stream" and o["name"] == name)


def result(res):
    return next(o["data"]["text/plain"] for o in res["outputs"] if o["output_type"] == "execute_result")


def test_transform_and_expand():
    src = "for i in range(2):\n    !echo {i}\nfiles = !ls\n%cd /tmp\nlen?"
    out = notebook.transform(src)
    assert "    __studio_shell__('echo {i}', capture=False)" in out
    assert "files = __studio_shell__('ls', capture=True)" in out
    assert "__studio_magic__('cd', '/tmp')" in out
    assert "help(len)" in out
    assert notebook.split_args('"../../Setup Scripts/a.ipynb" -s \'x y\' C:\\fw\\b.hex') == ["../../Setup Scripts/a.ipynb", "-s", "x y", "C:\\fw\\b.hex"]
    ns = {"PLATFORM": "CWLITEARM", "n": 3}
    assert notebook.expand("make PLATFORM={PLATFORM} -j{n+1} $n {{x}}", ns) == "make PLATFORM=CWLITEARM -j4 3 {x}"


def test_results_state_and_errors(client):
    r = run(client, "a = 20\na * 2 + 2")
    assert r["ok"] and result(r) == "42"
    assert result(run(client, "a")) == "20"
    r = run(client, "1/0")
    assert not r["ok"] and r["outputs"][-1]["ename"] == "ZeroDivisionError"
    assert result(run(client, "a + 1")) == "21"  # state survives errors


def test_tutorial_style_capture_reaches_trace_store(client):
    session = client.session
    session.store.clear()
    r = run(client, f"""
import chipwhisperer as cw
try:
    if not scope.connectStatus:
        scope.con()
except NameError:
    scope = cw.scope()
target = cw.target(scope, cw.targets.SimpleSerial2)
key = bytearray.fromhex('{KEY}')
target.set_key(key)
from tqdm.notebook import trange
for i in trange(12):
    text = bytearray(os.urandom(16)) if 'os' in dir() else bytearray(range(i, i + 16))
    scope.arm()
    target.simpleserial_write('p', text)
    scope.capture()
    wave = scope.get_last_trace()
    target.simpleserial_read('r', 16)
for i in range(8):
    cw.capture_trace(scope, target, bytearray(range(16)), key)
print(isinstance(scope, type(studio.session.scope)), len(studio.traces))
""")
    assert r["ok"], r["outputs"]
    assert text(r).strip().endswith("True 20"), r["outputs"]
    kinds = [o["name"] for o in r["outputs"] if o["output_type"] == "stream"]
    assert kinds == ["stdout", "stderr", "stdout"], kinds  # progress bar stays between the two prints
    assert len(session.store) == 20
    wave, tin, tout, key = session.store.get(0)
    assert key.hex() == KEY and len(tin) == 16 and len(tout) == 16 and len(wave) > 100
    assert session.target.aes_encrypt(tin) == tout if hasattr(session.target, "aes_encrypt") else True


def test_shell_magics_and_display(client, tmp_path):
    r = run(client, "x = 7\n%%writefile notes.txt\nhello")
    assert not r["ok"]  # a cell magic must start the cell
    run(client, "x = 7")
    r = run(client, "%%writefile " + str(tmp_path / "f.txt") + "\nhello {x}")
    assert r["ok"] and (tmp_path / "f.txt").read_text() == "hello {x}\n"
    r = run(client, "!echo value {x*6} $x")
    assert "value 42 7" in text(r)
    r = run(client, '%%bash -s "{x}" "two words"\necho "1=$1 2=$2"')
    assert "1=7 2=two words" in text(r)
    r = run(client, "lines = !printf 'a\\nb\\n'\nlines")
    assert result(r) == "['a', 'b']"
    r = run(client, "from IPython.display import HTML, display\ndisplay(HTML('<b>hi</b>'))")
    assert r["outputs"][0]["data"]["text/html"] == "<b>hi</b>"
    r = run(client, "import matplotlib.pyplot as plt\nplt.plot([1, 3, 2])\nNone")
    assert any("image/png" in o.get("data", {}) for o in r["outputs"])


def test_run_other_notebook_and_program_in_simulator(client, tmp_path):
    store = client.session.notebooks
    store.save("lib/setup.ipynb", {"cells": [{"cell_type": "code", "source": "SETUP_DONE = 123"}]})
    hexfile = tmp_path / "fw.hex"
    hexfile.write_text(":00000001FF\n")
    r = run(client, f'%run "setup.ipynb"\nprint(SETUP_DONE)\ncw.program_target(scope, cw.programmers.STM32FProgrammer, r"{hexfile}")', path="lib/main.ipynb")
    assert r["ok"], r["outputs"]
    assert "123" in text(r) and "Simulator: pretending to program fw.hex" in text(r)


def test_notebook_files_api(client):
    r = client.post("/api/notebooks/new", json={"name": "My test"}).json()
    assert r["path"] == "My test.ipynb"
    nb = client.get("/api/notebooks/file", params={"path": r["path"]}).json()
    assert nb["cells"] and all("id" in c for c in nb["cells"])
    nb["cells"] = [{"cell_type": "code", "source": "print('saved')", "outputs": [], "execution_count": None}]
    assert client.put("/api/notebooks/file", json={"path": r["path"], "notebook": nb}).status_code == 200
    ran = client.post("/api/notebooks/run", json={"path": r["path"]}).json()
    assert ran["ok"] and ran["cells_run"] == 1
    on_disk = json.load(open(os.path.join(client.session.notebooks.root, r["path"])))
    assert on_disk["nbformat"] == 4 and "".join(on_disk["cells"][0]["outputs"][0]["text"]) == "saved\n"
    up = client.post("/api/notebooks/import", files={"file": ("lab.ipynb", json.dumps(on_disk).encode(), "application/json")}).json()
    assert up["path"] == "imported/lab.ipynb"
    assert any(n["path"] == "imported/lab.ipynb" for n in client.get("/api/notebooks").json()["notebooks"])
    assert client.get("/api/notebooks/file", params={"path": "../../etc/passwd"}).status_code == 400
    assert client.delete("/api/notebooks/file", params={"path": "imported/lab.ipynb"}).json()["ok"]


def test_interrupt_and_restart(client):
    import threading
    import time
    threading.Timer(1.0, lambda: client.post("/api/kernel/interrupt")).start()
    t0 = time.time()
    r = run(client, "import time\nwhile True:\n    time.sleep(0.02)")
    assert not r["ok"] and r["outputs"][-1]["ename"] == "KeyboardInterrupt" and time.time() - t0 < 10
    assert client.post("/api/kernel/restart").json()["execution_count"] == 0
    r = run(client, "a")
    assert not r["ok"] and r["outputs"][-1]["ename"] == "NameError"


def test_notes_api(client):
    n = client.post("/api/notes", json={"name": "Lab"}).json()
    assert n["name"] == "Lab.md"
    client.put("/api/notes/Lab.md", json={"text": "key 2b7e"})
    assert client.get("/api/notes/Lab.md").json()["text"] == "key 2b7e"
    r = client.put("/api/notes/Lab.md", json={"text": "renamed", "rename": "Lab 2.md"}).json()
    assert r["name"] == "Lab 2.md"
    assert any(x["name"] == "Lab 2.md" for x in client.get("/api/notes").json())
    assert client.delete("/api/notes/Lab 2.md").json()["ok"]


def test_calculator_and_stats(client):
    c = lambda e: client.post("/api/calc", json={"expr": e}).json()  # noqa: E731
    assert c("0x2b ^ 0x7e")["value"] == 0x55
    assert c("hw(0xff) + sbox(0)")["value"] == 8 + 0x63
    assert c("mean(1, 2, 3, 10)")["value"] == 4
    assert c("x = 2**10")["variable"] == "x" and c("x + ans")["value"] == 2048
    assert client.post("/api/calc", json={"expr": "__import__('os')"}).status_code == 400
    assert client.post("/api/calc", json={"expr": "(1).__class__"}).status_code == 400
    assert client.post("/api/calc", json={"expr": "9**999999"}).status_code == 400
    st = client.post("/api/calc/stats", json={"values": [1, 2, 3, 4]}).json()
    assert st["mean"] == 2.5 and st["median"] == 2.5 and st["pk_pk"] == 3 and st["count"] == 4
    st = client.post("/api/calc/stats", json={"source": "trace", "index": 0, "start": 10, "end": 20}).json()
    assert st["count"] == 10
    st = client.post("/api/calc/stats", json={"source": "sample", "sample": 5}).json()
    assert st["count"] == len(client.session.store)


def test_stats_helper():
    st = tools.stats([2, 4, 4, 4, 5, 5, 7, 9])
    assert st["mean"] == 5 and round(st["std"], 4) == 2.1381 and st["rms"] > 5


def test_cw_plot_and_plt_show(client):
    run(client, "import chipwhisperer as cw\nimport numpy as np")
    r = run(client, "p = cw.plot(np.sin(np.arange(100) / 5)) * cw.plot(np.cos(np.arange(100) / 5))\np")
    assert r["ok"] and "image/png" in r["outputs"][-1]["data"], r["outputs"]
    r = run(client, "fig = cw.plot()\nfor k in range(3):\n    fig = fig * cw.plot(np.arange(10) * k)\nfig")
    assert r["ok"] and "image/png" in r["outputs"][-1]["data"]
    r = run(client, "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.show()\nprint('after show')")
    kinds = [o["output_type"] for o in r["outputs"]]
    assert kinds.index("display_data") < kinds.index("stream"), kinds  # figure shown before the later print


# ----- one kernel per notebook ---------------------------------------------------------
def run_in(client, kernel, code, timeout=60):
    r = client.post("/api/kernel/run", json={"code": code, "kernel": kernel, "timeout": timeout})
    assert r.status_code == 200, r.text
    return r.json()


def wait_idle(m, timeout=30):
    import time
    end = time.time() + timeout
    while time.time() < end:
        with m.lock:
            if m.current is None and not m.pending:
                return
        time.sleep(0.02)
    raise TimeoutError("kernels still busy")


def test_separate_namespaces(client):
    assert result(run_in(client, "ns/a.ipynb", "x = 'a'\nx")) == "'a'"
    assert result(run_in(client, "ns/b.ipynb", "x = 'b'\nx")) == "'b'"
    assert result(run_in(client, "ns/a.ipynb", "x")) == "'a'"
    r = run(client, "x")  # no kernel: the shared default kernel, which never saw x
    assert not r["ok"] and r["outputs"][-1]["ename"] == "NameError"
    a = client.get("/api/kernel", params={"kernel": "ns/a.ipynb"}).json()
    b = client.get("/api/kernel", params={"kernel": "ns/b.ipynb"}).json()
    assert a["kernel"] == "ns/a.ipynb" and a["path"] == "ns/a.ipynb" and a["started"] and a["execution_count"] == 2 and b["execution_count"] == 1
    assert [v["name"] for v in client.get("/api/kernel/variables", params={"kernel": "ns/b.ipynb"}).json()] == ["x"]
    assert client.get("/api/kernel", params={"kernel": "ns/never.ipynb"}).json()["started"] is False  # status does not create kernels
    assert client.get("/api/kernel/variables", params={"kernel": "ns/never.ipynb"}).json() == []
    assert "ns/never.ipynb" not in client.session.kernels.kernels
    ids = {k["kernel"] for k in client.get("/api/kernels").json()}
    assert {"default", "ns/a.ipynb", "ns/b.ipynb"} <= ids
    # both share Studio's hardware connection and trace store
    for k in ("ns/a.ipynb", "ns/b.ipynb"):
        r = run_in(client, k, "import chipwhisperer as cw\nscope = cw.scope()\nisinstance(scope, type(studio.session.scope))")
        assert result(r) == "True", r["outputs"]
    assert client.session.kernels.get("ns/a.ipynb").ns["studio"].session is client.session.kernels.get("ns/b.ipynb").ns["studio"].session


def test_per_notebook_restart_and_interrupt(client):
    import time
    m = client.session.kernels
    run_in(client, "ri/a.ipynb", "keep = 1")
    run_in(client, "ri/b.ipynb", "gone = 2")
    st = client.post("/api/kernel/restart", json={"kernel": "ri/b.ipynb"}).json()
    assert st["kernel"] == "ri/b.ipynb" and st["execution_count"] == 0
    assert not run_in(client, "ri/b.ipynb", "gone")["ok"]
    assert result(run_in(client, "ri/a.ipynb", "keep")) == "1"  # restarting b left a alone
    # a loops forever; b's cell waits in the queue behind it; interrupting a must not cancel b
    client.post("/api/kernel/execute", json={"kernel": "ri/a.ipynb", "cells": [{"id": "loop", "code": "import time\nwhile True:\n    time.sleep(0.02)"}]})
    client.post("/api/kernel/execute", json={"kernel": "ri/b.ipynb", "cells": [{"id": "after", "code": "after = 3"}]})
    for _ in range(200):
        if m.status("ri/a.ipynb")["busy"]:
            break
        time.sleep(0.02)
    st_b = client.get("/api/kernel", params={"kernel": "ri/b.ipynb"}).json()
    assert st_b["queued"] == ["after"] and st_b["running_kernel"] == "ri/a.ipynb" and not st_b["busy"]
    assert client.post("/api/kernel/interrupt").json()["kernel"] == "default"  # no kernel given: only the default kernel, which is idle
    assert m.status("ri/a.ipynb")["busy"]
    client.post("/api/kernel/interrupt", json={"kernel": "ri/a.ipynb"})
    wait_idle(m)
    assert m.status("ri/a.ipynb")["execution_count"] == 3 and result(run_in(client, "ri/b.ipynb", "after")) == "3"
    assert result(run_in(client, "ri/a.ipynb", "keep")) == "1"  # an interrupt keeps the namespace


def test_global_serial_execution_order(client):
    m = client.session.kernels
    code = "import threading, time\nlog = studio.session.__dict__.setdefault('_order_log', [])\nt0 = time.perf_counter()\ntime.sleep(0.03)\nlog.append(({tag!r}, t0, time.perf_counter(), threading.current_thread().name))"
    order = []
    for i in range(3):
        for k in ("so/a.ipynb", "so/b.ipynb", None):
            tag = f"{k}#{i}"
            order.append(tag)
            assert client.post("/api/kernel/execute", json={"kernel": k, "cells": [{"id": tag, "code": code.format(tag=tag)}]}).status_code == 200
    wait_idle(m)
    log = client.session.__dict__.pop("_order_log")
    assert [x[0] for x in log] == order  # one global FIFO queue across notebooks
    assert all(x[3] == "cw-hardware" for x in log)  # always on Studio's hardware thread
    assert all(log[i][2] <= log[i + 1][1] for i in range(len(log) - 1))  # never two cells at once
    assert m.status("so/a.ipynb")["execution_count"] == 3 and m.status("so/b.ipynb")["execution_count"] == 3


def test_output_events_carry_the_kernel(client, monkeypatch):
    bus = client.session.bus
    seen = []
    orig = bus.publish_event

    def spy(ev):
        if ev.kind == "nb":
            seen.append(dict(ev.payload))
        return orig(ev)
    monkeypatch.setattr(bus, "publish_event", spy)
    run_in(client, "ev/a.ipynb", "print('from a')")
    run_in(client, "ev/b.ipynb", "print('from b')")
    run(client, "print('from default')")
    outs = {}
    for e in seen:
        if e["kind"] == "output":
            outs[(e["kernel"], e["path"])] = outs.get((e["kernel"], e["path"]), "") + e["output"]["text"]
    assert outs == {("ev/a.ipynb", "ev/a.ipynb"): "from a\n", ("ev/b.ipynb", "ev/b.ipynb"): "from b\n", ("default", None): "from default\n"}
    assert {e["kind"] for e in seen} >= {"queued", "running", "output", "done"} and all("kernel" in e for e in seen)
    seen.clear()
    client.post("/api/kernel/restart", json={"kernel": "ev/a.ipynb"})
    assert [(e["kind"], e["kernel"]) for e in seen] == [("restarted", "ev/a.ipynb")]


def test_rename_keeps_kernel_and_delete_frees_it(client):
    m = client.session.kernels
    client.put("/api/notebooks/file", json={"path": "rn/old.ipynb", "notebook": {"cells": []}})
    run_in(client, "rn/old.ipynb", "v = 41")
    r = client.post("/api/notebooks/rename", json={"path": "rn/old.ipynb", "to": "rn/new"}).json()
    assert r["path"] == "rn/new.ipynb" and m.find("rn/old.ipynb") is None
    assert result(run_in(client, "rn/new.ipynb", "v + 1")) == "42"
    assert m.find("rn/new.ipynb").path == "rn/new.ipynb"
    client.put("/api/notebooks/file", json={"path": "rn/other.ipynb", "notebook": {"cells": []}})
    assert client.post("/api/notebooks/rename", json={"path": "rn/new.ipynb", "to": "rn/other.ipynb"}).status_code == 409
    # a temporary id (unsaved notebook) takes its path when saved
    run_in(client, "tmp-123", "w = 5")
    assert client.post("/api/kernels/rename", json={"kernel": "tmp-123", "to": "rn/saved.ipynb"}).json()["execution_count"] == 1
    assert result(run_in(client, "rn/saved.ipynb", "w")) == "5"
    assert client.delete("/api/notebooks/file", params={"path": "rn/new.ipynb"}).json()["ok"]
    assert m.find("rn/new.ipynb") is None


def test_closed_notebooks_release_their_namespace(client):
    import gc
    import weakref
    m = client.session.kernels
    grace = m.release_grace
    try:
        m.release_grace = 0.0
        run_in(client, "mem/a.ipynb", "import numpy as np\nclass Big:\n    data = None\nbig = Big()\nbig.data = np.ones(1 << 20)\ndef keep():\n    return big\nbig")
        run_in(client, "mem/b.ipynb", "y = 1")
        kern = m.find("mem/a.ipynb")
        obj, kref = weakref.ref(kern.ns["big"]), weakref.ref(kern)
        del kern
        # two windows have a open; closing it in one keeps the kernel, closing it in the last frees it
        client.post("/api/kernels/attach", json={"client": "w1", "kernels": ["mem/a.ipynb", "mem/b.ipynb"]})
        client.post("/api/kernels/attach", json={"client": "w2", "kernels": ["mem/a.ipynb"]})
        client.post("/api/kernels/attach", json={"client": "w1", "kernels": ["mem/b.ipynb"]})
        assert m.find("mem/a.ipynb") is not None
        client.post("/api/kernels/attach", json={"client": "w2", "kernels": []})
        assert m.find("mem/a.ipynb") is None and m.find("mem/b.ipynb") is not None
        gc.collect()
        assert obj() is None and kref() is None, gc.get_referrers(obj()) if obj() else "kernel still referenced"
        # with a grace period (page reloads), a quick re-attach keeps the kernel; windows that stop reporting are forgotten
        m.release_grace = 60.0
        client.post("/api/kernels/attach", json={"client": "w1", "kernels": []})
        client.post("/api/kernels/attach", json={"client": "w3", "kernels": ["mem/b.ipynb"]})
        assert m.reap() == [] and result(run_in(client, "mem/b.ipynb", "y")) == "1"
        import time
        later = time.time() + m.client_ttl + 1
        assert m.reap(later) == [] and m.find("mem/b.ipynb") is not None  # w3 is forgotten, the grace period starts
        assert m.reap(later + m.release_grace) == ["mem/b.ipynb"]
        # an explicit shut down, and kernels never opened in a window (API, MCP) stay until then
        run_in(client, "mem/c.ipynb", "z = 1")
        assert m.reap(time.time() + 1e6) == [] and m.find("mem/c.ipynb") is not None
        assert client.post("/api/kernels/shutdown", json={"kernel": "mem/c.ipynb"}).json()["shutdown"]
        assert m.find("mem/c.ipynb") is None and not client.post("/api/kernels/shutdown", json={"kernel": "mem/c.ipynb"}).json()["shutdown"]
        assert client.post("/api/kernels/shutdown", json={}).json() == {"kernel": "default", "shutdown": False, "restarted": True}
    finally:
        m.release_grace = grace


# ----- regressions found in the 0.5 notebook review ------------------------------------
def test_magics_only_where_a_statement_starts(client):
    # a line starting with % or ! inside brackets, a multi-line string or after a backslash is Python, not a magic
    r = run(client, 's = ("abc %d"\n     % 5)\nt = """\n!not shell\n%not magic\n"""\nu = 7 \\\n    % 4\nd = {\n    "k": 1\n}\n!echo shell {u}\n(s, t, u)')
    assert r["ok"], r["outputs"]
    assert result(r) == "('abc 5', '\\n!not shell\\n%not magic\\n', 3)"
    assert "shell 3" in text(r)
    assert "__studio_magic__" not in notebook.transform("x = [1,\n%2]")
    assert "__studio_shell__" in notebook.transform("if True:\n    !ls")


def test_quiet_cell_output_is_sent_without_waiting_for_the_next_print(client, monkeypatch):
    import time
    bus = client.session.bus
    seen = []
    orig = bus.publish_event

    def spy(ev):
        if ev.kind == "nb" and ev.payload.get("kind") == "output":
            seen.append((time.time(), ev.payload["output"].get("text", "")))
        return orig(ev)
    monkeypatch.setattr(bus, "publish_event", spy)
    t0 = time.time()
    run(client, "import time\nprint('a')\nprint('b')\ntime.sleep(1.5)\nprint('c')")
    early = "".join(t for at, t in seen if at - t0 < 1.0)
    assert early == "a\nb\n", seen  # 'b' used to wait for 'c', 1.5 s later


def test_figures_and_svg_format(client):
    r = run(client, "from matplotlib.figure import Figure\nf = Figure()\nf.add_subplot().plot([1, 2])\nf")
    assert r["ok"] and "image/png" in r["outputs"][-1]["data"], r["outputs"]  # a Figure made without pyplot used to show nothing
    r = run(client, "import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.plot([1, 2])\ndisplay(fig)\nplt.close(fig)")
    assert [o["output_type"] for o in r["outputs"]] == ["display_data"] and "image/png" in r["outputs"][0]["data"]
    r = run_in(client, "fig/svg.ipynb", "%config InlineBackend.figure_format = 'svg'\nimport matplotlib.pyplot as plt\nplt.plot([3, 1])\nplt.show()")
    assert r["ok"] and r["outputs"][0]["data"]["image/svg+xml"].lstrip().startswith("<?xml"), r["outputs"]
    r = run_in(client, "fig/svg.ipynb", "from IPython.display import set_matplotlib_formats\nset_matplotlib_formats('png')\nplt.plot([1]); plt.show()")
    assert "image/png" in r["outputs"][0]["data"]
    assert "image/png" in run(client, "plt.plot([1]); plt.show()")["outputs"][0]["data"]  # the format is per kernel


def test_variables_never_touch_hardware_and_survive_a_disconnect(client):
    s = client.session
    run_in(client, "vars/a.ipynb", "import chipwhisperer as cw\nscope = cw.scope()\ntarget = cw.target(scope)\nbig = list(range(10**6))\nreal = studio.session.scope")
    vs = {v["name"]: v for v in client.get("/api/kernel/variables", params={"kernel": "vars/a.ipynb"}).json()}
    assert vs["scope"]["repr"].startswith("<Studio's scope") and vs["target"]["repr"].startswith("<Studio's target")
    assert len(vs["big"]["repr"]) <= 120 and vs["big"]["repr"].endswith("...]")
    assert vs["real"]["repr"] == f"<{type(s.scope).__module__}.{type(s.scope).__name__}>" or not type(s.scope).__module__.startswith("chipwhisperer")
    s.worker.call(s.disconnect_target)
    s.worker.call(s.disconnect_scope)
    try:
        r = client.get("/api/kernel/variables", params={"kernel": "vars/a.ipynb"})
        assert r.status_code == 200, r.text  # used to fail: the scope stand-in raised from isinstance()
        assert {v["name"]: v["repr"] for v in r.json()}["scope"] == "<Studio's scope: not connected>"
        assert client.get("/api/kernels").status_code == 200
    finally:
        run(client, "import chipwhisperer as cw\nscope = cw.scope()\ntarget = cw.target(scope)")


def test_waiting_run_returns_when_its_cell_is_cancelled(client):
    import threading
    import time
    m = client.session.kernels
    client.post("/api/kernel/execute", json={"kernel": "cx/a.ipynb", "cells": [{"id": "spin", "code": "import time\nwhile True:\n    time.sleep(0.01)"}]})
    res = {}
    th = threading.Thread(target=lambda: res.update(run_in(client, "cx/b.ipynb", "1", timeout=30)))
    th.start()
    for _ in range(200):
        if m.status("cx/b.ipynb")["queued"]:
            break
        time.sleep(0.01)
    t0 = time.time()
    client.post("/api/kernel/interrupt", json={"kernel": "cx/b.ipynb"})
    th.join(10)
    assert not th.is_alive() and time.time() - t0 < 5, "the waiting call hung until its timeout"
    assert res["ok"] is False and res.get("cancelled") and res["outputs"][0]["ename"] == "Cancelled"
    client.post("/api/kernel/interrupt", json={"kernel": "cx/a.ipynb"})
    wait_idle(m)


def test_queued_event_comes_before_running(client, monkeypatch):
    bus = client.session.bus
    seen = []
    orig = bus.publish_event

    def spy(ev):
        if ev.kind == "nb" and ev.payload.get("cell", "").startswith("ord"):
            seen.append((ev.payload["cell"], ev.payload["kind"]))
        return orig(ev)
    monkeypatch.setattr(bus, "publish_event", spy)
    for i in range(20):
        client.post("/api/kernel/execute", json={"kernel": "ord.ipynb", "cells": [{"id": f"ord{i}", "code": "pass"}]})
    wait_idle(client.session.kernels)
    for i in range(20):
        kinds = [k for c, k in seen if c == f"ord{i}"]
        assert kinds[:2] == ["queued", "running"] and kinds[-1] == "done", (i, kinds)


def test_cd_lasts_until_restart(client, tmp_path):
    (tmp_path / "sub").mkdir()
    run_in(client, "cd/a.ipynb", f"%cd {tmp_path / 'sub'}")
    assert result(run_in(client, "cd/a.ipynb", "import os\nos.getcwd()")) == repr(str(tmp_path / "sub"))
    assert result(run_in(client, "cd/b.ipynb", "import os\nos.getcwd()")) == repr(os.path.join(client.session.notebooks.root, "cd"))  # other notebooks keep their folder
    client.post("/api/kernel/restart", json={"kernel": "cd/a.ipynb"})
    assert result(run_in(client, "cd/a.ipynb", "import os\nos.getcwd()")) == repr(os.path.join(client.session.notebooks.root, "cd"))


@pytest.mark.skipif(os.name == "nt", reason="uses sleep from a POSIX shell")
def test_stop_kills_a_silent_shell_command(client):
    import threading
    import time
    threading.Timer(0.8, lambda: client.post("/api/kernel/interrupt", json={"kernel": "sh.ipynb"})).start()
    t0 = time.time()
    r = run_in(client, "sh.ipynb", "!sleep 20")
    assert not r["ok"] and r["outputs"][-1]["ename"] == "KeyboardInterrupt" and time.time() - t0 < 8


def test_shutdown_does_not_wait_for_a_busy_hardware_thread(client):
    import time
    m = client.session.kernels
    run_in(client, "sd/b.ipynb", "x = 1")
    client.post("/api/kernel/execute", json={"kernel": "sd/a.ipynb", "cells": [{"id": "spin", "code": "import time\nwhile True:\n    time.sleep(0.01)"}]})
    for _ in range(200):
        if m.status("sd/a.ipynb")["busy"]:
            break
        time.sleep(0.01)
    t0 = time.time()
    assert client.post("/api/kernels/shutdown", json={"kernel": "sd/b.ipynb"}).json()["shutdown"]
    assert time.time() - t0 < 3  # used to wait 10 s for the hardware thread
    client.post("/api/kernel/interrupt", json={"kernel": "sd/a.ipynb"})
    wait_idle(m)


def test_saves_refuse_stale_or_deleted_files_and_announce_changes(client, monkeypatch):
    bus = client.session.bus
    seen = []
    orig = bus.publish_event

    def spy(ev):
        if ev.kind == "nb" and ev.payload.get("kind") == "file":
            seen.append(dict(ev.payload))
        return orig(ev)
    monkeypatch.setattr(bus, "publish_event", spy)
    path = "sync/a.ipynb"
    assert client.put("/api/notebooks/file", json={"path": path, "notebook": {"cells": [{"cell_type": "code", "source": "1"}]}}).status_code == 200  # no base_mtime: plain overwrite, as before
    nb = client.get("/api/notebooks/file", params={"path": path}).json()
    base = nb["mtime"]
    assert base and seen[-1] == {"kind": "file", "action": "saved", "path": path, "mtime": base, "client": None}
    r = client.put("/api/notebooks/file", json={"path": path, "notebook": nb, "base_mtime": base, "client": "w1"})
    assert r.status_code == 200 and seen[-1]["client"] == "w1"
    import time
    time.sleep(0.01)
    client.put("/api/notebooks/file", json={"path": path, "notebook": {"cells": [{"cell_type": "code", "source": "agent"}]}})
    stale = client.put("/api/notebooks/file", json={"path": path, "notebook": nb, "base_mtime": r.json()["mtime"], "client": "w1"})
    assert stale.status_code == 409 and stale.json()["detail"].startswith("NotebookConflict")
    assert client.get("/api/notebooks/file", params={"path": path}).json()["cells"][0]["source"] == "agent"  # not overwritten
    assert client.delete("/api/notebooks/file", params={"path": path}).json()["ok"]
    assert seen[-1] == {"kind": "file", "action": "deleted", "path": path}
    gone = client.put("/api/notebooks/file", json={"path": path, "notebook": nb, "base_mtime": base, "client": "w1"})
    assert gone.status_code == 410 and gone.json()["detail"].startswith("NotebookDeleted")
    assert not os.path.exists(client.session.notebooks.path(path))  # autosave never brings a deleted notebook back


def test_notebook_run_keeps_edits_saved_meanwhile_and_does_not_recreate(client):
    import threading
    import time
    store = client.session.notebooks
    path = "sync/run.ipynb"
    store.save(path, {"cells": [{"id": "c1", "cell_type": "code", "source": "import time\ntime.sleep(0.8)\nprint('ran')"}, {"id": "m1", "cell_type": "markdown", "source": "old"}]})
    th = threading.Thread(target=lambda: client.post("/api/notebooks/run", json={"path": path}))
    th.start()
    time.sleep(0.3)
    nb = store.load(path)
    nb["cells"][1]["source"] = "edited while running"
    store.save(path, nb)
    th.join(30)
    out = store.load(path)
    assert out["cells"][1]["source"] == "edited while running" and "ran" in out["cells"][0]["outputs"][0]["text"]
    th = threading.Thread(target=lambda: client.post("/api/notebooks/run", json={"path": path}))
    th.start()
    time.sleep(0.3)
    client.delete("/api/notebooks/file", params={"path": path})
    th.join(30)
    assert not os.path.exists(store.path(path))
