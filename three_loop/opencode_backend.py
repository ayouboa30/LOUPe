"""Backend delegating generation to a locally installed OpenCode CLI.

Local inference on this hardware tops out at what a ~3B model can do, and
measured evaluation puts that at 92% on simple verifiable tasks and much
lower on anything requiring real knowledge. Routing the debate through
OpenCode lifts that ceiling to whatever frontier model the user has
configured, while 3loop keeps what it is actually good at: the multi-agent
concertation, the OCR capture and the voice input.

The subprocess runs **windowless but not hidden**: no console flashes on
screen, yet every invocation is logged to ``~/.3loop/opencode.log`` and the
UI shows that an external process is running. A background process nobody
can see is impossible to debug and hard to justify in an open-source tool;
this keeps the convenience without the opacity.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .backend import SharedLLMBackend
from .models import SourceMatch, TaskKind
from .skills import load_skill

#: Windows-only flag that starts the child without allocating a console.
#: Without it every call flashes a black window over whatever the user is
#: doing; it does not make the process any less visible to Task Manager.
_CREATE_NO_WINDOW = 0x08000000

DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

#: 3loop forwards arbitrary user questions verbatim, so the agent it hands
#: them to matters - but ``opencode run --agent`` only accepts *primary*
#: agents (``opencode agent list``: build, compaction, plan, summary,
#: title). Subagents like ``explore``/``general`` are not valid here: passed
#: anyway, the CLI prints "is a subagent, not a primary agent. Falling back
#: to default agent" to stderr and silently uses ``build`` regardless - a
#: first version of this backend requested ``explore`` and got ``build``
#: every single time without ever surfacing the warning.
#:
#: Of the five primary agents, ``plan`` refuses the debate protocol outright
#: (answers "I'm in plan mode, what's the debate topic?"), and
#: compaction/summary/title are single-purpose internals. ``build`` is what
#: actually answers the protocol, so it is used explicitly - narrowing
#: nothing, since every agent inherits
#: ``{"permission": "*", "action": "allow"}`` from the user's global config
#: regardless of which one is named. The isolated working directory below is
#: the real containment; the agent name is not a sandbox.
DEFAULT_AGENT = "build"


def _workspace() -> Path:
    """An empty directory to run OpenCode in.

    Defence in depth: the agent choice limits which tools exist, this limits
    what they could reach. Running in the user's project directory would put
    their real files one bad completion away from a file-touching tool.
    """

    path = Path.home() / ".3loop" / "opencode-workspace"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.home()
    return path


def _log_path() -> Path:
    return Path.home() / ".3loop" / "opencode.log"


def _append_log(text: str) -> None:
    """Record one invocation. Failures here must never break a run."""

    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except OSError:
        pass


def find_opencode() -> str | None:
    """Locate the OpenCode executable, including the npm global shim."""

    found = shutil.which("opencode")
    if found:
        return found
    # npm installs a .cmd shim on Windows that `which` may miss depending on
    # PATHEXT; check the standard global prefix directly.
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / "opencode.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode.cmd",
        Path.home() / ".opencode" / "bin" / "opencode",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def list_models(timeout: float = 60.0) -> list[str]:
    """Return the model ids OpenCode reports, or an empty list if unavailable."""

    executable = find_opencode()
    if executable is None:
        return []
    try:
        completed = subprocess.run(
            [executable, "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if "/" in line and not line.strip().startswith(("#", "-"))
    ]


#: Appended as the last paragraph, never as the opening one: tried as an
#: opening constraint it gets answered rather than followed ("Understood,
#: what is your question?"). As a trailing note on a task the agent has
#: already read in full, it is obeyed.
_NO_FILES_CONSTRAINT = (
    "Contrainte: reponds uniquement a partir de tes connaissances, sans "
    "consulter, explorer ni modifier aucun fichier. Renvoie uniquement "
    "l'objet JSON demande, rien d'autre."
)


def build_opencode_prompt(
    *,
    instruction: str,
    task: str,
    kind: TaskKind,
    history: str = "",
    sources: Sequence[SourceMatch] = (),
    research_digest: str = "",
) -> str:
    """Build a prompt from scratch for the OpenCode backend.

    Deliberately a separate template from ``prompting.build_prefix``/
    ``with_role`` rather than a transformation of their output. That local
    template is shaped to help llama.cpp reuse its KV prefix, a concern
    that does not exist for OpenCode (a fresh agent subprocess every call);
    and its ``3LOOP_ACTION=...`` protocol markers read, to a coding agent,
    as the name of a project to go inspect - measured: asked to state the
    Arzela-Ascoli theorem, the agent instead described this repository's
    ``pipeline.py`` line by line, once it found it on disk.

    Two things were measured necessary to get a real answer instead of a
    clarifying question or an invented schema:

    * ``instruction`` must open the message and state the job in plain
      prose - a heading-like opener ("FORMATTING RULES:") gets answered
      ("What formatting rules would you like?") instead of acted on.
    * the file-access constraint must close the message, not open it, for
      the same reason.
    """

    parts = [instruction.strip()]
    skill = load_skill(kind)
    if skill:
        parts.append(f"Regles de formatage:\n{skill}")
    parts.append(f"Tache:\n{task}")
    if research_digest.strip():
        parts.append(f"Resume de recherche:\n{research_digest.strip()}")
    elif sources:
        parts.append(
            "Sources:\n" + "\n".join(f"- {source.url}" for source in sources)
        )
    if history.strip() and not history.startswith("(Aucun historique"):
        parts.append(f"Cycles precedents:\n{history}")
    parts.append(_NO_FILES_CONSTRAINT)
    return "\n\n".join(parts)


def _extract_text(stdout: str) -> str:
    """Concatenate the ``text`` events of OpenCode's NDJSON stream.

    ``opencode run --format json`` emits one JSON object per line
    (step_start / text / step_finish). Only the ``text`` parts carry the
    answer; anything unparseable is skipped rather than aborting, since a
    single malformed line should not lose a completed response.
    """

    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "text":
            continue
        part = event.get("part") or {}
        text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks).strip()


class OpenCodeBackend(SharedLLMBackend):
    """Run one completion per call through ``opencode run``.

    ``serialize_requests=False``: each call is its own short-lived process
    talking to a remote API, so they can overlap the way any HTTP backend
    does. Sessions are deliberately *not* continued between calls - each
    call already carries the task and history it needs via
    ``build_opencode_prompt``, and a carried-over OpenCode session would
    duplicate that context on top.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = 300.0,
        executable: str | None = None,
        agent: str = DEFAULT_AGENT,
    ) -> None:
        super().__init__(serialize_requests=False)
        resolved = executable or find_opencode()
        if resolved is None:
            raise RuntimeError(
                "OpenCode est introuvable. Installe-le (npm i -g opencode-ai) "
                "ou verifie qu'il est dans le PATH."
            )
        self.executable = resolved
        self.model = model
        self.timeout = timeout
        self.agent = agent

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        # OpenCode exposes neither temperature nor a token cap on `run`; the
        # provider defaults apply. Both are honoured by the local backends,
        # so behaviour differs here by design rather than by oversight.
        #
        # `system_prompt` is deliberately dropped: OpenCode agents already
        # carry their own, and prepending a second role assignment
        # ("You are the 3loop compact debate engine...") makes them answer
        # it conversationally - measured: "Understood. I'll run a
        # self-contained debate... What's the motion or topic?" instead of
        # doing the task. Callers talking to this backend build `prompt`
        # with `build_opencode_prompt`, which needs no such prefix.
        del temperature, max_tokens, system_prompt
        message = prompt

        import asyncio

        def invoke() -> str:
            started = time.time()
            try:
                completed = subprocess.run(
                    [
                        self.executable,
                        "run",
                        "--format",
                        "json",
                        "--agent",
                        self.agent,
                        "--dir",
                        str(_workspace()),
                        "-m",
                        self.model,
                        # No message argument here: it goes over stdin
                        # instead (see `input=` below). `opencode.cmd` is an
                        # npm-generated batch shim, and a multi-hundred-char
                        # argument containing quotes, backslashes (LaTeX)
                        # and newlines gets corrupted by Windows batch-file
                        # argument parsing when passed positionally - the
                        # CLI would receive an empty/garbled message and the
                        # agent answered "what is the debate subject?" even
                        # though the task was plainly in the source string.
                        # `opencode run` reads the message from stdin
                        # whenever none is given positionally, which sidesteps
                        # that parsing entirely.
                    ],
                    input=message,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    # ``--dir`` alone is not enough: the child inherits the
                    # parent's working directory, and a code-reading agent
                    # will happily walk it. Measured before this was set, the
                    # agent answered a maths question by describing
                    # ``pipeline.py`` line by line.
                    cwd=str(_workspace()),
                    creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except subprocess.TimeoutExpired:
                _append_log(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TIMEOUT après "
                    f"{self.timeout}s modele={self.model}"
                )
                raise RuntimeError(
                    f"OpenCode n'a pas repondu en {self.timeout:.0f}s."
                ) from None
            except OSError as exc:
                _append_log(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERREUR {exc}")
                raise RuntimeError(f"Impossible de lancer OpenCode: {exc}") from exc

            elapsed = time.time() - started
            text = _extract_text(completed.stdout)
            _append_log(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] modele={self.model} "
                f"code={completed.returncode} {elapsed:.1f}s "
                f"prompt={len(message)}c reponse={len(text)}c"
            )
            if not text:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(
                    "OpenCode n'a renvoye aucun texte"
                    + (f": {detail[:300]}" if detail else ".")
                )
            return text

        return await asyncio.to_thread(invoke)
