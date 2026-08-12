"""Turn a pile of search results into a cited answer, and name what is missing.

The federated search already finds papers across eight providers, but it
stops at a list of titles: the user still has to read everything to learn
whether the literature answers their question. This module is the step
after - it reads the abstracts that were actually retrieved and produces a
synthesis whose every claim points back at a numbered source.

The part that matters for research is ``gaps``. A synthesis that only
summarises what exists tells a researcher nothing about where to work;
naming what the retrieved literature does *not* establish is the signal that
turns a search into a starting point. It is deliberately framed as "not
covered by these N sources" rather than "novel": a bounded federated search
over abstracts cannot support a claim about the whole field, and saying
otherwise would be the kind of overreach that makes an automated literature
review untrustworthy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: Abstracts are the expensive part of the prompt and the long tail adds
#: little: past roughly this many characters a provider abstract is usually
#: boilerplate (funding, licensing, author lists) rather than findings.
_ABSTRACT_BUDGET = 700

#: More sources than this and a small local model starts losing track of
#: which claim came from which number, which defeats the point of citing.
_MAX_SOURCES = 12


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _as_prose(value: object) -> str:
    """Flatten a synthesis field that may arrive as a list of paragraphs.

    Models answer the "synthese" slot with a list about as often as with a
    string. Passing that straight through ``str()`` printed a Python list
    repr - quotes, brackets and all - into the user's review, which was
    observed on the first real run against retrieved abstracts.
    """

    if isinstance(value, list):
        parts = [_clean(item) for item in value]
        return "\n\n".join(part for part in parts if part)
    return _clean(value)


def format_sources(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Number the records once so prompt, answer and UI share one indexing.

    Citations are only meaningful if ``[3]`` means the same paper everywhere.
    Returning the numbered list (rather than numbering inside the prompt
    builder) is what lets the caller render the same numbers next to the
    links the user clicks.
    """

    sources: list[dict[str, Any]] = []
    for index, record in enumerate(records[:_MAX_SOURCES], start=1):
        title = _clean(record.get("title"))
        if not title:
            continue
        authors = [a for a in (record.get("authors") or []) if _clean(a)][:3]
        sources.append(
            {
                "n": index,
                "title": title,
                "abstract": _clean(record.get("abstract"))[:_ABSTRACT_BUDGET],
                "year": record.get("year"),
                "authors": authors,
                "venue": _clean(record.get("venue")),
                "url": _clean(record.get("url")),
                "doi": _clean(record.get("doi")),
                "provider": _clean(record.get("provider")),
            }
        )
    # Renumber after dropping untitled records so the numbering stays dense:
    # a gap in the list would look like a source the model failed to cite.
    for position, source in enumerate(sources, start=1):
        source["n"] = position
    return sources


def build_synthesis_prompt(
    question: str,
    sources: Sequence[Mapping[str, Any]],
    *,
    library_excerpts: str = "",
) -> str:
    """Build the literature-review prompt from already-numbered sources."""

    blocks = []
    for source in sources:
        header = f"[{source['n']}] {source['title']}"
        meta = ", ".join(
            part
            for part in (
                ", ".join(source.get("authors") or []),
                str(source.get("year") or ""),
                source.get("venue") or "",
            )
            if part
        )
        body = source.get("abstract") or "(resume indisponible chez ce fournisseur)"
        blocks.append(f"{header}\n{meta}\n{body}" if meta else f"{header}\n{body}")

    library_section = ""
    if library_excerpts.strip():
        library_section = (
            "\n\nEXTRAITS DE LA BIBLIOTHEQUE LOCALE DE L'UTILISATEUR "
            "(documents qu'il a lui-meme importes ; cite-les [L]) :\n"
            f"{library_excerpts.strip()}\n"
        )

    return (
        "Tu realises une revue de litterature. Tu disposes uniquement des "
        "resumes ci-dessous : ne t'appuie sur aucune connaissance exterieure, "
        "et n'invente aucune reference.\n\n"
        f"QUESTION DE RECHERCHE :\n{question}\n\n"
        f"SOURCES RECUPEREES ({len(sources)}) :\n" + "\n\n".join(blocks)
        + library_section
        + "\n\nProduis exactement cet objet JSON, rien d'autre :\n"
        '{"synthese":"UNE SEULE CHAINE de texte, pas une liste",\n'
        ' "consensus":["point sur lequel plusieurs sources concordent, avec [n]"],\n'
        ' "desaccords":["contradiction ou divergence entre sources, avec [n]"],\n'
        ' "lacunes":["ce que ces sources ne permettent PAS de conclure sur la question"],\n'
        ' "sources_cles":[1,2]}\n\n'
        # A small local model follows a worked example far more reliably than
        # a rule: the first run against real abstracts produced zero
        # citations from an abstract instruction alone, and returned
        # "synthese" as a list despite the field being described as prose.
        'Exemple du style attendu pour "synthese" (adapte-le au contenu reel) :\n'
        '"Les methodes A et B reduisent la memoire de moitie [1][3], mais au '
        'prix d\'une perte de qualite mesuree seulement sur un banc d\'essai [2]."\n\n'
        "Regles imperatives :\n"
        "- \"synthese\" est UNE chaine de caracteres, jamais une liste.\n"
        "- CHAQUE phrase de \"synthese\" se termine par au moins une citation "
        "[n] renvoyant a une source ci-dessus. Une phrase sans citation est "
        "une erreur.\n"
        f"- N'utilise que les numeros 1 a {len(sources)}. N'invente aucun autre numero.\n"
        "- Si les sources ne repondent pas a la question, ecris-le dans "
        "\"lacunes\" plutot que de combler par des generalites.\n"
        "- Laisse une liste vide si tu n'as rien de solide a y mettre.\n"
        "- Ecris en francais."
    )


def _first_json_object(raw: str) -> dict[str, Any] | None:
    """Extract the first balanced JSON object, ignoring prose or fences."""

    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(raw[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    return value if isinstance(value, dict) else None
        start = raw.find("{", start + 1)
    return None


def _unescape(value: str) -> str:
    """Undo JSON string escapes on a fragment too broken for json.loads."""

    return (
        value.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\/", "/")
        .replace("\\\\", "\\")
    )


def _string_list(value: object, *, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        cleaned = _clean(value)
        return [cleaned] if cleaned else []
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        cleaned = _clean(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
        if cleaned:
            out.append(cleaned)
    return out[:limit]


_CITATION = re.compile(r"\[(\d{1,2})\]")


def cited_numbers(text: str, *, valid: int) -> list[int]:
    """Citation numbers actually used, dropping any the search never returned.

    A model that invents ``[14]`` when nine sources were retrieved would
    otherwise render as a dead link. Silently dropping it is right here: the
    surrounding claim is still supported by whatever valid citations remain,
    and the caller reports the count so an answer citing nothing is visible.
    """

    seen: list[int] = []
    for match in _CITATION.finditer(text or ""):
        number = int(match.group(1))
        if 1 <= number <= valid and number not in seen:
            seen.append(number)
    return seen


def parse_synthesis(raw: str, *, source_count: int) -> dict[str, Any]:
    """Parse the model's review, keeping only citations that can resolve.

    Falls back to treating the whole reply as prose rather than failing: a
    model that answered usefully but ignored the JSON shape should still
    reach the user, since the synthesis text is the part they read.
    """

    payload = _first_json_object(raw) or {}
    synthesis = _as_prose(payload.get("synthese") or payload.get("synthesis") or "")
    if not synthesis:
        # No parseable object. Most often that means the reply was cut off
        # mid-JSON (token budget), which still contains a usable answer -
        # but handing the raw text over would print `{"synthese":"...` on
        # screen. Recover the field's value directly, then fall back to the
        # plain reply for a model that ignored the format entirely.
        stripped = re.sub(r"```[a-z]*|```", "", raw or "").strip()
        truncated = re.search(
            r'"(?:synthese|synthesis)"\s*:\s*"(.*?)(?:"\s*[,}]|$)', stripped, re.S
        )
        if truncated:
            synthesis = _clean(_unescape(truncated.group(1)))
        elif not stripped.lstrip().startswith("{"):
            synthesis = _clean(stripped)

    key_sources = []
    for value in payload.get("sources_cles") or payload.get("key_sources") or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= source_count and number not in key_sources:
            key_sources.append(number)

    consensus = _string_list(payload.get("consensus"))
    disagreements = _string_list(payload.get("desaccords") or payload.get("disagreements"))
    gaps = _string_list(payload.get("lacunes") or payload.get("gaps"))

    # Counted over the whole review, not just the summary paragraph. Smaller
    # models reliably cite inside the bullet lists while leaving the prose
    # uncited - measured on Qwen3 1.7B against real abstracts - and scoring
    # only the prose fired the "cites nothing" warning on reviews whose
    # every gap named its sources. The user reads all of it, so all of it
    # counts.
    everything = "\n".join([synthesis, *consensus, *disagreements, *gaps])

    return {
        "synthesis": synthesis,
        "consensus": consensus,
        "disagreements": disagreements,
        "gaps": gaps,
        "key_sources": key_sources,
        "citations": cited_numbers(everything, valid=source_count),
        "synthesis_citations": cited_numbers(synthesis, valid=source_count),
        "source_count": source_count,
    }


def synthesis_as_note(question: str, result: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]) -> str:
    """Render the review as markdown for the research notebook.

    The numbered reference list is rebuilt from the sources rather than from
    the model's output, so the citations in the text always resolve to a real
    link even if the model referred to a source it did not discuss.
    """

    lines = [f"**Question :** {question}", "", result.get("synthesis") or ""]

    for title, key in (
        ("Consensus", "consensus"),
        ("Désaccords", "disagreements"),
        ("Lacunes (non couvert par ces sources)", "gaps"),
    ):
        items = result.get(key) or []
        if items:
            lines += ["", f"**{title}**"] + [f"- {item}" for item in items]

    if sources:
        lines += ["", "**Sources**"]
        for source in sources:
            reference = f"[{source['n']}] {source['title']}"
            if source.get("year"):
                reference += f" ({source['year']})"
            if source.get("url"):
                reference += f" — {source['url']}"
            lines.append(reference)

    return "\n".join(lines).strip()
