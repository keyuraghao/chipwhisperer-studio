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
