"""Native desktop entry point: local engine server + a pywebview window.

Falls back to the system's default browser if the native WebView2 window
cannot be created (missing runtime, driver issue, etc.), and always leaves
a crash log next to nothing failing silently in windowed/no-console mode.
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from three_loop.server import run_server

# The floating mascot is a pure Win32 layered window (ctypes) and the
# platform-specific bonus features it exposes (WinRT OCR/speech) only exist
# on Windows. Importing it is wrapped so the core chat app - which runs
# through pywebview, itself cross-platform (Windows/macOS/Linux) - still
# starts on any OS; only the widget is skipped elsewhere.
try:
    from three_loop.native_widget import NativeWidget
except Exception:
    NativeWidget = None  # type: ignore[assignment,misc]


def _log_path() -> Path:
    base = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    return base / "3loop_crash.log"


def _write_log(text: str) -> None:
    try:
        _log_path().write_text(text, encoding="utf-8")
    except OSError:
        pass


def _show_message_box(title: str, message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00000030)  # MB_ICONWARNING
    except Exception:
        pass


def _free_port() -> int:
    """Pick an unused local port so multiple launches never collide."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _open_native_window(url: str, *, port: int) -> bool:
    """Try the embedded WebView2 window. Return False if it could not run."""

    try:
        import webview
    except Exception:
        _write_log(f"import webview failed:\n{traceback.format_exc()}")
        return False

    try:
        main_window = webview.create_window(
            "3loop",
            url,
            width=1360,
            height=880,
            min_size=(960, 640),
            background_color="#0b0f14",
        )

        def _open_main() -> None:
            try:
                main_window.restore()
                main_window.show()
            except Exception:
                pass

        # Clicking the window's own close button must not end the process:
        # the mascot is meant to survive it and stay reachable on the
        # desktop. pywebview's "closing" event fires before the native
        # window is actually torn down, and any subscriber returning
        # ``False`` cancels that teardown (see webview/event.py - a handler
        # returning exactly ``False`` is what flips ``args.Cancel``). So the
        # window is hidden instead of destroyed, and the process keeps
        # running: the engine server thread and the widget's own message
        # loop are untouched.
        def _hide_instead_of_close() -> bool:
            try:
                main_window.hide()
            except Exception:
                pass
            return False

        main_window.events.closing += _hide_instead_of_close

        # A pure Win32 layered window instead of a second pywebview/WebView2
        # window: three different transparency tricks against a hosted
        # WebView2 control (layered color-key, DWM frame extension, WinForms
        # TransparencyKey) all failed to show the desktop through it, since
        # a browser control paints via its own composition surface. Talking
        # to Win32 directly with UpdateLayeredWindow has no such surface in
        # the way, so real per-pixel transparency actually works.
        if NativeWidget is not None:
            # Right-click on the mascot is the only way to actually quit,
            # since the main window no longer does: it destroys the widget's
            # own window (ending its message loop) and then tears the whole
            # process down. os._exit rather than a graceful webview.destroy()
            # + return, because at this point the user has explicitly asked
            # to end the app and there is nothing left worth waiting on
            # (daemon threads: the engine server, any in-flight background
            # prompt) - a clean shutdown path would just be dead code we'd
            # never verified to fully unblock webview.start().
            def _quit_app() -> None:
                try:
                    main_window.destroy()
                except Exception:
                    pass
                os._exit(0)

            NativeWidget(_open_main, port=port, on_close=_quit_app).start()

        webview.start()
        return True
    except Exception:
        _write_log(f"webview.start() failed:\n{traceback.format_exc()}")
        return False


def main() -> None:
    """Start the local engine server, then open the app window or a browser tab."""

    try:
        port = _free_port()
        threading.Thread(target=run_server, args=(port,), daemon=True).start()
        time.sleep(0.3)
        url = f"http://127.0.0.1:{port}/"

        if _open_native_window(url, port=port):
            return

        # Native window unavailable (e.g. no WebView2 runtime): the engine
        # server is plain HTTP, so any browser can drive the same UI.
        webbrowser.open(url)
        _show_message_box(
            "3loop",
            "La fenetre native n'a pas pu s'ouvrir (WebView2 manquant probablement).\n"
            "3loop a ete lance dans ton navigateur par defaut a la place:\n"
            f"{url}\n\n"
            "Pour la fenetre native, installe le WebView2 Runtime:\n"
            "https://go.microsoft.com/fwlink/p/?LinkId=2124703",
        )
        while True:
            time.sleep(3600)
    except Exception:
        _write_log(f"main() failed:\n{traceback.format_exc()}")
        _show_message_box("3loop - erreur au demarrage", traceback.format_exc()[-1200:])


if __name__ == "__main__":
    main()
