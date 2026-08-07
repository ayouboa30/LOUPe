import io

from three_loop.documents import extract_text
from three_loop.research.storage import ResearchWorkspace


def test_extracts_plain_text() -> None:
    assert extract_text("notes.txt", b"Contenu important du projet.") == (
        "Contenu important du projet."
    )


def test_extracts_pdf_text() -> None:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    doc = canvas.Canvas(buf)
    doc.drawString(100, 700, "Rapport de test 3loop")
    doc.save()

    text = extract_text("rapport.pdf", buf.getvalue())

    assert "Rapport de test 3loop" in text


def test_rejects_unsupported_format() -> None:
    try:
        extract_text("image.png", b"\x89PNG")
        assert False, "should have raised"
    except ValueError as exc:
        assert "non supporte" in str(exc)


def test_rejects_empty_extraction() -> None:
    try:
        extract_text("vide.txt", b"   \n  ")
        assert False, "should have raised"
    except ValueError as exc:
        assert "Aucun texte" in str(exc)


def test_corrupt_pdf_raises_a_clear_error() -> None:
    try:
        extract_text("casse.pdf", b"not a real pdf")
        assert False, "should have raised"
    except ValueError as exc:
        assert "illisible" in str(exc) or "corrompu" in str(exc)


def test_document_context_selects_relevant_local_excerpt_with_global_budget(tmp_path) -> None:
    workspace = ResearchWorkspace(tmp_path / "research")
    unrelated = workspace.import_document(
        "general.txt",
        ("Notes générales sans rapport avec la question. " * 120).encode("utf-8"),
    )
    relevant = workspace.import_document(
        "orion.txt",
        (
            ("Préambule documentaire sans détail utile. " * 120)
            + "Le protocole ORION chiffre les sauvegardes locales avec AES-256. "
            + ("Annexe de contexte. " * 80)
        ).encode("utf-8"),
    )

    context = workspace.document_context(
        [unrelated["version_id"], relevant["version_id"]],
        "Quel chiffrement utilise le protocole ORION ?",
        max_tokens=96,
    )

    assert "ORION" in context["text"]
    assert "AES-256" in context["text"]
    assert context["version_ids"] == [unrelated["version_id"], relevant["version_id"]]
    assert len(context["text"]) <= int(96 * 3.6)
    assert len(context["excerpts"]) >= 1
