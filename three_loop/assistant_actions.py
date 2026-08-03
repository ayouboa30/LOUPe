"""Background actions triggered from the floating widget.

Three things live here:

- ``listen_and_transcribe``: one-shot voice capture via the modern WinRT
  speech recognizer (``Windows.Media.SpeechRecognition``), fully offline,
  no extra API key.
- ``capture_and_ocr``: whole-screen screenshot read with the Windows OCR
  engine (``Windows.Media.Ocr``), same offline, no-API-key story.
- ``run_prompt_in_background``: fires the resulting text at the local
  3loop engine (the same HTTP+SSE endpoint the web UI uses) on a
  background thread and hands the final answer to a callback, so the
  widget can pop a toast without blocking the UI thread.

The widget reuses whatever backend/model/API key the user last configured
in the web UI (persisted by server.py on every /api/run call) so a
mic/OCR question gets answered the same way a typed one would.
"""

from __future__ import annotations

import asyncio
import os
import json
import threading
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageGrab

from .compact import compact_text
from .models import SearchResult

_LAST_CONFIG_PATH = Path.home() / ".3loop" / "last_run_config.json"


def load_last_run_config() -> dict[str, Any]:
    try:
        return json.loads(_LAST_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"backend": "demo"}


def save_last_run_config(payload: dict[str, Any]) -> None:
    try:
        _LAST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = {k: v for k, v in payload.items() if k not in ("prompt", "session_id")}
        _LAST_CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    except OSError:
        pass


def ocr_image(image: Image.Image) -> str:
    """Read an already-captured image with the Windows OCR engine.

    Split from the capture step so the caller controls *what* gets grabbed:
    the magnifying glass now lets the user drag-select a region instead of
    always taking the whole screen, and this function doesn't need to know
    which.
    """

    import winocr
    from winrt.windows.media.ocr import OcrEngine

    languages = list(OcrEngine.available_recognizer_languages)
    if not languages:
        raise RuntimeError(
            "Aucune langue OCR installee sur Windows. Parametres > Heure et "
            "langue > Langue et region > Ajouter un pack de langue avec OCR."
        )
    lang = languages[0].language_tag

    async def _run() -> str:
        result = await winocr.recognize_pil(image, lang=lang)
        return result.text

    return asyncio.run(_run())


def capture_and_ocr() -> str:
    """Screenshot the whole screen and read it with the Windows OCR engine."""

    return ocr_image(ImageGrab.grab())


#: A full-screen OCR pass returns everything: window titles, the taskbar,
#: menu labels. At ~14 ms per prompt token that noise costs real time, so the
#: text is compacted and capped before it reaches the model.
_SCREEN_TEXT_MAX_TOKENS = 900

#: Below this, there is nothing to work with at all (OCR found no text, or
#: only a stray character). Deliberately low: capture is now a user-driven
#: drag-select rather than an automatic full-screen grab, so a short but
#: meaningful selection - a single error message, a label - is the normal
#: case, not noise to filter out. Measured: a 34-character error message
#: ("ZeroDivisionError division by zero") was being silently dropped by the
#: higher threshold this used to be, tuned for full-screen captures where
#: most of the text was incidental UI chrome.
_MIN_USEFUL_CHARS = 6


def compact_screen_text(ocr_text: str, *, max_tokens: int = _SCREEN_TEXT_MAX_TOKENS) -> str:
    """Compact an OCR capture, keeping the *top* of the screen.

    Two things differ from the generic ``compact_text`` and both matter:

    * **Keep the head, not the tail.** OCR returns text roughly top to
      bottom, so the content the user is looking at comes first and the
      taskbar/status bar comes last. Trimming from the front - which is
      right for a conversation history - throws away exactly the part worth
      explaining. Measured on a capture containing a Python traceback: the
      generic trimmer dropped the exception entirely.
    * **Deduplicate lines.** A full-screen grab repeats menu labels, tab
      titles and taskbar entries; the same fragment can appear a dozen
      times and each copy costs prompt tokens for nothing.
    """

    cleaned = compact_text(ocr_text or "")
    seen: set[str] = set()
    kept: list[str] = []
    budget = int(max_tokens * 3.6)  # ~chars per token for this tokenizer family
    used = 0
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        if used + len(line) + 1 > budget:
            kept.append("[...bas de l'ecran omis...]")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def build_screen_reading_prompt(ocr_text: str) -> str | None:
    """Turn raw OCR output into a prompt asking the model to explain it.

    Returns ``None`` when the capture holds nothing worth sending, so the
    caller can say so instead of asking the model to comment on an empty
    screen.
    """

    compacted = compact_screen_text(ocr_text or "")
    if len(compacted) < _MIN_USEFUL_CHARS:
        return None
    return (
        "Voici le texte lu a l'ecran de l'utilisateur par OCR. Il est brut, "
        "non ordonne, et melange le contenu utile avec des elements "
        "d'interface (titres de fenetres, menus, barre des taches).\n\n"
        f"---\n{compacted}\n---\n\n"
        "Explique ce qui est affiche: de quoi il s'agit, ce que la personne "
        "est en train de faire, et le point important a en retenir. Ignore "
        "les elements d'interface. Si quelque chose demande une action ou "
        "signale une erreur, dis-le en premier. Reponds en francais, de "
        "maniere concise."
    )


def _search_query_from_screen_text(compacted: str, *, max_chars: int = 120) -> str:
    """A short query from OCR'd text: its first non-trivial line.

    The full capture is often several sentences; search engines want a
    handful of words, not a paragraph, so only the opening fragment (the
    part of the screen the user's eye lands on first) is used.
    """

    first_line = next((line for line in compacted.splitlines() if len(line) > 8), compacted)
    return first_line[:max_chars].strip()


def build_screen_search_prompt(ocr_text: str, sources: Sequence[SearchResult]) -> str | None:
    """Explain a screen capture *and* what a web search about it turned up.

    Where ``build_screen_reading_prompt`` only has the OCR text to go on,
    this also hands the model a handful of search results so its answer can
    be grounded in something beyond what's visible on screen - e.g. an error
    message explained with the fix that shows up when you search for it.
    """

    compacted = compact_screen_text(ocr_text or "")
    if len(compacted) < _MIN_USEFUL_CHARS:
        return None
    rendered_sources = "\n".join(
        f"- {source.title or source.url} ({source.url}): {source.snippet}"
        for source in sources
    ) or "(aucun resultat trouve)"
    return (
        "Voici le texte lu a l'ecran de l'utilisateur par OCR (brut, "
        "non ordonne, melange contenu utile et elements d'interface) et des "
        "resultats d'une recherche web lancee a partir de ce texte.\n\n"
        f"TEXTE A L'ECRAN:\n---\n{compacted}\n---\n\n"
        f"RESULTATS DE RECHERCHE:\n{rendered_sources}\n\n"
        "Explique ce qui est affiche a l'ecran en t'appuyant sur les "
        "resultats de recherche quand ils sont pertinents (une erreur "
        "expliquee avec sa solution trouvee en ligne, un terme technique "
        "defini, etc.). Ignore les elements d'interface et les resultats "
        "hors sujet. Reponds en francais, de maniere concise."
    )


async def search_from_screen_text(ocr_text: str, *, max_results: int = 5) -> list[SearchResult]:
    """Run a web search seeded by what was read off the screen.

    Failures here (network down, DuckDuckGo unreachable) return an empty
    list rather than raising: the caller can still explain the screen
    content from OCR alone, which is strictly better than failing the whole
    action because the web search step didn't work.
    """

    from .web import DuckDuckGoSearchProvider

    compacted = compact_screen_text(ocr_text or "")
    query = _search_query_from_screen_text(compacted)
    if not query:
        return []
    try:
        results = await DuckDuckGoSearchProvider().search(query, max_results=max_results)
        return list(results)
    except Exception:
        return []


#: HRESULT 0x80045509 ("the speech privacy policy was not accepted"),
#: surfaced by pythonnet/winrt as this negative decimal. Windows requires
#: the *user* to have opted into online speech recognition once via
#: Settings before any WinRT app - UWP or plain desktop - can use it; there
#: is no API to accept the policy on the user's behalf. Measured directly:
#: a first, un-diagnosed version of this function just failed with "le
#: micro ne marche pas" and no indication why.
_PRIVACY_POLICY_NOT_ACCEPTED = -2147199735


def listen_and_transcribe(timeout_seconds: float = 12.0) -> str:
    """Blocking one-shot dictation via the WinRT speech recognizer."""

    from winrt.windows.media.speechrecognition import (
        SpeechRecognizer,
        SpeechRecognizerState,
    )

    async def _run() -> str:
        recognizer = SpeechRecognizer()
        await recognizer.compile_constraints_async()
        try:
            result = await asyncio.wait_for(
                recognizer.recognize_async(), timeout=timeout_seconds
            )
            return (result.text or "").strip()
        finally:
            if recognizer.state != SpeechRecognizerState.IDLE:
                try:
                    await recognizer.stop_recognition_async()
                except Exception:
                    pass

    try:
        return asyncio.run(_run())
    except OSError as exc:
        if getattr(exc, "winerror", None) == _PRIVACY_POLICY_NOT_ACCEPTED:
            _open_speech_privacy_settings()
            raise RuntimeError(
                "La reconnaissance vocale en ligne de Windows n'est pas "
                "activee. J'ai ouvert les parametres : Confidentialite et "
                "securite > Voix > active 'Reconnaissance vocale en ligne', "
                "puis reessaie."
            ) from exc
        raise


def _open_speech_privacy_settings() -> None:
    """Open Windows' speech privacy settings page, best-effort."""

    try:
        os.startfile("ms-settings:privacy-speech")  # noqa: S606 - user-facing Settings URI
    except OSError:
        pass


def run_prompt_in_background(
    prompt: str,
    *,
    port: int,
    on_done: Callable[[str, bool], None],
    on_started: Callable[[], None] | None = None,
) -> None:
    """POST ``prompt`` to the local engine; call ``on_done(answer, success)`` once it's back.

    ``on_started`` fires the moment the request is dispatched, before any
    network round trip. Without it, a slow-but-working backend (a CLI
    coding agent measured at 20-65s per call, or a multi-cycle debate) is
    indistinguishable from the widget having silently done nothing - there
    is no progress feedback between the click and the eventual toast.
    """

    def _worker() -> None:
        if on_started is not None:
            try:
                on_started()
            except Exception:
                pass
        payload = dict(load_last_run_config())
        payload["prompt"] = prompt
        payload["session_id"] = "widget"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/run",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        final_solution: str | None = None
        error_message: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                event_name = None
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.startswith("event:"):
                        event_name = line[len("event:") :].strip()
                    elif line.startswith("data:") and event_name:
                        data = json.loads(line[len("data:") :].strip())
                        if event_name == "run_completed":
                            final_solution = data.get("final_solution")
                        elif event_name == "error":
                            error_message = data.get("message")
                        event_name = None
        except Exception as exc:  # network/engine failure while backgrounded
            error_message = str(exc)

        if final_solution:
            on_done(final_solution, True)
        else:
            on_done(error_message or "Aucune reponse recue.", False)

    threading.Thread(target=_worker, daemon=True).start()


