"""Command line entry point: ``cw-studio`` / ``python -m cwstudio``."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser


def _free_port(host: str, preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host if host != "0.0.0.0" else "127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def _wait_ready(host: str, port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


DESKTOP_ID = "chipwhisperer-studio"


def desktop_entry(install: bool = True) -> int:
    """Add (or remove) a Linux applications-menu entry with Studio's icon. Executables carry no icon on Linux; desktop environments take it from a .desktop file."""
    if not sys.platform.startswith("linux"):
        print("Desktop entries are for Linux. On Windows the executable has the icon; on macOS use ChipWhispererStudio.app from the bundle.")
        return 1
    import shutil
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    desktop = os.path.join(data, "applications", DESKTOP_ID + ".desktop")
    icon = os.path.join(data, "icons", "hicolor", "512x512", "apps", DESKTOP_ID + ".png")
    if not install:
        for path in (desktop, icon):
            if os.path.exists(path):
                os.remove(path)
        print("Removed the ChipWhisperer Studio menu entry.")
        return 0
    if getattr(sys, "frozen", False):  # standalone bundle: start through its launcher script
        folder = os.path.dirname(os.path.abspath(sys.executable))
        launcher = os.path.join(folder, "chipwhisperer-studio.sh")
        cmd = [launcher if os.path.exists(launcher) else sys.executable]
    else:
        exe = shutil.which("cw-studio")
        cmd = [exe] if exe else [sys.executable, "-m", "cwstudio"]
    os.makedirs(os.path.dirname(desktop), exist_ok=True)
    os.makedirs(os.path.dirname(icon), exist_ok=True)
    shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icon.png"), icon)

    def quote(arg):  # Desktop Entry Specification quoting for the Exec key
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"' if any(c in arg for c in " \t\"'\\`$;&|<>()") else arg
    with open(desktop, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\nName=ChipWhisperer Studio\nGenericName=Side-channel analysis\n"
                "Comment=Capture, analyse and glitch with ChipWhisperer hardware\n"
                f"Exec={' '.join(quote(a) for a in cmd)}\nIcon={DESKTOP_ID}\nTerminal=true\n"
                "Categories=Development;Electronics;\nKeywords=ChipWhisperer;side-channel;CPA;glitch;power analysis;\n")
    os.chmod(desktop, 0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(desktop)], capture_output=True)
    print(f"Added ChipWhisperer Studio to the applications menu ({desktop}).")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["mcp"]:
        from cwstudio.mcp_server import main as mcp_main
        return mcp_main(argv[1:])
    if argv[:1] in (["--install-desktop"], ["--remove-desktop"]):
        return desktop_entry(install=argv[0] == "--install-desktop")
    if argv[:1] == ["--ccwrap"]:
        from cwstudio.ccwrap import main as ccwrap_main
        return ccwrap_main(argv[1:])
    ap = argparse.ArgumentParser(prog="cw-studio", description="ChipWhisperer Studio", epilog="Run 'cw-studio mcp --help' for the Model Context Protocol server. On Linux, 'cw-studio --install-desktop' adds Studio with its icon to the applications menu ('--remove-desktop' takes it out again).")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 for remote access)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ap.add_argument("--window", action="store_true", help="open in a native window (needs pywebview)")
    ap.add_argument("--simulate", action="store_true", help="pre-select the simulator on the Connect page")
    ap.add_argument("--data-dir", default=None, help="folder for exports/firmware uploads")
    ap.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    args = ap.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    import uvicorn
    from cwstudio.app import create_app
    from cwstudio.session import Session

    session = Session(simulate=args.simulate, data_dir=args.data_dir)
    app = create_app(session)
    port = _free_port(args.host, args.port)
    url_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{url_host}:{port}/"
    if args.simulate:
        url += "?simulate=1"

    def opener():
        if _wait_ready(url_host, port):
            print(f"ChipWhisperer Studio running at {url}", flush=True)
            if args.window:
                try:
                    import webview  # type: ignore
                    webview.create_window("ChipWhisperer Studio", url, width=1500, height=950)
                    webview.start()
                    server.should_exit = True
                    return
                except ImportError:
                    print("pywebview not installed; falling back to the browser", flush=True)
            if not args.no_browser:
                webbrowser.open(url)

    config = uvicorn.Config(app, host=args.host, port=port, log_level=args.log_level, ws_max_size=64 * 1024 * 1024,
                            timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    app.state.server = server
    threading.Thread(target=opener, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
