"""Shared machinery for backends that delegate to an external coding-agent CLI.

Extracted from what was originally OpenCode-only code once two more CLIs
(Claude Code, Codex) needed the exact same shape: locate the executable,
run it windowless with the prompt on stdin (never as a CLI argument - a
multi-hundred-char argument with quotes/backslashes/newlines gets corrupted
by Windows batch-file shims, measured on OpenCode's own ``.cmd``), isolate
it from the user's real files, log every invocation, and build a prompt
that states the job in plain prose instead of 3loop's internal protocol
markers (which read to a coding agent as a project to go inspect - measured
on OpenCode: asked a maths question, it described this repo's pipeline.py).

Each subclass only supplies its own argv and its own output parser; the
process plumbing here is identical across all three.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from abc import abstractmethod
from collections.abc import Sequence
from pathlib import Path

from .backend import SharedLLMBackend
from .models import SourceMatch, TaskKind
from .skills import load_skill

#: Windows-only flag that starts the child without allocating a console.
#: Without it every call flashes a black window over whatever the user is
#: doing; it does not make the process any less visible to Task Manager.
_CREATE_NO_WINDOW = 0x08000000


def find_executable(name: str, *extra_candidates: Path) -> str | None:
    """Locate a CLI on PATH, falling back to common npm/user-install paths.

    ``shutil.which`` alone misses npm's Windows ``.cmd`` shims often enough
    (depends on ``PATHEXT``) that OpenCode detection needed this fallback;
    the same candidates are worth checking for any Node-based agent CLI.
    """

    found = shutil.which(name)
    if found:
        return found
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "npm" / f"{name}.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / f"{name}.cmd",
        Path.home() / f".{name}" / "bin" / name,
        Path.home() / ".local" / "bin" / name,
        *extra_candidates,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _workspace(agent_name: str) -> Path:
    """An empty directory to run the agent CLI in.

    Defence in depth: the CLI's own sandbox/permission flags limit what it
    can reach, this limits what it starts out able to see. Running in the
    user's project directory would put their real files one bad completion
    away from a file-touching tool. Each agent gets its own subdirectory so
    a crash-looping one can't fill another's.
    """

    path = Path.home() / ".3loop" / f"{agent_name}-workspace"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.home()
    return path


def _append_log(agent_name: str, text: str) -> None:
    """Record one invocation. Failures here must never break a run."""

    try:
        path = Path.home() / ".3loop" / f"{agent_name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except OSError:
        pass


#: Appended as the last paragraph, never as the opening one: tried as an
#: opening constraint on OpenCode it got answered rather than followed
#: ("Understood, what is your question?"). As a trailing note on a task the
#: agent has already read in full, it is obeyed. Applies equally to any
#: file-touching coding CLI, not just OpenCode.
_NO_FILES_CONSTRAINT = (
    "Contrainte: reponds uniquement a partir de tes connaissances, sans "
    "consulter, explorer ni modifier aucun fichier. Renvoie uniquement "
    "l'objet JSON demande, rien d'autre."
)


def build_cli_agent_prompt(
    *,
    instruction: str,
    task: str,
    kind: TaskKind,
    history: str = "",
    sources: Sequence[SourceMatch] = (),
    research_digest: str = "",
) -> str:
    """Build a prompt from scratch for a CLI coding-agent backend.

    Deliberately a separate template from ``prompting.build_prefix``/
    ``with_role`` rather than a transformation of their output. That local
    template is shaped to help llama.cpp reuse its KV prefix, a concern
    that does not exist for a fresh agent subprocess invocation; and its
    ``3LOOP_ACTION=...`` protocol markers read, to a coding agent, as the
    name of a project to go inspect.

    Two things were measured necessary (on OpenCode) to get a real answer
    instead of a clarifying question or an invented schema:

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


class CLIAgentBackend(SharedLLMBackend):
    """Base for backends that run one completion per call through a CLI agent.

    ``serialize_requests=False``: each call is its own short-lived process
    talking to a remote API, so they can overlap the way any HTTP backend
    does. No session/conversation is carried between calls - each call
    already carries the task and history it needs via
    ``build_cli_agent_prompt``, and a carried-over agent session would
    duplicate that context on top.
    """

    #: Short name used for the workspace directory and log file
    #: (e.g. "opencode", "claude-code", "codex").
    agent_name: str = "cli-agent"

    def __init__(
        self,
        model: str,
        *,
        timeout: float = 300.0,
        executable: str | None = None,
    ) -> None:
        super().__init__(serialize_requests=False)
        resolved = executable or self.find()
        if resolved is None:
            raise RuntimeError(self.not_found_message())
        self.executable = resolved
        self.model = model
        self.timeout = timeout

    @classmethod
    @abstractmethod
    def find(cls) -> str | None:
        """Locate this agent's executable, or ``None`` if not installed."""

    @classmethod
    def not_found_message(cls) -> str:
        return f"{cls.agent_name} est introuvable. Installe-le ou verifie qu'il est dans le PATH."

    @abstractmethod
    def build_argv(self) -> list[str]:
        """CLI arguments (excluding the executable itself and the prompt)."""

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str) -> str:
        """Extract the answer text from the process output.

        Must never raise on malformed output - return ``""`` instead, so the
        caller's "no text" check produces one clear error rather than a
        traceback.
        """

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        # None of these CLIs expose temperature or a token cap on their
        # non-interactive entry points; provider defaults apply. Both are
        # honoured by the local backends, so behaviour differs here by
        # design rather than oversight.
        #
        # `system_prompt` is deliberately dropped: these agents already
        # carry their own, and prepending a second role assignment
        # ("You are the 3loop compact debate engine...") makes them answer
        # it conversationally instead of doing the task - measured on
        # OpenCode. Callers talking to this backend build `prompt` with
        # `build_cli_agent_prompt`, which needs no such prefix.
        del temperature, max_tokens, system_prompt
        message = prompt
        workspace = str(_workspace(self.agent_name))

        def invoke() -> str:
            started = time.time()
            try:
                completed = subprocess.run(
                    [self.executable, *self.build_argv()],
                    input=message,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    # The child inherits the parent's working directory
                    # otherwise, and a code-reading agent will happily walk
                    # it - measured on OpenCode: it answered a maths
                    # question by describing this repo's pipeline.py.
                    cwd=workspace,
                    creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
            except subprocess.TimeoutExpired:
                _append_log(
                    self.agent_name,
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TIMEOUT apres "
                    f"{self.timeout}s modele={self.model}",
                )
                raise RuntimeError(
                    f"{self.agent_name} n'a pas repondu en {self.timeout:.0f}s."
                ) from None
            except OSError as exc:
                _append_log(
                    self.agent_name, f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERREUR {exc}"
                )
                raise RuntimeError(f"Impossible de lancer {self.agent_name}: {exc}") from exc

            elapsed = time.time() - started
            text = self.parse_output(completed.stdout, completed.stderr)
            _append_log(
                self.agent_name,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] modele={self.model} "
                f"code={completed.returncode} {elapsed:.1f}s "
                f"prompt={len(message)}c reponse={len(text)}c",
            )
            if not text:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(
                    f"{self.agent_name} n'a renvoye aucun texte"
                    + (f": {detail[:300]}" if detail else ".")
                )
            return text

        return await asyncio.to_thread(invoke)
