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
import json
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PIL import ImageGrab

from .compact import compact_text

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


def capture_and_ocr() -> str:
    """Screenshot the whole screen and read it with the Windows OCR engine."""

    import winocr
    from winrt.windows.media.ocr import OcrEngine

    image = ImageGrab.grab()
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


#: A full-screen OCR pass returns everything: window titles, the taskbar,
#: menu labels. At ~14 ms per prompt token that noise costs real time, so the
#: text is compacted and capped before it reaches the model.
_SCREEN_TEXT_MAX_TOKENS = 900

#: Below this, the capture is menu chrome rather than content worth reading.
_MIN_USEFUL_CHARS = 40


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

    return asyncio.run(_run())


def run_prompt_in_background(
    prompt: str,
    *,
    port: int,
    on_done: Callable[[str, bool], None],
) -> None:
    """POST ``prompt`` to the local engine; call ``on_done(answer, success)`` once it's back."""

    def _worker() -> None:
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


