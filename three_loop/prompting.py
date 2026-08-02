"""Shared-prefix prompt assembly - the reason extra agents are affordable.

llama.cpp reuses the longest token prefix that matches the previous call, so
what an agent's prompt *shares* with the last one is nearly free while
anything before the first difference must be re-prefilled at 14.3 ms/token.

Measured on Qwen2.5-Coder-3B Q4_K_M, five role calls over a ~750-token
context:

    role marker in a short trailing suffix   17.5 s total  (~1.7 s / agent)
    role marker woven in early               61.9 s total  (~12.7 s / agent)

Same model, same tokens, 3.5x apart. Every agent therefore builds its prompt
as ``build_prefix(...) + role suffix``, and the suffix stays short. Adding a
sixth agent costs ~1.7 s instead of ~12.7 s; that is what makes a context
agent or a research agent worth having at all.

Ordering inside the prefix runs most-stable-first so it also survives across
cycles: fixed protocol, then formatting rules, then the task, then sources,
then the history that grows by appending.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import SourceMatch, TaskKind
from .skills import load_skill

#: Identical on every call, so it always sits in the reused prefix.
PROTOCOL_HEADER = "3LOOP protocole multi-agents."


def build_prefix(
    *,
    task: str,
    kind: TaskKind,
    history: str = "",
    sources: Sequence[SourceMatch] = (),
    research_digest: str = "",
) -> str:
    """Assemble the context every agent of a cycle shares, verbatim.

    Sections are appended in increasing order of volatility so that a later
    call keeps as long a matching prefix as possible. Empty sections are
    omitted rather than emitted as placeholders: boilerplate like
    "(aucune source)" costs real tokens and measurably distracts 3B-class
    models on short tasks.
    """

    parts = [f"{PROTOCOL_HEADER}\n3LOOP_KIND={kind.value}"]

    skill = load_skill(kind)
    if skill:
        parts.append(f"FORMATTING RULES:\n{skill}")

    parts.append(f"TASK:\n{task}")

    if research_digest.strip():
        parts.append(f"RESEARCH DIGEST:\n{research_digest.strip()}")
    elif sources:
        parts.append(
            "SOURCES:\n" + "\n".join(f"- {source.url}" for source in sources)
        )

    if history.strip() and not history.startswith("(Aucun historique"):
        parts.append(f"PREVIOUS CYCLES:\n{history}")

    return "\n\n".join(parts)


def with_role(prefix: str, role_instruction: str) -> str:
    """Attach one agent's short instruction to the shared prefix.

    Keep ``role_instruction`` short: it is the only part re-prefilled per
    agent, so its length is the marginal cost of adding that agent.
    """

    return f"{prefix}\n\n{role_instruction.strip()}"
