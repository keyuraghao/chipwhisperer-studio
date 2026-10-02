"""ChipWhisperer Studio window for Linux: a GTK window with a WebKitGTK view of the local Studio server.

This file is run by the system Python (``/usr/bin/python3``), not by Studio's own Python: GTK and WebKitGTK come from the Linux distribution (python3-gi and gir1.2-webkit2-4.1), so Studio does not have to bundle them. It only uses the standard library and PyGObject.

    python3 gtk_window.py --url http://127.0.0.1:8765/ [--title T] [--icon icon.png] [--check]

Downloads open a save dialog, links to other sites open in the default browser, and the process exits when the window is closed (Studio then shuts down) or when Studio goes away.
"""
import argparse
import os
import signal
import sys
from urllib.parse import urlparse


def load_gi():
    """Import GTK 3 and WebKit2 (API 4.1, or 4.0 on older systems). Raises ImportError or ValueError with the reason."""
    import gi
    gi.require_version("Gtk", "3.0")
    last = None
    for api in ("4.1", "4.0"):
        try:
            gi.require_version("WebKit2", api)
            break
        except ValueError as e:
            last = e
    else:
        raise last
    from gi.repository import GLib
    # Before GTK is imported (which opens the display): the X11 WM_CLASS and the Wayland app id come from the program name, and they must match the desktop entry so the dock and taskbar show Studio's name and icon
    GLib.set_prgname(PRGNAME)
    from gi.repository import Gio, Gtk, WebKit2
    return Gio, GLib, Gtk, WebKit2


PRGNAME = "chipwhisperer-studio"


def page_language(environ=None):
    """The language tag for navigator.language, from the locale. WebKit passes the locale through unchecked, and a value that is not a valid BCP 47 tag (C, POSIX, or a malformed LANG) makes Intl throw, which stops the plots from loading: fall back to en-US then."""
    import re
    environ = os.environ if environ is None else environ
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = environ.get(var, "")
        if not val:
            continue  # an empty variable does not count (POSIX locale precedence)
        tag = val.split(".")[0].split("@")[0].replace("_", "-")
        if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z]{4})?(-(?:[A-Za-z]{2}|[0-9]{3}))?", tag):
            return tag
        return "en-US"  # C, POSIX or a malformed value: the first set variable decides, like the C library
    return "en-US"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=False)
    ap.add_argument("--title", default="ChipWhisperer Studio")
    ap.add_argument("--icon")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=950)
    ap.add_argument("--check", action="store_true", help="only check that GTK and WebKitGTK can be loaded")
    args = ap.parse_args()
    try:
        Gio, GLib, Gtk, WebKit2 = load_gi()
    except Exception as e:  # noqa: BLE001
        print(f"UNAVAILABLE {type(e).__name__}: {e}", flush=True)
        return 3
    if args.check:
        print(f"OK WebKitGTK {WebKit2.get_major_version()}.{WebKit2.get_minor_version()}", flush=True)
        return 0

    origin = urlparse(args.url)
    parent = os.getppid()
    GLib.set_application_name(args.title)

    win = Gtk.Window(title=args.title)
    width, height = args.width, args.height
    try:  # fit a small or scaled screen (a 1366x768 laptop, 200 % scaling): at most 92 % of the monitor's work area
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        width, height = min(width, int(area.width * 0.92)), min(height, int(area.height * 0.92))
    except Exception:  # noqa: BLE001 (no monitor information, for example some Wayland compositors)
        pass
    win.set_default_size(max(width, 640), max(height, 480))
    if args.icon and os.path.exists(args.icon):
        # Several sizes rather than the 512 px file alone: X11 drops an icon larger than the server's request size (_NET_WM_ICON then stays empty and the taskbar shows a generic icon)
        try:
            from gi.repository import GdkPixbuf
            pix = GdkPixbuf.Pixbuf.new_from_file(args.icon)
            icons = [pix.scale_simple(n, n, GdkPixbuf.InterpType.HYPER) for n in (16, 24, 32, 48, 64, 128, 256) if n < pix.get_width()]
            win.set_icon_list(icons or [pix])
            Gtk.Window.set_default_icon_list(icons or [pix])
        except Exception as e:  # noqa: BLE001
            print(f"cannot load the window icon {args.icon}: {e}", file=sys.stderr, flush=True)

    # navigator.language comes from the locale; with LANG=C it is "C", which is not a valid language tag and makes Intl (used by the plots) throw
    WebKit2.WebContext.get_default().set_preferred_languages([page_language()])

    view = WebKit2.WebView()
    settings = view.get_settings()
    settings.set_property("javascript-can-access-clipboard", True)
    settings.set_property("enable-developer-extras", bool(os.environ.get("CWSTUDIO_DEVTOOLS")))
    if os.environ.get("CWSTUDIO_DEVTOOLS") or os.environ.get("CWSTUDIO_WINDOW_SNAPSHOT"):
        settings.set_property("enable-write-console-messages-to-stdout", True)  # JavaScript console messages in the terminal, for debugging
    settings.set_property("enable-back-forward-navigation-gestures", False)

    def same_origin(uri):
        u = urlparse(uri or "")
        return u.scheme in ("http", "https") and u.netloc == origin.netloc

    def open_outside(uri):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            print(f"cannot open {uri}: {e}", file=sys.stderr, flush=True)

    def on_policy(_view, decision, kind):
        T = WebKit2.PolicyDecisionType
        if kind in (T.NAVIGATION_ACTION, T.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(("data:", "blob:")):
                return False  # downloads of generated files (PNG export) are handled by WebKit
            if not same_origin(uri):
                decision.ignore()  # the wiki, NewAE driver pages and other sites open in the default browser
                open_outside(uri)
                return True
            if kind == T.NEW_WINDOW_ACTION:  # a Studio page meant for a separate tab (the API endpoint list): open it in the browser
                decision.ignore()
                open_outside(uri)
                return True
            return False
        if kind == T.RESPONSE:
            resp = decision.get_response()
            headers = resp.get_http_headers()
            disp = headers.get_one("Content-Disposition") if headers is not None else None
            if (disp and disp.lower().startswith("attachment")) or not decision.is_mime_type_supported():
                decision.download()
                return True
        return False

    test_dir = os.environ.get("CWSTUDIO_WINDOW_DOWNLOAD_DIR")  # automated tests: save downloads here without a dialog

    def on_download(_ctx, download):
        def decide(dl, suggested):
            if test_dir:
                dl.set_destination(GLib.filename_to_uri(os.path.join(test_dir, suggested or "download"), None))
                dl.connect("finished", lambda *_: print(f"DOWNLOADED {suggested}", flush=True))
                return True
            dlg = Gtk.FileChooserDialog(title="Save file", parent=win, action=Gtk.FileChooserAction.SAVE)
            dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Save", Gtk.ResponseType.ACCEPT)
            dlg.set_do_overwrite_confirmation(True)
            dlg.set_current_name(suggested or "download")
            downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            if downloads and os.path.isdir(downloads):
                dlg.set_current_folder(downloads)
            ok = dlg.run() == Gtk.ResponseType.ACCEPT
            path = dlg.get_filename()
            dlg.destroy()
            if ok and path:
                dl.set_destination(GLib.filename_to_uri(path, None))
            else:
                dl.cancel()
            return True
        download.connect("decide-destination", decide)

    def on_create(_view, action):
        # window.open() and target=_blank links that WebKit hands over as a request for a new view: Studio has one window, so they open in the default browser
        uri = action.get_request().get_uri()
        if uri and not uri.startswith(("data:", "blob:", "about:")):
            open_outside(uri)
        return None

    upload = os.environ.get("CWSTUDIO_WINDOW_UPLOAD")  # automated tests: answer file choosers (input type=file) with this file instead of showing the GTK dialog
    if upload:
        def on_file_chooser(_view, request):
            request.select_files([upload])
            print(f"CHOSE {upload}", flush=True)
            return True
        view.connect("run-file-chooser", on_file_chooser)

    view.connect("decide-policy", on_policy)
    view.connect("create", on_create)
    view.connect("web-process-terminated", lambda v, _reason: v.reload())  # the page's web process crashed or was killed: show Studio again instead of a blank window
    view.get_context().connect("download-started", on_download)

    def watch_parent():
        if os.getppid() != parent:  # Studio exited without closing us
            Gtk.main_quit()
            return False
        return True
    GLib.timeout_add_seconds(2, watch_parent)

    snap = os.environ.get("CWSTUDIO_WINDOW_SNAPSHOT")  # automated tests: save a PNG of the page once it has loaded
    script = os.environ.get("CWSTUDIO_WINDOW_SCRIPT")  # automated tests: JavaScript to run after loading
    if snap or script:
        def loaded(_v, event):
            if event != WebKit2.LoadEvent.FINISHED:
                return

            def run_script():
                if hasattr(view, "evaluate_javascript"):  # WebKitGTK 2.40 and later
                    view.evaluate_javascript(script, -1, None, None, None, None, None)
                else:
                    view.run_javascript(script, None, None, None)
                return False

            def take():
                def done(v, res):
                    v.get_snapshot_finish(res).write_to_png(snap)
                    print(f"SNAPSHOT {snap}", flush=True)
                view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None, done)
                return False
            if script:
                GLib.timeout_add(2500, run_script)
            if snap:
                GLib.timeout_add(5000, take)
        view.connect("load-changed", loaded)

    def quit_on_signal():
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE
    try:
        from gi.repository import GLibUnix  # PyGObject 3.52 and later
        signal_add = GLibUnix.signal_add
    except ImportError:
        signal_add = GLib.unix_signal_add
    for sig in (signal.SIGINT, signal.SIGTERM):  # Ctrl+C in the terminal or Studio closing the window (after /api/shutdown): quit cleanly, without a traceback
        signal_add(GLib.PRIORITY_DEFAULT, sig, quit_on_signal)

    win.add(view)
    win.connect("destroy", Gtk.main_quit)
    view.load_uri(args.url)
    win.show_all()
    print("OPENED", flush=True)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
