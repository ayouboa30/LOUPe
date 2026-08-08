"""Background actions triggered from the floating widget.

Three things live here:

- ``listen_and_transcribe``: one-shot voice capture via the native speech
  recognizer on Windows; other platforms return an explicit unavailable
  status instead of importing WinRT.
- ``capture_and_ocr``: whole-screen screenshot read with Windows OCR or the
  optional cross-platform Tesseract bridge, same offline, no-API-key story.
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
import sys
import threading
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageGrab

from .compact import compact_text
from .models import SearchResult
from .screen_watcher import INTERVAL_CHOICES_MINUTES

_LAST_CONFIG_PATH = Path.home() / ".3loop" / "last_run_config.json"


def load_last_run_config() -> dict[str, Any]:
    try:
        return json.loads(_LAST_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"backend": "demo"}


def save_last_run_config(payload: dict[str, Any]) -> None:
    try:
        _LAST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = {
            k: v
            for k, v in payload.items()
            if k not in ("prompt", "question", "session_id", "conversation", "conversation_append")
        }
        _LAST_CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
    except OSError:
        pass


#: Where the research-assistant mode remembers its own state. Deliberately a
#: separate file from last_run_config.json: that one is rewritten by the
#: server on every /api/run call (it mirrors the web UI's backend/model/key
#: choice), so anything the companion stored there would be dropped the next
#: time the user ran a prompt from the browser.
ASSISTANT_SETTINGS_PATH = Path.home() / ".3loop" / "assistant_settings.json"

#: Cadence used when nothing valid is persisted, in minutes. Mirrors
#: ``screen_watcher.DEFAULT_INTERVAL_SECONDS`` (300s) and is where every
#: unknown or malformed interval lands.
_DEFAULT_ASSISTANT_INTERVAL_MINUTES = 5


def _coerce_interval_minutes(value: Any) -> int:
    """Snap any value onto one of the intervals the menu actually offers.

    The cadence is not a free-form number: the companion's context menu
    exposes ``INTERVAL_CHOICES_MINUTES`` (2/5/10/15) as radio items, and a
    persisted value outside that set could never be shown as selected - the
    UI would look like nothing was chosen. Hand-edited files, an older build
    writing a different scale (seconds instead of minutes), or a caller
    passing a stray number all end up on the default instead.

    Never raises: it is called from both the loader and the saver, and
    neither is allowed to fail.
    """

    # bool is a subclass of int, so True would otherwise slip through as 1.
    if isinstance(value, bool):
        return _DEFAULT_ASSISTANT_INTERVAL_MINUTES
    if isinstance(value, (int, float)):
        candidate = int(value)
        if candidate in INTERVAL_CHOICES_MINUTES:
            return candidate
    return _DEFAULT_ASSISTANT_INTERVAL_MINUTES


def load_assistant_settings() -> dict[str, Any]:
    """Read the persisted research-assistant settings.

    Returns ``{"enabled": bool, "interval_minutes": int}``, always populated,
    so callers can index it without guarding.

    The mode is **off by default, and re-validated as off on anything
    dubious**. It periodically screenshots and OCRs whatever is on screen,
    which is only ever acceptable as an explicit, deliberate opt-in: a fresh
    install, a wiped profile, a truncated or hand-broken settings file must
    all mean "not watching". That is also why only a real JSON ``true`` turns
    it on - ``bool("false")`` is ``True``, and a truthy accident in the file
    must not start reading someone's screen.

    A missing file, invalid JSON, a JSON document that isn't an object, and
    wrong value types are all normal outcomes here (the file is user-visible
    in ``~/.3loop`` and written by a background thread that can be killed
    mid-write), so none of them raise.
    """

    settings: dict[str, Any] = {
        "enabled": False,
        "interval_minutes": _DEFAULT_ASSISTANT_INTERVAL_MINUTES,
    }
    try:
        raw = json.loads(ASSISTANT_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings
    if not isinstance(raw, dict):
        return settings
    settings["enabled"] = raw.get("enabled") is True
    settings["interval_minutes"] = _coerce_interval_minutes(raw.get("interval_minutes"))
    return settings


def save_assistant_settings(*, enabled: bool, interval_minutes: int) -> None:
    """Persist the research-assistant state so a restart keeps it.

    Keyword-only because ``(True, 5)`` at a call site reads as nothing at
    all, and the two arguments are trivial to swap.

    The interval is normalised before being written, so the file never holds
    a value the menu cannot display as selected even if a caller passes one.
    Like ``save_last_run_config``, filesystem errors are swallowed: this is
    called from the companion's UI callbacks, where a read-only or missing
    home directory must not take the mascot down - losing the preference is
    the acceptable outcome.
    """

    try:
        ASSISTANT_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": bool(enabled),
            "interval_minutes": _coerce_interval_minutes(interval_minutes),
        }
        ASSISTANT_SETTINGS_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _ocr_image_windows(image: Image.Image) -> str:
    """Read an image with the Windows OCR runtime."""

    try:
        import winocr
        from winrt.windows.media.ocr import OcrEngine
    except ImportError as exc:
        raise RuntimeError(
            "OCR Windows indisponible dans cette installation. Installez "
            "winocr, winrt-runtime et winrt-Windows.Media.Ocr, puis relancez "
            "3loop."
        ) from exc

    languages = list(OcrEngine.available_recognizer_languages)
    if not languages:
        raise RuntimeError(
            "Aucune langue OCR n'est installee sur Windows. Ouvrez "
            "Parametres > Heure et langue > Langue et region, installez une "
            "langue avec le module Reconnaissance vocale/OCR, puis relancez."
        )

    # Prefer French for this UI, then English, then the first language that
    # Windows actually exposes. A missing preferred pack is not an error.
    preferred = ("fr-FR", "fr", "en-US", "en")
    available = [str(item.language_tag) for item in languages]
    lang = next(
        (candidate for candidate in preferred if candidate in available),
        available[0],
    )

    async def _run() -> str:
        result = await winocr.recognize_pil(image.convert("RGB"), lang=lang)
        return (result.text or "").strip()

    try:
        return asyncio.run(_run())
    except Exception as exc:
        raise RuntimeError(
            f"Le moteur OCR Windows a echoue avec la langue {lang}: {exc}"
        ) from exc


def _ocr_image_tesseract(image: Image.Image) -> str:
    """Read an image with the cross-platform Tesseract command-line engine."""

    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError(
            "OCR indisponible sous Linux/macOS. Installez l'extra "
            "desktop-linux (`pip install pytesseract`) et le moteur système "
            "Tesseract (par exemple `sudo apt install tesseract-ocr "
            "tesseract-ocr-fra`)."
        ) from exc

    try:
        text = pytesseract.image_to_string(image.convert("RGB"), lang="fra+eng")
    except Exception as exc:
        raise RuntimeError(
            "Tesseract n'est pas disponible ou aucune langue fra+eng n'est "
            "installee. Installez `tesseract-ocr`, `tesseract-ocr-fra` et "
            "`tesseract-ocr-eng`, puis relancez 3loop."
        ) from exc
    return (text or "").strip()


def ocr_image(image: Image.Image) -> str:
    """Read an image with the native OCR engine available on this platform."""

    if sys.platform == "win32":
        return _ocr_image_windows(image)
    return _ocr_image_tesseract(image)


def capture_screen() -> Image.Image:
    """Capture all available screens where the platform backend supports it."""

    try:
        return ImageGrab.grab(all_screens=sys.platform == "win32")
    except TypeError:
        # Older Pillow releases do not expose ``all_screens``. Keep the
        # primary-screen fallback instead of breaking the whole OCR action.
        return ImageGrab.grab()


def capture_and_ocr() -> str:
    """Screenshot the whole screen and read it with the platform OCR engine."""

    return ocr_image(capture_screen())


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


def build_screen_reading_prompt(ocr_text: str, question: str | None = None) -> str | None:
    """Build a model prompt from OCR text and an optional user question."""

    compacted = compact_screen_text(ocr_text or "")
    if len(compacted) < _MIN_USEFUL_CHARS:
        return None
    question = (question or "").strip()
    question_part = (
        f"QUESTION DE L'UTILISATEUR:\n{question}\n\n"
        if question
        else "Aucune question precise n'a ete fournie: fais une lecture generale.\n\n"
    )
    return (
        "Voici le texte lu a l'ecran de l'utilisateur par OCR. Il est brut, "
        "non ordonne, et melange le contenu utile avec des elements "
        "d'interface (titres de fenetres, menus, barre des taches).\n\n"
        f"---\n{compacted}\n---\n\n"
        f"{question_part}"
        "Reponds d'abord a la question si elle existe. Explique ce qui est "
        "affiche: de quoi il s'agit, ce que la personne est en train de "
        "faire, et le point important a en retenir. Ignore les elements "
        "d'interface. Si quelque chose demande une action ou signale une "
        "erreur, dis-le en premier. Reponds en francais, de maniere concise."
    )


def _search_query_from_screen_text(
    compacted: str,
    *,
    question: str | None = None,
    max_chars: int = 120,
) -> str:
    """Create a concise web query from the capture and optional question."""

    question = (question or "").strip()
    first_line = next((line for line in compacted.splitlines() if len(line) > 8), compacted)
    if question:
        return f"{question} {first_line}"[:max_chars].strip()
    return first_line[:max_chars].strip()


def build_screen_search_prompt(
    ocr_text: str,
    sources: Sequence[SearchResult],
    question: str | None = None,
) -> str | None:
    """Build an OCR answer grounded in web results and an optional question."""

    compacted = compact_screen_text(ocr_text or "")
    if len(compacted) < _MIN_USEFUL_CHARS:
        return None
    rendered_sources = "\n".join(
        f"- {source.title or source.url} ({source.url}): {source.snippet}"
        for source in sources
    ) or "(aucun resultat trouve)"
    question = (question or "").strip()
    question_part = (
        f"QUESTION DE L'UTILISATEUR:\n{question}\n\n" if question else ""
    )
    return (
        "Voici le texte lu a l'ecran de l'utilisateur par OCR (brut, "
        "non ordonne, melange contenu utile et elements d'interface) et des "
        "resultats d'une recherche web lancee a partir de ce texte.\n\n"
        f"TEXTE A L'ECRAN:\n---\n{compacted}\n---\n\n"
        f"{question_part}"
        f"RESULTATS DE RECHERCHE:\n{rendered_sources}\n\n"
        "Reponds d'abord a la question si elle existe. Explique ce qui est "
        "affiche a l'ecran en t'appuyant sur les resultats de recherche quand "
        "ils sont pertinents (une erreur expliquee avec sa solution trouvee "
        "en ligne, un terme technique defini, etc.). Ignore les elements "
        "d'interface et les resultats hors sujet. Reponds en francais, de "
        "maniere concise."
    )


async def search_from_screen_text(
    ocr_text: str,
    *,
    question: str | None = None,
    max_results: int = 5,
) -> list[SearchResult]:
    """Run a web search seeded by OCR and an optional user question."""

    from .web import DuckDuckGoSearchProvider

    compacted = compact_screen_text(ocr_text or "")
    query = _search_query_from_screen_text(compacted, question=question)
    if not query:
        return []
    try:
        results = await DuckDuckGoSearchProvider().search(query, max_results=max_results)
        return list(results)
    except Exception:
        return []


def search_from_text(text: str, *, max_results: int = 4) -> list[SearchResult]:
    """Blocking, model-free web search for callers that have no event loop.

    The Win32 companion runs on a message pump plus plain daemon threads, and
    the periodic screen watcher drives its passes from its own timer thread:
    there is no asyncio loop in either to ``await search_from_screen_text``
    from. Owning the ``asyncio.run`` here keeps that plumbing in one place
    instead of scattering loop management through the UI code, and keeps the
    watcher's injected ``search`` callable a plain synchronous function.

    No model is involved - this is the raw search step, not the "explain the
    screen" flow - so it costs a single network round trip and can run on
    every watch pass.

    Failures return an empty list rather than propagating. Two distinct
    things can go wrong and both must stay quiet: the search itself (already
    absorbed by ``search_from_screen_text``) and ``asyncio.run`` refusing to
    start - it raises if the calling thread already owns a running loop.
    Suggestions here are unsolicited, offered while the user is doing
    something else, so having nothing to say is a normal outcome; killing the
    watcher thread mid-pass is not.

    ``max_results`` defaults lower than the async version's: these land in a
    small speech bubble, where a handful of links is all that fits.
    """

    # Built before the try so it can be closed explicitly below: when
    # asyncio.run refuses to start, the coroutine object has already been
    # created and never runs, which makes Python print
    # "RuntimeWarning: coroutine ... was never awaited" to stderr. A silent
    # failure that still writes to the console is not silent, and this runs
    # unattended on every watch pass.
    pending = search_from_screen_text(text, max_results=max_results)
    try:
        return asyncio.run(pending)
    except Exception:
        pending.close()  # no-op if it already ran to completion
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
    """Blocking one-shot dictation via the native speech stack."""

    if sys.platform != "win32":
        raise RuntimeError(
            "La dictee native n'est pas encore disponible sous Linux/macOS. "
            "Utilise le champ texte de l'interface; le portage Whisper/Vosk "
            "pourra etre ajoute sans activer de service cloud."
        )

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

    if sys.platform != "win32":
        return
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




def run_research_in_background(
    question: str,
    *,
    port: int,
    on_done: Callable[[list[dict[str, str]], bool], None],
    on_started: Callable[[], None] | None = None,
) -> None:
    """Run the standalone triangulated web search without blocking the mascot."""

    def _worker() -> None:
        if on_started is not None:
            try:
                on_started()
            except Exception:
                pass

        payload = dict(load_last_run_config())
        payload["question"] = question
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/research",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        sources: list[dict[str, str]] = []
        success = False
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                event_name = None
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.startswith("event:"):
                        event_name = line[len("event:") :].strip()
                    elif line.startswith("data:") and event_name:
                        data = json.loads(line[len("data:") :].strip())
                        if event_name == "research_completed":
                            sources = list(data.get("sources") or data.get("results") or [])
                            success = not bool(data.get("error"))
                        elif event_name == "error":
                            success = False
                        event_name = None
        except Exception:
            success = False

        try:
            on_done(sources, success)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()
