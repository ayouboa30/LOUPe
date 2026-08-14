"""Native desktop entry point: local engine server + a pywebview window.

Falls back to the system's default browser if the native WebView2 window
cannot be created (missing runtime, driver issue, etc.), and always leaves
a crash log next to nothing failing silently in windowed/no-console mode.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

# The server import is intentionally delayed until ``main``. In a frozen,
# windowed build an import-time native DLL failure otherwise happens before
# ``_log_path`` exists, leaving the user with only a generic Windows
# LoadLibrary dialog and no diagnostic file.

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
    """Show the Windows fallback notice without making non-Windows fragile."""

    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x00000030)  # MB_ICONWARNING
    except Exception:
        pass


def _free_port() -> int:
    """Pick an unused local port so multiple launches never collide."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
_OLLAMA_START_TIMEOUT = 12.0
_OLLAMA_STOP_TIMEOUT = 5.0
# This reference is set only after this process has started ``ollama serve``.
# It must never point at a service that existed before 3loop opened.
_owned_ollama_process: subprocess.Popen[bytes] | None = None


def _ollama_is_available(timeout: float = 0.75) -> bool:
    """Return whether the standard local Ollama API can list its models."""

    try:
        with urllib.request.urlopen(_OLLAMA_TAGS_URL, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _stop_ollama_process(process: subprocess.Popen[bytes]) -> None:
    """Stop one owned Ollama child without affecting any other instance."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError as exc:
        _write_log(f"Impossible d'arrêter l'instance Ollama de 3loop: {exc}\n")
        return
    try:
        process.wait(timeout=_OLLAMA_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=_OLLAMA_STOP_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _write_log(f"L'instance Ollama de 3loop ne s'est pas arrêtée: {exc}\n")


def _stop_owned_ollama_server() -> None:
    """Release only the Ollama service that this 3loop process started."""

    global _owned_ollama_process
    process, _owned_ollama_process = _owned_ollama_process, None
    if process is not None:
        _stop_ollama_process(process)


def _ensure_ollama_server() -> bool:
    """Start the standard local Ollama service once, before opening 3loop.

    The bundled app intentionally does not ship model weights or ``ollama``.
    It only starts the user-installed local runtime when its normal API is
    absent. A pre-existing system service is reused untouched, and a missing
    executable never prevents the rest of the application from opening. When
    3loop starts the service itself, the child process is remembered and is
    stopped again when the user actually quits the desktop app.
    """

    global _owned_ollama_process
    if _ollama_is_available():
        return True

    executable = shutil.which("ollama")
    if executable is None:
        _write_log("Ollama introuvable dans PATH; les modèles locaux sont indisponibles.\n")
        return False

    try:
        process = subprocess.Popen(
            [executable, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        _write_log(f"Impossible de démarrer Ollama automatiquement: {exc}\n")
        return False

    deadline = time.monotonic() + _OLLAMA_START_TIMEOUT
    while time.monotonic() < deadline:
        if _ollama_is_available():
            # Keeping the Popen reference is the proof that 3loop owns this
            # child. A service that was already reachable never reaches here.
            if process.poll() is None:
                _owned_ollama_process = process
            return True
        if process.poll() is not None:
            _write_log("Ollama s'est arrêté avant d'exposer son API locale.\n")
            return False
        time.sleep(0.2)

    _stop_ollama_process(process)
    _write_log("Ollama n'a pas répondu dans le délai de démarrage.\n")
    return False


def _open_native_window(
    url: str,
    *,
    port: int,
    on_quit: Callable[[], None] | None = None,
) -> bool:
    """Try the embedded WebView2 window. Return False if it could not run."""

    try:
        import webview
    except Exception:
        _write_log(f"import webview failed:\n{traceback.format_exc()}")
        return False

    try:
        main_window = webview.create_window(
            "LOUPe beta 0.1.5",
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
            # The mascot owns the application lifetime on Windows. On Linux
            # and macOS it is intentionally unavailable, so the main window's
            # close button must really close pywebview instead of hiding the
            # only visible entry point forever.
            if NativeWidget is None:
                if on_quit is not None:
                    try:
                        on_quit()
                    except Exception:
                        _write_log(
                            "Nettoyage à la fermeture impossible:\n"
                            f"{traceback.format_exc()}"
                        )
                return True
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
            # process down.
            def _quit_app() -> None:
                try:
                    main_window.destroy()
                finally:
                    if on_quit is not None:
                        try:
                            on_quit()
                        except Exception:
                            _write_log(
                                "Nettoyage Ollama à la fermeture impossible:\n"
                                f"{traceback.format_exc()}"
                            )
                    # The engine server remains a daemon thread and must not
                    # survive an explicit desktop quit after its owned Ollama
                    # child has been stopped above.
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
        # Local model selection depends on Ollama's standard API. Start the
        # user-installed service before /api/config is first requested, so the
        # model selector is populated on the app's first paint rather than
        # requiring a separate terminal command and a manual page reload.
        _ensure_ollama_server()

        # Keep this import inside the guarded startup path so native import
        # failures are written next to the executable instead of disappearing
        # before the windowed process can initialize logging.
        from three_loop.server import run_server

        port = _free_port()
        threading.Thread(target=run_server, args=(port,), daemon=True).start()
        time.sleep(0.3)
        url = f"http://127.0.0.1:{port}/"

        if _open_native_window(url, port=port, on_quit=_stop_owned_ollama_server):
            return

        # Native window unavailable (e.g. no WebView2 runtime): the engine
        # server is plain HTTP, so any browser can drive the same UI.
        webbrowser.open(url)
        fallback_message = (
            "La fenetre native n'a pas pu s'ouvrir.\n"
            "LOUPe a ete lance dans ton navigateur par defaut:\n"
            f"{url}"
        )
        if sys.platform == "win32":
            fallback_message += (
                "\n\nPour la fenetre native, installe le WebView2 Runtime:\n"
                "https://go.microsoft.com/fwlink/?LinkId=2124703"
            )
        else:
            fallback_message += (
                "\n\nPour la fenetre integree sous Linux, installe pywebview "
                "avec un backend GTK ou Qt et ses bibliotheques systeme."
            )
        _show_message_box("LOUPe", fallback_message)
        while True:
            time.sleep(3600)
    except Exception:
        _write_log(f"main() failed:\n{traceback.format_exc()}")
        _show_message_box("LOUPe - erreur au demarrage", traceback.format_exc()[-1200:])
    finally:
        _stop_owned_ollama_server()


if __name__ == "__main__":
    main()
