"""Starting Studio: a free port even when ports are reserved, one Studio at a time, errors shown without a console, studio.log, and how the UI's files are served."""
import http.server
import importlib
import json
import mimetypes
import os
import socket
import sys
import tempfile
import threading
import types

import pytest

from cwstudio import cli, window


def test_free_port_asks_the_os_when_all_candidates_are_taken(monkeypatch):
    """Windows with Hyper-V, WSL2 or Docker reserves blocks of ports (WinError 10013): Studio takes any free port instead of failing on the preferred one."""
    monkeypatch.setattr(cli, "_port_free", lambda host, port: False)
    port = cli._free_port("127.0.0.1", 8765)
    assert port not in range(8765, 8815) and port > 0
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))  # really free


def _serve_json(payload):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200 if self.path == "/api/meta" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


STUDIO_META = {"version": "9.9.9", "scope_kinds": [], "mcp_command": {"command": "cw-studio", "args": ["mcp"]}}


def test_running_studio_is_recognised():
    srv = _serve_json(STUDIO_META)
    other = _serve_json({"version": "1.0", "name": "something else"})
    try:
        assert cli._running_studio("127.0.0.1", srv.server_address[1]) == "9.9.9"
        assert cli._running_studio("127.0.0.1", other.server_address[1]) is None
    finally:
        srv.shutdown()
        other.shutdown()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert cli._running_studio("127.0.0.1", free) is None


def test_second_start_opens_the_running_studio(monkeypatch, tmp_path, capsys):
    srv = _serve_json(STUDIO_META)
    port = srv.server_address[1]
    opened = []
    monkeypatch.setattr(cli, "DEFAULT_PORT", port)
    monkeypatch.setattr(cli, "open_browser", lambda url: opened.append(url))
    monkeypatch.setattr(window, "open_window", lambda *a, **k: pytest.fail("no second window"))
    try:
        for mode in ("window", "browser"):
            monkeypatch.setenv("CWSTUDIO_DEFAULT_UI", mode)
            assert cli.main(["--data-dir", str(tmp_path), "--log-level", "error", "--simulate"]) == 0
        assert opened == [f"http://127.0.0.1:{port}/?simulate=1"] * 2
        assert "already running" in capsys.readouterr().out
    finally:
        srv.shutdown()


def test_headless_never_hands_over_and_reports_a_port_it_cannot_open(monkeypatch, tmp_path, capsys):
    """--no-browser (scripts, the MCP server's embedded Studio) always starts its own server; when the port cannot be opened it says so and returns non-zero."""
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    monkeypatch.setattr(cli, "DEFAULT_PORT", port)
    monkeypatch.setattr(cli, "_running_studio", lambda *a, **k: pytest.fail("headless must not look for a running Studio"))
    monkeypatch.setattr(cli, "_free_port", lambda host, preferred: preferred)
    try:
        assert cli.main(["--no-browser", "--port", str(port), "--data-dir", str(tmp_path), "--log-level", "error"]) == 1
        assert "did not start" in capsys.readouterr().err
    finally:
        busy.close()


def test_errors_without_a_console_show_a_dialog(monkeypatch, capsys):
    shown = []
    monkeypatch.setattr(cli, "error_dialog", lambda title, msg: shown.append(msg))
    monkeypatch.setattr(cli, "_log_file", "/home/u/ChipWhispererStudio/studio.log")
    monkeypatch.setattr(cli, "_streams_redirected", False)
    cli._startup_failed("with a console")  # pytest runs without a terminal but is not a frozen build: no dialog
    assert not shown
    monkeypatch.setattr(cli, "_streams_redirected", True)
    cli._startup_failed("Studio did not start")
    assert shown and "Studio did not start" in shown[0] and "studio.log" in shown[0]

    def crash(argv):
        raise RuntimeError("boom at startup")
    monkeypatch.setattr(cli, "_run", crash)
    assert cli.main([]) == 1
    assert "RuntimeError: boom at startup" in shown[-1]
    assert "boom at startup" in capsys.readouterr().err


def test_frozen_build_always_writes_studio_log(monkeypatch, tmp_path):
    """The macOS app gets the null device as output rather than none: a frozen build still writes studio.log, with a header line per start, and moves a large log aside."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli, "_streams_redirected", False)
    monkeypatch.setattr(cli, "_log_file", None)
    monkeypatch.setattr(cli, "_log_stream", None)
    log = tmp_path / "ChipWhispererStudio" / "studio.log"
    log.parent.mkdir()
    log.write_text("x" * (cli.LOG_MAX_BYTES + 1))
    null = open(os.devnull, "w")
    monkeypatch.setattr(sys, "stdout", null)
    monkeypatch.setattr(sys, "stderr", null)
    try:
        cli._ensure_streams()
        assert cli._streams_redirected and not cli.has_console()
        assert sys.stdout is sys.stderr and sys.stdout is not null
        print("hello from the app", file=sys.stderr, flush=True)
        text = log.read_text()
        assert text.startswith("===== ChipWhisperer Studio ") and "frozen=True" in text and "Python " in text and "hello from the app" in text
        assert (tmp_path / "ChipWhispererStudio" / "studio.log.1").stat().st_size > cli.LOG_MAX_BYTES
    finally:
        if sys.stdout is not null:
            sys.stdout.close()
        null.close()


def test_ui_files_have_the_right_type_and_are_revalidated(monkeypatch):
    """Windows can map .js to text/plain in the registry; browsers then refuse the UI's modules. Studio sets the types itself, and asks for revalidation so an upgrade never mixes old and new modules."""
    from starlette.testclient import TestClient
    import cwstudio.app as app_module
    from cwstudio.session import Session
    mimetypes.add_type("text/plain", ".js")  # what such a registry does
    try:
        app_module = importlib.reload(app_module)
        assert mimetypes.guess_type("x.js")[0] == "text/javascript"
        assert mimetypes.guess_type("x.mjs")[0] == "text/javascript"
        assert mimetypes.guess_type("x.css")[0] == "text/css"
        session = Session(simulate=True, data_dir=tempfile.mkdtemp())
        with TestClient(app_module.create_app(session)) as c:
            r = c.get("/static/js/app.js")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
            assert r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
            assert c.get("/static/js/app.js", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
            r = c.get("/static/css/app.css")
            assert r.headers["content-type"].startswith("text/css") and r.headers["cache-control"] == "no-cache"
            assert not c.app.state.page_served.is_set()
            r = c.get("/")
            assert r.headers["content-type"].startswith("text/html") and r.headers["cache-control"] == "no-cache"
            assert c.app.state.page_served.is_set()
            assert "__studioReady" in r.text  # the notice shown when the interface cannot load
    finally:
        mimetypes.add_type("text/javascript", ".js")


def _fake_webview(monkeypatch):
    destroyed = threading.Event()

    class FakeWindow:
        def destroy(self):
            destroyed.set()

    def start(func=None, args=None, gui=None, private_mode=True, storage_path=None, icon=None):
        threading.Thread(target=func, daemon=True).start()
        assert destroyed.wait(10), "window was not closed"
    fake = types.SimpleNamespace(settings={}, create_window=lambda *a, **k: FakeWindow(), start=start)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(sys, "platform", "darwin")
    return destroyed


def test_window_that_never_loads_falls_back_to_the_browser(monkeypatch):
    """pywebview 6 on Windows only logs "WebView2 initialization failed" and shows a blank window: Studio closes it and opens the browser."""
    _fake_webview(monkeypatch)
    monkeypatch.setattr(window, "LOAD_TIMEOUT", 0.3)
    with pytest.raises(window.WindowUnavailable, match="did not load"):
        window.open_window("http://127.0.0.1:1/", loaded=lambda: False)


def test_window_that_loads_stays_open(monkeypatch):
    import time
    destroyed = _fake_webview(monkeypatch)
    monkeypatch.setattr(window, "LOAD_TIMEOUT", 0.3)
    stop_at = time.time() + 1.0
    window.open_window("http://127.0.0.1:1/", loaded=lambda: True, should_close=lambda: time.time() > stop_at)
    assert destroyed.is_set() and time.time() >= stop_at
