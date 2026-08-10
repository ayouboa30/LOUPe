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

This was the first CLI-agent backend built, and ``cli_agent_backend.py``
was extracted from it once Claude Code and Codex needed the exact same
subprocess/workspace/prompt machinery. ``OpenCodeBackend`` now subclasses
that shared base; only what is genuinely OpenCode-specific (its ``--agent``
flag, its NDJSON event shape) stays here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .cli_agent_backend import (
    CLIAgentBackend,
    _CREATE_NO_WINDOW,
    _workspace as _shared_workspace,
    build_cli_agent_prompt,
    find_executable,
    login_hint as _login_hint,
)

DEFAULT_MODEL = "opencode/deepseek-v4-flash-free"

#: Single wording for the "not installed" case, reused by ``/api/config`` as
#: the UI hint and by ``not_found_message`` as the raised error - see the
#: matching constants in ``coding_cli_backends``. It states plainly that no
#: API key is involved: OpenCode is authenticated once in the user's own
#: terminal (``opencode auth login``) and 3loop only bridges that session.
OPENCODE_INSTALL_HINT = (
    "OpenCode est introuvable. Installe la CLI (npm i -g opencode-ai), puis "
    "lance `opencode auth login` pour te connecter. 3loop se branche sur ta "
    "CLI locale et ne demande aucune cle API."
)

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
    """An empty directory to run OpenCode in (see ``cli_agent_backend``)."""

    return _shared_workspace("opencode")


def find_opencode() -> str | None:
    """Locate the OpenCode executable, including the npm global shim."""

    return find_executable(
        "opencode",
        Path.home() / ".opencode" / "bin" / "opencode",
    )


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


#: Kept as the historical, OpenCode-specific name; identical to
#: ``cli_agent_backend.build_cli_agent_prompt``; now shared by Claude Code
#: and Codex too.
build_opencode_prompt = build_cli_agent_prompt


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


def _error_text(value: object) -> str:
    """Extract a useful message from OpenCode's varying error payloads."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("message", "reason", "detail", "description"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key in ("error", "cause", "data", "part"):
            candidate = _error_text(value.get(key))
            if candidate:
                return candidate
        for key in ("name", "code"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return ""
    if isinstance(value, list):
        for item in value:
            candidate = _error_text(item)
            if candidate:
                return candidate
    return ""


def _extract_error(*streams: str) -> str:
    """Read a structured ``type=error`` event without hiding its cause."""

    for stream in streams:
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "error":
                continue
            message = _error_text(event.get("error"))
            if not message:
                message = _error_text(event.get("message"))
            if not message:
                message = _error_text(event)
            if message:
                return message
    return ""


class OpenCodeBackend(CLIAgentBackend):
    """Run one completion per call through ``opencode run``."""

    agent_name = "opencode"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = 300.0,
        executable: str | None = None,
        agent: str = DEFAULT_AGENT,
        **kwargs: object,
    ) -> None:
        self.agent = agent
        super().__init__(model, timeout=timeout, executable=executable, **kwargs)

    @classmethod
    def find(cls) -> str | None:
        return find_opencode()

    @classmethod
    def not_found_message(cls) -> str:
        return OPENCODE_INSTALL_HINT

    def build_argv(self, workspace=None) -> list[str]:
        return [
            "run",
            "--format",
            "json",
            "--agent",
            self.agent,
            "--dir",
            str(workspace or _workspace()),
            "-m",
            self.model,
        ]

    def parse_output(self, stdout: str, stderr: str) -> str:
        return _extract_text(stdout)

    def explain_failure(self, stdout: str, stderr: str) -> str:
        # Same contract as the sibling CLI backends: parse_output only
        # extracts, this explains an empty extraction. It used to raise from
        # inside parse_output, which worked but meant each backend reported
        # failure its own way.
        error = _extract_error(stdout, stderr)
        if not error:
            return ""
        return f"OpenCode : {error[:300]}" + _login_hint(error, "lance `opencode auth login`")
