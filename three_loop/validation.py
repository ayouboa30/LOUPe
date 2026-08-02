"""Lightweight validators and task-kind inference used by the reward signal."""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Awaitable, Callable

from .models import TaskKind


ExternalValidator = Callable[[str, TaskKind], float | Awaitable[float]]


def infer_task_kind(task: str) -> TaskKind:
    """Infer a conservative output kind from common code/math vocabulary."""

    lowered = task.lower()
    code_terms = (
        "code",
        "python",
        "program",
        "programme",
        "fonction",
        "algorithm",
        "algorithme",
        "implémente",
        "implement",
        "api",
        "class ",
    )
    math_terms = (
        "math",
        "mathemat",
        "mathématique",
        "equation",
        "équation",
        "theorem",
        "théorème",
        "proof",
        "preuve",
        "integral",
        "intégrale",
        "latex",
        "derive",
        "dérive",
    )
    if any(term in lowered for term in code_terms):
        return TaskKind.CODE
    if any(term in lowered for term in math_terms):
        return TaskKind.MATH
    return TaskKind.GENERAL


def extract_code(solution: str) -> str:
    """Extract the first fenced code block or use the complete response."""

    match = re.search(r"```(?:python|py)?\s*\n?(.*?)```", solution, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return solution.strip()


def validate_python(solution: str) -> float:
    """Return ``1.0`` when the generated Python parses, otherwise ``0.0``."""

    code = extract_code(solution)
    if not code:
        return 0.0
    try:
        ast.parse(code)
    except SyntaxError:
        return 0.0
    return 1.0


def validate_format(solution: str, kind: TaskKind) -> float:
    """Score basic structural validity without claiming semantic correctness."""

    if not solution.strip():
        return 0.0
    if kind is TaskKind.CODE:
        return validate_python(solution)
    if kind is TaskKind.MATH:
        has_math_signal = any(
            token in solution for token in ("=", "\\(", "\\[", "\\begin", "∫", "\\frac")
        )
        return 1.0 if has_math_signal else 0.5
    return 0.5


async def validate_solution(
    solution: str,
    kind: TaskKind,
    external_validator: ExternalValidator | None = None,
) -> float:
    """Combine a local format check with optional external feedback.

    An external validator is authoritative when provided.  It can be either a
    normal callable or an async callable and must return a score in ``[0, 1]``.
    """

    local_score = validate_format(solution, kind)
    if external_validator is None:
        return local_score
    result = external_validator(solution, kind)
    if inspect.isawaitable(result):
        result = await result
    return min(1.0, max(0.0, float(result)))
