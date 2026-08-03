import io

from three_loop.documents import extract_text


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
