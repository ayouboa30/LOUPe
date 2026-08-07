"""Local-first scientific research workspace.

The package deliberately depends only on the Python standard library.  It is
safe to import in the frozen desktop build; storage is initialized lazily by
``get_workspace`` so importing 3loop never writes to disk by itself.
"""

from .storage import ResearchWorkspace, get_workspace

__all__ = ["ResearchWorkspace", "get_workspace"]
