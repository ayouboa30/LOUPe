import asyncio

from three_loop.backend import SharedLLMBackend
from three_loop.models import AgentRole, SourceMatch, TaskKind
from three_loop.prompting import build_prefix
from three_loop.support import ContextAgent, ResearchAgent

TASK = "Concevoir un reseau de neurones"


class _StubBackend(SharedLLMBackend):
    def __init__(self, reply: str) -> None:
        super().__init__()
        self.reply = reply
        self.prompts: list[str] = []

    async def _complete(self, prompt, *, temperature, system_prompt, max_tokens):
        del temperature, system_prompt, max_tokens
        self.prompts.append(prompt)
        return self.reply


def test_context_agent_skips_the_call_when_content_is_already_short() -> None:
    """A call costs ~1.7 s; shrinking a short turn would never repay it."""

    backend = _StubBackend("distilled")
    agent = ContextAgent(backend)

    out = asyncio.run(
        agent.distill("Court.", task=TASK, kind=TaskKind.CODE, speaker="Redacteur")
    )

    assert out == "Court."
    assert backend.prompts == []


def test_context_agent_distills_long_content() -> None:
    backend = _StubBackend("- point un\n- point deux")
    agent = ContextAgent(backend)

    out = asyncio.run(
        agent.distill("x " * 400, task=TASK, kind=TaskKind.CODE, speaker="Redacteur")
    )

    assert out == "- point un\n- point deux"
    assert len(backend.prompts) == 1


def test_context_agent_keeps_the_original_when_distillation_grew() -> None:
    """A distillation that got longer is a failed one - it would cost prefill."""

    long_content = "y " * 400
    backend = _StubBackend("z " * 900)
    agent = ContextAgent(backend)

    out = asyncio.run(
        agent.distill(long_content, task=TASK, kind=TaskKind.CODE, speaker="Redacteur")
    )

    assert out == long_content.strip()


def test_support_agents_build_on_the_shared_prefix() -> None:
    """Both must land inside the reused KV prefix, or they cost ~12.7 s each."""

    prefix = build_prefix(task=TASK, kind=TaskKind.CODE)

    context_backend = _StubBackend("ok")
    asyncio.run(
        ContextAgent(context_backend).distill(
            "x " * 400, task=TASK, kind=TaskKind.CODE, speaker="Redacteur"
        )
    )

    source = SourceMatch(
        url="https://example.org/a",
        domain="example.org",
        title="A",
        snippet="contenu",
        agent_ids=("heuristic",),
    )
    research_backend = _StubBackend("- fait [1]")
    asyncio.run(
        ResearchAgent(research_backend).digest([source], task=TASK, kind=TaskKind.CODE)
    )

    assert context_backend.prompts[0].startswith(prefix)
    assert research_backend.prompts[0].startswith(prefix)


def test_research_agent_returns_nothing_without_sources() -> None:
    backend = _StubBackend("unused")

    out = asyncio.run(ResearchAgent(backend).digest([], task=TASK, kind=TaskKind.CODE))

    assert out == ""
    assert backend.prompts == []


def test_research_agent_caps_snippet_length_before_prompting() -> None:
    """A raw page is thousands of tokens; it must never reach the prompt whole."""

    source = SourceMatch(
        url="https://example.org/a",
        domain="example.org",
        title="A",
        snippet="w" * 5000,
        agent_ids=("heuristic",),
    )
    backend = _StubBackend("- fait [1]")

    asyncio.run(ResearchAgent(backend).digest([source], task=TASK, kind=TaskKind.CODE))

    assert "w" * 500 not in backend.prompts[0]


def test_support_roles_do_not_vote() -> None:
    from three_loop.models import AGENT_ROLES

    assert AgentRole.CONTEXT not in AGENT_ROLES
    assert AgentRole.RESEARCHER not in AGENT_ROLES
    assert len(AGENT_ROLES) == 3
