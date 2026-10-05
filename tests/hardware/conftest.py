"""Real-hardware session for ChipWhisperer Studio: skipped unless CWSTUDIO_HW is set (husky or sim).

Env: CWSTUDIO_HW (husky|sim), CWSTUDIO_HW_SN, CWSTUDIO_HW_PLATFORM (default CWHUSKY), CWSTUDIO_HW_PROGRAMMER, CWSTUDIO_HW_SKIP_PROGRAM=1, CWSTUDIO_HW_MANUAL=1, CWSTUDIO_HW_DATA (Studio data folder for the session).
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

import pytest

HW = os.environ.get("CWSTUDIO_HW", "").strip().lower()
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SUMMARY = os.path.join(HERE, "last_run.json")

if HW not in ("husky", "sim"):
    collect_ignore_glob = ["test_*.py"]  # normal runs never import the hardware stages

STAGES = ["environment", "discovery", "scope", "firmware", "target", "capture", "analysis", "glitch", "husky", "notebook", "mcp", "robustness", "disconnect"]
# a stage that fails stops the stages that depend on it
DEPENDS = {"discovery": ["environment"], "scope": ["environment"], "firmware": ["scope"], "target": ["scope"], "capture": ["target"], "analysis": ["capture"],
           "glitch": ["target"], "husky": ["scope"], "notebook": ["scope"], "mcp": ["scope"], "robustness": ["scope"], "disconnect": []}

_results = {"mode": HW, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "env": {}, "stages": {}}
_failed = set()


def cfg():
    return {"hw": HW, "sn": os.environ.get("CWSTUDIO_HW_SN") or None, "platform": os.environ.get("CWSTUDIO_HW_PLATFORM") or "CWHUSKY",
            "programmer": os.environ.get("CWSTUDIO_HW_PROGRAMMER") or None, "skip_program": os.environ.get("CWSTUDIO_HW_SKIP_PROGRAM") == "1",
            "manual": os.environ.get("CWSTUDIO_HW_MANUAL") == "1"}


def stage_of(item):
    m = item.get_closest_marker("stage")
    return m.args[0] if m else None


def pytest_configure(config):
    config.addinivalue_line("markers", "stage(name): hardware session stage")


def pytest_collection_modifyitems(config, items):
    order = {s: i for i, s in enumerate(STAGES)}
    items.sort(key=lambda it: order.get(stage_of(it) or "", 99))


def pytest_runtest_setup(item):
    st = stage_of(item)
    if st is None:
        return
    for dep in DEPENDS.get(st, []):
        if dep in _failed:
            pytest.skip(f"stage {dep} failed earlier, so {st} cannot run")


def pytest_runtest_makereport(item, call):
    st = stage_of(item)
    if st is None or call.when != "call" and not (call.when == "setup" and call.excinfo is not None):
        return
    rec = _results["stages"].setdefault(st, {"tests": {}, "measurements": {}})
    if call.excinfo is None:
        outcome = "passed"
    elif call.excinfo.errisinstance(pytest.skip.Exception):
        outcome = "skipped: " + str(call.excinfo.value)
    else:
        outcome = "failed: " + str(call.excinfo.value).splitlines()[0][:300] if str(call.excinfo.value) else "failed"
        _failed.add(st)
    rec["tests"][item.name] = outcome


def pytest_sessionfinish(session, exitstatus):
    if HW not in ("husky", "sim"):
        return
    for st in _results["stages"].values():
        outs = list(st["tests"].values())
        st["result"] = "failed" if any(o.startswith("failed") for o in outs) else "passed" if any(o == "passed" for o in outs) else "skipped"
    _results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(SUMMARY, "w") as f:
        json.dump(_results, f, indent=1, default=str)
    print(f"\nHardware session summary written to {SUMMARY}")


@pytest.fixture(scope="session")
def report():
    """report(stage, key, value): print a measurement and keep it for last_run.json."""
    def _r(stage, key, value):
        _results["stages"].setdefault(stage, {"tests": {}, "measurements": {}})["measurements"][key] = value
        print(f"[{stage}] {key} = {value}")
    _results["env"] = cfg()
    return _r


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def data_dir():
    """The Studio data folder for the session: firmware sources and toolchains reused from .studio-dev (see tools/hw_session.sh), a temporary one otherwise."""
    d = os.environ.get("CWSTUDIO_HW_DATA") or os.path.join(os.path.dirname(REPO), ".studio-dev", "hw-data")
    if os.path.isdir(os.path.join(d, "firmware")):
        return d
    return tempfile.mkdtemp(prefix="cwstudio-hw-")


@pytest.fixture(scope="session")
def studio():
    """A real Studio (uvicorn on a free port, in this process) so the MCP server and websockets can attach; yields an object with .url, .http (httpx client) and .session."""
    import httpx
    import uvicorn
    from cwstudio.app import create_app
    from cwstudio.session import Session

    session = Session(simulate=HW == "sim", data_dir=data_dir())
    app = create_app(session)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", log_config=None, timeout_graceful_shutdown=3))
    app.state.server = server
    th = threading.Thread(target=server.run, name="studio-hw", daemon=True)
    th.start()
    end = time.time() + 30
    while not server.started and time.time() < end:
        time.sleep(0.05)
    assert server.started, "Studio did not start"

    class S:
        pass
    s = S()
    s.url = f"http://127.0.0.1:{port}"
    s.http = httpx.Client(base_url=s.url, timeout=600, trust_env=False)
    s.session = session
    try:
        yield s
    finally:
        s.http.close()
        server.should_exit = True
        th.join(15)


@pytest.fixture(scope="session")
def api(studio):
    """JSON helper: api.get/post/put/delete return parsed JSON and assert a 2xx unless ok=False."""
    h = studio.http

    class Api:
        def _do(self, method, path, json_body=None, ok=True, **kw):
            r = h.request(method, path, json=json_body, **kw)
            if ok:
                assert r.status_code < 300, f"{method} {path} -> {r.status_code}: {r.text[:500]}"
            return r if not ok else (r.json() if "json" in r.headers.get("content-type", "") else r)

        def get(self, path, **kw):
            return self._do("GET", path, **kw)

        def post(self, path, body=None, **kw):
            return self._do("POST", path, body or {}, **kw)

        def put(self, path, body=None, **kw):
            return self._do("PUT", path, body or {}, **kw)

        def delete(self, path, **kw):
            return self._do("DELETE", path, **kw)

        def wait_job(self, timeout=600):
            end = time.time() + timeout
            while time.time() < end:
                st = self.get("/api/status")
                job = st.get("job")
                if job is None or not job["running"]:
                    return st
                time.sleep(0.1)
            raise TimeoutError("job did not finish")
    return Api()


def scope_kind():
    return "sim" if HW == "sim" else "husky"


def target_kind():
    return "sim" if HW == "sim" else "SimpleSerial2"


sys.path.insert(0, HERE)
