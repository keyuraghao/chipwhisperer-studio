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
