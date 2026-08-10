"""Single-context debate path for reducing prefill and network round trips."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence

from .agents import parse_vote
from .backend import SharedLLMBackend
from .models import AGENT_ROLES, AgentRole, LatentDebateResult, SourceMatch, TaskKind
from .cli_agent_backend import CLIAgentBackend, build_cli_agent_prompt
from .prompting import build_prefix, with_role


_FULL_SCHEMA = (
    '{"heuristic":"one sentence", "critique":"one sentence",\n'
    '"final_solution":"complete answer", "votes":[\n'
    '{"role":"heuristic","resolved":true,"confidence":0.0,"rationale":"one sentence"},\n'
    '{"role":"critic","resolved":true,"confidence":0.0,"rationale":"one sentence"},\n'
    '{"role":"writer","resolved":true,"confidence":0.0,"rationale":"one sentence"}]}'
)

#: Same contract minus the fields that only populate the side panel. The
#: votes are kept - the pipeline needs them to decide whether to run another
#: cycle - but their prose rationale is dropped, since only the boolean and
#: the confidence drive that decision.
_LAZY_SCHEMA = (
    '{"final_solution":"complete answer", "votes":[\n'
    '{"role":"heuristic","resolved":true,"confidence":0.0},\n'
    '{"role":"critic","resolved":true,"confidence":0.0},\n'
    '{"role":"writer","resolved":true,"confidence":0.0}]}'
)


def _schema(lazy: bool) -> str:
    return _LAZY_SCHEMA if lazy else _FULL_SCHEMA


_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class SolutionStreamer:
    """Decode a JSON string field while the JSON is still being generated.

    The compact debate returns one JSON object, so the user would normally
    see nothing until the whole thing is parsed - on a local CPU that is the
    entire generation, tens of seconds of a frozen screen. ``final_solution``
    is the first field of the schema, so it can be surfaced as it is produced.

    Everything here is about *partial* input: a chunk boundary can fall in
    the middle of a ``\\uXXXX`` escape or right after a lone backslash, and
    emitting those raw would put stray characters on screen that a later
    correction cannot take back. Anything not yet provably complete is held
    until the next chunk.

    Only ever an optimisation for display: the authoritative answer is still
    the fully parsed object, so a stream that gives up early costs a less
    lively screen, never a wrong answer.
    """

    def __init__(self, field: str = "final_solution") -> None:
        self._needle = f'"{field}"'
        self._raw = ""
        self._value_start: int | None = None
        self._cursor = 0
        self.finished = False

    def feed(self, chunk: str) -> str:
        """Add generated text and return whatever became readable."""

        if self.finished or not chunk:
            return ""
        self._raw += chunk
        if self._value_start is None:
            self._value_start = self._locate_value()
            if self._value_start is None:
                return ""
            self._cursor = self._value_start
        return self._decode_available()

    def _locate_value(self) -> int | None:
        """Index just past the opening quote of the field's value."""

        key = self._raw.find(self._needle)
        if key < 0:
            return None
        index = key + len(self._needle)
        # Tolerate whitespace around the colon; the model formats freely.
        while index < len(self._raw) and self._raw[index].isspace():
            index += 1
        if index >= len(self._raw) or self._raw[index] != ":":
            return None
        index += 1
        while index < len(self._raw) and self._raw[index].isspace():
            index += 1
        if index >= len(self._raw) or self._raw[index] != '"':
            return None
        return index + 1

    def _decode_available(self) -> str:
        out: list[str] = []
        index = self._cursor
        raw = self._raw
        limit = len(raw)
        while index < limit:
            char = raw[index]
            if char == '"':
                self.finished = True
                break
            if char != "\\":
                out.append(char)
                index += 1
                continue
            # An escape needs its payload before it can be decoded; stopping
            # here leaves the backslash in the buffer for the next chunk.
            if index + 1 >= limit:
                break
            marker = raw[index + 1]
            if marker == "u":
                if index + 6 > limit:
                    break
                try:
                    code = int(raw[index + 2 : index + 6], 16)
                except ValueError:
                    out.append(raw[index : index + 6])
                    index += 6
                    continue
                # Anything above the BMP - emoji, most notably - arrives as a
                # UTF-16 surrogate *pair* when the model escapes its output.
                # Decoding each half on its own yields two lone surrogates,
                # which are not valid characters and render as tofu. Found by
                # a property test feeding one character per chunk.
                if 0xD800 <= code <= 0xDBFF:
                    if index + 12 > limit:
                        # Low half not generated yet: wait rather than emit
                        # something that cannot be corrected afterwards.
                        break
                    low_escape = raw[index + 6 : index + 8] == "\\u"
                    try:
                        low = int(raw[index + 8 : index + 12], 16) if low_escape else -1
                    except ValueError:
                        low = -1
                    if 0xDC00 <= low <= 0xDFFF:
                        out.append(chr(0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)))
                        index += 12
                        continue
                    out.append("�")
                    index += 6
                    continue
                if 0xDC00 <= code <= 0xDFFF:
                    # Low half with no high half: unpaired, so unrepresentable.
                    out.append("�")
                    index += 6
                    continue
                out.append(chr(code))
                index += 6
                continue
            out.append(_ESCAPES.get(marker, marker))
            index += 2
        self._cursor = index
        return "".join(out)


class LatentDebateCoordinator:
    """Ask one model context to run the three identities and the vote.

    This is intentionally called *compact* in the implementation: it reuses
    one autoregressive context and one request, but does not claim that a
    normal instruction-tuned model accepts arbitrary hidden vectors as input.
    The model's KV cache carries the shared context internally, while the
    returned JSON keeps the public pipeline contract unchanged.
    """

    def __init__(
        self,
        backend: SharedLLMBackend,
        *,
        max_tokens: int,
        lazy_debate_fields: bool = False,
        on_partial_solution: Callable[[str], None] | None = None,
    ) -> None:
        self.backend = backend
        # Display-only hook: receives the answer as it is generated so the
        # screen is not frozen for the whole call. The parsed object below
        # stays the source of truth for what is finally shown and stored.
        self.on_partial_solution = on_partial_solution
        # ``heuristic``, ``critique`` and the three ``rationale`` fields are
        # 65% of the generated tokens and are never displayed unless the user
        # opens the side panel. Decoding costs 53 ms/token against 14.6 ms
        # for a prompt token, so not generating them is the single largest
        # remaining saving: measured 34.1 s -> 27.0 s (-21%) on an otherwise
        # identical call. They are filled in with placeholders and can be
        # produced on demand later, when the context is still in the KV cache
        # and a follow-up call prefills in "append" mode (~1 s, not ~9.5 s).
        self.lazy_debate_fields = lazy_debate_fields
        # This single call carries the work of six normal ones (heuristic,
        # critique, final answer, three votes), so it needs a materially
        # bigger budget than any one of them - too tight a cap truncates the
        # JSON mid-object on anything longer than a one-liner, which is
        # exactly what broke on questions like "design a deep learning
        # architecture".
        self.max_tokens = max(max_tokens, 900)

    async def run(
        self,
        task: str,
        *,
        kind: TaskKind,
        cycle: int,
        history: str,
        sources: Sequence[SourceMatch] = (),
        research_digest: str = "",
        temperatures: Mapping[AgentRole, float],
    ) -> LatentDebateResult:
        """Generate all contributions in one concise structured completion."""

        if isinstance(self.backend, CLIAgentBackend):
            prompt = self._build_cli_agent_prompt(
                task, kind=kind, cycle=cycle, history=history,
                sources=sources, research_digest=research_digest,
            )
            temperature = sum(temperatures.values()) / len(temperatures)
            raw = await self.backend.complete(
                prompt, temperature=temperature, system_prompt=None,
                max_tokens=self.max_tokens,
            )
            return await self._parse_or_retry(raw, task=task, temperature=temperature)

        # The shared prefix is built by ``prompting.build_prefix`` so that the
        # debate, the context agent and the research agent all present
        # llama.cpp with the same leading tokens and land in its reused KV
        # prefix. Everything role-specific goes in the short tail below; see
        # prompting.py for the 17.5 s vs 61.9 s measurement behind that rule.
        #
        # Small (3B-class) models also drift off-task when the question sits
        # far from where generation starts, so the task is repeated in that
        # tail - it is the last thing read before generation and cheap to
        # re-prefill.
        prefix = build_prefix(
            task=task,
            kind=kind,
            history=history,
            sources=sources,
            research_digest=research_digest,
        )
        prompt = with_role(
            prefix,
            "3LOOP_ACTION=latent_debate\n"
            f"3LOOP_CYCLE={cycle}\n"
            "Run these internal roles in order inside this same context:\n"
            "1. heuristic: propose a concrete solution sketch;\n"
            "2. critic: identify the most important flaw and its correction;\n"
            "3. writer: produce the final answer in the requested format;\n"
            "4. each role votes on the final answer.\n"
            "Be concise. Do not emit chain-of-thought. \"final_solution\" must "
            "hold only the answer itself: no role names, no vote list, no "
            "commentary about this format.\n"
            # Only "final_solution" is shown to the user; the other fields
            # just populate a side panel. Measured on a realistic response,
            # they were 65% of the generated tokens while decode ran at
            # 22.4 tok/s - i.e. most of the generation time was spent on
            # text nobody reads. In lazy mode they are not requested at all,
            # so the instruction capping their length goes with them.
            + ("" if self.lazy_debate_fields else
               "Keep \"heuristic\", \"critique\" and every \"rationale\" to one "
               "short sentence each (15 words maximum). Spend the length budget "
               "on \"final_solution\" instead.\n")
            +
            # The protocol and the formatting rules are in English, which is
            # enough to make the model answer in English regardless of the
            # question's language. Stating it explicitly is what keeps a
            # French question answered in French.
            "Write \"final_solution\" in the same language as the task below.\n"
            f"Answer this exact task, nothing else: {task}\n"
            "Return only one valid JSON object:\n" + _schema(self.lazy_debate_fields),
        )
        temperature = sum(temperatures.values()) / len(temperatures)
        on_token = None
        if self.on_partial_solution is not None:
            streamer = SolutionStreamer()
            report = self.on_partial_solution

            def on_token(fragment: str) -> None:
                readable = streamer.feed(fragment)
                if readable:
                    report(readable)

        raw = await self.backend.complete(
            prompt,
            temperature=temperature,
            system_prompt=(
                "You are the 3loop compact debate engine. Keep all role "
                "communication inside this single model context."
            ),
            max_tokens=self.max_tokens,
            on_token=on_token,
        )
        return await self._parse_or_retry(raw, task=task, temperature=temperature)

    async def _parse_or_retry(
        self,
        raw: str,
        *,
        task: str,
        temperature: float,
    ) -> LatentDebateResult:
        """Parse a compact turn, then retry once with a direct-answer prompt.

        A malformed protocol response must not become a fake answer, nor make
        the whole run fail when the model can still answer normally. The
        retry is deliberately limited to the no-answer case and requests
        ordinary prose, so it cannot reproduce the same JSON-only failure.
        """

        try:
            return parse_latent_debate(raw, task=task)
        except ValueError:
            retry_prompt = (
                "Le précédent essai a produit une réponse structurée vide ou invalide. "
                "Réponds à nouveau à la demande ci-dessous. Fournis uniquement la "
                "réponse finale destinée à l’utilisateur : pas de JSON, pas de rôles, "
                "pas de votes et pas de commentaire sur ce protocole.\n\n"
                f"DEMANDE :\n{task}"
            )
            retry = await self.backend.complete(
                retry_prompt,
                temperature=temperature,
                system_prompt=(
                    "You are the 3loop final-answer engine. Return a useful, "
                    "direct answer to the user's task."
                ),
                max_tokens=self.max_tokens,
            )
            try:
                return _fallback_from_prose(retry, task=task)
            except ValueError as retry_error:
                raise ValueError(
                    "Le modèle n’a fourni aucune réponse exploitable après une relance "
                    "automatique en mode détaillé. Réessaie avec un autre modèle local "
                    "ou désactive le mode Thinking."
                ) from retry_error

    @staticmethod
    def _build_cli_agent_prompt(
        task: str,
        *,
        kind: TaskKind,
        cycle: int,
        history: str,
        sources: Sequence[SourceMatch],
        research_digest: str,
    ) -> str:
        """CLI-agent-shaped variant of the debate prompt built above.

        Used for OpenCode, Claude Code and Codex alike - all three are a
        fresh subprocess per call rather than a persistent local model, so
        none of them benefit from (or need) the KV-prefix-oriented local
        template. Kept as a second, separately maintained template rather
        than a transform of the local one - see ``build_cli_agent_prompt``
        for why. The debate schema itself (roles, conciseness rule, JSON
        shape) is the same information, just framed as plain instructions
        instead of terse protocol markers.
        """

        instruction = (
            "Tu dois mener un debat interne a trois roles (heuristique, "
            "critique, redacteur) puis produire une reponse finale, en JSON "
            "uniquement.\n"
            "1. heuristique: propose une esquisse de solution concrete;\n"
            "2. critique: identifie le defaut principal et sa correction;\n"
            "3. redacteur: produit la reponse finale dans le format demande;\n"
            "4. chaque role vote sur la reponse finale.\n"
            "Sois concis, n'emets aucune reflexion intermediaire. "
            '"final_solution" ne doit contenir que la reponse elle-meme: ni '
            "noms de role, ni liste de votes, ni commentaire sur ce format.\n"
            # Same token-budget reasoning as the local template: only
            # final_solution is ever shown to the user.
            'Garde "heuristic", "critique" et chaque "rationale" a une '
            'phrase courte (15 mots maximum). Consacre le volume a '
            '"final_solution".\n'
            f"Cycle {cycle}. Reponds dans la meme langue que la tache "
            "ci-dessous. Renvoie exactement cet objet JSON, rien d'autre:\n"
            '{"heuristic":"une phrase", "critique":"une phrase",\n'
            '"final_solution":"reponse complete", "votes":[\n'
            '{"role":"heuristic","resolved":true,"confidence":0.0,"rationale":"une phrase"},\n'
            '{"role":"critic","resolved":true,"confidence":0.0,"rationale":"une phrase"},\n'
            '{"role":"writer","resolved":true,"confidence":0.0,"rationale":"une phrase"}]}'
        )
        return build_cli_agent_prompt(
            instruction=instruction,
            task=task,
            kind=kind,
            history=history,
            sources=sources,
            research_digest=research_digest,
        )


def parse_latent_debate(raw: str, *, task: str = "") -> LatentDebateResult:
    """Parse the compact JSON response, tolerating a truncated tail.

    A small model asked to fit heuristic + critique + final answer + three
    votes into one budget will sometimes get cut off by ``max_tokens``
    before the closing braces. Rather than treat that as a hard failure,
    this repairs the JSON when it can, and otherwise falls back to using
    whatever prose the model did produce as the answer - a truncated but
    real response beats an error for the user.
    """

    payload = _extract_json(raw)
    if payload is None:
        return _fallback_from_prose(raw, task=task)
    try:
        # Only ``final_solution`` and the votes are load-bearing: the first is
        # what the user reads, the second decides whether another cycle runs.
        # ``heuristic``/``critique`` merely populate the side panel and are
        # absent by design in lazy mode, so a missing one is not a parse
        # failure - treating it as one would wrongly mark the votes unresolved
        # and burn an extra cycle.
        heuristic = _optional_text(payload, "heuristic")
        critique = _optional_text(payload, "critique")
        final_solution = _strip_protocol_leakage(_required_text(payload, "final_solution"))
        raw_votes = payload.get("votes")
        if not isinstance(raw_votes, list):
            raise ValueError("compact debate response has no votes list")
        by_role: dict[AgentRole, object] = {}
        for item in raw_votes:
            if not isinstance(item, dict):
                continue
            try:
                role = AgentRole(str(item.get("role", "")))
            except ValueError:
                continue
            by_role[role] = item
        if set(by_role) != set(AGENT_ROLES):
            raise ValueError("compact debate response must contain three role votes")
        votes = tuple(
            parse_vote(json.dumps(by_role[role]), role) for role in AGENT_ROLES
        )
    except ValueError:
        return _fallback_from_prose(raw, task=task, payload=payload)
    return LatentDebateResult(heuristic, critique, final_solution, votes)


_FINAL_SOLUTION_KEY = re.compile(r'"final_solution"\s*:\s*"')

#: Keys that can legitimately follow the answer, used to find where an
#: unescaped answer string ends when the model emitted invalid JSON.
_NEXT_KEY = re.compile(r'"\s*,\s*"(?:votes|heuristic|critique)"\s*:')


def extract_final_solution(raw: str) -> str | None:
    """Pull the answer out of a response even when it is not valid JSON.

    Two failure modes make ``json.loads`` useless here, and both destroy an
    otherwise perfect answer if they are not handled:

    * **Raw newlines inside the string.** Models routinely emit
      ``"final_solution": "```python\\nimport torch\\n..."`` with literal
      newlines, which JSON forbids. That breaks *every* answer containing
      code, which is most of them.
    * **Truncation mid-answer.** When generation stops inside the value
      there is no closing quote at all, so any regex requiring one fails and
      the caller falls back to a placeholder - throwing away the part the
      model did produce.

    Returns the raw (unescaped) answer text, or ``None`` if the field is
    absent entirely.
    """

    start = _FINAL_SOLUTION_KEY.search(raw)
    if start is None:
        return None
    body = raw[start.end() :]

    # Prefer ending at the next known key: that survives unescaped quotes
    # and newlines inside the answer itself.
    boundary = _NEXT_KEY.search(body)
    if boundary is not None:
        value = body[: boundary.start()]
    else:
        # Otherwise walk to the closing quote, honouring backslash escapes.
        # Falls through to end-of-string when the response was truncated.
        value, escaped = [], False
        for char in body:
            if escaped:
                value.append(char)
                escaped = False
            elif char == "\\":
                value.append(char)
                escaped = True
            elif char == '"':
                break
            else:
                value.append(char)
        value = "".join(value)
        value = re.sub(r'\s*[,}\]]*\s*$', "", value)

    # Undo JSON escaping without requiring the whole document to parse.
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return (
            value.replace('\\n', '\n').replace('\\t', '\t')
            .replace('\\"', '"').replace('\\\\', '\\')
        )


#: Keys a model reaches for when it does not follow our schema exactly.
_ANSWER_KEYS = (
    "final_solution", "answer", "reponse", "response", "solution",
    "resultat", "result", "texte", "text", "content", "message",
)


def _answer_from_foreign_json(source: object) -> str | None:
    """Recover the answer from JSON that does not match our schema.

    Models occasionally invent their own shape - ``{"answer": ...}``, or a
    nested object. Left alone the caller would display the raw braces to the
    user. Known answer keys are tried first, at any depth; failing that the
    longest string in the structure is used, which in practice is the prose
    answer sitting among short metadata values.
    """

    if isinstance(source, str):
        try:
            source = json.loads(source.strip())
        except (json.JSONDecodeError, AttributeError):
            return None
    if not isinstance(source, (dict, list)):
        return None

    strings: list[str] = []

    def walk(node: object) -> str | None:
        if isinstance(node, dict):
            for key in _ANSWER_KEYS:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in node.values():
                found = walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found:
                    return found
        elif isinstance(node, str) and node.strip():
            strings.append(node.strip())
        return None

    named = walk(source)
    if named:
        return named
    # No recognised key: the answer is almost always the longest string,
    # since the rest are labels, roles and short metadata.
    return max(strings, key=len) if strings else None


def _fallback_from_prose(
    raw: str, *, task: str, payload: dict[str, object] | None = None
) -> LatentDebateResult:
    """Best-effort result when the JSON is missing or unrecoverably broken.

    Prefers a partially-recovered ``final_solution`` field (common: only the
    trailing votes array got cut off) over the raw text, and always marks
    the cycle as unresolved so the pipeline retries rather than silently
    trusting a malformed turn.
    """

    solution = None
    if payload is not None:
        candidate = payload.get("final_solution")
        if isinstance(candidate, str) and candidate.strip():
            solution = candidate
    if solution is None:
        # The structured parse failed outright, but the answer text itself is
        # normally still there - a code block with raw newlines, or a value
        # cut off mid-sentence, both of which defeat json.loads while
        # carrying a perfectly usable answer.
        solution = extract_final_solution(raw)
    if solution is None:
        # The model answered with JSON, but not *our* JSON - a different key
        # name, or a nested shape. Showing the raw object would put a blob of
        # braces and quotes in front of the user where an answer belongs.
        solution = _answer_from_foreign_json(payload if payload is not None else raw)
    if solution is None:
        stripped = re.sub(r'^\s*\{?\s*"?heuristic"?\s*:.*', "", raw, flags=re.DOTALL).strip()
        # If that strip consumed the entire response (raw was itself just
        # the leaked JSON preamble, no trailing prose survives it), there is
        # no answer to show. Never manufacture a fake answer from the task.
        solution = stripped
    solution = _strip_protocol_leakage(solution or "")
    if not solution:
        raise ValueError(
            "La réponse compacte ne contient aucune solution exploitable ; "
            "une relance en mode détaillé est nécessaire."
        )

    low_confidence_vote = (
        '{{"resolved": false, "confidence": 0.3, '
        '"rationale": "Reponse compacte tronquee ou mal formee, a revoir."}}'
    )
    votes = tuple(parse_vote(low_confidence_vote, role) for role in AGENT_ROLES)
    return LatentDebateResult(
        heuristic="(non disponible: reponse compacte tronquee)",
        critique="(non disponible: reponse compacte tronquee)",
        final_solution=solution,
        votes=votes,
    )


def _escape_control_chars_in_strings(text: str) -> str:
    """Escape raw newlines/tabs that appear inside JSON string values.

    Models emit fenced code blocks with literal newlines inside the string
    (``"final_solution": "```python\\nimport torch"``), which JSON forbids.
    Left alone this rejects essentially every answer containing code, so the
    control characters are escaped in place rather than the answer discarded.
    """

    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif char == "\n":
                out.append("\\n")
                continue
            elif char == "\r":
                out.append("\\r")
                continue
            elif char == "\t":
                out.append("\\t")
                continue
        elif char == '"':
            in_string = True
        out.append(char)
    return "".join(out)


def _extract_json(raw: str) -> dict[str, object] | None:
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    candidate = match.group(0) if match else raw
    for attempt in (candidate, _escape_control_chars_in_strings(candidate)):
        try:
            payload = json.loads(attempt)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            continue
    return _repair_truncated_json(_escape_control_chars_in_strings(candidate))


def _close_open_structures(text: str) -> str:
    """Append closing brackets/braces in the correct (LIFO) nesting order.

    Scans outside of string literals so a ``{`` or ``[`` typed inside a
    quoted value never gets mistaken for real JSON structure.
    """

    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    closing = "".join(reversed(stack))
    return (text + '"' + closing) if in_string else (text + closing)


def _repair_truncated_json(candidate: str) -> dict[str, object] | None:
    """Back off to the last complete field and close what was left open.

    A ``max_tokens`` cutoff can land anywhere - mid-string, mid-key, between
    fields. Each attempt closes every open ``{``/``[`` in the right order;
    if that still doesn't parse, the text is trimmed back to its last
    top-level comma and retried, until something valid comes out or there
    is nothing left to trim.
    """

    text = candidate
    for _ in range(50):
        try:
            payload = json.loads(_close_open_structures(text))
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass
        last_comma = text.rfind(",")
        if last_comma == -1:
            return None
        text = text[:last_comma]
    return None


#: Small models sometimes echo the protocol's own vocabulary into the answer
#: (e.g. a trailing ``Votes: ["heuristic", ...]`` line). These are artefacts of
#: the compact single-call format, never part of the user-facing solution.
_LEAKED_PROTOCOL_LINE = re.compile(
    r"^\s*(votes?|heuristic|critic|writer|rationale|resolved|confidence)\s*[:=].*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _strip_protocol_leakage(solution: str) -> str:
    """Drop trailing protocol echoes so the user only sees the answer."""

    cleaned = _LEAKED_PROTOCOL_LINE.sub("", solution)
    return cleaned.strip() or solution.strip()


#: Shown in the side panel when a debate field was not generated. Not an
#: error state: in lazy mode these are deliberately omitted to save decode.
NOT_GENERATED = "(non genere: champ de panneau, disponible a la demande)"


def _optional_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return NOT_GENERATED


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"compact debate response is missing {key}")
    return value.strip()


def _render_sources(sources: Sequence[SourceMatch]) -> str:
    if not sources:
        return "(aucune)"
    return "\n".join(f"- {source.url}" for source in sources)
