"""ChipWhisperer Studio window for Linux: a GTK window with a WebKitGTK view of the local Studio server.

This file is run by the system Python (``/usr/bin/python3``), not by Studio's own Python: GTK and WebKitGTK come from the Linux distribution (python3-gi and gir1.2-webkit2-4.1), so Studio does not have to bundle them. It only uses the standard library and PyGObject.

    python3 gtk_window.py --url http://127.0.0.1:8765/ [--title T] [--icon icon.png] [--check]

Downloads open a save dialog, links to other sites open in the default browser, and the process exits when the window is closed (Studio then shuts down) or when Studio goes away.
"""
import argparse
import os
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
    from gi.repository import Gio, GLib, Gtk, WebKit2
    return Gio, GLib, Gtk, WebKit2


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
    GLib.set_prgname("chipwhisperer-studio")  # matches the desktop entry, so the dock and taskbar show Studio's icon
    GLib.set_application_name(args.title)

    win = Gtk.Window(title=args.title)
    win.set_default_size(args.width, args.height)
    if args.icon and os.path.exists(args.icon):
        try:
            win.set_icon_from_file(args.icon)
            Gtk.Window.set_default_icon_from_file(args.icon)
        except GLib.Error:
            pass

    # navigator.language comes from the locale; with LANG=C it is "C", which is not a valid language tag and makes Intl (used by the plots) throw
    lang = "en-US"
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "").split(".")[0].split("@")[0]
        if val and val not in ("C", "POSIX"):
            lang = val.replace("_", "-")
            break
    WebKit2.WebContext.get_default().set_preferred_languages([lang])

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

    view.connect("decide-policy", on_policy)
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

    win.add(view)
    win.connect("destroy", Gtk.main_quit)
    view.load_uri(args.url)
    win.show_all()
    print("OPENED", flush=True)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
