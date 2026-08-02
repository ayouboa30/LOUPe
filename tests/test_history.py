from three_loop.history import ConversationHistory
from three_loop.models import AgentRole, AgentTurn


def _history_with(*cycles: int) -> ConversationHistory:
    history = ConversationHistory()
    for cycle in cycles:
        history.add_turn(
            AgentTurn(cycle, AgentRole.WRITER, f"Reponse du cycle {cycle}.", 0.5)
        )
    return history


def test_render_keeps_wording_readable() -> None:
    rendered = _history_with(1).render(before_cycle=2)

    assert "Reponse du cycle 1." in rendered
    assert "Cycle 1" in rendered


def test_render_is_append_only_across_cycles() -> None:
    """Cycle N+1's history must extend cycle N's verbatim.

    This is what lets llama.cpp reuse the KV prefix instead of re-prefilling
    the whole transcript: measured 1.02 s versus 9.45 s on a ~724-token
    prefix. A renderer that rewrites earlier text would silently forfeit it.
    """

    history = _history_with(1, 2, 3)

    after_two = history.render(before_cycle=3)
    after_three = history.render(before_cycle=4)

    assert after_three.startswith(after_two)


def test_render_over_budget_drops_whole_leading_entries() -> None:
    history = _history_with(*range(1, 40))

    rendered = history.render(before_cycle=40, max_chars=300)

    assert len(rendered) <= 300 + 60  # marker line allowed on top
    assert "omis" in rendered
    assert "Reponse du cycle 39." in rendered  # most recent kept
    assert "Reponse du cycle 1." not in rendered


def test_render_reports_no_history_on_first_cycle() -> None:
    assert ConversationHistory().render(before_cycle=1) == "(Aucun historique: premier cycle.)"
