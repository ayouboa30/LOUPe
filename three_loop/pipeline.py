"""The asynchronous three-agent Markov debate pipeline."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Mapping

from .agents import CriticAgent, HeuristicAgent, RoleAgent, WriterAgent
from .backend import SharedLLMBackend
from .compact import compact_text
from .history import ConversationHistory
from .latent import LatentDebateCoordinator
from .support import ContextAgent, ResearchAgent
from .models import (
    AGENT_ROLES,
    AgentRole,
    AgentTurn,
    ConsensusResult,
    CycleResult,
    EventType,
    PipelineEvent,
    RunResult,
    SourceMatch,
    TaskKind,
    WebResearchResult,
)
from .temperature import TemperatureOptimizer
from .validation import ExternalValidator, infer_task_kind, validate_solution
from .web import SearchProvider, triangulate_sources


RewardFunction = Callable[[CycleResult], float | Awaitable[float]]


@dataclass
class PipelineConfig:
    """Runtime limits and optional evaluators for one pipeline instance."""

    max_cycles: int = 5
    required_votes: int = 2
    research_enabled: bool = False
    max_search_results: int = 5
    min_source_agents: int = 2
    max_tokens: int = 2048
    history_max_chars: int = 6000
    compact_debate: bool = False
    #: Distill each cycle's answer before it enters the history. Costs one
    #: extra call (~1.7 s, shared prefix) and repays it on every later cycle
    #: that would otherwise re-prefill the full verbose turn.
    context_agent_enabled: bool = True
    #: Summarise search hits before they reach the debate. Raw web text runs
    #: to thousands of tokens; at 14.3 ms/token that would dominate a cycle.
    research_digest_enabled: bool = True
    #: Skip generating the side-panel-only debate fields. They are 65% of the
    #: generated tokens and decode costs 53 ms/token against 14.6 ms for a
    #: prompt token, so dropping them measured 34.1 s -> 27.0 s (-21%).
    lazy_debate_fields: bool = False
    external_validator: ExternalValidator | None = None
    reward_function: RewardFunction | None = None

    def __post_init__(self) -> None:
        """Validate configuration before any model call is made."""

        if self.max_cycles < 1:
            raise ValueError("max_cycles must be at least one")
        if self.required_votes < 2 or self.required_votes > 3:
            raise ValueError("required_votes must be 2 or 3 for a three-agent vote")
        if self.max_search_results < 1:
            raise ValueError("max_search_results must be at least one")
        if self.min_source_agents < 1:
            raise ValueError("min_source_agents must be at least one")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least one")
        if self.history_max_chars < 512:
            raise ValueError("history_max_chars must be at least 512")


class ThreeLoopPipeline:
    """Run heuristic -> critic -> writer cycles over a shared LLM backend.

    Agents are sequential in the solution path, while the three final votes
    and the three research requests are independent asynchronous tasks.  A
    complete transcript is passed into every subsequent cycle.  The pipeline
    emits :class:`PipelineEvent` objects so a UI can render progress without
    knowing anything about provider internals.
    """

    def __init__(
        self,
        backend: SharedLLMBackend,
        *,
        optimizer: TemperatureOptimizer | None = None,
        config: PipelineConfig | None = None,
        search_provider: SearchProvider | None = None,
        agents: Mapping[AgentRole, RoleAgent] | None = None,
        support_backend: SharedLLMBackend | None = None,
    ) -> None:
        self.backend = backend
        # Support roles only summarise; they do not reason about the task, so
        # they can run on a much smaller model. Measured on this hardware,
        # Qwen2.5-Coder 0.5B (0.37 GB) reaches 260 tok/s prefill and 58 tok/s
        # decode against 69/19 for the 3B - roughly 3-4x on both phases, for
        # work where the quality difference does not show.
        self.support_backend = support_backend or backend
        self.config = config or PipelineConfig()
        self.optimizer = optimizer or TemperatureOptimizer()
        self.search_provider = search_provider
        self._latent_coordinator = LatentDebateCoordinator(
            backend,
            max_tokens=self.config.max_tokens,
            lazy_debate_fields=self.config.lazy_debate_fields,
        )
        self._context_agent = ContextAgent(self.support_backend)
        self._research_agent = ResearchAgent(self.support_backend)
        if agents is None:
            self.agents: dict[AgentRole, RoleAgent] = {
                AgentRole.HEURISTIC: HeuristicAgent(
                    backend, max_tokens=self.config.max_tokens
                ),
                AgentRole.CRITIC: CriticAgent(
                    backend, max_tokens=self.config.max_tokens
                ),
                AgentRole.WRITER: WriterAgent(
                    backend, max_tokens=self.config.max_tokens
                ),
            }
        else:
            missing = set(AGENT_ROLES) - set(agents)
            if missing:
                raise ValueError(f"agents missing roles: {sorted(role.value for role in missing)}")
            self.agents = dict(agents)
        self._run_locks: dict[int, asyncio.Lock] = {}

    async def run(
        self,
        task: str,
        *,
        kind: TaskKind | str | None = None,
        research: bool | None = None,
    ) -> RunResult:
        """Execute a run and return its terminal result."""

        result: RunResult | None = None
        async for event in self.stream(task, kind=kind, research=research):
            if event.result is not None:
                result = event.result
        if result is None:  # defensive guard for future event implementations
            raise RuntimeError("pipeline ended without a terminal result")
        return result

    async def stream(
        self,
        task: str,
        *,
        kind: TaskKind | str | None = None,
        research: bool | None = None,
    ) -> AsyncIterator[PipelineEvent]:
        """Yield live execution events while the debate is running."""

        async with self._lock_for_current_loop():
            async for event in self._stream_unlocked(
                task,
                kind=kind,
                research=research,
            ):
                yield event

    def _lock_for_current_loop(self) -> asyncio.Lock:
        """Serialize runs per event loop without binding the object forever."""

        loop_id = id(asyncio.get_running_loop())
        lock = self._run_locks.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[loop_id] = lock
        return lock

    async def _stream_unlocked(
        self,
        task: str,
        *,
        kind: TaskKind | str | None,
        research: bool | None,
    ) -> AsyncIterator[PipelineEvent]:
        if not task.strip():
            raise ValueError("task must not be empty")
        resolved_kind = _normalize_kind(kind, task)
        use_research = (
            self.config.research_enabled if research is None else research
        )
        history = ConversationHistory()
        cycles: list[CycleResult] = []
        start_history = len(self.optimizer.history)
        last_solution = ""

        try:
            yield PipelineEvent(
                EventType.RUN_STARTED,
                "Execution 3loop demarree.",
                data={
                    "kind": resolved_kind.value,
                    "max_cycles": self.config.max_cycles,
                    "shared_backend_id": id(self.backend),
                },
            )

            for cycle in range(1, self.config.max_cycles + 1):
                temperatures = {
                    role: self.optimizer.sample_temperature(role)
                    for role in AGENT_ROLES
                }
                yield PipelineEvent(
                    EventType.CYCLE_STARTED,
                    f"Cycle {cycle}/{self.config.max_cycles}.",
                    cycle=cycle,
                    data={"temperatures": temperatures},
                )

                research_result = WebResearchResult()
                research_digest = ""
                if use_research:
                    queries = await self._generate_independent_queries(
                        task,
                        kind=resolved_kind,
                        cycle=cycle,
                        temperatures=temperatures,
                    )
                    for role in AGENT_ROLES:
                        query = queries[role.value]
                        history.add_query(cycle, role, query)
                        yield PipelineEvent(
                            EventType.RESEARCH_QUERY,
                            f"{role.label} a propose une requete.",
                            cycle=cycle,
                            role=role,
                            content=query,
                        )
                    if self.search_provider is None:
                        research_result = WebResearchResult(
                            queries=queries,
                            errors={
                                "provider": (
                                    "Aucun SearchProvider configure; intersection non effectuee."
                                )
                            },
                        )
                    else:
                        research_result = await triangulate_sources(
                            queries,
                            self.search_provider,
                            max_results=self.config.max_search_results,
                            min_agents=self.config.min_source_agents,
                        )
                    rendered_sources = _render_sources(research_result.sources)
                    history.add_sources(cycle, rendered_sources)
                    yield PipelineEvent(
                        EventType.RESEARCH_SOURCES,
                        f"{len(research_result.sources)} source(s) conservee(s) par triangulation.",
                        cycle=cycle,
                        data={"research": research_result},
                    )

                    # Raw snippets can run to thousands of tokens; at
                    # 14.3 ms per prompt token that alone would outweigh the
                    # whole debate, so only a digest ever reaches the prompt.
                    if self.config.research_digest_enabled and research_result.sources:
                        research_digest = await self._research_agent.digest(
                            research_result.sources,
                            task=task,
                            kind=resolved_kind,
                        )
                        if research_digest:
                            yield PipelineEvent(
                                EventType.AGENT_OUTPUT,
                                "Agent 5 a resume les sources.",
                                cycle=cycle,
                                role=AgentRole.RESEARCHER,
                                content=research_digest,
                            )

                prior_history = history.render(
                    before_cycle=cycle,
                    max_chars=self.config.history_max_chars,
                )
                if self.config.compact_debate:
                    compact = await self._latent_coordinator.run(
                        task,
                        kind=resolved_kind,
                        cycle=cycle,
                        history=prior_history,
                        sources=research_result.sources,
                        research_digest=research_digest,
                        temperatures=temperatures,
                    )
                    heuristic = compact.heuristic
                    critique = compact.critique
                    writer = compact.final_solution
                    votes = compact.votes
                else:
                    heuristic = await self.agents[AgentRole.HEURISTIC].produce(
                        task,
                        kind=resolved_kind,
                        cycle=cycle,
                        history=prior_history,
                        sources=research_result.sources,
                        temperature=temperatures[AgentRole.HEURISTIC],
                    )
                    # Compact the handoff between roles the same way cycle
                    # boundaries are compacted (see history.render): each
                    # role only needs the gist of what the previous one
                    # produced, and stripping vowels/punctuation cuts the
                    # prefill cost of that carried-over context on CPU.
                    critique = await self.agents[AgentRole.CRITIC].produce(
                        task,
                        compact_text(heuristic),
                        kind=resolved_kind,
                        cycle=cycle,
                        history=prior_history,
                        sources=research_result.sources,
                        temperature=temperatures[AgentRole.CRITIC],
                    )
                    writer = await self.agents[AgentRole.WRITER].produce(
                        task,
                        compact_text(heuristic),
                        compact_text(critique),
                        kind=resolved_kind,
                        cycle=cycle,
                        history=prior_history,
                        sources=research_result.sources,
                        temperature=temperatures[AgentRole.WRITER],
                    )
                    votes = tuple(
                        await asyncio.gather(
                            *(
                                self.agents[role].evaluate(
                                    task,
                                    writer,
                                    kind=resolved_kind,
                                    cycle=cycle,
                                    history=history.render(
                                        before_cycle=cycle,
                                        max_chars=min(self.config.history_max_chars, 2000),
                                    ),
                                    temperature=temperatures[role],
                                )
                                for role in AGENT_ROLES
                            )
                        )
                    )

                last_solution = writer

                # What goes into the history is re-read by every later cycle,
                # so it is distilled once here rather than re-prefilled in
                # full each time. ``last_solution`` keeps the verbatim answer:
                # the user sees the full text, only the model sees the digest.
                if self.config.context_agent_enabled:
                    distilled = await self._context_agent.distill(
                        writer,
                        task=task,
                        kind=resolved_kind,
                        speaker=AgentRole.WRITER.label,
                    )
                    if distilled != writer:
                        yield PipelineEvent(
                            EventType.AGENT_OUTPUT,
                            "Agent 4 a distille le contexte du cycle.",
                            cycle=cycle,
                            role=AgentRole.CONTEXT,
                            content=distilled,
                        )
                else:
                    distilled = writer

                history.add_turn(
                    AgentTurn(cycle, AgentRole.HEURISTIC, heuristic, temperatures[AgentRole.HEURISTIC])
                )
                history.add_turn(
                    AgentTurn(cycle, AgentRole.CRITIC, critique, temperatures[AgentRole.CRITIC])
                )
                history.add_turn(
                    AgentTurn(cycle, AgentRole.WRITER, distilled, temperatures[AgentRole.WRITER])
                )
                yield PipelineEvent(
                    EventType.AGENT_OUTPUT,
                    "Agent 1 a produit une heuristique.",
                    cycle=cycle,
                    role=AgentRole.HEURISTIC,
                    content=heuristic,
                )
                yield PipelineEvent(
                    EventType.AGENT_OUTPUT,
                    "Agent 2 a critique et corrige le brouillon.",
                    cycle=cycle,
                    role=AgentRole.CRITIC,
                    content=critique,
                )
                yield PipelineEvent(
                    EventType.AGENT_OUTPUT,
                    "Agent 3 a redige la solution finale du cycle.",
                    cycle=cycle,
                    role=AgentRole.WRITER,
                    content=writer,
                )
                consensus = ConsensusResult(
                    votes=votes,
                    required_votes=self.config.required_votes,
                )
                history.add_consensus(cycle, consensus)
                for vote in votes:
                    yield PipelineEvent(
                        EventType.VOTE,
                        f"{vote.role.label}: "
                        f"{'RESOLU' if vote.resolved else 'A REVOIR'}.",
                        cycle=cycle,
                        role=vote.role,
                        content=vote.rationale,
                        data={"vote": vote, "consensus": consensus},
                    )

                validation_score = await validate_solution(
                    writer,
                    resolved_kind,
                    self.config.external_validator,
                )
                provisional = CycleResult(
                    cycle=cycle,
                    heuristic=heuristic,
                    critique=critique,
                    final_solution=writer,
                    consensus=consensus,
                    reward=0.0,
                    temperatures=temperatures,
                    sources=research_result.sources,
                    validation_score=validation_score,
                )
                reward = await self._calculate_reward(provisional)
                cycle_result = replace(provisional, reward=reward)
                cycles.append(cycle_result)

                observations = self.optimizer.update_all(
                    reward,
                    temperatures,
                    cycle=cycle,
                )
                for observation in observations:
                    yield PipelineEvent(
                        EventType.PRIOR_UPDATED,
                        f"Prior {observation.role.value} mis a jour: "
                        f"T moyenne={observation.mean_temperature:.3f}.",
                        cycle=cycle,
                        role=observation.role,
                        data={"observation": observation, "posterior": self.optimizer.snapshot()},
                    )
                yield PipelineEvent(
                    EventType.CYCLE_COMPLETED,
                    f"Cycle {cycle} termine: recompense={reward:.3f}, "
                    f"votes={consensus.approved_count}/3.",
                    cycle=cycle,
                    data={
                        "cycle_result": cycle_result,
                        "validation_score": validation_score,
                        "posterior": self.optimizer.snapshot(),
                    },
                )

                if consensus.reached:
                    result = self._build_result(
                        task,
                        resolved_kind,
                        cycles,
                        consensus_reached=True,
                        last_solution=last_solution,
                        start_history=start_history,
                    )
                    yield PipelineEvent(
                        EventType.RUN_COMPLETED,
                        f"Consensus atteint au cycle {cycle}.",
                        cycle=cycle,
                        result=result,
                    )
                    return

            result = self._build_result(
                task,
                resolved_kind,
                cycles,
                consensus_reached=False,
                last_solution=last_solution,
                start_history=start_history,
            )
            yield PipelineEvent(
                EventType.RUN_COMPLETED,
                "Limite de cycles atteinte sans majorite absolue.",
                cycle=self.config.max_cycles,
                result=result,
            )
        except Exception as exc:
            yield PipelineEvent(
                EventType.ERROR,
                f"Erreur pendant 3loop: {type(exc).__name__}: {exc}",
                data={"exception": repr(exc)},
            )
            raise

    async def _generate_independent_queries(
        self,
        task: str,
        *,
        kind: TaskKind,
        cycle: int,
        temperatures: Mapping[AgentRole, float],
    ) -> dict[str, str]:
        """Ask all identities independently before any web result is shared."""

        queries = await asyncio.gather(
            *(
                self.agents[role].search_query(
                    task,
                    kind=kind,
                    cycle=cycle,
                    temperature=temperatures[role],
                )
                for role in AGENT_ROLES
            )
        )
        return {role.value: query for role, query in zip(AGENT_ROLES, queries)}

    async def _calculate_reward(self, cycle_result: CycleResult) -> float:
        """Compute a transparent reward from votes, validation, and speed."""

        if self.config.reward_function is not None:
            raw_reward = self.config.reward_function(cycle_result)
            if inspect.isawaitable(raw_reward):
                raw_reward = await raw_reward
            return min(1.0, max(0.0, float(raw_reward)))

        vote_ratio = cycle_result.consensus.approved_count / max(
            len(cycle_result.consensus.votes), 1
        )
        speed_score = max(
            0.0,
            1.0 - (cycle_result.cycle - 1) / self.config.max_cycles,
        )
        reward = (
            0.55 * vote_ratio
            + 0.30 * cycle_result.validation_score
            + 0.15 * speed_score
        )
        if cycle_result.consensus.reached:
            reward = max(reward, 0.85)
        return min(1.0, max(0.0, reward))

    def _build_result(
        self,
        task: str,
        kind: TaskKind,
        cycles: list[CycleResult],
        *,
        consensus_reached: bool,
        last_solution: str,
        start_history: int,
    ) -> RunResult:
        """Build a result containing only observations from this run."""

        observations = self.optimizer.history[start_history:]
        return RunResult(
            task=task,
            kind=kind,
            final_solution=last_solution,
            cycles=tuple(cycles),
            consensus_reached=consensus_reached,
            completed_cycles=len(cycles),
            temperature_history=tuple(observations),
        )


ThreeLoop = ThreeLoopPipeline


def _normalize_kind(kind: TaskKind | str | None, task: str) -> TaskKind:
    if kind is None:
        return infer_task_kind(task)
    return kind if isinstance(kind, TaskKind) else TaskKind(kind)


def _render_sources(sources: tuple[SourceMatch, ...]) -> str:
    if not sources:
        return ""
    return "\n".join(
        f"{source.url} ({source.match_type}, {source.domain})"
        for source in sources
    )
