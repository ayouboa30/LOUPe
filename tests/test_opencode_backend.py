import json

from three_loop.opencode_backend import DEFAULT_MODEL, _extract_text


def _event(kind: str, **part):
    return json.dumps({"type": kind, "part": part})


def test_extract_text_concatenates_only_text_events() -> None:
    """OpenCode emits NDJSON; only `text` parts carry the answer."""

    stdout = "\n".join(
        [
            _event("step_start", type="step-start"),
            _event("text", type="text", text="Hello "),
            _event("text", type="text", text="world"),
            _event("step_finish", type="step-finish", tokens={"total": 12}),
        ]
    )

    assert _extract_text(stdout) == "Hello world"


def test_extract_text_skips_malformed_lines_without_losing_the_answer() -> None:
    """One bad line must not discard a response that otherwise completed."""

    stdout = "\n".join(
        [
            "not json at all",
            _event("text", type="text", text="partie 1 "),
            "{ truncated json",
            _event("text", type="text", text="partie 2"),
        ]
    )

    assert _extract_text(stdout) == "partie 1 partie 2"


def test_extract_text_returns_empty_when_no_text_event() -> None:
    stdout = _event("step_finish", type="step-finish")

    assert _extract_text(stdout) == ""


def test_extract_text_ignores_non_string_text_fields() -> None:
    stdout = "\n".join(
        [
            _event("text", type="text", text=None),
            _event("text", type="text", text="reel"),
        ]
    )

    assert _extract_text(stdout) == "reel"


def test_default_model_is_the_free_deepseek_flash() -> None:
    assert DEFAULT_MODEL == "opencode/deepseek-v4-flash-free"


def test_default_agent_is_a_valid_primary_agent() -> None:
    """`opencode run --agent` only accepts primary agents.

    Passing a subagent name (`explore`, `general`) does not error - the CLI
    silently falls back to the default primary agent (`build`) and only
    warns on stderr, which a caller not checking stderr would never notice.
    Of the primary agents (build, compaction, plan, summary, title), `plan`
    refuses the debate protocol and the rest are single-purpose internals,
    so `build` is what is actually used.
    """

    from three_loop.opencode_backend import DEFAULT_AGENT

    assert DEFAULT_AGENT == "build"
    assert DEFAULT_AGENT not in ("explore", "general")  # subagents, not valid here


def test_opencode_runs_in_an_isolated_empty_workspace() -> None:
    """Defence in depth: never run a file-touching agent in the user's project."""

    from pathlib import Path
    from three_loop.opencode_backend import _workspace

    workspace = _workspace()

    assert workspace != Path.cwd()
    assert ".3loop" in str(workspace)


def test_build_opencode_prompt_opens_with_the_instruction() -> None:
    """A heading-like opener gets answered rather than acted on (measured)."""

    from three_loop.models import TaskKind
    from three_loop.opencode_backend import build_opencode_prompt

    prompt = build_opencode_prompt(
        instruction="Fais ceci precisement.", task="calcule 2+2", kind=TaskKind.GENERAL,
    )

    assert prompt.startswith("Fais ceci precisement.")


def test_build_opencode_prompt_ends_with_the_no_files_constraint() -> None:
    """Tried as the opening line this gets answered ("what is your question?")
    instead of followed; it must close the message."""

    from three_loop.models import TaskKind
    from three_loop.opencode_backend import build_opencode_prompt

    prompt = build_opencode_prompt(
        instruction="Fais ceci.", task="calcule 2+2", kind=TaskKind.GENERAL,
    )

    assert prompt.rstrip().endswith("rien d'autre.")
    assert "sans consulter" in prompt


def test_build_opencode_prompt_omits_empty_sections() -> None:
    from three_loop.models import TaskKind
    from three_loop.opencode_backend import build_opencode_prompt

    prompt = build_opencode_prompt(
        instruction="Fais ceci.", task="calcule 2+2", kind=TaskKind.GENERAL,
    )

    assert "Cycles precedents" not in prompt
    assert "Sources:" not in prompt
    assert "Resume de recherche" not in prompt


def test_build_opencode_prompt_never_leaks_the_3loop_protocol_markers() -> None:
    """These read to a coding agent as a project name to go inspect (measured)."""

    from three_loop.models import TaskKind
    from three_loop.opencode_backend import build_opencode_prompt

    prompt = build_opencode_prompt(
        instruction="Fais ceci.", task="calcule 2+2", kind=TaskKind.GENERAL,
        history="[Cycle 1 - Agent 3 - Redacteur]\nReponse.",
    )

    assert "3LOOP" not in prompt
