"""Shared machinery for locally installed coding-agent CLI backends.

The regular path is deliberately conservative: every CLI runs in an isolated
workspace and has no write permission. A user can explicitly opt into a real
project directory for a coding task; only then does the backend keep the child
process interactive and relay clear permission/input requests back to LOUPe.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from .backend import SharedLLMBackend
from .models import SourceMatch, TaskKind
from .skills import load_skill

#: Windows-only flag that starts the child without allocating a console.
_CREATE_NO_WINDOW = 0x08000000

#: OpenCode persists session/provider state in a process-wide SQLite database.
#: Separate HTTP requests create separate backend instances, so an asyncio lock
#: on one instance is not enough: concurrent CLI children can still contend on
#: that database and fail with "database is locked". Keep all local coding CLI
#: processes single-file at the host process boundary; this does not touch user
#: data and only queues the already-running requests.
_CLI_PROCESS_LOCK = threading.Lock()
_CLI_LOCK_RETRIES = 3
_CLI_LOCK_RETRY_DELAY_SECONDS = 1.0


def find_executable(name: str, *extra_candidates: Path) -> str | None:
    """Locate a CLI on PATH, with common npm/user-install fallbacks."""

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
    """Return an empty, per-agent workspace for read-only requests."""

    path = Path.home() / ".3loop" / f"{agent_name}-workspace"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.home()
    return path


def _resolve_workspace(
    agent_name: str,
    *,
    allow_writes: bool,
    workspace_path: str | Path | None,
) -> Path:
    """Return the isolated workspace or the explicitly approved project dir."""

    if not allow_writes:
        return _workspace(agent_name)
    if not workspace_path:
        raise ValueError("Un dossier de travail est obligatoire en mode écriture.")
    candidate = Path(workspace_path).expanduser().resolve()
    if not candidate.is_dir():
        raise ValueError(f"Dossier de travail introuvable : {candidate}")
    if candidate.parent == candidate:
        raise ValueError("Le mode écriture ne peut pas viser la racine du disque.")
    return candidate


def _append_log(agent_name: str, text: str) -> None:
    """Record one invocation. Logging failures must never break a run."""

    try:
        path = Path.home() / ".3loop" / f"{agent_name}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n")
    except OSError:
        pass


_NO_FILES_CONSTRAINT = (
    "Contrainte: reponds uniquement a partir de tes connaissances, sans "
    "consulter, explorer ni modifier aucun fichier. Renvoie uniquement "
    "l'objet JSON demande, rien d'autre."
)

_WRITABLE_FILES_CONSTRAINT = (
    "Mode execution autorise: travaille uniquement dans le dossier de travail "
    "fourni par l'hote. Tu peux lire les fichiers necessaires et modifier les "
    "fichiers demandes par la tache. N'accede pas a un autre dossier, ne "
    "supprime pas de donnees sans le demander, et termine par un resume court "
    "des fichiers modifies et des verifications effectuees."
)


def build_cli_agent_prompt(
    *,
    instruction: str,
    task: str,
    kind: TaskKind,
    history: str = "",
    sources: Sequence[SourceMatch] = (),
    research_digest: str = "",
    allow_writes: bool = False,
    workspace_path: str = "",
) -> str:
    """Build a plain-language prompt suitable for a fresh CLI subprocess."""

    parts = [instruction.strip()]
    skill = load_skill(kind)
    if skill:
        parts.append(f"Regles de formatage:\n{skill}")
    parts.append(f"Tache:\n{task}")
    if research_digest.strip():
        parts.append(f"Resume de recherche:\n{research_digest.strip()}")
    elif sources:
        parts.append("Sources:\n" + "\n".join(f"- {source.url}" for source in sources))
    if history.strip() and not history.startswith("(Aucun historique"):
        parts.append(f"Cycles precedents:\n{history}")
    constraint = _WRITABLE_FILES_CONSTRAINT if allow_writes else _NO_FILES_CONSTRAINT
    if allow_writes and workspace_path.strip():
        constraint += f" Dossier autorise: {workspace_path.strip()}."
    parts.append(constraint)
    return "\n\n".join(parts)


def _interaction_text(value: Any) -> str:
    """Find a short human-facing question in a structured CLI event."""

    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("question", "prompt", "message", "text", "reason", "description"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _cli_interaction_from_line(line: str, *, channel: str) -> dict[str, Any] | None:
    """Recognise explicit permission/input events without mistaking prose for one.

    There is no shared interactive wire protocol across OpenCode, Claude Code
    and Codex. This recognises only unambiguous structured event kinds or a
    traditional confirmation prompt. Everything else remains normal CLI output.
    """

    stripped = line.strip()
    if not stripped:
        return None
    event: dict[str, Any] | None = None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            event = parsed
    except json.JSONDecodeError:
        pass

    if event is not None:
        candidates: list[str] = []
        for value in (event.get("type"), event.get("subtype"), event.get("kind")):
            if isinstance(value, str):
                candidates.append(value.lower())
        for key in ("request", "item", "permission", "approval"):
            nested = event.get(key)
            if isinstance(nested, dict):
                for value in (nested.get("type"), nested.get("subtype"), nested.get("kind")):
                    if isinstance(value, str):
                        candidates.append(value.lower())
        markers = (
            "permission", "approval", "approve", "question", "input_required",
            "tool_confirmation", "can_use_tool", "command_execution_request",
            "file_change_request", "user_input",
        )
        if not any(any(marker in candidate for marker in markers) for candidate in candidates):
            return None
        question = _interaction_text(event)
        if not question:
            for key in ("request", "item", "permission", "approval"):
                question = _interaction_text(event.get(key))
                if question:
                    break
        if not question:
            question = "Le CLI demande une décision avant de continuer."
        permission_markers = ("permission", "approval", "approve", "can_use_tool", "confirmation")
        kind = "permission" if any(
            any(marker in candidate for marker in permission_markers)
            for candidate in candidates
        ) else "question"
        return {
            "interaction_id": f"cli_{uuid.uuid4().hex}",
            "kind": kind,
            "question": question[:4000],
            "channel": channel,
            "protocol": "json",
        }

    lowered = stripped.lower()
    has_prompt_shape = "?" in stripped or bool(
        re.search(r"\[\s*[yn](?:/[yn])?\s*\]", lowered)
    )
    has_interaction_word = bool(
        re.search(
            r"\b(permission|approve|approval|allow|autoris\w*|confirm\w*|continue|answer|repond\w*)\b",
            lowered,
        )
    )
    if not has_prompt_shape or not has_interaction_word:
        return None
    kind = "permission" if re.search(
        r"\b(permission|approve|approval|allow|autoris\w*|confirm\w*)\b", lowered
    ) else "question"
    return {
        "interaction_id": f"cli_{uuid.uuid4().hex}",
        "kind": kind,
        "question": stripped[:4000],
        "channel": channel,
        "protocol": "text",
    }


class CLIAgentBackend(SharedLLMBackend):
    """Base for backends that delegate one completion to a local coding CLI.

    Read-only calls remain short-lived, stdin-closed subprocesses. Interactive
    handling is enabled only in an explicit write session: it keeps stdin open
    and waits for the browser's explicit approval, refusal or free-text reply.
    No code path auto-approves a tool request.
    """

    agent_name: str = "cli-agent"

    def __init__(
        self,
        model: str,
        *,
        timeout: float = 300.0,
        executable: str | None = None,
        allow_writes: bool = False,
        workspace_path: str | Path | None = None,
        interaction_callback: Callable[[dict[str, Any]], Awaitable[Any] | Any] | None = None,
        interaction_timeout: float = 120.0,
    ) -> None:
        super().__init__(serialize_requests=False)
        resolved = executable or self.find()
        if resolved is None:
            raise RuntimeError(self.not_found_message())
        self.executable = resolved
        self.model = model
        self.timeout = float(timeout)
        self.allow_writes = bool(allow_writes)
        self.workspace_path = str(workspace_path).strip() if workspace_path else ""
        self.interaction_callback = interaction_callback
        self.interaction_timeout = max(5.0, float(interaction_timeout))

    @classmethod
    @abstractmethod
    def find(cls) -> str | None:
        """Locate this agent's executable, or ``None`` if not installed."""

    @classmethod
    def not_found_message(cls) -> str:
        return f"{cls.agent_name} est introuvable. Installe-le ou verifie qu'il est dans le PATH."

    @abstractmethod
    def build_argv(self, workspace: Path | None = None) -> list[str]:
        """CLI arguments, excluding the executable and prompt."""

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str) -> str:
        """Extract answer text from process output without raising on bad output."""

    def _message_for_run(self, prompt: str, workspace: Path) -> str:
        """Add the matching access constraint when a caller used a generic prompt."""

        message = prompt.rstrip()
        if self.allow_writes:
            message = message.replace(_NO_FILES_CONSTRAINT, _WRITABLE_FILES_CONSTRAINT)
            if _WRITABLE_FILES_CONSTRAINT not in message:
                message += "\n\n" + _WRITABLE_FILES_CONSTRAINT
            if f"Dossier autorise: {workspace}." not in message:
                message += f" Dossier autorise: {workspace}."
        elif _NO_FILES_CONSTRAINT not in message:
            message += "\n\n" + _NO_FILES_CONSTRAINT
        return message

    async def _complete(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None,
        max_tokens: int | None,
    ) -> str:
        # Headless CLIs do not consistently expose temperature/token controls;
        # their native defaults apply. Their own system prompts are retained.
        del temperature, max_tokens, system_prompt
        workspace = _resolve_workspace(
            self.agent_name,
            allow_writes=self.allow_writes,
            workspace_path=self.workspace_path,
        )
        message = self._message_for_run(prompt, workspace)
        event_loop = asyncio.get_running_loop()
        interactive = self.allow_writes and self.interaction_callback is not None

        def invoke() -> str:
            started = time.time()
            try:
                # The CLI may be constructed once per HTTP request, so the
                # backend's per-instance asyncio lock cannot protect OpenCode's
                # shared SQLite state. Serialize the child process itself.
                with _CLI_PROCESS_LOCK:
                    if interactive:
                        stdout, stderr, returncode = self._invoke_interactive(
                            message, workspace, event_loop
                        )
                    else:
                        completed = None
                        for attempt in range(_CLI_LOCK_RETRIES + 1):
                            completed = subprocess.run(
                                [self.executable, *self.build_argv(workspace)],
                                input=message,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=self.timeout,
                                cwd=str(workspace),
                                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                            )
                            detail = f"{completed.stdout}\n{completed.stderr}".lower()
                            if completed.returncode == 0 or "database is locked" not in detail:
                                break
                            if attempt < _CLI_LOCK_RETRIES:
                                time.sleep(_CLI_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
                        assert completed is not None
                        stdout, stderr, returncode = (
                            completed.stdout,
                            completed.stderr,
                            completed.returncode,
                        )
            except subprocess.TimeoutExpired:
                _append_log(
                    self.agent_name,
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] TIMEOUT apres "
                    f"{self.timeout:.0f}s modele={self.model}",
                )
                raise RuntimeError(
                    f"{self.agent_name} n'a pas repondu en {self.timeout:.0f}s."
                ) from None
            except OSError as exc:
                _append_log(
                    self.agent_name,
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERREUR {exc}",
                )
                raise RuntimeError(f"Impossible de lancer {self.agent_name}: {exc}") from exc

            text = self.parse_output(stdout, stderr)
            elapsed = time.time() - started
            _append_log(
                self.agent_name,
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] modele={self.model} "
                f"code={returncode} {elapsed:.1f}s prompt={len(message)}c "
                f"reponse={len(text)}c writes={int(self.allow_writes)}",
            )
            if not text:
                detail = (stderr or stdout or "").strip()
                raise RuntimeError(
                    f"{self.agent_name} n'a renvoye aucun texte"
                    + (f": {detail[:300]}" if detail else ".")
                )
            return text

        return await asyncio.to_thread(invoke)

    def _invoke_interactive(
        self,
        message: str,
        workspace: Path,
        event_loop: asyncio.AbstractEventLoop,
    ) -> tuple[str, str, int]:
        """Keep a write-session child alive while relaying explicit prompts."""

        process = subprocess.Popen(
            [self.executable, *self.build_argv(workspace)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(workspace),
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        events: queue.Queue[tuple[str, str]] = queue.Queue()
        readers: list[threading.Thread] = []

        def collect(stream: Any, channel: str) -> None:
            try:
                for line in stream:
                    events.put((channel, line))
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        for stream, channel in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            if stream is None:
                continue
            reader = threading.Thread(
                target=collect,
                args=(stream, channel),
                daemon=True,
                name=f"{self.agent_name}-output-{channel}",
            )
            reader.start()
            readers.append(reader)

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            if process.stdin is not None:
                process.stdin.write(message + ("" if message.endswith("\n") else "\n"))
                process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while True:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(
                        [self.executable, *self.build_argv(workspace)], self.timeout
                    )
                try:
                    channel, line = events.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is None:
                        continue
                    for reader in readers:
                        reader.join(timeout=0.5)
                    while True:
                        try:
                            channel, line = events.get_nowait()
                        except queue.Empty:
                            break
                        self._record_cli_line(
                            channel, line, stdout_parts, stderr_parts, process, event_loop
                        )
                    break
                self._record_cli_line(
                    channel, line, stdout_parts, stderr_parts, process, event_loop
                )
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        return "".join(stdout_parts), "".join(stderr_parts), process.wait()

    def _record_cli_line(
        self,
        channel: str,
        line: str,
        stdout_parts: list[str],
        stderr_parts: list[str],
        process: subprocess.Popen[str],
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        if channel == "stdout":
            stdout_parts.append(line)
        else:
            stderr_parts.append(line)
        request = _cli_interaction_from_line(line, channel=channel)
        if request is None:
            return
        request.update({
            "agent": self.agent_name,
            "model": self.model,
            "allow_writes": True,
        })
        answer: Any = {"decision": "deny", "reason": "Aucune interface d'approbation disponible."}
        if self.interaction_callback is not None:
            async def ask() -> Any:
                result = self.interaction_callback(request)
                if inspect.isawaitable(result):
                    result = await result
                return result

            future = asyncio.run_coroutine_threadsafe(ask(), event_loop)
            try:
                answer = future.result(timeout=self.interaction_timeout)
            except Exception as exc:
                future.cancel()
                answer = {"decision": "deny", "reason": f"Demande expirée ou indisponible: {exc}"}
        self._write_cli_answer(process, request, answer)

    @staticmethod
    def _write_cli_answer(
        process: subprocess.Popen[str], request: dict[str, Any], answer: Any
    ) -> None:
        """Write exactly one explicit human decision back to the child stdin."""

        if process.stdin is None:
            return
        value = dict(answer) if isinstance(answer, dict) else {"decision": str(answer or "deny")}
        decision = str(value.get("decision", "")).strip().lower()
        if request.get("protocol") == "json":
            payload = dict(value)
            payload["decision"] = decision or "deny"
            wire = json.dumps(payload, ensure_ascii=False)
        else:
            free_text = str(value.get("answer", "")).strip()
            if free_text:
                wire = free_text
            elif decision in {"approve", "approved", "allow", "yes", "y", "accept"}:
                wire = "y"
            else:
                wire = "n"
        try:
            process.stdin.write(wire + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
