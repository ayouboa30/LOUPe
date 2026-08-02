"""Role-specific agents and robust parsers for LLM-generated evaluations."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from .backend import SharedLLMBackend
from .models import AgentRole, SourceMatch, TaskKind, Vote
from .skills import load_skill


class RoleAgent(ABC):
    """Base identity backed by the same shared LLM instance."""

    def __init__(self, backend: SharedLLMBackend, *, max_tokens: int = 2048) -> None:
        self.backend = backend
        self.max_tokens = max_tokens

    @property
    @abstractmethod
    def role(self) -> AgentRole:
        """Return the stable identity used in prompts and priors."""

    @abstractmethod
    async def produce(self, *args: object, **kwargs: object) -> str:
        """Generate the role-specific contribution for a cycle."""

        raise NotImplementedError

    async def search_query(
        self,
        task: str,
        *,
        kind: TaskKind,
        cycle: int,
        temperature: float,
    ) -> str:
        """Generate one independent, concise search query."""

        prompt = f"""TASK:
{task}

3LOOP_ACTION=search_query
3LOOP_ROLE={self.role.value}
3LOOP_CYCLE={cycle}
3LOOP_KIND={kind.value}

Generate exactly one web search query. Do not mention other agents and do not
use the previous debate. Prefer precise technical terms, standards, papers,
or official documentation relevant to the task."""
        raw = await self.backend.complete(
            prompt,
            temperature=temperature,
            system_prompt=(
                "You are a component of the 3loop framework. Follow the role "
                "and action declared in the user prompt."
            ),
            max_tokens=min(self.max_tokens, 256),
        )
        return _parse_query(raw)

    async def evaluate(
        self,
        task: str,
        final_solution: str,
        *,
        kind: TaskKind,
        cycle: int,
        history: str,
        temperature: float,
    ) -> Vote:
        """Evaluate the final answer and parse a binary JSON vote."""

        prompt = f"""TASK:
{task}

FINAL SOLUTION:
{final_solution}

FULL DEBATE HISTORY:
{history}

3LOOP_ACTION=vote
3LOOP_ROLE={self.role.value}
3LOOP_CYCLE={cycle}
3LOOP_KIND={kind.value}

Judge whether the final solution really solves the task. Check correctness,
unstated assumptions, edge cases, and the requested output format. Return only
valid JSON with this exact shape:
{{"resolved": true or false, "confidence": number from 0 to 1,
"rationale": "short evidence-based explanation"}}"""
        raw = await self.backend.complete(
            prompt,
            temperature=temperature,
            system_prompt=(
                "You are a component of the 3loop framework. Follow the role "
                "and action declared in the user prompt."
            ),
            max_tokens=min(self.max_tokens, 512),
        )
        return parse_vote(raw, self.role)

    async def _ask(
        self,
        prompt: str,
        *,
        action: str,
        temperature: float,
    ) -> str:
        """Call the shared backend with role and action metadata."""

        return await self.backend.complete(
            prompt,
            temperature=temperature,
            system_prompt=(
                "You are a component of the 3loop framework. Follow the role "
                "and action declared in the user prompt."
            ),
            max_tokens=self.max_tokens,
        )


class HeuristicAgent(RoleAgent):
    """Agent 1: explores a first solution, sketch, or mathematical route."""

    @property
    def role(self) -> AgentRole:
        return AgentRole.HEURISTIC

    async def produce(
        self,
        task: str,
        *,
        kind: TaskKind,
        cycle: int,
        history: str,
        sources: Sequence[SourceMatch] = (),
        temperature: float,
    ) -> str:
        """Generate a candidate approach from the full previous history."""

        prompt = f"""TASK:
{task}

FULL DEBATE HISTORY:
{history}

3LOOP_ACTION=produce
3LOOP_ROLE={self.role.value}
3LOOP_CYCLE={cycle}
3LOOP_KIND={kind.value}

TRIANGULATED SOURCES:
{_render_sources(sources)}

Act as the heuristic explorer. Produce a concrete first draft: identify
hypotheses, derive an algorithm or proof plan, and expose likely edge cases.
Do not pretend to have verified anything that is not established yet."""
        return await self._ask(prompt, action="produce", temperature=temperature)


class CriticAgent(RoleAgent):
    """Agent 2: attacks the draft and supplies precise corrections."""

    @property
    def role(self) -> AgentRole:
        return AgentRole.CRITIC

    async def produce(
        self,
        task: str,
        draft: str,
        *,
        kind: TaskKind,
        cycle: int,
        history: str,
        sources: Sequence[SourceMatch] = (),
        temperature: float,
    ) -> str:
        """Find logical gaps, boundary failures, and actionable fixes."""

        prompt = f"""TASK:
{task}

FULL DEBATE HISTORY:
{history}

3LOOP_ACTION=produce
3LOOP_ROLE={self.role.value}
3LOOP_CYCLE={cycle}
3LOOP_KIND={kind.value}

AGENT 1 DRAFT:
{draft}

TRIANGULATED SOURCES:
{_render_sources(sources)}

Act as an adversarial reviewer. Check soundness, hidden assumptions,
termination, complexity, syntax, numerical stability, and boundary cases as
appropriate. Then give a corrected plan that Agent 3 can apply directly."""
        return await self._ask(prompt, action="produce", temperature=temperature)


class WriterAgent(RoleAgent):
    """Agent 3: integrates the debate into the requested final format."""

    @property
    def role(self) -> AgentRole:
        return AgentRole.WRITER

    async def produce(
        self,
        task: str,
        draft: str,
        critique: str,
        *,
        kind: TaskKind,
        cycle: int,
        history: str,
        sources: Sequence[SourceMatch] = (),
        temperature: float,
    ) -> str:
        """Write clean code, LaTeX, or a well-structured general answer."""

        format_instruction = {
            TaskKind.CODE: (
                "Return production-quality code in a fenced block when useful, "
                "including validation and a concise usage note."
            ),
            TaskKind.MATH: (
                "Return a rigorous derivation in readable LaTeX, state hypotheses, "
                "and check the result or limiting cases."
            ),
            TaskKind.GENERAL: "Return a precise, structured final answer.",
        }[kind]
        skill = load_skill(kind)
        skill_section = f"\nFORMATTING RULES FOR THIS ANSWER:\n{skill}\n" if skill else ""
        prompt = f"""TASK:
{task}

FULL DEBATE HISTORY:
{history}

3LOOP_ACTION=produce
3LOOP_ROLE={self.role.value}
3LOOP_CYCLE={cycle}
3LOOP_KIND={kind.value}

AGENT 1 DRAFT:
{draft}

AGENT 2 CRITIQUE AND CORRECTIONS:
{critique}

TRIANGULATED SOURCES:
{_render_sources(sources)}
{skill_section}
You are the final editor. Integrate valid corrections rather than merely
summarizing the debate. {format_instruction} Do not include internal voting
or chain-of-thought commentary."""
        return await self._ask(prompt, action="produce", temperature=temperature)


def parse_vote(raw: str, role: AgentRole) -> Vote:
    """Parse strict JSON first, then use a conservative text fallback."""

    payload = _extract_json_object(raw)
    if payload is not None:
        resolved = _to_bool(payload.get("resolved", payload.get("solved", False)))
        confidence = _to_float(payload.get("confidence", 0.5), default=0.5)
        rationale = str(payload.get("rationale", payload.get("reason", raw))).strip()
        return Vote(role, resolved, confidence, rationale or "Aucune justification.")

    lowered = raw.lower()
    negative = any(token in lowered for token in ("not resolved", "unresolved", "non resolu", "false"))
    positive = any(token in lowered for token in ("resolved", "resolu", "true"))
    resolved = positive and not negative
    confidence_match = re.search(r"confidence\s*[:=]\s*(0?\.\d+|1(?:\.0+)?)", lowered)
    confidence = float(confidence_match.group(1)) if confidence_match else 0.5
    return Vote(role, resolved, confidence, raw.strip() or "Aucune justification.")


def _parse_query(raw: str) -> str:
    """Extract a one-line query from plain text or a small JSON response."""

    payload = _extract_json_object(raw)
    if payload and payload.get("query"):
        return _clean_query(str(payload["query"]))
    for line in raw.splitlines():
        candidate = _clean_query(line)
        if candidate:
            return candidate
    return _clean_query(raw) or "technical solution verification"


def _render_sources(sources: Sequence[SourceMatch]) -> str:
    if not sources:
        return "(Aucune source triangulee.)"
    return "\n".join(
        f"- {source.url} ({source.domain}; commun a {', '.join(source.agent_ids)}) "
        f"{source.title}: {source.snippet}"
        for source in sources
    )


def _extract_json_object(raw: str) -> dict[str, object] | None:
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"true", "yes", "oui", "1", "resolved", "resolu"}


def _to_float(value: object, *, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def _clean_query(value: str) -> str:
    return value.strip().strip("`\"'").lstrip("- ").strip()
