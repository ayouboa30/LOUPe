"""Conversation history shared by all cycles and agent prompts."""

from __future__ import annotations

from dataclasses import dataclass

from .compact import compact_text
from .models import AgentRole, AgentTurn, ConsensusResult


@dataclass(frozen=True)
class HistoryEntry:
    """One rendered entry in the persistent debate transcript."""

    cycle: int
    speaker: str
    content: str


class ConversationHistory:
    """Store every generated turn so the Markov loop has full context."""

    def __init__(self) -> None:
        self._entries: list[HistoryEntry] = []

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        """Return an immutable view of the transcript."""

        return tuple(self._entries)

    def add_turn(self, turn: AgentTurn) -> None:
        """Append an agent contribution."""

        self._entries.append(
            HistoryEntry(turn.cycle, turn.role.label, turn.content)
        )

    def add_conversation(self, content: str) -> None:
        """Seed the debate with turns from the user's previous chat messages."""

        if content.strip():
            self._entries.append(HistoryEntry(0, "Conversation precedente", content))

    def add_query(self, cycle: int, role: AgentRole, query: str) -> None:
        """Record a web query without treating it as a solution turn."""

        self._entries.append(HistoryEntry(cycle, f"{role.label} / recherche", query))

    def add_sources(self, cycle: int, rendered_sources: str) -> None:
        """Record the triangulated sources used by a cycle."""

        if rendered_sources:
            self._entries.append(HistoryEntry(cycle, "Triangulation web", rendered_sources))

    def add_consensus(self, cycle: int, consensus: ConsensusResult) -> None:
        """Record every vote so later cycles can inspect prior decisions."""

        vote_lines = [
            f"{vote.role.label}: {'RESOLU' if vote.resolved else 'A REVOIR'} "
            f"({vote.confidence:.2f}) - {vote.rationale}"
            for vote in consensus.votes
        ]
        self._entries.append(
            HistoryEntry(
                cycle,
                "Vote de consensus",
                "\n".join(vote_lines),
            )
        )

    def render(
        self,
        *,
        before_cycle: int | None = None,
        max_chars: int | None = None,
    ) -> str:
        """Render prior cycles for an LLM prompt.

        ``before_cycle`` excludes entries from the cycle currently being
        built: those turns are already passed to each agent as direct
        arguments (draft, critique...), so repeating them here would only
        make every prompt longer without adding information.

        Each entry is passed through ``compact_text`` (collapsed whitespace,
        filler phrases dropped) since prefill dominates CPU wall-clock time
        and those are tokens the next cycle gains nothing from.

        Rendering is append-only: cycle N+1 produces exactly cycle N's text
        plus the new entries. That matters because this block is placed last
        in the prompt, where llama.cpp's KV prefix reuse turns an unchanged
        prefix into a near-free re-read (measured 1.02 s versus 9.45 s). When
        the budget is exceeded, whole entries are dropped from the front
        rather than slicing mid-sentence, so what remains stays coherent.
        """

        entries = (
            self._entries
            if before_cycle is None
            else [entry for entry in self._entries if entry.cycle < before_cycle]
        )
        if not entries:
            return "(Aucun historique: premier cycle.)"

        sections = [
            f"[Cycle {entry.cycle} - {entry.speaker}]\n{compact_text(entry.content)}"
            for entry in entries
        ]
        rendered = "\n\n".join(sections)
        if max_chars is None or max_chars < 1 or len(rendered) <= max_chars:
            return rendered

        # Over budget: drop whole leading entries, keeping the most recent
        # ones intact. The full transcript stays in ``self._entries`` for
        # auditability; only this prompt view is bounded.
        kept: list[str] = []
        total = 0
        for section in reversed(sections):
            if kept and total + len(section) + 2 > max_chars:
                break
            kept.append(section)
            total += len(section) + 2
        kept.reverse()
        return "[Cycles anciens omis pour reduire le prefill]\n" + "\n\n".join(kept)
