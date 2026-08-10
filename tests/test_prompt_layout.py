"""Guards on prompt layout, which is what makes CPU inference tolerable.

llama.cpp reuses the longest matching KV prefix from the previous call.
Measured on Qwen2.5-Coder-3B Q4_K_M, five role calls over a ~750-token
context: role marker in a short trailing suffix costs 17.5 s total
(~1.7 s/agent), the same roles with the marker woven in early cost 61.9 s
(~12.7 s/agent). Prefill is ~94% of wall-clock time, so these ordering
properties are the difference between a usable and an unusable app.

There is a deliberate trade-off encoded here. Role-specific text sits *last*,
after the growing history, which means it is re-prefilled every cycle
(~200 tokens, ~2.9 s). Moving it earlier would cache it across cycles but
would break prefix sharing *between agents*, costing ~11 s per extra agent.
Sharing between agents wins.
"""

import asyncio

from three_loop.backend import SharedLLMBackend
from three_loop.latent import LatentDebateCoordinator
from three_loop.models import AGENT_ROLES, TaskKind
from three_loop.prompting import build_prefix, with_role

TASK = "Demontre le theoreme de Pythagore"


class _CapturingBackend(SharedLLMBackend):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def _complete(self, prompt, *, temperature, system_prompt, max_tokens, on_token=None):
        del temperature, system_prompt, max_tokens
        self.prompts.append(prompt)
        return (
            '{"heuristic":"h","critique":"c","final_solution":"s","votes":['
            '{"role":"heuristic","resolved":true,"confidence":0.9,"rationale":"r"},'
            '{"role":"critic","resolved":true,"confidence":0.9,"rationale":"r"},'
            '{"role":"writer","resolved":true,"confidence":0.9,"rationale":"r"}]}'
        )


def _run(coordinator, *, cycle, history):
    return asyncio.run(
        coordinator.run(
            TASK,
            kind=TaskKind.MATH,
            cycle=cycle,
            history=history,
            temperatures={role: 0.5 for role in AGENT_ROLES},
        )
    )


def test_every_agent_shares_one_long_prefix() -> None:
    """The invariant that makes extra agents cost ~1.7 s instead of ~12.7 s."""

    prefix = build_prefix(task=TASK, kind=TaskKind.MATH, history="[Cycle 1]\nx")
    prompts = [
        with_role(prefix, "ROLE=context. Distille."),
        with_role(prefix, "ROLE=researcher. Resume."),
        with_role(prefix, "3LOOP_ACTION=latent_debate\nRun these roles..."),
    ]

    for prompt in prompts:
        assert prompt.startswith(prefix)
    # The shared part must dominate, otherwise there is nothing worth reusing.
    shortest_tail = min(len(p) - len(prefix) for p in prompts)
    assert len(prefix) > 4 * shortest_tail


def test_debate_prompt_is_built_on_the_shared_prefix() -> None:
    backend = _CapturingBackend()
    _run(LatentDebateCoordinator(backend, max_tokens=512), cycle=1, history="")

    expected_prefix = build_prefix(task=TASK, kind=TaskKind.MATH)
    assert backend.prompts[0].startswith(expected_prefix)


def test_prefix_up_to_history_is_stable_as_cycles_accumulate() -> None:
    backend = _CapturingBackend()
    coordinator = LatentDebateCoordinator(backend, max_tokens=512)

    _run(coordinator, cycle=1, history="(Aucun historique: premier cycle.)")
    _run(coordinator, cycle=2, history="[Cycle 1 - Redacteur]\nPremiere reponse.")

    first, second = backend.prompts
    # Cycle 1 has no history section, so everything it holds before the role
    # tail must still be a prefix of cycle 2's prompt.
    stable = first.split("3LOOP_ACTION=")[0]
    assert second.startswith(stable)
    assert len(stable) > 200


def test_history_sits_after_the_task_and_before_the_role_tail() -> None:
    backend = _CapturingBackend()
    _run(
        LatentDebateCoordinator(backend, max_tokens=512),
        cycle=3,
        history="[Cycle 2 - Redacteur]\nContenu precedent.",
    )

    prompt = backend.prompts[0]
    assert prompt.index("TASK:") < prompt.index("PREVIOUS CYCLES:")
    assert prompt.index("PREVIOUS CYCLES:") < prompt.index("3LOOP_ACTION=")


def test_empty_sections_are_omitted_entirely() -> None:
    """Placeholder boilerplate costs real tokens and distracts small models."""

    prefix = build_prefix(task=TASK, kind=TaskKind.MATH)

    assert "PREVIOUS CYCLES:" not in prefix
    assert "SOURCES:" not in prefix
    assert "RESEARCH DIGEST:" not in prefix


def test_research_digest_replaces_raw_sources_in_the_prefix() -> None:
    """Raw web text would swamp prefill; only the digest may enter the prompt."""

    from three_loop.models import SourceMatch

    source = SourceMatch(
        url="https://example.org/a",
        domain="example.org",
        title="A",
        snippet="x" * 4000,
        agent_ids=("heuristic",),
    )
    prefix = build_prefix(
        task=TASK,
        kind=TaskKind.MATH,
        sources=[source],
        research_digest="- fait utile [1]",
    )

    assert "RESEARCH DIGEST:" in prefix
    assert "x" * 100 not in prefix
