"""Dependency-free bibliography interchange for BibTeX, RIS and CSL-JSON."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class BibliographicEntry:
    """A normalized reference independent from an interchange format."""

    cite_key: str
    entry_type: str = "article"
    title: str = ""
    authors: tuple[str, ...] = ()
    year: int | None = None
    journal: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    publisher: str = ""
    keywords: tuple[str, ...] = ()
    note: str = ""
    external_ids: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cite_key": self.cite_key,
            "entry_type": self.entry_type,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "journal": self.journal,
            "abstract": self.abstract,
            "doi": self.doi,
            "url": self.url,
            "publisher": self.publisher,
            "keywords": list(self.keywords),
            "note": self.note,
            "external_ids": dict(self.external_ids),
        }


def parse_bibliography(content: str, format_name: str) -> list[BibliographicEntry]:
    """Parse one of the supported formats and reject unknown formats."""

    normalized = format_name.strip().lower().lstrip(".")
    if normalized in {"bib", "bibtex"}:
        return parse_bibtex(content)
    if normalized == "ris":
        return parse_ris(content)
    if normalized in {"csl", "csl-json", "csl_json", "json"}:
        return parse_csl_json(content)
    raise ValueError("Format bibliographique non supporté (BibTeX, RIS ou CSL-JSON).")


def parse_bibtex(content: str) -> list[BibliographicEntry]:
    entries: list[BibliographicEntry] = []
    for entry_type, key, body in _bib_entries(content):
        fields = _bib_fields(body)
        authors = tuple(_split_authors(fields.get("author", "")))
        entries.append(
            BibliographicEntry(
                cite_key=key or _make_key(fields.get("title", "reference"), fields.get("year")),
                entry_type=entry_type.lower(),
                title=_clean_value(fields.get("title", "")),
                authors=authors,
                year=_year(fields.get("year")),
                journal=_clean_value(fields.get("journal", fields.get("booktitle", ""))),
                abstract=_clean_value(fields.get("abstract", "")),
                doi=_normalize_doi(fields.get("doi", "")),
                url=_clean_value(fields.get("url", "")),
                publisher=_clean_value(fields.get("publisher", "")),
                keywords=tuple(
                    part.strip() for part in _clean_value(fields.get("keywords", "")).split(",") if part.strip()
                ),
                note=_clean_value(fields.get("note", "")),
                external_ids={
                    name: _clean_value(fields[name])
                    for name in ("pmid", "eprint", "arxivid")
                    if fields.get(name)
                },
            )
        )
    return [entry for entry in entries if entry.title or entry.doi]


def parse_ris(content: str) -> list[BibliographicEntry]:
    entries: list[BibliographicEntry] = []
    current: dict[str, list[str]] = {}
    entry_index = 0

    def flush() -> None:
        nonlocal entry_index
        if not current:
            return
        entry_index += 1
        title = _clean_value(" ".join(current.get("TI", current.get("T1", []))))
        authors = tuple(_clean_value(value) for value in current.get("AU", []) if _clean_value(value))
        year = _year(" ".join(current.get("PY", current.get("Y1", []))))
        doi = _normalize_doi(" ".join(current.get("DO", [])))
        external_ids = {}
        if current.get("AN"):
            external_ids["accession"] = _clean_value(" ".join(current["AN"]))
        entries.append(
            BibliographicEntry(
                cite_key=_make_key(title or f"reference{entry_index}", str(year or "")),
                entry_type=_ris_type(" ".join(current.get("TY", ["JOUR"]))),
                title=title,
                authors=authors,
                year=year,
                journal=_clean_value(" ".join(current.get("JO", current.get("JF", [])))),
                abstract=_clean_value(" ".join(current.get("AB", []))),
                doi=doi,
                url=_clean_value(" ".join(current.get("UR", []))),
                publisher=_clean_value(" ".join(current.get("PB", []))),
                keywords=tuple(_clean_value(value) for value in current.get("KW", [])),
                note=_clean_value(" ".join(current.get("N1", []))),
                external_ids=external_ids,
            )
        )
        current.clear()

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r")
        if len(line) < 5 or line[2:5] != "  -":
            continue
        tag = line[:2].upper()
        value = line[6:].strip()
        if tag == "ER":
            flush()
        elif tag == "TY" and current:
            flush()
        else:
            current.setdefault(tag, []).append(value)
    flush()
    return [entry for entry in entries if entry.title or entry.doi]


def parse_csl_json(content: str) -> list[BibliographicEntry]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("CSL-JSON invalide.") from exc
    items = payload if isinstance(payload, list) else [payload]
    entries: list[BibliographicEntry] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            continue
        authors = tuple(_csl_name(author) for author in item.get("author", []) if _csl_name(author))
        issued = item.get("issued", {})
        date_parts = issued.get("date-parts", []) if isinstance(issued, Mapping) else []
        year = _year(date_parts[0][0] if date_parts and date_parts[0] else None)
        title = _clean_value(str(item.get("title", "")))
        key = _clean_value(str(item.get("id", ""))) or _make_key(title or f"reference{index}", str(year or ""))
        entries.append(
            BibliographicEntry(
                cite_key=key,
                entry_type=_csl_type(str(item.get("type", "article"))),
                title=title,
                authors=authors,
                year=year,
                journal=_clean_value(str(item.get("container-title", ""))),
                abstract=_clean_value(str(item.get("abstract", ""))),
                doi=_normalize_doi(str(item.get("DOI", ""))),
                url=_clean_value(str(item.get("URL", ""))),
                publisher=_clean_value(str(item.get("publisher", ""))),
                keywords=tuple(
                    _clean_value(str(value)) for value in item.get("keyword", "").split(",")
                    if _clean_value(str(value))
                ) if isinstance(item.get("keyword", ""), str) else tuple(),
                note=_clean_value(str(item.get("note", ""))),
                external_ids={
                    **({
                        str(name): str(value)
                        for name, value in (item.get("external_ids", {}) or {}).items()
                        if value
                    } if isinstance(item.get("external_ids", {}), Mapping) else {}),
                    **{
                        str(name): str(value)
                        for name, value in item.items()
                        if name in {" PMID", "PMID", "arXiv"} and value
                    },
                },
            )
        )
    return [entry for entry in entries if entry.title or entry.doi]


def export_bibliography(entries: Iterable[BibliographicEntry], format_name: str) -> str:
    normalized = format_name.strip().lower().lstrip(".")
    values = list(entries)
    if normalized in {"bib", "bibtex"}:
        return export_bibtex(values)
    if normalized == "ris":
        return export_ris(values)
    if normalized in {"csl", "csl-json", "csl_json", "json"}:
        return json.dumps([_entry_csl(entry) for entry in values], ensure_ascii=False, indent=2) + "\n"
    raise ValueError("Format bibliographique non supporté (BibTeX, RIS ou CSL-JSON).")


def export_bibtex(entries: Iterable[BibliographicEntry]) -> str:
    blocks: list[str] = []
    for entry in entries:
        fields: list[tuple[str, str]] = []
        for name, value in (
            ("title", entry.title),
            ("author", " and ".join(entry.authors)),
            ("year", str(entry.year or "")),
            ("journal", entry.journal),
            ("abstract", entry.abstract),
            ("doi", entry.doi),
            ("url", entry.url),
            ("publisher", entry.publisher),
            ("keywords", ", ".join(entry.keywords)),
            ("note", entry.note),
        ):
            if value:
                fields.append((name, value))
        for name, value in entry.external_ids.items():
            if value and name not in {field_name for field_name, _ in fields}:
                fields.append((name.lower(), value))
        body = ",\n".join(f"  {name} = {{{_bib_escape(value)}}}" for name, value in fields)
        blocks.append(f"@{entry.entry_type}{{{_safe_key(entry.cite_key)},\n{body}\n}}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_ris(entries: Iterable[BibliographicEntry]) -> str:
    blocks: list[str] = []
    for entry in entries:
        lines = [f"TY  - {_ris_entry_type(entry.entry_type)}"]
        lines.extend(f"AU  - {author}" for author in entry.authors)
        if entry.title:
            lines.append(f"TI  - {entry.title}")
        if entry.journal:
            lines.append(f"JO  - {entry.journal}")
        if entry.year:
            lines.append(f"PY  - {entry.year}")
        if entry.abstract:
            lines.append(f"AB  - {entry.abstract}")
        if entry.doi:
            lines.append(f"DO  - {entry.doi}")
        if entry.url:
            lines.append(f"UR  - {entry.url}")
        if entry.publisher:
            lines.append(f"PB  - {entry.publisher}")
        lines.extend(f"KW  - {keyword}" for keyword in entry.keywords)
        if entry.note:
            lines.append(f"N1  - {entry.note}")
        lines.append("ER  -")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _entry_csl(entry: BibliographicEntry) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": entry.cite_key,
        "type": entry.entry_type,
        "title": entry.title,
        "author": [{"literal": value} for value in entry.authors],
    }
    if entry.year:
        item["issued"] = {"date-parts": [[entry.year]]}
    for key, value in (
        ("container-title", entry.journal),
        ("abstract", entry.abstract),
        ("DOI", entry.doi),
        ("URL", entry.url),
        ("publisher", entry.publisher),
        ("note", entry.note),
    ):
        if value:
            item[key] = value
    if entry.keywords:
        item["keyword"] = ", ".join(entry.keywords)
    item.update(entry.external_ids)
    return item


def _bib_entries(content: str) -> Iterable[tuple[str, str, str]]:
    index = 0
    while True:
        match = re.search(r"@([A-Za-z]+)\s*\{", content[index:])
        if not match:
            return
        start = index + match.start()
        body_start = index + match.end()
        depth = 1
        cursor = body_start
        quoted = False
        escaped = False
        while cursor < len(content) and depth:
            char = content[cursor]
            if char == '"' and not escaped:
                quoted = not quoted
            elif not quoted:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
            cursor += 1
        if depth:
            return
        body = content[body_start:cursor - 1]
        comma = body.find(",")
        if comma >= 0:
            yield match.group(1), body[:comma].strip(), body[comma + 1:]
        index = cursor


def _bib_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    index = 0
    while index < len(body):
        while index < len(body) and body[index] in " \t\r\n,":
            index += 1
        match = re.match(r"([A-Za-z][\w-]*)\s*=\s*", body[index:])
        if not match:
            break
        name = match.group(1).lower()
        index += match.end()
        value, index = _bib_value(body, index)
        fields[name] = value
    return fields


def _bib_value(body: str, index: int) -> tuple[str, int]:
    if index >= len(body):
        return "", index
    if body[index] == "{":
        depth = 1
        cursor = index + 1
        while cursor < len(body) and depth:
            if body[cursor] == "{":
                depth += 1
            elif body[cursor] == "}":
                depth -= 1
            cursor += 1
        return body[index + 1:cursor - 1], cursor
    if body[index] == '"':
        cursor = index + 1
        escaped = False
        while cursor < len(body):
            if body[cursor] == '"' and not escaped:
                return body[index + 1:cursor], cursor + 1
            escaped = body[cursor] == "\\" and not escaped
            if body[cursor] != "\\":
                escaped = False
            cursor += 1
        return body[index + 1:], len(body)
    cursor = body.find(",", index)
    return (body[index:] if cursor < 0 else body[index:cursor]).strip(), len(body) if cursor < 0 else cursor


def _split_authors(value: str) -> list[str]:
    return [_clean_value(part) for part in re.split(r"\s+and\s+", value, flags=re.I) if _clean_value(part)]


def _csl_name(value: Any) -> str:
    if not isinstance(value, Mapping):
        return _clean_value(str(value))
    if value.get("literal"):
        return _clean_value(str(value["literal"]))
    return _clean_value(" ".join(str(value.get(key, "")) for key in ("given", "family") if value.get(key)))


def _clean_value(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\\([{}$%_&#])", r"\1", text)
    return " ".join(text.replace("\n", " ").split()).strip("{} ")


def _normalize_doi(value: Any) -> str:
    text = _clean_value(value).lower()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text).rstrip(".,;)")


def _year(value: Any) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _make_key(title: str, year: Any) -> str:
    words = re.findall(r"[A-Za-z0-9]+", title.lower())[:4]
    return _safe_key("".join(words) or "reference") + str(_year(year) or "")


def _safe_key(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9:_-]+", "", value)
    return value or "reference"


def _bib_escape(value: str) -> str:
    return str(value).replace("{", "\\{").replace("}", "\\}")


def _ris_type(value: str) -> str:
    return {"JOUR": "article", "CPAPER": "inproceedings", "CONF": "inproceedings", "BOOK": "book", "THES": "thesis"}.get(value.upper(), "article")


def _ris_entry_type(value: str) -> str:
    return {"article": "JOUR", "inproceedings": "CPAPER", "book": "BOOK", "thesis": "THES"}.get(value.lower(), "GEN")


def _csl_type(value: str) -> str:
    return {"article-journal": "article", "paper-conference": "inproceedings", "chapter": "inbook"}.get(value, value or "article")
