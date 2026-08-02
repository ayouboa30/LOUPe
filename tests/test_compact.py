from three_loop.compact import compact_text, strip_vowels


def test_compact_text_collapses_whitespace_and_blank_lines() -> None:
    assert compact_text("a   b\n\n\n\nc") == "a b\n\nc"


def test_compact_text_drops_filler_phrases_but_keeps_the_content() -> None:
    out = compact_text("Il est important de noter que le gradient explose.")
    assert out == "le gradient explose."


def test_compact_text_keeps_wording_readable() -> None:
    """The pipeline feeds this back to the model - it must stay real text."""

    assert compact_text("Bonjour, le monde! 42.") == "Bonjour, le monde! 42."


def test_compact_text_budget_keeps_the_tail_and_marks_the_cut() -> None:
    text = "\n\n".join(f"paragraphe numero {i}" for i in range(200))

    out = compact_text(text, max_tokens=50)

    assert len(out) < len(text)
    assert "contexte ancien omis" in out
    assert "paragraphe numero 199" in out  # most recent content survives
    assert "paragraphe numero 0\n" not in out


def test_compact_text_under_budget_is_untouched() -> None:
    assert compact_text("court", max_tokens=100) == "court"


def test_strip_vowels_is_not_used_by_compact_text() -> None:
    """Vowel stripping measured *worse* (+17% tokens); it must stay opt-in.

    Kept as an explicit guard so it cannot silently come back into the
    prefill path - see the module docstring for the measurement.
    """

    assert strip_vowels("Bonjour le monde") == "Bnjr l mnd"
    assert compact_text("Bonjour le monde") == "Bonjour le monde"
