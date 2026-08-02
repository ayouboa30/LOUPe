"""Core immutable data structures used by the 3loop framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class AgentRole(str, Enum):
    """Identities that can speak during a cycle.

    The first three debate and vote (see ``AGENT_ROLES``). ``CONTEXT`` and
    ``RESEARCHER`` are support roles: they shape what the debating roles are
    given to read, but they cast no vote and carry no temperature prior.
    """

    HEURISTIC = "heuristic"
    CRITIC = "critic"
    WRITER = "writer"
    CONTEXT = "context"
    RESEARCHER = "researcher"

    @property
    def label(self) -> str:
        """Return a human-readable role name."""

        return {
            AgentRole.HEURISTIC: "Agent 1 - Heuristique",
            AgentRole.CRITIC: "Agent 2 - Critique",
            AgentRole.WRITER: "Agent 3 - Redacteur",
            AgentRole.CONTEXT: "Agent 4 - Contexte",
            AgentRole.RESEARCHER: "Agent 5 - Chercheur",
        }[self]


#: The roles that debate and vote. Deliberately not every ``AgentRole``:
#: consensus arithmetic and the temperature priors are defined over these
#: three only, so support roles must never leak into it.
AGENT_ROLES: tuple[AgentRole, ...] = (
    AgentRole.HEURISTIC,
    AgentRole.CRITIC,
    AgentRole.WRITER,
)


class TaskKind(str, Enum):
    """Output domain used to tailor prompts and lightweight validation."""

    CODE = "code"
    MATH = "math"
    GENERAL = "general"


class EventType(str, Enum):
    """Events emitted by :class:`ThreeLoopPipeline.stream`."""

    RUN_STARTED = "run_started"
    CYCLE_STARTED = "cycle_started"
    RESEARCH_QUERY = "research_query"
    RESEARCH_SOURCES = "research_sources"
    AGENT_OUTPUT = "agent_output"
    VOTE = "vote"
    PRIOR_UPDATED = "prior_updated"
    CYCLE_COMPLETED = "cycle_completed"
    RUN_COMPLETED = "run_completed"
    ERROR = "error"


@dataclass(frozen=True)
class SearchResult:
    """One result returned by a search provider."""

    url: str
    title: str = ""
    snippet: str = ""


@dataclass(frozen=True)
class SourceMatch:
    """A source independently observed by at least two agents."""

    url: str
    domain: str
    title: str = ""
    snippet: str = ""
    agent_ids: tuple[str, ...] = ()
    match_type: str = "url"


@dataclass(frozen=True)
class WebResearchResult:
    """Queries, raw provider results, and their robust intersection."""

    queries: Mapping[str, str] = field(default_factory=dict)
    results_by_agent: Mapping[str, tuple[SearchResult, ...]] = field(
        default_factory=dict
    )
    sources: tuple[SourceMatch, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTurn:
    """A generated contribution stored in the complete debate history."""

    cycle: int
    role: AgentRole
    content: str
    temperature: float


@dataclass(frozen=True)
class Vote:
    """One agent's binary consensus judgment."""

    role: AgentRole
    resolved: bool
    confidence: float
    rationale: str

    def __post_init__(self) -> None:
        """Keep confidence in the documented probability range."""

        object.__setattr__(self, "confidence", min(1.0, max(0.0, self.confidence)))


@dataclass(frozen=True)
class LatentDebateResult:
    """One-pass compact debate result produced by a shared model context."""

    heuristic: str
    critique: str
    final_solution: str
    votes: tuple[Vote, ...]


@dataclass(frozen=True)
class ConsensusResult:
    """Majority vote over the three evaluations of the final answer."""

    votes: tuple[Vote, ...]
    required_votes: int = 2

    @property
    def approved_count(self) -> int:
        """Number of agents that marked the solution as resolved."""

        return sum(vote.resolved for vote in self.votes)

    @property
    def reached(self) -> bool:
        """Whether the configured absolute majority was reached."""

        return self.approved_count >= self.required_votes

    @property
    def confidence(self) -> float:
        """Average confidence across the available votes."""

        if not self.votes:
            return 0.0
        return sum(vote.confidence for vote in self.votes) / len(self.votes)


@dataclass(frozen=True)
class TemperatureObservation:
    """Temperature and posterior state recorded after one agent update."""

    cycle: int
    role: AgentRole
    temperature: float
    alpha: float
    beta: float
    mean_temperature: float
    reward: float


@dataclass(frozen=True)
class CycleResult:
    """All artifacts produced by one Markov cycle."""

    cycle: int
    heuristic: str
    critique: str
    final_solution: str
    consensus: ConsensusResult
    reward: float
    temperatures: Mapping[AgentRole, float]
    sources: tuple[SourceMatch, ...] = ()
    validation_score: float = 0.0


@dataclass(frozen=True)
class RunResult:
    """Terminal result of a complete 3loop execution."""

    task: str
    kind: TaskKind
    final_solution: str
    cycles: tuple[CycleResult, ...]
    consensus_reached: bool
    completed_cycles: int
    temperature_history: tuple[TemperatureObservation, ...]


@dataclass(frozen=True)
class PipelineEvent:
    """A serializable-ish event suitable for a CLI or a live UI."""

    event_type: EventType
    message: str
    cycle: int | None = None
    role: AgentRole | None = None
    content: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    result: RunResult | None = None
