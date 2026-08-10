"""Claude Code and Codex CLI backends - siblings of OpenCodeBackend.

Both measured directly on this machine (not assumed from docs alone):

    echo "17 fois 23" | claude -p --output-format json --permission-mode plan
    -> {"type":"result","subtype":"success","is_error":bool,"result":"...",...}
    (this session's account returns "Not logged in" for standalone `claude`,
    so the JSON *shape* is measured but full protocol-following behaviour
    is not - unlike Codex and OpenCode, which were verified end to end.)

    echo "17 fois 23" | codex exec --json --sandbox read-only
    -> NDJSON: {"type":"item.completed","item":{"type":"agent_message","text":"391"}}
    (verified end to end: stdin read correctly, read-only sandbox blocks
    writes, correct answer returned.)

Neither backend takes an API key, and neither ever should: the user has
already installed and authenticated these CLIs for their own terminal work,
so 3loop bridges that existing session instead of asking for a second
credential it would then have to store. What replaces the key is detection -
``find()`` plus the install hints below - so "not installed" is answered with
the exact command to fix it rather than with a failed call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence

from .cli_agent_backend import (
    CLIAgentBackend,
    _CREATE_NO_WINDOW,
    find_executable,
    login_hint as _login_hint,
)

#: "sonnet" is Claude Code's own rolling alias for its latest Sonnet model
#: (confirmed in ``claude --help``: "--model ... Provide an alias for the
#: latest model"), so this does not go stale as models are released.
CLAUDE_CODE_DEFAULT_MODEL = "sonnet"

#: Empty, deliberately: Codex reads its default model from the user's own
#: ``~/.codex/config.toml`` when ``--model`` is omitted (measured on this
#: machine: that file names a locally-configured model, not a universal
#: default) - hardcoding one here would override a choice the user already
#: made outside 3loop for no reason.
CODEX_DEFAULT_MODEL = ""

#: Shown by the UI when the CLI is missing, and used as the error raised when
#: the user picks that backend anyway. Both say the same two things on
#: purpose: the exact install command, and that 3loop asks for no API key -
#: the account/subscription already attached to the user's own CLI session is
#: what pays for the call. Anthropic now deprecates the npm install in favour
#: of the native installer, so the Windows one-liner is quoted rather than
#: ``npm i -g @anthropic-ai/claude-code``.
CLAUDE_CODE_INSTALL_HINT = (
    "Claude Code est introuvable. Installe la CLI (Windows PowerShell : "
    "irm https://claude.ai/install.ps1 | iex), puis lance `claude` une fois "
    "pour te connecter. 3loop se branche sur ta CLI locale et ne demande "
    "aucune cle API."
)

#: The scope matters: ``npm i -g codex`` installs an unrelated 2012 package.
CODEX_INSTALL_HINT = (
    "Codex est introuvable. Installe la CLI (npm i -g @openai/codex), puis "
    "lance `codex login` pour te connecter. 3loop se branche sur ta CLI "
    "locale et ne demande aucune cle API."
)

#: A version probe is pure decoration for the UI, and it runs on the
#: ``/api/config`` path that every page load blocks on. Three seconds is
#: already generous for a CLI printing one line; a shim that hangs longer
#: than that must cost the user a missing version string, not a frozen app.
CLI_VERSION_TIMEOUT = 3.0


def cli_version(
    executable: str | None,
    *,
    args: Sequence[str] = ("--version",),
    timeout: float = CLI_VERSION_TIMEOUT,
) -> str:
    """Return a CLI's reported version, or ``""`` if it cannot be determined.

    Never raises and never blocks for long: a missing binary, a non-zero
    exit, a CLI that ignores ``--version``, a hung npm shim and a timeout all
    map to the same empty string. Callers treat "unknown version" as normal,
    because it is - some of these CLIs print their version to stderr, some
    prefix it with a product name, and an offline/logged-out CLI may refuse
    the call entirely while still being perfectly usable afterwards.

    Both streams are inspected (stderr second) and only the first non-empty
    line is kept, trimmed: the point is a short label next to a dropdown, not
    a full banner.
    """

    if not executable:
        return ""
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            # Same reason as every other call in this package: without it a
            # black console flashes over whatever the user is doing.
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    for stream in (completed.stdout, completed.stderr):
        for line in (stream or "").splitlines():
            cleaned = line.strip()
            if cleaned:
                return cleaned[:120]
    return ""


class ClaudeCodeBackend(CLIAgentBackend):
    """Delegates to a locally installed Claude Code CLI (``claude -p``)."""

    agent_name = "claude-code"

    def __init__(self, model: str = CLAUDE_CODE_DEFAULT_MODEL, **kwargs) -> None:
        super().__init__(model, **kwargs)

    @classmethod
    def find(cls) -> str | None:
        return find_executable("claude")

    @classmethod
    def not_found_message(cls) -> str:
        # The install hint *is* the not-found message: one wording for the
        # UI hint, the /api/config payload and the raised error, so the user
        # never gets a vaguer version of the same problem depending on where
        # it surfaces.
        return CLAUDE_CODE_INSTALL_HINT

    def build_argv(self, workspace=None) -> list[str]:
        return [
            "-p",
            "--output-format",
            "json",
            # Plan mode is read-only. In write mode use Claude's normal
            # permission flow so the host can surface any approval request
            # instead of silently accepting edits.
            "--permission-mode",
            "default" if self.allow_writes else "plan",
            "--model",
            self.model,
        ]

    def parse_output(self, stdout: str, stderr: str) -> str:
        del stderr
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result" and not event.get("is_error"):
                result = event.get("result")
                if isinstance(result, str):
                    return result.strip()
        return ""

    def explain_failure(self, stdout: str, stderr: str) -> str:
        # Measured on this machine with a logged-out CLI: the failure arrives
        # as a perfectly ordinary-looking success envelope -
        #   {"type":"result","subtype":"success","is_error":true,
        #    "result":"Not logged in · Please run /login"}
        # so ``subtype`` cannot be trusted and ``is_error`` is the real flag.
        # Before this hook the user saw that whole JSON blob; the sentence
        # they actually need is one field inside it.
        del stderr
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "result" or not event.get("is_error"):
                continue
            message = str(event.get("result") or "").strip()
            if not message:
                continue
            return f"Claude Code : {message[:300]}" + _login_hint(
                message, "lance `claude` dans un terminal pour te connecter"
            )
        return ""


class CodexBackend(CLIAgentBackend):
    """Delegates to a locally installed OpenAI Codex CLI (``codex exec``)."""

    agent_name = "codex"

    def __init__(self, model: str = CODEX_DEFAULT_MODEL, **kwargs) -> None:
        super().__init__(model, **kwargs)

    @classmethod
    def find(cls) -> str | None:
        return find_executable("codex")

    @classmethod
    def not_found_message(cls) -> str:
        return CODEX_INSTALL_HINT

    def build_argv(self, workspace=None) -> list[str]:
        argv = [
            "exec",
            "--json",
            # Read-only is the default. Workspace-write is only selected
            # after the user explicitly supplies a directory in the UI.
            "--sandbox",
            "workspace-write" if self.allow_writes else "read-only",
        ]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def parse_output(self, stdout: str, stderr: str) -> str:
        del stderr
        chunks: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks).strip()

    def explain_failure(self, stdout: str, stderr: str) -> str:
        """Surface Codex's own error line rather than its NDJSON transcript.

        Codex reports trouble either as a typed ``error`` event or, when it
        never got far enough to emit NDJSON at all (missing login, bad
        config), as a plain sentence on stderr.
        """

        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") not in ("error", "item.error"):
                continue
            message = str(
                event.get("message") or (event.get("error") or {}).get("message") or ""
            ).strip()
            if message:
                return f"Codex : {message[:300]}" + _login_hint(
                    message, "lance `codex login`"
                )
        plain = (stderr or "").strip()
        if plain:
            first = plain.splitlines()[0].strip()
            return f"Codex : {first[:300]}" + _login_hint(plain, "lance `codex login`")
        return ""
