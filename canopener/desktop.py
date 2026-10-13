"""The app window (pywebview / Edge WebView2) over the local server, and `serve` for a browser.

The FastAPI app runs on a background thread; closing the window stops it, and the server's shutdown stops
cyclic transmits, finishes a recording and closes every adapter. If a CANopener already answers on the port,
this only opens a window onto it (single instance).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from .paths import app_dir, logs_dir

log = logging.getLogger(__name__)
HOST = "127.0.0.1"


class PeerResetFilter(logging.Filter):
    """Windows asyncio (Python 3.11) logs an ERROR with a traceback when it shuts down a socket the other end has
    already reset, e.g. the window that just closed (WinError 10054 in _call_connection_lost). Nothing is lost:
    drop that one record, keep every other asyncio error."""
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, ConnectionResetError) and "_call_connection_lost" in record.getMessage())


def setup_logging() -> None:
    """Log to logs/canopener.log next to the app (the windowed exe has no console)."""
    fh = logging.handlers.RotatingFileHandler(logs_dir() / "canopener.log", maxBytes=1_000_000, backupCount=3,
                                              encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.addHandler(fh)
    if sys.stderr is not None and hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("canopener").setLevel(logging.INFO)
    logging.getLogger("canopen").setLevel(logging.CRITICAL)
    logging.getLogger("can").setLevel(logging.ERROR)       # missing vendor libraries are not errors here
    logging.getLogger("asyncio").addFilter(PeerResetFilter())


def probe(port: int, host: str = HOST) -> tuple[str, str | None]:
    """What holds the port: ("free", None), ("canopener", version) or ("other", None)."""
    try:
        socket.create_connection((host, port), timeout=1.0).close()
    except OSError:
        return "free", None
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/about", timeout=3) as r:
            info = json.loads(r.read())
        if isinstance(info, dict) and info.get("app") == "canopener":
            return "canopener", str(info.get("version"))
    except (OSError, ValueError):
        pass
    return "other", None


def message(text: str, error: bool = False) -> None:
    """A Windows message box (blocks until OK); stderr elsewhere."""
    (log.error if error else log.info)(text.replace("\n", " "))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "CANopener", 0x10 if error else 0x40)
    except (AttributeError, OSError):
        print(text, file=sys.stderr)


def make_service():
    from .service import Service
    return Service()


def start_server(svc, port: int, host: str = HOST):
    """uvicorn on a daemon thread; returns (server, thread). Stop with server.should_exit = True."""
    import uvicorn

    from .server import create_app

    ws_impl = "auto"
    try:
        from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
        from websockets.exceptions import InvalidState

        class WebSockets(WebSocketsSansIOProtocol):
            """uvicorn's shutdown sends a close frame on every open websocket and crashes on one the client is
            already closing (the window that just went away), skipping the lifespan shutdown."""
            def shutdown(self) -> None:
                try:
                    super().shutdown()
                except InvalidState:
                    self.transport.close()
        ws_impl = WebSockets
    except ImportError:
        pass

    app = create_app(svc)
    app.state.allow_remote = host not in ("127.0.0.1", "localhost", "::1")
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", ws=ws_impl,
                            log_config=None, timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    t.start()
    return server, t


def wait_ready(port: int, thread: threading.Thread, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and thread.is_alive():
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/api/about", timeout=2) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def _enable_edit_menu(window) -> None:
    """pywebview switches WebView2's own right-click menu off unless debug is on (its SHOW_DEFAULT_MENUS setting
    only works on macOS). Switch it back on so text fields get Cut / Copy / Paste; the UI suppresses the menu
    everywhere else (core.js)."""
    if sys.platform != "win32":                  # only the Windows (WebView2) window needs this
        return
    try:
        from System import Func, Type

        form = window.native

        def enable():
            form.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True
        form.Invoke(Func[Type](enable))
    except Exception:
        log.warning("cannot enable the text field menu", exc_info=True)


def open_window(title: str, url: str, on_closing=None) -> bool:
    """Show the UI in a native window and block until it is closed. False if no window could be shown."""
    try:
        import webview
    except Exception:
        log.exception("pywebview unavailable")
        return False
    renderer_ok = []

    def initialized(renderer):
        log.info("window renderer: %s", renderer)
        renderer_ok.append(renderer == "edgechromium")
        return renderer_ok[-1]                     # False cancels the window: MSHTML cannot run the UI
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True     # CSV/sequence export and recording downloads (Save dialog)
        # text_select: pywebview otherwise puts user-select: none on the whole page (no mouse text selection)
        w = webview.create_window(title, url, width=1440, height=900, min_size=(960, 600), text_select=True)
        w.events.initialized += initialized
        w.events.loaded += lambda: _enable_edit_menu(w)
        if on_closing is not None:
            w.events.closing += on_closing
        storage = os.path.join(os.environ.get("LOCALAPPDATA") or str(app_dir()), "CANopener", "webview")
        webview.start(private_mode=False, storage_path=storage)   # keeps the UI's layout settings (localStorage)
    except Exception:
        log.exception("window failed")
        return False
    if renderer_ok and not renderer_ok[-1]:
        log.error("WebView2 runtime not available; using the browser instead")
        return False
    return True


_mutex = None


def claim_instance(port: int) -> bool:
    """True if this is the only CANopener starting on `port` (a named mutex, so two quick double-clicks cannot
    both start a server). Elsewhere than Windows: always True (the port probe then decides)."""
    global _mutex
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return True
    _mutex = k32.CreateMutexW(None, False, f"Local\\CANopener-{port}")
    return k32.GetLastError() != 183                     # ERROR_ALREADY_EXISTS


def _title() -> str:
    from .service import build_info
    return f"CANopener {build_info().get('version', '?')}"


def run(args) -> int:
    """`canopener app`: returns the process exit code."""
    setup_logging()
    port = getattr(args, "port", 8775)
    url = f"http://{HOST}:{port}/ui/"
    first = claim_instance(port)
    state, version = probe(port)
    if not first and state == "free":                  # the other instance is still starting its server
        for _ in range(100):
            time.sleep(0.2)
            state, version = probe(port)
            if state != "free":
                break
    if state == "canopener":
        log.info("CANopener %s already runs on port %d: opening a window onto it", version, port)
        if not open_window(f"CANopener {version}", url):
            webbrowser.open(url)
        return 0
    if state == "other":
        message(f"Port {port} is in use by another program.\n\n"
                f"Close it, or start CANopener on another port: canopener-cli.exe app --port 8776", error=True)
        return 2

    svc = make_service()
    stopped, done = threading.Event(), threading.Event()
    shutdown = svc.shutdown

    def shutdown_once() -> None:                  # the server's lifespan calls this; the fallback below too
        if stopped.is_set():
            done.wait(60)
            return
        stopped.set()
        try:
            shutdown()
            log.info("stopped transmits, closed recording and adapters")
        finally:
            done.set()
    svc.shutdown = shutdown_once

    def safe_now(*_):
        """Window closing (also at Windows logoff): stop transmitting before anything else is torn down."""
        try:
            svc.engine.tx.stop_all()
            if svc.engine.player is not None:
                svc.engine.player.stop()
        except Exception:
            log.exception("stop on window close failed")

    server, thread = start_server(svc, port)
    if not wait_ready(port, thread):
        server.should_exit = True
        thread.join(10)
        svc.save_on_exit = False                       # never overwrite another instance's settings
        shutdown_once()
        message(f"CANopener could not start its server on port {port}.\n\nSee {logs_dir() / 'canopener.log'}",
                error=True)
        return 1
    title = _title()
    log.info("%s serving %s (MCP: http://%s:%d/mcp/)", title, url, HOST, port)

    if not open_window(title, url, on_closing=safe_now):
        webbrowser.open(url)
        message(f"CANopener is running in your browser at {url}\n\nClick OK to stop CANopener.")

    log.info("window closed: stopping")
    safe_now()
    server.should_exit = True
    thread.join(15)
    if thread.is_alive():
        log.warning("server did not stop within 15 s")
    try:
        shutdown_once()
    except Exception:
        log.exception("shutdown failed")
    log.info("stopped")
    return 0


def serve(args) -> int:
    """`canopener serve`: the server in the foreground; Ctrl+C stops it."""
    if os.environ.get("CANOPENER_WINDOWED"):
        message("Use canopener-cli.exe serve (the windowed exe has no console to stop it from).", error=True)
        return 2
    setup_logging()
    port, host = getattr(args, "port", 8775), getattr(args, "host", HOST)
    state, _ = probe(port)
    if state != "free":
        print(f"error: port {port} is already in use ({state})")
        return 2
    svc = make_service()
    server, thread = start_server(svc, port, host)
    if not wait_ready(port, thread):
        svc.save_on_exit = False
        svc.shutdown()
        print("error: the server did not start; see logs/canopener.log")
        return 1
    print(f"CANopener serving http://{HOST}:{port}/ui/  (MCP: http://{HOST}:{port}/mcp/)  Ctrl+C stops it.")
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    server.should_exit = True
    thread.join(15)
    svc.shutdown()                                     # no-op if the server's lifespan already did it
    return 0


def exit_now(code: int) -> None:
    """End without the interpreter's teardown (unloading pythonnet's .NET runtime takes seconds after the
    window has gone). run() has already stopped the server and closed the adapters."""
    logging.shutdown()
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except (AttributeError, OSError, ValueError):
            pass
    os._exit(code)
