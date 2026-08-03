"""Fetch a web page and keep only the readable content.

Stdlib only (urllib + html.parser), matching the DuckDuckGo provider in
web.py: the whole point of this codebase's local-first story is that the
desktop build never needs an extra HTTP client bundled in.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

#: Elements whose entire subtree is chrome, not content: navigation, ads,
#: embedded scripts/styles, structured-data blobs.
_SKIP_TAGS = {
    "script", "style", "nav", "header", "footer", "aside", "noscript",
    "form", "svg", "iframe", "button",
}

#: Block-level elements get a line break on exit, so paragraphs/headings/
#: list items don't run together into one wall of text.
_BLOCK_TAGS = {
    "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "br", "blockquote", "pre", "section", "article",
}

_WHITESPACE = re.compile(r"[ \t]+")
_ANY_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth == 0 and data.strip():
            # Collapse whitespace *within* this text node here, including
            # newlines: those are source formatting, not paragraph breaks.
            # Only the "\n" markers inserted on block-tag boundaries below
            # are meant to survive as real line breaks.
            self._chunks.append(_ANY_WHITESPACE.sub(" ", data.strip()))
            self._chunks.append(" ")

    def text(self) -> str:
        joined = "".join(self._chunks)
        joined = _WHITESPACE.sub(" ", joined)
        lines = (line.strip() for line in joined.splitlines())
        joined = "\n".join(line for line in lines if line)
        return _BLANK_LINES.sub("\n\n", joined).strip()


def extract_readable_text(html: str) -> tuple[str, str]:
    """Return ``(title, body_text)`` from raw HTML, chrome stripped out."""

    parser = _TextExtractor()
    parser.feed(html)
    return parser.title.strip(), parser.text()


def fetch_page(url: str, *, timeout: float = 15.0, max_bytes: int = 3_000_000) -> tuple[str, str]:
    """Download ``url`` and return ``(title, readable_text)``.

    Raises ``ValueError`` on anything that stops a usable page from coming
    back (bad scheme, HTTP error, non-HTML response, timeout) with a message
    fit to show the user directly, rather than leaking a raw exception.
    """

    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"URL invalide (http/https requis): {url}")

    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (3loop)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower() and content_type:
                raise ValueError(f"Contenu non-HTML ({content_type}): {url}")
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Erreur HTTP {exc.code} en recuperant {url}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Impossible de joindre {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ValueError(f"Delai depasse en recuperant {url}") from exc

    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
    charset = response.headers.get_content_charset() or "utf-8"
    html = raw.decode(charset, errors="replace")

    title, text = extract_readable_text(html)
    if not text:
        raise ValueError(f"Aucun contenu lisible extrait de {url}")
    return title, text
