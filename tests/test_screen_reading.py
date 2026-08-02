"""Guards on the magnifying-glass flow: capture -> OCR -> compact -> explain.

No question is asked of the user. An earlier version opened a text dialog
first, which is where the flow died: the dialog could land behind other
windows, and cancelling it left the mascot busy with nothing on screen to
say why.
"""

from three_loop.assistant_actions import build_screen_reading_prompt, compact_screen_text


def test_keeps_the_top_of_the_screen_not_the_bottom() -> None:
    """OCR reads top-to-bottom: the content is first, the taskbar is last.

    Trimming from the front (right for a conversation history) drops exactly
    what needs explaining - measured: a Python traceback vanished entirely.
    """

    capture = (
        "Traceback (most recent call last)\n"
        "ZeroDivisionError: division by zero\n"
        + "\n".join(f"barre des taches {i}" for i in range(4000))
    )

    compacted = compact_screen_text(capture, max_tokens=200)

    assert "ZeroDivisionError" in compacted
    assert "omis" in compacted  # the cut is signposted, not silent


def test_repeated_interface_chrome_is_deduplicated() -> None:
    """Menu labels and tab titles repeat across a full-screen grab."""

    capture = "Contenu important\n" + "Menu Demarrer\n" * 300

    compacted = compact_screen_text(capture)

    assert compacted.count("Menu Demarrer") == 1
    assert "Contenu important" in compacted


def test_an_empty_or_chrome_only_capture_produces_no_prompt() -> None:
    """Better to say nothing was readable than to ask the model about noise."""

    assert build_screen_reading_prompt("") is None
    assert build_screen_reading_prompt("   \n  \n ") is None
    assert build_screen_reading_prompt("Fichier Edition Aide") is None


def test_prompt_asks_for_an_explanation_and_never_for_a_question() -> None:
    capture = "Erreur 404 - la page demandee est introuvable sur ce serveur"

    prompt = build_screen_reading_prompt(capture)

    assert prompt is not None
    assert "Explique ce qui est affiche" in prompt
    assert capture in prompt
    # The model is told the input is noisy OCR, so it does not treat
    # interface fragments as content.
    assert "OCR" in prompt
