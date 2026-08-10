import asyncio
import json

import pytest

from three_loop import (
    AgentRole,
    DemoBackend,
    EventType,
    PipelineConfig,
    SearchResult,
    TaskKind,
    ThreeLoopPipeline,
)


def test_pipeline_restarts_after_failed_consensus_and_keeps_full_history() -> None:
    pipeline = ThreeLoopPipeline(
        DemoBackend(resolved_after=2),
        config=PipelineConfig(max_cycles=3),
    )

    result = asyncio.run(pipeline.run("Write Python code for a parser", kind=TaskKind.CODE))

    assert result.consensus_reached is True
    assert result.completed_cycles == 2
    assert len(result.cycles) == 2
    assert len(result.temperature_history) == 6
    assert result.cycles[0].consensus.reached is False
    assert result.cycles[1].consensus.reached is True
    assert result.cycles[-1].validation_score == 1.0


def test_stream_exposes_ordered_agents_votes_and_shared_backend_identity() -> None:
    pipeline = ThreeLoopPipeline(DemoBackend())

    async def collect():
        return [event async for event in pipeline.stream("Solve this math proof")]

    events = asyncio.run(collect())
    assert events[0].event_type is EventType.RUN_STARTED
    outputs = [event.role for event in events if event.event_type is EventType.AGENT_OUTPUT]
    assert outputs == [AgentRole.HEURISTIC, AgentRole.CRITIC, AgentRole.WRITER]
    assert sum(event.event_type is EventType.VOTE for event in events) == 3
    assert events[-1].event_type is EventType.RUN_COMPLETED
    assert events[0].data["shared_backend_id"] == id(pipeline.backend)


def test_research_generates_three_queries_and_passes_only_intersection() -> None:
    class SharedProvider:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def search(self, query: str, *, max_results: int = 5):
            self.queries.append(query)
            return [SearchResult("https://docs.example.org/shared")]

    provider = SharedProvider()
    pipeline = ThreeLoopPipeline(
        DemoBackend(),
        config=PipelineConfig(max_cycles=1),
        search_provider=provider,
    )

    async def collect():
        return [event async for event in pipeline.stream("Find the Python API", research=True)]

    events = asyncio.run(collect())
    research_event = next(
        event for event in events if event.event_type is EventType.RESEARCH_SOURCES
    )
    assert len(provider.queries) == 3
    assert len(research_event.data["research"].sources) == 1


def test_compact_debate_uses_one_model_call_per_cycle() -> None:
    backend = DemoBackend(resolved_after=1)
    pipeline = ThreeLoopPipeline(
        backend,
        config=PipelineConfig(max_cycles=1, compact_debate=True),
    )

    result = asyncio.run(pipeline.run("Write Python code for a parser", kind=TaskKind.CODE))

    assert result.consensus_reached is True
    assert backend.calls == 1
    assert result.cycles[0].final_solution.startswith("```python")


def test_compact_debate_strips_protocol_echo_from_the_answer() -> None:
    from three_loop.latent import parse_latent_debate

    vote = '{"role": "%s", "resolved": true, "confidence": 0.9, "rationale": "ok"}'
    raw = json.dumps(
        {
            "heuristic": "plan",
            "critique": "fix",
            "final_solution": 'def f():\n    return 1\n\nVotes: ["heuristic", "critic"]',
            "votes": [
                json.loads(vote % role)
                for role in ("heuristic", "critic", "writer")
            ],
        }
    )

    result = parse_latent_debate(raw)

    assert result.final_solution == "def f():\n    return 1"


def test_compact_debate_recovers_from_truncated_json() -> None:
    from three_loop.latent import parse_latent_debate

    # A max_tokens cutoff lands mid-vote-array: no closing braces/brackets.
    raw = (
        '{"heuristic": "plan initial", "critique": "verifier les couches", '
        '"final_solution": "Un CNN avec 3 couches de convolution suivies '
        'd\'une couche dense.", "votes": [{"role": "heuristic", "resolved"'
    )

    result = parse_latent_debate(raw, task="architecture de deep learning")

    assert "CNN" in result.final_solution
    assert len(result.votes) == 3
    assert all(vote.resolved is False for vote in result.votes)


def test_compact_debate_falls_back_to_raw_text_when_json_is_unrecoverable() -> None:
    from three_loop.latent import parse_latent_debate

    raw = "Voici une reponse en texte libre, sans JSON du tout."

    result = parse_latent_debate(raw, task="une question")

    assert result.final_solution == raw
    assert len(result.votes) == 3
    assert all(vote.resolved is False for vote in result.votes)


def test_compact_debate_recovers_final_solution_text_when_structure_is_unrecoverable() -> None:
    """The whole JSON blob must never leak to the user as the visible answer."""

    from three_loop.latent import parse_latent_debate

    # Malformed beyond repair (bad nesting/mismatched braces the bracket-stack
    # repair can't fix), but "final_solution" itself is intact plain text.
    raw = (
        '{"heuristic": "x", "critique": "y", '
        '"final_solution": "La demonstration du theoreme de Pythagore.", '
        '"votes": [{{{malformed'
    )

    result = parse_latent_debate(raw, task="demontre pythagore")

    assert result.final_solution == "La demonstration du theoreme de Pythagore."
    assert "heuristic" not in result.final_solution
    assert len(result.votes) == 3
    assert all(vote.resolved is False for vote in result.votes)


def test_compact_debate_accepts_code_with_raw_newlines_in_the_json_string() -> None:
    """Models emit fenced code with literal newlines, which JSON forbids.

    Rejecting those responses discarded a perfectly good answer and, worse,
    marked the votes unresolved so the pipeline burned another cycle.
    """

    from three_loop.latent import parse_latent_debate

    raw = (
        '{"heuristic":"esquisse", "critique":"ok", "final_solution":"```python\n'
        "import torch\n"
        "class VAE(torch.nn.Module):\n"
        "    pass\n"
        '```", "votes":['
        '{"role":"heuristic","resolved":true,"confidence":0.9,"rationale":"r"},'
        '{"role":"critic","resolved":true,"confidence":0.9,"rationale":"r"},'
        '{"role":"writer","resolved":true,"confidence":0.9,"rationale":"r"}]}'
    )

    result = parse_latent_debate(raw, task="Ecris un VAE")

    assert "import torch" in result.final_solution
    assert "class VAE" in result.final_solution
    assert all(vote.resolved for vote in result.votes)


def test_compact_debate_recovers_an_answer_truncated_mid_string() -> None:
    """Generation cut off inside final_solution must not lose what was written."""

    from three_loop.latent import parse_latent_debate

    raw = '{"heuristic":"h", "critique":"c", "final_solution":"import torch\nclass VAE'

    result = parse_latent_debate(raw, task="Ecris un VAE")

    assert "class VAE" in result.final_solution
    assert "tronquee pour" not in result.final_solution


def test_extract_final_solution_returns_none_when_field_is_absent() -> None:
    from three_loop.latent import extract_final_solution

    assert extract_final_solution('{"heuristic":"h"}') is None


def test_lazy_mode_omits_side_panel_fields_from_the_requested_schema() -> None:
    """Those fields are 65% of generated tokens and are never displayed."""

    from three_loop.backend import SharedLLMBackend
    from three_loop.latent import LatentDebateCoordinator
    from three_loop.models import AGENT_ROLES as ROLES, TaskKind as TK

    class Capture(SharedLLMBackend):
        def __init__(self):
            super().__init__()
            self.prompt = ""

        async def _complete(self, prompt, *, temperature, system_prompt, max_tokens, on_token=None):
            self.prompt = prompt
            return (
                '{"final_solution":"reponse","votes":['
                '{"role":"heuristic","resolved":true,"confidence":0.9},'
                '{"role":"critic","resolved":true,"confidence":0.9},'
                '{"role":"writer","resolved":true,"confidence":0.9}]}'
            )

    backend = Capture()
    coordinator = LatentDebateCoordinator(backend, max_tokens=512, lazy_debate_fields=True)
    result = asyncio.run(
        coordinator.run("tache", kind=TK.GENERAL, cycle=1, history="",
                        temperatures={r: 0.5 for r in ROLES})
    )

    assert '"rationale"' not in backend.prompt
    assert '"heuristic":"one sentence"' not in backend.prompt
    # The answer and the votes still arrive: they drive display and control flow.
    assert result.final_solution == "reponse"
    assert all(vote.resolved for vote in result.votes)


def test_missing_side_panel_fields_are_not_a_parse_failure() -> None:
    """Treating them as one would mark votes unresolved and burn a cycle."""

    from three_loop.latent import NOT_GENERATED, parse_latent_debate

    raw = (
        '{"final_solution":"la reponse","votes":['
        '{"role":"heuristic","resolved":true,"confidence":0.9},'
        '{"role":"critic","resolved":true,"confidence":0.9},'
        '{"role":"writer","resolved":true,"confidence":0.9}]}'
    )

    result = parse_latent_debate(raw, task="t")

    assert result.final_solution == "la reponse"
    assert result.heuristic == NOT_GENERATED
    assert all(vote.resolved for vote in result.votes)


def test_foreign_json_never_reaches_the_user_as_raw_braces() -> None:
    """A model that invents its own schema must not print braces at the user.

    Recovery tries known answer keys at any depth, then the longest string,
    which in practice is the prose answer among short metadata values.
    """

    from three_loop.latent import parse_latent_debate

    cases = {
        '{"reponse":{"texte":"bonjour le monde"},"meta":{"x":1}}': "bonjour le monde",
        '{"answer":"la vraie reponse","confidence":0.9}': "la vraie reponse",
        '{"aaa":"court","bbb":"une reponse nettement plus longue"}':
            "une reponse nettement plus longue",
    }

    for raw, expected in cases.items():
        result = parse_latent_debate(raw, task="test")
        assert result.final_solution == expected
        assert not result.final_solution.lstrip().startswith("{")


def test_plain_prose_is_left_untouched() -> None:
    """The JSON recovery must not fire on an ordinary text answer."""

    from three_loop.latent import parse_latent_debate

    raw = "Juste du texte normal."

    assert parse_latent_debate(raw, task="t").final_solution == raw


def test_compact_debate_rejects_protocol_only_text_instead_of_inventing_an_answer() -> None:
    """An invalid compact response must never echo the user's task as a fake answer."""

    from three_loop.latent import parse_latent_debate

    with pytest.raises(ValueError, match="aucune solution exploitable"):
        parse_latent_debate('{"heuristic": "", "critique": ""}', task="Fais la démonstration de Pythagore")


def test_stream_cancellation_stops_before_a_completion_event() -> None:
    """A pending cancellation must prevent both model work and RUN_COMPLETED."""

    from three_loop.pipeline import PipelineCancelled

    backend = DemoBackend()
    pipeline = ThreeLoopPipeline(backend, config=PipelineConfig(max_cycles=1))

    async def collect() -> list:
        events = []
        with pytest.raises(PipelineCancelled):
            async for event in pipeline.stream(
                "Démontre le théorème de Pythagore",
                cancel_requested=lambda: True,
            ):
                events.append(event)
        return events

    events = asyncio.run(collect())

    assert backend.calls == 0
    assert all(event.event_type is not EventType.RUN_COMPLETED for event in events)
