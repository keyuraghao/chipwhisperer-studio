"""Studio's own window: choosing window or browser, the Linux GTK helper and the fallback when no window is possible."""
import os
import subprocess
import sys

import pytest

from cwstudio import cli, window


def test_default_mode_follows_environment_and_build(monkeypatch, tmp_path):
    monkeypatch.delenv("CWSTUDIO_DEFAULT_UI", raising=False)
    assert cli.default_mode() == "window"
    monkeypatch.setenv("CWSTUDIO_DEFAULT_UI", "browser")
    assert cli.default_mode() == "browser"
    monkeypatch.delenv("CWSTUDIO_DEFAULT_UI")
    # a frozen Web build carries a marker file next to its runtime
    (tmp_path / "cwstudio_variant.txt").write_text("web")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert cli.default_mode() == "browser"
    (tmp_path / "cwstudio_variant.txt").write_text("app")
    assert cli.default_mode() == "window"


def test_cw_studio_web_defaults_to_browser(monkeypatch):
    seen = {}
    monkeypatch.delenv("CWSTUDIO_DEFAULT_UI", raising=False)
    monkeypatch.setattr(cli, "main", lambda argv=None: seen.setdefault("mode", cli.default_mode()) and 0)
    cli.main_web([])
    assert seen["mode"] == "browser"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="desktop entries are for Linux")
def test_desktop_entry_terminal_matches_build(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    for ui, terminal in (("window", "false"), ("browser", "true")):
        monkeypatch.setenv("CWSTUDIO_DEFAULT_UI", ui)
        assert cli.desktop_entry(True) == 0
        text = (tmp_path / "applications" / "chipwhisperer-studio.desktop").read_text()
        assert f"Terminal={terminal}" in text and "Icon=chipwhisperer-studio" in text
    assert cli.desktop_entry(False) == 0 and not (tmp_path / "applications" / "chipwhisperer-studio.desktop").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the GTK helper is for Linux")
def test_window_unavailable_without_display(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(window.WindowUnavailable, match="no graphical display"):
        window.open_window("http://127.0.0.1:1/")
    assert "display" in window.available()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the GTK helper is for Linux")
def test_window_unavailable_names_the_package(monkeypatch, tmp_path):
    """A python3 without PyGObject or WebKitGTK gives a reason that says what to install, so Studio can fall back to the browser."""
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\necho 'UNAVAILABLE ImportError: No module named gi'\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setattr(window, "_gtk_pythons", lambda: [str(fake)])
    with pytest.raises(window.WindowUnavailable) as e:
        window.find_gtk_python()
    assert "gir1.2-webkit2-4.1" in str(e.value) and "No module named gi" in str(e.value)


def test_gtk_helper_check_mode_runs():
    """The helper is a plain script for the system Python: its --check either reports WebKitGTK or says why it is unavailable, never crashes."""
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    r = subprocess.run([py, window.GTK_HELPER, "--check"], capture_output=True, text=True, timeout=60)
    assert (r.returncode == 0 and r.stdout.startswith("OK")) or (r.returncode == 3 and r.stdout.startswith("UNAVAILABLE")), (r.returncode, r.stdout, r.stderr)


# ----- regression tests: window lifecycle, ports, locale, pywebview arguments ----------------------------------------
import importlib.util
import socket
import threading
import time
import types
import urllib.request


def _load_helper():
    """The GTK helper is a plain script for the system Python; its pure functions load without GTK."""
    spec = importlib.util.spec_from_file_location("cwstudio_gtk_window", window.GTK_HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("env, tag", [
    ({"LANG": "de_DE.UTF-8"}, "de-DE"),
    ({"LANG": "sr_RS.UTF-8@latin"}, "sr-RS"),
    ({"LANG": "zh_CN.GB18030"}, "zh-CN"),
    ({"LANG": "C"}, "en-US"),
    ({"LANG": "POSIX"}, "en-US"),
    ({"LANG": "C.UTF-8"}, "en-US"),
    ({"LANG": "de_DE.UTF-8", "LC_ALL": "C"}, "en-US"),  # LC_ALL wins, as in the C library
    ({"LANG": "de_DE.UTF-8", "LC_ALL": ""}, "de-DE"),  # an empty variable does not count
    ({"LANG": "C LC_ALL=C"}, "en-US"),  # a malformed value once reached WebKit, Intl threw and the plots did not load
    ({"LANG": "garbage!!"}, "en-US"),
    ({}, "en-US"),
])
def test_page_language_is_always_a_valid_tag(env, tag):
    assert _load_helper().page_language(env) == tag


def test_port_left_in_time_wait_is_reused():
    """A Studio that just closed leaves connections in TIME_WAIT; restarting must keep the port (and so the page origin and its saved settings)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen()
    cli_sock = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()  # the server side closes first, so its end of the connection stays in TIME_WAIT
    srv.close()
    time.sleep(0.1)
    cli_sock.close()
    if os.name != "nt":
        assert cli._free_port("127.0.0.1", port) == port


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
def test_port_in_use_is_skipped(bind_host):
    busy = socket.socket()
    busy.bind((bind_host, 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        for host in ("127.0.0.1", "0.0.0.0"):
            got = cli._free_port(host, port)
            if got == port:
                diag = []
                for p in (port, port + 1):
                    for h in ("127.0.0.1", "0.0.0.0"):
                        for opt in ("none", "excl"):
                            with socket.socket() as t:
                                if opt == "excl" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                                    t.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                                try:
                                    t.bind((h, p)); r = "ok"
                                except OSError as e:
                                    r = f"err{e.errno}"
                            diag.append(f"{p}/{h}/{opt}:{r}")
                    with socket.socket() as c:
                        c.settimeout(1.0)
                        diag.append(f"{p}/connect_ex:{c.connect_ex(('127.0.0.1', p))}")
                    diag.append(f"{p}/port_free:{cli._port_free('127.0.0.1', p)}/listening:{cli._listening('127.0.0.1', p)}")
                raise AssertionError(f"busy={bind_host}:{port} host={host} got={got} " + " ".join(diag))
    finally:
        busy.close()


def test_local_url_for_wildcard_and_ipv6_hosts():
    assert cli.local_host("0.0.0.0") == "127.0.0.1"
    assert cli.local_host("::") == "::1"
    assert cli.local_host("192.168.1.5") == "192.168.1.5"


def test_no_text_browser_without_a_display(monkeypatch, capsys):
    """Over SSH, webbrowser.open would start lynx or w3m in the terminal and block: Studio prints the address instead."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("BROWSER", raising=False)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    cli.open_browser("http://127.0.0.1:8765/")
    assert not opened and "ssh -L 8765:127.0.0.1:8765" in capsys.readouterr().out
    monkeypatch.setenv("DISPLAY", ":0")
    cli.open_browser("http://127.0.0.1:8765/")
    assert opened == ["http://127.0.0.1:8765/"]


def test_system_programs_get_the_users_library_path(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    env = window.system_env({"LD_LIBRARY_PATH": "/bundle/_internal", "LD_LIBRARY_PATH_ORIG": "/opt/lib", "PYTHONHOME": "/bundle", "HOME": "/home/u"})
    assert env["LD_LIBRARY_PATH"] == "/opt/lib" and "LD_LIBRARY_PATH_ORIG" not in env and "PYTHONHOME" not in env and env["HOME"] == "/home/u"
    assert "LD_LIBRARY_PATH" not in window.system_env({"LD_LIBRARY_PATH": "/bundle/_internal"})


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="desktop entries are for Linux")
def test_desktop_entry_matches_window_class(monkeypatch, tmp_path):
    """The window's WM_CLASS (X11) and app id (Wayland) are chipwhisperer-studio; StartupWMClass ties the window to the menu entry and its icon."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert cli.desktop_entry(True) == 0
    assert "StartupWMClass=chipwhisperer-studio" in (tmp_path / "applications" / "chipwhisperer-studio.desktop").read_text()
    assert _load_helper().PRGNAME == "chipwhisperer-studio"


def test_pywebview_keeps_settings_and_uses_an_ico_on_windows(monkeypatch):
    def start(func=None, args=None, gui=None, debug=False, private_mode=True, storage_path=None, icon=None):
        pass
    for platform in ("win32", "darwin"):
        monkeypatch.setattr(sys, "platform", platform)
        kw = window.pywebview_start_kwargs(start)
        assert kw["private_mode"] is False  # pywebview's default private mode loses localStorage (theme, last tab) on every start
        assert os.path.isfile(kw["icon"])
        if platform == "win32":
            assert kw["icon"].endswith(".ico") and kw["gui"] == "edgechromium" and kw["storage_path"]  # Windows Forms cannot load a PNG as the window icon
        else:
            assert kw["icon"].endswith(".png") and kw["gui"] is None and "storage_path" not in kw

    def old_start(func=None, args=None, gui=None, debug=False):  # an older pywebview without these arguments
        pass
    assert set(window.pywebview_start_kwargs(old_start)) == {"gui"}


def test_pywebview_window_closes_when_studio_stops(monkeypatch):
    """/api/shutdown with the window open: the window is destroyed and webview.start() returns."""
    calls = {}
    destroyed = threading.Event()

    class FakeWindow:
        def destroy(self):
            destroyed.set()

    def start(func=None, args=None, gui=None, private_mode=True, storage_path=None, icon=None):
        calls["kw"] = {"private_mode": private_mode, "icon": icon}
        threading.Thread(target=func, daemon=True).start()
        assert destroyed.wait(10), "window was not closed"

    fake = types.SimpleNamespace(settings={"ALLOW_DOWNLOADS": False, "OPEN_EXTERNAL_LINKS_IN_BROWSER": False},
                                 create_window=lambda *a, **k: FakeWindow(), start=start)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(sys, "platform", "darwin")
    stop_at = time.time() + 0.5
    window.open_window("http://127.0.0.1:1/", should_close=lambda: time.time() > stop_at)
    assert destroyed.is_set() and calls["kw"]["private_mode"] is False
    assert fake.settings == {"ALLOW_DOWNLOADS": True, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the GTK helper is for Linux")
def test_gtk_window_closes_when_studio_stops(monkeypatch, tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("import time\nprint('OPENED', flush=True)\ntime.sleep(60)\n")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(window, "find_gtk_python", lambda: sys.executable)
    monkeypatch.setattr(window, "GTK_HELPER", str(helper))
    t0 = time.time()
    window.open_window("http://127.0.0.1:1/", should_close=lambda: time.time() - t0 > 0.5)
    assert time.time() - t0 < 10


def _studio_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_shutdown_from_the_ui_closes_the_window(monkeypatch, tmp_path):
    """cw-studio in window mode: POST /api/shutdown stops the server, the window is told to close, and Studio exits with the port freed."""
    port = _studio_port()
    seen = {}

    def fake_open_window(url, width=1500, height=950, should_close=None):
        seen["url"] = url
        threading.Thread(target=lambda: urllib.request.urlopen(urllib.request.Request(url + "api/shutdown", method="POST"), timeout=10).read(), daemon=True).start()
        end = time.time() + 20
        while not should_close():
            assert time.time() < end, "the window was never told to close"
            time.sleep(0.05)
        seen["closed"] = True
    monkeypatch.setattr(window, "open_window", fake_open_window)
    monkeypatch.delenv("CWSTUDIO_DEFAULT_UI", raising=False)
    assert cli.main(["--port", str(port), "--data-dir", str(tmp_path), "--log-level", "warning"]) == 0
    assert seen == {"url": f"http://127.0.0.1:{port}/", "closed": True}
    assert cli._port_free("127.0.0.1", port)


def test_window_mode_fails_fast_when_the_server_cannot_start(monkeypatch, tmp_path, capsys):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    monkeypatch.setattr(cli, "_free_port", lambda host, preferred: preferred)  # as if all 50 ports were taken
    monkeypatch.setattr(window, "open_window", lambda *a, **k: pytest.fail("no window without a server"))
    monkeypatch.delenv("CWSTUDIO_DEFAULT_UI", raising=False)
    try:
        t0 = time.time()
        assert cli.main(["--port", str(port), "--data-dir", str(tmp_path), "--log-level", "error"]) == 1
        assert time.time() - t0 < 15
        assert "did not start" in capsys.readouterr().err
    finally:
        busy.close()


def test_macos_process_serial_number_is_ignored(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "desktop_entry", lambda install=True: seen.setdefault("install", install) and 0)
    cli.main(["-psn_0_12345", "--install-desktop"])
    assert seen == {"install": True}
