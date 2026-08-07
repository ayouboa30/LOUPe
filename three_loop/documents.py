"""Text extraction for documents attached from the UI.

No storage layer: extraction is stateless, request in, compacted text out.
The browser is what holds the attached-documents list (name, text,
included-or-not) across a session - the same pattern already used for a
scraped URL's content, just for a locally-picked file instead of a link.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from .compact import compact_text


@dataclass(frozen=True)
class ExtractedPage:
    """One stable extraction unit before prompt compaction.

    ``index`` is one-based and follows the physical PDF order.  Keeping the
    un-compacted text here is what lets the scientific workspace preserve
    source offsets while the legacy ``extract_text`` API remains bounded for
    prompts.
    """

    index: int
    label: str
    text: str
    method: str
    error: str = ""

#: A large PDF/doc dumped whole would dominate a single prompt at
#: 14.3 ms/token; capped the same way OCR captures and scraped pages are.
_DOCUMENT_MAX_TOKENS = 2000


def extract_pages(name: str, data: bytes) -> tuple[ExtractedPage, ...]:
    """Extract source-preserving pages without compacting or merging them.

    Text-like and image inputs are represented as a single logical page.  A
    broken PDF page is retained with an error marker so a partial extraction
    never shifts the numbering of every page that follows it.
    """

    lower = name.lower()
    if lower.endswith(".pdf"):
        pages = _extract_pdf_pages(data)
    elif lower.endswith((".txt", ".md", ".markdown", ".csv", ".log")):
        pages = (
            ExtractedPage(1, "1", data.decode("utf-8", errors="replace"), "text_decode"),
        )
    elif lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")):
        pages = (ExtractedPage(1, "1", _extract_image_ocr(data), "windows_ocr"),)
    else:
        raise ValueError(
            f"Format non supporte pour {name} (pdf, texte ou image png/jpg/webp uniquement)."
        )
    if not any(page.text.strip() for page in pages):
        raise ValueError(f"Aucun texte lisible extrait de {name}.")
    return pages


def extract_text(name: str, data: bytes) -> str:
    """Extract compact text for the legacy prompt-attachment endpoint.

    The persistent workspace calls :func:`extract_pages` directly; this
    compatibility function deliberately performs compaction only after page
    provenance has had a chance to be retained.
    """

    pages = extract_pages(name, data)
    raw = "\n\n".join(page.text for page in pages)
    compacted = compact_text(raw, max_tokens=_DOCUMENT_MAX_TOKENS)
    if not compacted.strip():
        raise ValueError(f"Aucun texte lisible extrait de {name}.")
    return compacted


def _extract_image_ocr(data: bytes) -> str:
    """Read a screenshot attachment with the same Windows OCR path as the mascot."""

    try:
        from PIL import Image

        from .assistant_actions import ocr_image
        with Image.open(io.BytesIO(data)) as image:
            return ocr_image(image.convert("RGB"))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            "Image non supporte ou illisible: installez le pack OCR Windows "
            "et relancez 3loop."
        ) from exc


def _extract_pdf_pages(data: bytes) -> tuple[ExtractedPage, ...]:
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

    pages: list[ExtractedPage] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
            pages.append(ExtractedPage(index, str(index), text, "pypdf"))
        except Exception as exc:
            # Preserve the physical slot: silently skipping it would make all
            # later citation page numbers incorrect.
            pages.append(
                ExtractedPage(
                    index,
                    str(index),
                    "",
                    "pypdf",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return tuple(pages)


def _extract_pdf_text(data: bytes) -> str:
    """Compatibility wrapper retained for callers outside this module."""

    return "\n\n".join(page.text for page in _extract_pdf_pages(data))
