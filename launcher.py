"""Standalone Windows launcher for the bundled Streamlit application."""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Streamlit execs app.py as a standalone script at runtime, so PyInstaller's
# static import scanner never sees `from three_loop.streamlit_app import main`.
# Importing the package here forces the whole tree into the frozen bundle.
import three_loop.streamlit_app  # noqa: F401


def _resource_path(filename: str) -> Path:
    """Resolve a bundled resource in normal Python and PyInstaller modes."""

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return bundle_root / filename


def _open_browser() -> None:
    """Open the local UI after Streamlit has had time to bind its port."""

    time.sleep(2.0)
    webbrowser.open_new("http://localhost:8501")


def main() -> None:
    """Start Streamlit without requiring a shell command or PATH entry."""

    from streamlit.web import bootstrap

    app_path = _resource_path("app.py")
    if not app_path.exists():
        raise FileNotFoundError(f"Application bundle missing: {app_path}")

    os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    os.environ.setdefault("STREAMLIT_SERVER_PORT", "8501")
    threading.Thread(target=_open_browser, daemon=True).start()
    bootstrap.run(
        str(app_path),
        False,
        ["--server.port=8501", "--server.address=localhost"],
        {},
    )


if __name__ == "__main__":
    main()
