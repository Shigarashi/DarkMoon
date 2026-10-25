"""Desktop launcher: local server + native window (pywebview) + tray + global hotkey.

    python -m app.main              # desktop app
    python -m app.main --browser    # open in your normal browser instead
    python -m app.main --server     # headless server only (prints the URL)
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

import uvicorn

from . import __version__, paths, tools
from .agent import Agent
from .config import Config
from .storage import Store

INSTANCE_PORT = 47811
log = logging.getLogger("darkmoon")


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(paths.LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _single_instance(on_show) -> bool:
    """Return True if we are the first instance. Otherwise ask it to show itself."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            return True  # port taken by something else; just run
        return False
    srv.listen(2)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16) == b"show":
                        on_show()
            except OSError:
                break

    threading.Thread(target=loop, daemon=True).start()
    return True


def make_icon_image():
    from PIL import Image, ImageDraw
    ico = paths.APP_DIR / "assets" / "icon.png"
    if ico.exists():
        return Image.open(ico)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=16, fill=(124, 92, 255, 255))
    d.ellipse((18, 18, 46, 46), outline=(255, 255, 255, 255), width=6)
    return img


def _fix_windowless_streams() -> None:
    """pythonw.exe (no console) sets sys.stdout/stderr to None; libraries that
    print or check isatty() would crash. Point them at the null device."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _show_fatal(msg: str) -> None:
    """Show a startup error even when there is no console."""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg[-1800:], "Darkmoon couldn't start", 0x10)
            return
    except Exception:  # noqa: BLE001
        pass
    print(msg, file=sys.stderr)


def _set_windows_app_id() -> None:
    """Give Darkmoon its own taskbar identity (own icon, not grouped under Python)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Darkmoon.Desktop")
        except Exception:  # noqa: BLE001
            pass


def splash_html() -> str:
    import base64
    logo = paths.APP_DIR / "web" / "icons" / "icon-192.png"
    src = "data:image/png;base64," + base64.b64encode(logo.read_bytes()).decode() if logo.exists() else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;height:100%;background:#0b0b14;color:#e8e6f4;font-family:"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;overflow:hidden}}
body{{display:grid;place-items:center;background:radial-gradient(520px 360px at 50% 44%,rgba(169,156,255,.16),transparent 70%),#0b0b14}}
.wrap{{text-align:center;animation:rise .7s cubic-bezier(.2,.8,.2,1) both}}
img{{width:112px;height:112px;border-radius:28px;box-shadow:0 0 0 1px rgba(255,255,255,.06),0 18px 60px rgba(124,92,255,.35);animation:glow 2.4s ease-in-out infinite}}
h1{{margin:22px 0 6px;font-size:30px;font-weight:600;letter-spacing:.02em}}
p{{margin:0;color:#8d89ab;font-size:13px;letter-spacing:.14em;text-transform:uppercase}}
.bar{{width:120px;height:2px;margin:22px auto 0;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden}}
.bar i{{display:block;width:40%;height:100%;background:linear-gradient(90deg,transparent,#a99cff,transparent);animation:slide 1.2s ease-in-out infinite}}
@keyframes rise{{from{{opacity:0;transform:translateY(10px) scale(.98)}}}}
@keyframes glow{{50%{{box-shadow:0 0 0 1px rgba(255,255,255,.08),0 18px 80px rgba(169,156,255,.55)}}}}
@keyframes slide{{from{{transform:translateX(-100%)}}to{{transform:translateX(250%)}}}}
</style></head><body><div class="wrap"><img src="{src}" alt=""><h1>Darkmoon</h1><p>Waking up</p><div class="bar"><i></i></div></div></body></html>"""


def main() -> None:
    _fix_windowless_streams()
    try:
        _main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            log.critical("fatal startup error:\n%s", tb)
        except Exception:  # noqa: BLE001
            pass
        _show_fatal(f"{tb}\nFull log: {paths.LOG_FILE}")
        raise SystemExit(1)


def _main() -> None:
    ap = argparse.ArgumentParser(description="Darkmoon")
    ap.add_argument("--browser", action="store_true", help="open in the default browser")
    ap.add_argument("--server", action="store_true", help="run the server only")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--debug", action="store_true", help="enable webview devtools")
    ap.add_argument("--hidden", action="store_true", help="start minimized to the tray")
    args = ap.parse_args()

    paths.ensure_dirs()
    _setup_logging()
    log.info("Darkmoon %s starting; data dir %s", __version__, paths.DATA_DIR)

    cfg = Config()
    store = Store(paths.DB_FILE)
    tools.load_plugins([paths.BUILTIN_TOOL_PLUGINS_DIR, paths.USER_TOOL_PLUGINS_DIR])
    agent = Agent(cfg, store)

    from .server import TOKEN, create_app  # noqa: F401
    from .mcp import MCPManager
    from .mcp_oauth import oauth
    from .phone import PhoneAccess
    phone = PhoneAccess(cfg)
    app = create_app(cfg, store, agent, MCPManager(cfg), phone)
    phone.app = app

    port = args.port or int(cfg["ui"].get("port") or 0) or _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None, timeout_graceful_shutdown=1))
    threading.Thread(target=server.run, daemon=True).start()

    def wait_for_server(timeout: float = 15) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if server.started:
                return True
            time.sleep(0.05)
        return server.started

    url = f"http://127.0.0.1:{port}/"
    oauth.redirect_base = f"http://127.0.0.1:{port}"
    log.info("Serving on %s", url)
    if cfg["phone"].get("enabled"):
        phone.start()

    if args.server:
        wait_for_server()
        print(f"Darkmoon running at {url}  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
        return

    try:
        if args.browser:
            raise ImportError
        import webview  # pywebview
    except ImportError:
        wait_for_server()
        webbrowser.open(url)
        print(f"Darkmoon running at {url}  (Ctrl+C to stop)")
        _single_instance(lambda: webbrowser.open(url))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
        return

    ui = cfg["ui"]
    start_hidden = bool(args.hidden or ui.get("start_hidden", False))
    state = {"visible": not start_hidden, "quitting": False, "tray": None}
    class JsApi:
        """Functions the page can call through window.pywebview.api."""

        def pick_folder(self):
            try:
                fd = getattr(webview, "FileDialog", None)
                kind = fd.FOLDER if fd is not None else webview.FOLDER_DIALOG
                res = window.create_file_dialog(kind)
            except Exception as e:  # noqa: BLE001
                log.warning("folder dialog failed: %s", e)
                return None
            return res[0] if res else None

    _set_windows_app_id()
    # Show the logo splash immediately; switch to the app once the server is up.
    window = webview.create_window(
        "Darkmoon", html=splash_html(), width=int(ui.get("width", 1180)), height=int(ui.get("height", 800)),
        min_size=(720, 500), hidden=start_hidden, background_color="#0b0b14",
        text_select=True, js_api=JsApi(),
    )

    def load_app():
        started = time.time()
        if not wait_for_server():
            _show_fatal(f"The local server didn't start. See the log: {paths.LOG_FILE}")
            return
        time.sleep(max(0.0, 0.9 - (time.time() - started)))  # let the logo be seen, briefly
        window.load_url(url)

    def show():
        state["visible"] = True
        window.show()
        window.restore()

    def hide():
        state["visible"] = False
        window.hide()

    def toggle():
        hide() if state["visible"] else show()

    def quit_app():
        state["quitting"] = True
        if state["tray"]:
            try:
                state["tray"].stop()
            except Exception:  # noqa: BLE001
                pass
        server.should_exit = True
        window.destroy()

    if not _single_instance(show):
        return

    def on_closing():
        if ui.get("minimize_to_tray", True) and state["tray"] and not state["quitting"]:
            hide()
            return False
        quit_app()
        return True

    window.events.closing += on_closing

    # System tray ------------------------------------------------------------
    try:
        import pystray

        def run_js(code: str):
            show()
            window.evaluate_js(code)

        menu = pystray.Menu(
            pystray.MenuItem("Show / Hide", lambda: toggle(), default=True),
            pystray.MenuItem("New chat", lambda: run_js("window.Darkmoon && Darkmoon.newChat()")),
            pystray.MenuItem("Settings", lambda: run_js("window.Darkmoon && Darkmoon.openSettings()")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open workspace folder", lambda: _open(cfg.workspace)),
            pystray.MenuItem("Open data folder", lambda: _open(paths.DATA_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Darkmoon", lambda: quit_app()),
        )
        tray = pystray.Icon("Darkmoon", make_icon_image(), "Darkmoon", menu)
        state["tray"] = tray
        threading.Thread(target=tray.run, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        log.warning("tray unavailable: %s", e)

    # Global hotkey ----------------------------------------------------------
    hk = ui.get("hotkey")
    if hk:
        try:
            from pynput import keyboard
            keyboard.GlobalHotKeys({hk: toggle}).start()
            log.info("hotkey %s registered", hk)
        except Exception as e:  # noqa: BLE001
            log.warning("hotkey unavailable: %s", e)

    storage = paths.DATA_DIR / "webview"
    icon = paths.APP_DIR / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.png")
    webview.start(load_app, private_mode=False, storage_path=str(storage), debug=args.debug,
                  icon=str(icon) if icon.exists() else None)
    server.should_exit = True


def _open(p):
    from .server import _open_path
    _open_path(p)


if __name__ == "__main__":
    main()
