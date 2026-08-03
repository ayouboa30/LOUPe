"""Guards on the magnifying-glass flow: drag-select -> OCR -> web search ->
compact -> explain.

No question is asked of the user, and no auto full-screen capture happens.
An earlier version opened a text dialog first, which is where the flow
died: the dialog could land behind other windows, and cancelling it left
the mascot busy with nothing on screen to say why. A later version
auto-captured the whole screen and swept the mascot across it while
waiting, which the user found didn't match what was actually being
captured. Capture is now a deliberate mouse drag-select, so a short but
meaningful selection (a single error message) is the normal case, not
noise to filter out the way a full-screen grab's incidental chrome was.
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


def test_an_empty_capture_produces_no_prompt() -> None:
    """Better to say nothing was readable than to ask the model about noise."""

    assert build_screen_reading_prompt("") is None
    assert build_screen_reading_prompt("   \n  \n ") is None


def test_a_short_but_deliberately_selected_message_is_not_rejected() -> None:
    """The user drag-selected this on purpose - a short error is real content,
    not incidental chrome the way it would be in a full-screen grab."""

    prompt = build_screen_reading_prompt("ZeroDivisionError: division by zero")

    assert prompt is not None
    assert "ZeroDivisionError" in prompt


def test_prompt_asks_for_an_explanation_and_never_for_a_question() -> None:
    capture = "Erreur 404 - la page demandee est introuvable sur ce serveur"

    prompt = build_screen_reading_prompt(capture)

    assert prompt is not None
    assert "Explique ce qui est affiche" in prompt
    assert capture in prompt
    # The model is told the input is noisy OCR, so it does not treat
    # interface fragments as content.
    assert "OCR" in prompt


def test_screen_search_prompt_grounds_the_answer_in_search_results() -> None:
    from three_loop.assistant_actions import build_screen_search_prompt
    from three_loop.models import SearchResult

    sources = [
        SearchResult(
            url="https://example.org/fix", title="Fix for this error",
            snippet="Set the denominator to a non-zero value.",
        )
    ]

    prompt = build_screen_search_prompt("ZeroDivisionError: division by zero", sources)

    assert prompt is not None
    assert "RESULTATS DE RECHERCHE" in prompt
    assert "Fix for this error" in prompt
    assert "denominator" in prompt


def test_screen_search_prompt_is_none_for_an_empty_capture() -> None:
    from three_loop.assistant_actions import build_screen_search_prompt

    assert build_screen_search_prompt("", []) is None


def test_mic_privacy_policy_error_gets_a_clear_actionable_message(monkeypatch) -> None:
    """Measured root cause of "le micro ne marche pas": Windows requires the
    user to have opted into online speech recognition via Settings first.
    There is no API to accept that policy on the user's behalf, so the fix
    is a clear message (and opening the right Settings page), not a retry.
    """

    import three_loop.assistant_actions as aa

    def boom_asyncio_run(coro):
        coro.close()
        raise OSError(22, "speech privacy policy not accepted", None, -2147199735)

    opened = []
    monkeypatch.setattr(aa.asyncio, "run", boom_asyncio_run)
    monkeypatch.setattr(aa, "_open_speech_privacy_settings", lambda: opened.append(True))

    try:
        aa.listen_and_transcribe(timeout_seconds=1)
        assert False, "should have raised"
    except RuntimeError as exc:
        assert "reconnaissance vocale" in str(exc).lower()
        assert opened == [True]
