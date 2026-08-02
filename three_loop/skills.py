"""Loads the per-domain formatting instructions from skills/*.md.

Kept as plain markdown files instead of inline strings so the formatting
rules (LaTeX delimiters, code-block conventions, ...) can be tuned without
touching the prompt-building code, and so more domains can be added later
by just dropping in another file and registering it below.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from .models import TaskKind

_KIND_TO_FILE = {
    TaskKind.MATH: "math.md",
    TaskKind.CODE: "code.md",
    TaskKind.GENERAL: "general.md",
}


def _skills_dir() -> Path:
    # Same bundle-root resolution as server.py's _web_dir(): works both from
    # a source checkout and from a PyInstaller-frozen exe.
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / "skills"


@lru_cache(maxsize=None)
def load_skill(kind: TaskKind) -> str:
    filename = _KIND_TO_FILE.get(kind)
    if filename is None:
        return ""
    try:
        return (_skills_dir() / filename).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
