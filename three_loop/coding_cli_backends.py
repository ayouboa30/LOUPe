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
"""

from __future__ import annotations

import json

from .cli_agent_backend import CLIAgentBackend, find_executable

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


class ClaudeCodeBackend(CLIAgentBackend):
    """Delegates to a locally installed Claude Code CLI (``claude -p``)."""

    agent_name = "claude-code"

    def __init__(self, model: str = CLAUDE_CODE_DEFAULT_MODEL, **kwargs) -> None:
        super().__init__(model, **kwargs)

    @classmethod
    def find(cls) -> str | None:
        return find_executable("claude")

    def build_argv(self) -> list[str]:
        return [
            "-p",
            "--output-format",
            "json",
            # "plan" is Claude Code's own read-only mode: it can inspect but
            # not edit or run commands. Paired with the isolated workspace
            # (belt and suspenders, not a substitute for it).
            "--permission-mode",
            "plan",
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


class CodexBackend(CLIAgentBackend):
    """Delegates to a locally installed OpenAI Codex CLI (``codex exec``)."""

    agent_name = "codex"

    def __init__(self, model: str = CODEX_DEFAULT_MODEL, **kwargs) -> None:
        super().__init__(model, **kwargs)

    @classmethod
    def find(cls) -> str | None:
        return find_executable("codex")

    def build_argv(self) -> list[str]:
        argv = [
            "exec",
            "--json",
            # Blocks every write, including inside the isolated workspace -
            # this backend never needs to touch a filesystem at all.
            "--sandbox",
            "read-only",
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
