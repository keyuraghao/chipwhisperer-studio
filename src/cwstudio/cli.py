"""Command line entry point: ``cw-studio`` / ``python -m cwstudio``."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser


def _port_free(host: str, port: int) -> bool:
    """Whether uvicorn could listen on host:port. Like uvicorn, SO_REUSEADDR (except on Windows, where it would let two servers share the port), so connections of a Studio that just closed (TIME_WAIT) do not count: otherwise a restart moves to the next port, a different origin, and the UI forgets its settings (theme, last tab)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: an exclusive bind of the wildcard address fails while any socket holds the port on any address (a plain bind of 127.0.0.1 succeeds next to a listener on 0.0.0.0)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            host = "::" if family == socket.AF_INET6 else "0.0.0.0"
        try:
            s.bind((host, port))
        except OSError:
            return False
    return os.name == "nt" or not _listening(host, port)  # on Windows the exclusive wildcard bind already covers every address (and a probe of a free port takes a second)


def _listening(host: str, port: int) -> bool:
    """Whether a server already accepts connections on the port. macOS and Windows let a socket bind 127.0.0.1 while another listens on 0.0.0.0 (and the reverse), so the bind test alone misses those; a port left in TIME_WAIT refuses connections, so it still counts as free."""
    probe = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(host, host)
    family = socket.AF_INET6 if ":" in probe else socket.AF_INET
    targets = [(family, probe)] + ([(socket.AF_INET6, "::1")] if host in ("0.0.0.0", "") and socket.has_ipv6 else [])
    for fam, addr in targets:
        with socket.socket(fam, socket.SOCK_STREAM) as c:
            c.settimeout(1.0)
            try:
                if c.connect_ex((addr, port)) == 0:
                    return True
            except OSError:
                pass
    return False


def _free_port(host: str, preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 50)):
        if _port_free(host, port):
            return port
    return preferred


def _wait_ready(host: str, port: int, timeout: float = 20.0) -> bool:
    """Wait until something accepts connections on host:port (used by the MCP server for its embedded Studio)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _wait_started(server, timeout: float = 20.0, alive=None) -> bool:
    """Wait until this Studio's uvicorn server is listening (it sets started once its socket is open; connecting to the port instead could reach another program that holds it). Give up early once alive() says the server thread has stopped, for example because the port could not be opened."""
    end = time.time() + timeout
    while time.time() < end:
        if getattr(server, "started", False):
            return True
        if alive is not None and not alive():
            return False
        time.sleep(0.05)
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
        web = default_mode() == "browser"
        exe = shutil.which("cw-studio-web" if web else "cw-studio")
        cmd = [exe] if exe else [sys.executable, "-m", "cwstudio"] + (["--browser"] if web else [])
    os.makedirs(os.path.dirname(desktop), exist_ok=True)
    os.makedirs(os.path.dirname(icon), exist_ok=True)
    shutil.copyfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icon.png"), icon)

    def quote(arg):  # Desktop Entry Specification quoting for the Exec key
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"' if any(c in arg for c in " \t\"'\\`$;&|<>()") else arg
    with open(desktop, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\nName=ChipWhisperer Studio\nGenericName=Side-channel analysis\n"
                "Comment=Capture, analyse and glitch with ChipWhisperer hardware\n"
                f"Exec={' '.join(quote(a) for a in cmd)}\nIcon={DESKTOP_ID}\nStartupWMClass={DESKTOP_ID}\nTerminal={'true' if default_mode() == 'browser' else 'false'}\n"
                "Categories=Development;Electronics;\nKeywords=ChipWhisperer;side-channel;CPA;glitch;power analysis;\n")
    os.chmod(desktop, 0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(desktop)], capture_output=True)
    print(f"Added ChipWhisperer Studio to the applications menu ({desktop}).")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    argv = [a for a in argv if not a.startswith("-psn_")]  # macOS process serial number that Finder may pass to an application it opens
    if argv[:1] == ["mcp"]:
        from cwstudio.mcp_server import main as mcp_main
        return mcp_main(argv[1:])
    if argv[:1] in (["--install-desktop"], ["--remove-desktop"]):
        return desktop_entry(install=argv[0] == "--install-desktop")
    if argv[:1] == ["--ccwrap"]:
        from cwstudio.ccwrap import main as ccwrap_main
        return ccwrap_main(argv[1:])
    ap = argparse.ArgumentParser(prog="cw-studio", description="ChipWhisperer Studio: opens in its own window. Use --browser to use your web browser instead, or --no-browser to run only the server (remote use, scripts).", epilog="Run 'cw-studio mcp --help' for the Model Context Protocol server. On Linux, 'cw-studio --install-desktop' adds Studio with its icon to the applications menu ('--remove-desktop' takes it out again).")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 for remote access)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--browser", action="store_true", help="open the UI in your web browser instead of Studio's own window")
    ap.add_argument("--no-browser", action="store_true", help="run only the server: open no window and no browser")
    ap.add_argument("--window", action="store_true", help=argparse.SUPPRESS)  # the default now; kept so older shortcuts still work
    ap.add_argument("--simulate", action="store_true", help="pre-select the simulator on the Connect page")
    ap.add_argument("--data-dir", default=None, help="folder for exports/firmware uploads")
    ap.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    ap.add_argument("--app-window", dest="window_mode", action="store_true", help="open Studio's own window (the default unless this is the Web build)")
    args = ap.parse_args(argv)
    default = default_mode()
    mode = "headless" if args.no_browser else "browser" if args.browser else "window" if (args.window or args.window_mode) else default

    _ensure_streams()
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
    url_host = local_host(args.host)
    url = f"http://{'[' + url_host + ']' if ':' in url_host else url_host}:{port}/"
    if args.simulate:
        url += "?simulate=1"

    # One line per HTTP request only with --log-level debug: the UI polls, so at info level the access log buries Studio's own messages.
    config = uvicorn.Config(app, host=args.host, port=port, log_level=args.log_level, ws_max_size=64 * 1024 * 1024,
                            timeout_graceful_shutdown=3, access_log=args.log_level == "debug")
    server = uvicorn.Server(config)
    app.state.server = server

    if mode != "window":
        def opener():
            if _wait_started(server):
                print(f"ChipWhisperer Studio running at {url}", flush=True)
                if mode == "browser":
                    open_browser(url)
        threading.Thread(target=opener, daemon=True).start()
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        return 0

    # Own window: the server runs in the background and the window owns the main thread (macOS requires that); closing the window quits Studio, and shutting Studio down (/api/shutdown, Ctrl+C, SIGTERM) closes the window.
    def serve():
        try:
            server.run()
        except SystemExit:  # uvicorn exits when it cannot open the port; it has logged why
            pass
    thread = threading.Thread(target=serve, name="studio-server", daemon=True)
    thread.start()
    if not _wait_started(server, alive=thread.is_alive):
        print(f"ChipWhisperer Studio did not start (is port {port} on {args.host} in use?)", file=sys.stderr, flush=True)
        server.should_exit = True
        thread.join(10)
        return 1
    print(f"ChipWhisperer Studio running at {url}", flush=True)
    _sigterm_as_interrupt()
    from cwstudio import window
    try:
        window.open_window(url, should_close=lambda: not thread.is_alive())
    except window.WindowUnavailable as e:
        print(f"Studio cannot open its own window: {e}", flush=True)
        if has_display():
            print(fallback_message(url), flush=True)
        open_browser(url)
        try:
            while thread.is_alive():
                thread.join(0.5)
        except KeyboardInterrupt:
            pass
    except KeyboardInterrupt:
        pass
    server.should_exit = True
    thread.join(10)
    return 0


def has_console() -> bool:
    """True when Studio's output reaches a console or terminal the user sees: not for the windowed Windows executable (output goes to studio.log), the macOS app or a launcher started from a desktop menu."""
    if _streams_redirected:
        return False
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and stream.isatty():
                return True
        except (AttributeError, ValueError, OSError):
            pass
    return False


def fallback_message(url: str) -> str:
    """What to do when Studio opened in the browser because its own window is unavailable: with a console, quit there; without one, there is nothing to close."""
    if has_console():
        where = "close this console window" if sys.platform == "win32" else "close this terminal"
        return f"Opening it in your web browser instead. Press Ctrl+C here or {where} to quit."
    monitor = "Task Manager" if sys.platform == "win32" else "Activity Monitor" if sys.platform == "darwin" else "your system monitor (or kill the process)"
    return (f"Opening it in your web browser instead ({url}). Studio keeps running in the background without a window or console: "
            f"to quit, end ChipWhisperer Studio in {monitor}, or send POST {url.split('?')[0]}api/shutdown.")


def has_display() -> bool:
    """False on a Linux machine without a graphical session (SSH, a server), where only text browsers could open."""
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_browser(url: str) -> None:
    """Open url in the web browser. Without a graphical display Python would start a text browser (lynx, w3m) in the terminal and block, so say where Studio is instead."""
    if not has_display() and not os.environ.get("BROWSER"):
        from urllib.parse import urlparse
        port = urlparse(url).port
        print(f"There is no graphical display: open {url} from a browser on this machine, or forward the port (ssh -L {port}:127.0.0.1:{port} ...) and open it on yours. Press Ctrl+C to quit.", flush=True)
        return
    keys = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        if getattr(sys, "frozen", False):  # the browser is a system program: start it without the bundle's library path
            from cwstudio.window import system_env
            clean = system_env()
            for k in keys:
                if k in clean:
                    os.environ[k] = clean[k]
                else:
                    os.environ.pop(k, None)
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001
        print(f"Could not open a web browser ({e}): open {url} yourself.", flush=True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def local_host(host: str) -> str:
    """The address this machine uses to reach a server bound to host (a wildcard address means every interface, so use loopback)."""
    return {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(host, host)


def _sigterm_as_interrupt() -> None:
    """In window mode uvicorn runs in a thread and does not handle signals: make SIGTERM (logout, kill, a service manager) take the same clean path as Ctrl+C, so the window closes, the hardware is released and the port is freed."""
    def handler(_sig, _frame):
        raise KeyboardInterrupt
    try:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, AttributeError):
        pass


def default_mode() -> str:
    """'window' or 'browser': the Web build (bundle variant file, or the cw-studio-web command) opens the browser by default."""
    if os.environ.get("CWSTUDIO_DEFAULT_UI") in ("window", "browser"):
        return os.environ["CWSTUDIO_DEFAULT_UI"]
    if getattr(sys, "frozen", False):
        marker = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "cwstudio_variant.txt")
        try:
            with open(marker, encoding="utf-8") as f:
                if f.read().strip() == "web":
                    return "browser"
        except OSError:
            pass
    return "window"


def mcp_command() -> dict:
    """The command an MCP client should start for this installation, as {"command": ..., "args": [...]}.

    In a bundle it is the executable itself, except in the Windows window build, whose ChipWhispererStudio.exe is a windowed program without standard input and output: there the console executable cw-studio.exe next to it serves MCP. A pip install uses the cw-studio command, or python -m cwstudio when that is not on PATH.
    """
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        if sys.platform == "win32":
            console = os.path.join(os.path.dirname(exe), "cw-studio.exe")
            if os.path.exists(console):
                exe = console
        return {"command": exe, "args": ["mcp"]}
    import shutil
    if shutil.which("cw-studio"):
        return {"command": "cw-studio", "args": ["mcp"]}
    return {"command": sys.executable, "args": ["-m", "cwstudio", "mcp"]}


def main_web(argv=None):
    """Entry point of cw-studio-web: the same Studio, opening in the web browser by default."""
    os.environ.setdefault("CWSTUDIO_DEFAULT_UI", "browser")
    return main(argv)


_streams_redirected = False  # set when output goes to studio.log because there is no console


def _ensure_streams() -> None:
    """Without a console (the windowed Windows executable, pythonw) sys.stdout and sys.stderr are None; send output to a log file so logging and uvicorn work and errors are kept."""
    global _streams_redirected
    if sys.stdout is not None and sys.stderr is not None:
        return
    _streams_redirected = True
    folder = os.path.join(os.path.expanduser("~"), "ChipWhispererStudio")
    try:
        os.makedirs(folder, exist_ok=True)
        f = open(os.path.join(folder, "studio.log"), "a", encoding="utf-8", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


if __name__ == "__main__":
    sys.exit(main())
