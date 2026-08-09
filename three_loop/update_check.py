"""Tells the user when a newer LOUPe release exists on GitHub.

Never downloads or installs anything automatically - matches the app's own
principle elsewhere (OpenCode/Claude Code/Codex install only on an explicit
click) that nothing gets fetched or run without the user asking for it. This
only performs one unauthenticated GET against GitHub's public releases API
(no token, no user data sent) and reports whether the installed version is
behind, so the UI can point the user at the release page.

Fails silently on anything - no internet, GitHub down, rate-limited,
malformed response - since a background version check must never be able to
break the app or show a scary error for something the user didn't ask about.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

CURRENT_VERSION = "0.1.1"

_RELEASES_API = "https://api.github.com/repos/ayouboa30/LOUPe/releases/latest"

#: Tags look like "beta-0.1"; versions like "0.1.0". Both get reduced to the
#: same numeric-tuple shape so "0.1" and "0.1.0" compare equal instead of
#: looking like different versions because of a missing trailing zero.
_VERSION_NUMBERS = re.compile(r"\d+")


def _version_tuple(text: str) -> tuple[int, ...] | None:
    numbers = _VERSION_NUMBERS.findall(text or "")
    if not numbers:
        return None
    return tuple(int(n) for n in numbers)


def is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is a strictly newer version than ``current``.

    Compares parsed numeric tuples padded to equal length (so "0.2" beats
    "0.1.9"); falls back to a plain string inequality if either side has no
    parseable digits at all, which only flags a difference, never a
    direction - better to under-notify on a malformed tag than nag the user
    with "update available" every time it can't tell.
    """

    latest_tuple = _version_tuple(latest)
    current_tuple = _version_tuple(current)
    if latest_tuple is None or current_tuple is None:
        return False
    width = max(len(latest_tuple), len(current_tuple))
    latest_padded = latest_tuple + (0,) * (width - len(latest_tuple))
    current_padded = current_tuple + (0,) * (width - len(current_tuple))
    return latest_padded > current_padded


def check_for_update(
    current_version: str = CURRENT_VERSION, *, timeout: float = 5.0
) -> dict[str, object]:
    """Return whether a newer release is published, without raising.

    Always returns a dict with ``update_available``; on any failure that key
    is ``False`` and the rest describe nothing happened, rather than an
    error the UI would need special handling for.
    """

    result: dict[str, object] = {
        "update_available": False,
        "current_version": current_version,
        "latest_version": None,
        "release_url": None,
    }
    try:
        request = urllib.request.Request(
            _RELEASES_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "LOUPe-update-check",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return result

    tag = str(payload.get("tag_name") or "").strip()
    url = str(payload.get("html_url") or "").strip()
    if not tag:
        return result

    result["latest_version"] = tag
    result["release_url"] = url or f"https://github.com/ayouboa30/LOUPe/releases/tag/{tag}"
    result["update_available"] = is_newer(tag, current_version)
    return result
