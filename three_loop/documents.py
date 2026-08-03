"""Text extraction for documents attached from the UI.

No storage layer: extraction is stateless, request in, compacted text out.
The browser is what holds the attached-documents list (name, text,
included-or-not) across a session - the same pattern already used for a
scraped URL's content, just for a locally-picked file instead of a link.
"""

from __future__ import annotations

import io

from .compact import compact_text

#: A large PDF/doc dumped whole would dominate a single prompt at
#: 14.3 ms/token; capped the same way OCR captures and scraped pages are.
_DOCUMENT_MAX_TOKENS = 2000


def extract_text(name: str, data: bytes) -> str:
    """Extract and compact readable text from an uploaded file's bytes.

    Raises ``ValueError`` with a message fit to show the user directly on
    anything that stops readable text coming back (unsupported format,
    corrupt file, empty extraction).
    """

    lower = name.lower()
    if lower.endswith(".pdf"):
        raw = _extract_pdf_text(data)
    elif lower.endswith((".txt", ".md", ".markdown", ".csv", ".log")):
        raw = data.decode("utf-8", errors="replace")
    else:
        raise ValueError(
            f"Format non supporte pour {name} (pdf, txt, md, csv, log uniquement)."
        )
    compacted = compact_text(raw, max_tokens=_DOCUMENT_MAX_TOKENS)
    if not compacted.strip():
        raise ValueError(f"Aucun texte lisible extrait de {name}.")
    return compacted


def _extract_pdf_text(data: bytes) -> str:
    try:
        import pypdf
    except ImportError as exc:
        raise ValueError(
            "Le support PDF necessite le paquet 'pypdf' (pip install pypdf)."
        ) from exc

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"PDF illisible ou corrompu: {exc}") from exc

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            continue  # one broken page must not sink the rest of the document
    return "\n\n".join(pages)
