"""Support agents: they shape the context rather than debate inside it.

Neither votes and neither carries a temperature prior. Both exist to cut the
number of prompt tokens the debating roles have to read, which is what
actually costs time on CPU (prefill is ~94% of wall-clock at realistic
prompt sizes, 14.3 ms per prompt token).

Against a local llama.cpp backend both build their prompt as
``build_prefix(...) + short role suffix`` so they land inside llama.cpp's
reused KV prefix: measured ~1.7 s per extra agent that follows that rule,
versus ~12.7 s for one that does not. Against any CLI-agent backend
(OpenCode, Claude Code, Codex - all subclass ``CLIAgentBackend``) they use
``build_cli_agent_prompt`` instead - that measurement doesn't apply to a
fresh subprocess per call, and the local template's protocol markers read
to a coding agent as a project to go inspect.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .backend import SharedLLMBackend
from .cli_agent_backend import CLIAgentBackend, build_cli_agent_prompt
from .models import AgentRole, SourceMatch, TaskKind
from .prompting import build_prefix, with_role

_SYSTEM = (
    "You are a 3loop support agent. Follow the role instruction at the end "
    "of the prompt and answer with nothing else."
)


class ContextAgent:
    """Distills what an agent produced into the few points worth carrying.

    A verbose 200-token turn re-read by every downstream agent costs ~2.9 s
    of prefill each time. Distilling it once (~1.7 s, prefix reused) pays for
    itself from the first downstream reader and keeps paying every cycle
    afterwards, since the distilled form is what lands in the history.
    """

    role = AgentRole.CONTEXT

    def __init__(self, backend: SharedLLMBackend, *, max_tokens: int = 220) -> None:
        self.backend = backend
        self.max_tokens = max_tokens

    async def distill(
        self,
        content: str,
        *,
        task: str,
        kind: TaskKind,
        speaker: str,
        temperature: float = 0.2,
    ) -> str:
        """Compress one contribution, keeping decisions and constraints.

        Returns ``content`` unchanged when it is already short enough to be
        worth less than the call it would take to shrink it.
        """

        if len(content) < 400:
            return content.strip()

        instruction_body = (
            f"Voici la contribution de {speaker}:\n---\n{content}\n---\n"
            "Reecris-la en 3 points maximum. Garde les decisions, les "
            "contraintes, les chiffres et les noms propres. Supprime les "
            "reformulations et les politesses. Pas de commentaire."
        )
        if isinstance(self.backend, CLIAgentBackend):
            prompt = build_cli_agent_prompt(
                instruction=instruction_body, task=task, kind=kind,
            )
            system_prompt = None
        else:
            prefix = build_prefix(task=task, kind=kind)
            prompt = with_role(prefix, f"ROLE=context. {instruction_body}")
            system_prompt = _SYSTEM
        distilled = await self.backend.complete(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            max_tokens=self.max_tokens,
        )
        cleaned = distilled.strip()
        # A distillation that grew is a failed distillation - keep the
        # original rather than paying more prefill for less information.
        return cleaned if cleaned and len(cleaned) < len(content) else content.strip()


class ResearchAgent:
    """Turns raw search hits into a short digest before they reach the debate.

    Web pages are long: a 2000-token page costs ~28 s of prefill on its own,
    which would swamp everything else in a cycle. Only the digest is ever
    placed in the shared prefix; the raw snippets are dropped.
    """

    role = AgentRole.RESEARCHER

    def __init__(self, backend: SharedLLMBackend, *, max_tokens: int = 260) -> None:
        self.backend = backend
        self.max_tokens = max_tokens

    async def digest(
        self,
        sources: Sequence[SourceMatch],
        *,
        task: str,
        kind: TaskKind,
        temperature: float = 0.2,
    ) -> str:
        """Summarise the retrieved sources into a few task-relevant facts."""

        if not sources:
            return ""

        rendered = "\n".join(
            f"- [{index}] {source.title or source.domain} ({source.domain}): "
            f"{_trim(source.snippet)}"
            for index, source in enumerate(sources, start=1)
        )
        instruction_body = (
            f"Resultats de recherche:\n{rendered}\n\n"
            "Extrais uniquement les faits qui servent la tache, en 5 points "
            "maximum, chacun suivi de son numero de source entre crochets. "
            "Ignore ce qui est hors sujet. Pas de commentaire."
        )
        if isinstance(self.backend, CLIAgentBackend):
            prompt = build_cli_agent_prompt(
                instruction=instruction_body, task=task, kind=kind,
            )
            system_prompt = None
        else:
            prefix = build_prefix(task=task, kind=kind)
            prompt = with_role(prefix, f"ROLE=researcher. {instruction_body}")
            system_prompt = _SYSTEM
        digest = await self.backend.complete(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            max_tokens=self.max_tokens,
        )
        return digest.strip()


def _trim(snippet: str, *, limit: int = 240) -> str:
    """Cap one snippet before it even reaches the digest prompt."""

    flat = re.sub(r"\s+", " ", snippet or "").strip()
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "..."
