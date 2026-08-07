"""Scientific search connectors and an explainable federated search service.

The connectors deliberately use only fixed HTTPS API origins and the standard
library.  They return a common record shape so the UI and the local library do
not depend on provider-specific payloads.  Network access happens only when a
caller invokes ``search``; importing this module is offline-safe.
"""

from __future__ import annotations

import asyncio
import difflib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ScientificRecord:
    """Provider-neutral bibliographic or ML artifact record."""

    provider: str
    external_id: str
    title: str
    abstract: str = ""
    year: int | None = None
    authors: tuple[str, ...] = ()
    venue: str = ""
    doi: str = ""
    url: str = ""
    record_type: str = "paper"
    dataset: str = ""
    code_url: str = ""
    license: str = ""
    metrics: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize fields once so serialized records remain predictable."""

        object.__setattr__(self, "title", _clean(self.title))
        object.__setattr__(self, "abstract", _clean(self.abstract))
        object.__setattr__(self, "doi", _normalize_doi(self.doi))
        object.__setattr__(self, "url", _clean(self.url))
        object.__setattr__(self, "external_id", _clean(self.external_id))
        object.__setattr__(self, "authors", tuple(_clean(value) for value in self.authors if _clean(value)))
        object.__setattr__(self, "providers", tuple(dict.fromkeys(self.providers or (self.provider,))))
        if self.year is not None:
            try:
                object.__setattr__(self, "year", int(self.year))
            except (TypeError, ValueError):
                object.__setattr__(self, "year", None)

    @property
    def identity(self) -> str:
        """Return the strongest stable identity available for deduplication."""

        if self.doi:
            return f"doi:{self.doi}"
        return f"{self.provider}:{self.external_id}".lower()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation for API responses and provenance."""

        return {
            "provider": self.provider,
            "providers": list(self.providers),
            "external_id": self.external_id,
            "title": self.title,
            "abstract": self.abstract,
            "year": self.year,
            "authors": list(self.authors),
            "venue": self.venue,
            "doi": self.doi,
            "url": self.url,
            "record_type": self.record_type,
            "dataset": self.dataset,
            "code_url": self.code_url,
            "license": self.license,
            "metrics": list(self.metrics),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class SearchRequest:
    """A bounded federated search request."""

    question: str
    profile: str = "machine-learning"
    max_results: int = 10
    providers: tuple[str, ...] = ()
    timeout: float = 12.0

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("La question de recherche ne peut pas être vide.")
        object.__setattr__(self, "max_results", max(1, min(50, int(self.max_results))))
        object.__setattr__(self, "timeout", max(2.0, min(45.0, float(self.timeout))))


@dataclass(frozen=True)
class FederatedSearchResult:
    """Results, provider failures and the exact query plan used."""

    question: str
    profile: str
    queries: Mapping[str, str]
    results: tuple[ScientificRecord, ...]
    results_by_provider: Mapping[str, tuple[ScientificRecord, ...]]
    errors: Mapping[str, str]
    completed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "profile": self.profile,
            "queries": dict(self.queries),
            "results": [record.as_dict() for record in self.results],
            "results_by_provider": {
                name: [record.as_dict() for record in records]
                for name, records in self.results_by_provider.items()
            },
            "errors": dict(self.errors),
            "completed_at": self.completed_at,
            "counts": {
                "results": len(self.results),
                "providers": len(self.results_by_provider),
                "errors": len(self.errors),
            },
        }


class ScientificConnector(Protocol):
    """Minimal connector contract used by the federation service."""

    name: str
    label: str
    capabilities: tuple[str, ...]

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        """Search one fixed provider and return normalized records."""


class ConnectorError(RuntimeError):
    """A provider failed without making the whole federation fail."""


class _HttpConnector:
    """Common bounded HTTP implementation for fixed provider origins."""

    name = "http"
    label = "Scientific API"
    capabilities: tuple[str, ...] = ("papers",)
    allowed_hosts: frozenset[str] = frozenset()
    user_agent = "3loop-scientific-research/1.0"

    def _read(self, url: str, *, timeout: float, accept: str = "application/json") -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in self.allowed_hosts:
            raise ConnectorError("Destination de connecteur non autorisée.")
        request = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": self.user_agent},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(4_000_000)
                if len(body) >= 4_000_000:
                    raise ConnectorError("Réponse fournisseur trop volumineuse.")
                return body
        except urllib.error.HTTPError as exc:
            raise ConnectorError(f"HTTP {exc.code} renvoyé par {self.name}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectorError(f"Fournisseur {self.name} indisponible: {exc}") from exc

    def _json(self, url: str, *, timeout: float) -> Any:
        try:
            return json.loads(self._read(url, timeout=timeout).decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"Réponse JSON invalide de {self.name}.") from exc


class CrossrefConnector(_HttpConnector):
    name = "crossref"
    label = "Crossref"
    capabilities = ("papers", "doi", "venues", "metadata")
    allowed_hosts = frozenset({"api.crossref.org"})

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        params = urllib.parse.urlencode({"query.bibliographic": query, "rows": max_results})
        payload = self._json(f"https://api.crossref.org/works?{params}", timeout=timeout)
        items = payload.get("message", {}).get("items", [])
        records: list[ScientificRecord] = []
        for item in items[:max_results]:
            title = _first(item.get("title"))
            if not title:
                continue
            doi = _normalize_doi(str(item.get("DOI", "")))
            issued = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
            year = _year_from_parts(issued.get("date-parts"))
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=doi or str(item.get("URL", "")),
                    title=title,
                    abstract=_strip_markup(str(item.get("abstract", ""))),
                    year=year,
                    authors=tuple(_author_name(author) for author in item.get("author", [])),
                    venue=_first(item.get("container-title")),
                    doi=doi,
                    url=str(item.get("URL", "")) or (f"https://doi.org/{doi}" if doi else ""),
                    record_type=str(item.get("type", "paper")),
                    raw={"publisher": item.get("publisher", ""), "license": item.get("license", [])},
                )
            )
        return records


class OpenAlexConnector(_HttpConnector):
    name = "openalex"
    label = "OpenAlex"
    capabilities = ("papers", "authors", "citations", "open-access", "metadata")
    allowed_hosts = frozenset({"api.openalex.org"})

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        params = urllib.parse.urlencode({"search": query, "per-page": max_results})
        payload = self._json(f"https://api.openalex.org/works?{params}", timeout=timeout)
        records: list[ScientificRecord] = []
        for item in payload.get("results", [])[:max_results]:
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            doi = _normalize_doi(str(item.get("doi", "")))
            locations = item.get("locations") or []
            best = next((location for location in locations if location.get("is_oa")), locations[0] if locations else {})
            url = str(best.get("landing_page_url") or best.get("pdf_url") or item.get("id", ""))
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=str(item.get("id", "")),
                    title=title,
                    abstract=_openalex_abstract(item.get("abstract_inverted_index")),
                    year=item.get("publication_year"),
                    authors=tuple(
                        str(author.get("author", {}).get("display_name", ""))
                        for author in item.get("authorships", [])
                        if author.get("author", {}).get("display_name")
                    ),
                    venue=str((item.get("primary_location") or {}).get("source", {}).get("display_name", "")),
                    doi=doi,
                    url=url,
                    record_type=str(item.get("type", "paper")),
                    raw={"cited_by_count": item.get("cited_by_count", 0), "open_access": item.get("open_access", {})},
                )
            )
        return records


class ArxivConnector(_HttpConnector):
    name = "arxiv"
    label = "arXiv"
    capabilities = ("papers", "preprints", "ml", "metadata")
    allowed_hosts = frozenset({"export.arxiv.org"})
    _atom = "{http://www.w3.org/2005/Atom}"

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        params = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0, "max_results": max_results})
        body = self._read(f"https://export.arxiv.org/api/query?{params}", timeout=timeout, accept="application/atom+xml")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ConnectorError("Réponse Atom arXiv invalide.") from exc
        records: list[ScientificRecord] = []
        for entry in root.findall(f"{self._atom}entry")[:max_results]:
            identifier = _xml_text(entry, f"{self._atom}id")
            title = _xml_text(entry, f"{self._atom}title")
            if not title:
                continue
            published = _xml_text(entry, f"{self._atom}published")
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=identifier.rsplit("/", 1)[-1],
                    title=title,
                    abstract=_xml_text(entry, f"{self._atom}summary"),
                    year=_year_from_iso(published),
                    authors=tuple(
                        _xml_text(author, f"{self._atom}name")
                        for author in entry.findall(f"{self._atom}author")
                    ),
                    venue="arXiv",
                    url=identifier,
                    record_type="preprint",
                    tags=tuple(
                        category.attrib.get("term", "")
                        for category in entry.findall(f"{self._atom}category")
                        if category.attrib.get("term")
                    ),
                )
            )
        return records


class PubMedConnector(_HttpConnector):
    name = "pubmed"
    label = "PubMed"
    capabilities = ("papers", "biomedicine", "metadata")
    allowed_hosts = frozenset({"eutils.ncbi.nlm.nih.gov"})

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        params = urllib.parse.urlencode({"db": "pubmed", "term": query, "retmode": "json", "retmax": max_results})
        search = self._json(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}", timeout=timeout)
        ids = search.get("esearchresult", {}).get("idlist", [])[:max_results]
        if not ids:
            return ()
        fetch_params = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
        body = self._read(
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?{fetch_params}",
            timeout=timeout,
            accept="application/xml",
        )
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ConnectorError("Réponse XML PubMed invalide.") from exc
        records: list[ScientificRecord] = []
        for article in root.findall(".//PubmedArticle")[:max_results]:
            pmid = _xml_text(article, ".//PMID")
            title = _xml_text(article, ".//ArticleTitle")
            if not title:
                continue
            doi = ""
            for identifier in article.findall(".//ArticleId"):
                if identifier.attrib.get("IdType", "").lower() == "doi":
                    doi = identifier.text or ""
                    break
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=pmid,
                    title=title,
                    abstract=" ".join(
                        _clean("".join(node.itertext()))
                        for node in article.findall(".//AbstractText")
                    ),
                    year=_year_from_iso(_xml_text(article, ".//PubDate")),
                    authors=tuple(
                        " ".join(
                            value for value in (
                                author.findtext("ForeName"), author.findtext("LastName")
                            ) if value
                        )
                        for author in article.findall(".//Author")
                    ),
                    venue=_xml_text(article, ".//Journal/Title"),
                    doi=doi,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                    raw={"pmid": pmid},
                )
            )
        return records


class SemanticScholarConnector(_HttpConnector):
    name = "semantic_scholar"
    label = "Semantic Scholar"
    capabilities = ("papers", "citations", "authors", "ml", "metadata")
    allowed_hosts = frozenset({"api.semanticscholar.org"})

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        fields = "title,abstract,year,authors,venue,externalIds,url,openAccessPdf"
        params = urllib.parse.urlencode({"query": query, "limit": max_results, "fields": fields})
        payload = self._json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}", timeout=timeout)
        records: list[ScientificRecord] = []
        for item in payload.get("data", [])[:max_results]:
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            external_ids = item.get("externalIds") or {}
            doi = _normalize_doi(str(external_ids.get("DOI", "")))
            oa = item.get("openAccessPdf") or {}
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=str(item.get("paperId", "")),
                    title=title,
                    abstract=str(item.get("abstract", "") or ""),
                    year=item.get("year"),
                    authors=tuple(str(author.get("name", "")) for author in item.get("authors", [])),
                    venue=str(item.get("venue", "")),
                    doi=doi,
                    url=str(oa.get("url") or item.get("url") or ""),
                    raw={"citation_count": item.get("citationCount", 0)},
                )
            )
        return records


class PapersWithCodeConnector(_HttpConnector):
    """Optional ML artifact connector using the public Papers with Code API."""

    name = "papers_with_code"
    label = "Papers with Code"
    capabilities = ("ml", "benchmarks", "code", "datasets")
    allowed_hosts = frozenset({"paperswithcode.com"})

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        # The endpoint is intentionally optional: a provider failure is
        # isolated by federation and never prevents paper search.
        encoded = urllib.parse.quote(query.strip(), safe="")
        payload = self._json(f"https://paperswithcode.com/api/v1/papers/?page=1&items_per_page={max_results}&q={encoded}", timeout=timeout)
        items = payload.get("results", payload if isinstance(payload, list) else [])
        records: list[ScientificRecord] = []
        for item in items[:max_results]:
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            paper_id = str(item.get("id") or item.get("paper_url") or title)
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=paper_id,
                    title=title,
                    abstract=str(item.get("abstract", "") or ""),
                    year=_safe_int(item.get("year")),
                    url=_absolute_url(str(item.get("paper_url") or item.get("url") or ""), "https://paperswithcode.com"),
                    code_url=_absolute_url(str(item.get("repository_url") or ""), "https://github.com"),
                    record_type="ml_artifact",
                    tags=("benchmark", "code"),
                    raw=item if isinstance(item, dict) else {},
                )
            )
        return records


class HuggingFaceConnector(_HttpConnector):
    """Optional ML model/dataset search over Hugging Face's public API."""

    name = "huggingface"
    label = "Hugging Face"
    capabilities = ("ml", "models", "datasets", "model-cards", "licenses")
    allowed_hosts = frozenset({"huggingface.co"})

    def search(self, query: str, *, max_results: int, timeout: float) -> Sequence[ScientificRecord]:
        encoded = urllib.parse.quote(query.strip(), safe="")
        payload = self._json(f"https://huggingface.co/api/models?search={encoded}&limit={max_results}", timeout=timeout)
        records: list[ScientificRecord] = []
        for item in payload[:max_results] if isinstance(payload, list) else []:
            model_id = str(item.get("id", "")).strip()
            if not model_id:
                continue
            tags = tuple(str(tag) for tag in item.get("tags", []) if tag)
            records.append(
                ScientificRecord(
                    provider=self.name,
                    external_id=model_id,
                    title=model_id,
                    url=f"https://huggingface.co/{model_id}",
                    record_type="model",
                    license=str(item.get("pipeline_tag", "")),
                    tags=tags,
                    raw={"downloads": item.get("downloads", 0), "likes": item.get("likes", 0)},
                )
            )
        return records


CONNECTOR_FACTORIES: Mapping[str, Callable[[], ScientificConnector]] = {
    "crossref": CrossrefConnector,
    "openalex": OpenAlexConnector,
    "arxiv": ArxivConnector,
    "pubmed": PubMedConnector,
    "semantic_scholar": SemanticScholarConnector,
    "papers_with_code": PapersWithCodeConnector,
    "huggingface": HuggingFaceConnector,
}

DEFAULT_PROVIDERS = ("crossref", "openalex", "arxiv", "semantic_scholar")
ML_PROVIDERS = DEFAULT_PROVIDERS + ("papers_with_code", "huggingface")


def connector_catalog() -> list[dict[str, Any]]:
    """Describe available connectors without contacting any provider."""

    return [
        {
            "name": name,
            "label": factory().label,
            "capabilities": list(factory().capabilities),
            "network": True,
        }
        for name, factory in CONNECTOR_FACTORIES.items()
    ]


def build_search_plan(question: str, *, profile: str = "machine-learning") -> dict[str, Any]:
    """Build public, bounded query variants for the research trace."""

    question = " ".join(question.split())
    if profile == "machine-learning":
        queries = {
            "method": f"{question} method architecture paper",
            "evidence": f"{question} dataset benchmark metric baseline ablation",
            "reproducibility": f"{question} code model card license reproducibility",
        }
        dimensions = [
            "task", "architecture", "dataset", "benchmark", "metric",
            "baseline", "ablation", "hardware", "license", "reproducibility",
        ]
    else:
        queries = {"primary": f"{question} primary research paper", "evidence": f"{question} evidence limitations"}
        dimensions = ["question", "method", "evidence", "limitations", "date"]
    return {"profile": profile, "question": question, "queries": queries, "dimensions": dimensions}


class FederatedSearchService:
    """Run fixed connectors concurrently and merge records conservatively."""

    def __init__(self, connectors: Mapping[str, ScientificConnector] | None = None) -> None:
        self.connectors = dict(connectors or {name: factory() for name, factory in CONNECTOR_FACTORIES.items()})

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "label": connector.label,
                "capabilities": list(connector.capabilities),
                "network": True,
            }
            for name, connector in self.connectors.items()
        ]

    def search(self, request: SearchRequest) -> FederatedSearchResult:
        selected = tuple(request.providers) or (ML_PROVIDERS if request.profile == "machine-learning" else DEFAULT_PROVIDERS)
        selected = tuple(name for name in selected if name in self.connectors)
        plan = build_search_plan(request.question, profile=request.profile)
        queries = _provider_queries(plan, selected)

        async def run_all() -> tuple[dict[str, tuple[ScientificRecord, ...]], dict[str, str]]:
            async def run_one(name: str) -> tuple[str, tuple[ScientificRecord, ...], str | None]:
                connector = self.connectors[name]
                try:
                    values = await asyncio.to_thread(
                        connector.search,
                        queries[name],
                        max_results=request.max_results,
                        timeout=request.timeout,
                    )
                    return name, tuple(values), None
                except Exception as exc:  # provider failures are partial results
                    return name, (), f"{type(exc).__name__}: {exc}"

            values = await asyncio.gather(*(run_one(name) for name in selected))
            return (
                {name: records for name, records, _ in values},
                {name: error for name, _, error in values if error},
            )

        by_provider, errors = asyncio.run(run_all())
        merged = deduplicate_records(
            record for records in by_provider.values() for record in records
        )
        return FederatedSearchResult(
            question=request.question,
            profile=request.profile,
            queries=queries,
            results=tuple(merged),
            results_by_provider=by_provider,
            errors=errors,
            completed_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )


def deduplicate_records(records: Sequence[ScientificRecord] | Any) -> list[ScientificRecord]:
    """Merge exact identifiers and high-confidence title duplicates."""

    result: list[ScientificRecord] = []
    by_identity: dict[str, int] = {}
    for record in records:
        if not record.title:
            continue
        exact = record.identity
        index = by_identity.get(exact)
        if index is None:
            index = _similar_record_index(result, record)
        if index is None:
            by_identity[exact] = len(result)
            result.append(record)
            continue
        result[index] = _merge_records(result[index], record)
        by_identity[exact] = index
    result.sort(key=lambda item: (item.year or 0, len(item.providers), item.title.lower()), reverse=True)
    return result


def _provider_queries(plan: Mapping[str, Any], providers: Sequence[str]) -> dict[str, str]:
    queries = plan["queries"]
    result: dict[str, str] = {}
    for provider in providers:
        if provider in {"papers_with_code", "huggingface"}:
            result[provider] = queries.get("reproducibility", queries.get("method", plan["question"]))
        elif provider in {"crossref", "openalex", "arxiv", "semantic_scholar"}:
            result[provider] = queries.get("method", plan["question"])
        else:
            result[provider] = queries.get("evidence", plan["question"])
    return result


def _merge_records(left: ScientificRecord, right: ScientificRecord) -> ScientificRecord:
    providers = tuple(dict.fromkeys((*left.providers, *right.providers, left.provider, right.provider)))
    return replace(
        left,
        abstract=left.abstract or right.abstract,
        year=left.year or right.year,
        authors=left.authors or right.authors,
        venue=left.venue or right.venue,
        doi=left.doi or right.doi,
        url=left.url or right.url,
        dataset=left.dataset or right.dataset,
        code_url=left.code_url or right.code_url,
        license=left.license or right.license,
        metrics=tuple(dict.fromkeys((*left.metrics, *right.metrics))),
        tags=tuple(dict.fromkeys((*left.tags, *right.tags))),
        providers=providers,
        raw={**dict(left.raw), **dict(right.raw)},
    )


def _similar_record_index(records: Sequence[ScientificRecord], candidate: ScientificRecord) -> int | None:
    normalized = _title_key(candidate.title)
    for index, existing in enumerate(records):
        if candidate.year and existing.year and abs(candidate.year - existing.year) > 1:
            continue
        score = difflib.SequenceMatcher(None, normalized, _title_key(existing.title)).ratio()
        if score >= 0.94:
            return index
    return None


def _title_key(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def _clean(value: str) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return _clean(str(value[0])) if value else ""
    return _clean(str(value or ""))


def _author_name(value: Mapping[str, Any]) -> str:
    return _clean(" ".join(str(value.get(key, "")) for key in ("given", "family") if value.get(key)))


def _normalize_doi(value: str) -> str:
    value = _clean(value).lower()
    value = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.rstrip(".,;)")


def _year_from_parts(parts: Any) -> int | None:
    try:
        return int(parts[0][0]) if parts and parts[0] else None
    except (IndexError, TypeError, ValueError):
        return None


def _year_from_iso(value: str) -> int | None:
    match = re.search(r"(?:19|20)\d{2}", value or "")
    return int(match.group(0)) if match else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strip_markup(value: str) -> str:
    return _clean(re.sub(r"<[^>]+>", " ", value or ""))


def _openalex_abstract(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in value.items():
        for index in indexes or []:
            positions.append((int(index), str(word)))
    return " ".join(word for _, word in sorted(positions))


def _xml_text(node: ET.Element, path: str) -> str:
    child = node.find(path)
    return _clean("".join(child.itertext())) if child is not None else ""


def _absolute_url(value: str, base: str) -> str:
    value = value.strip()
    if not value:
        return ""
    return urllib.parse.urljoin(base, value)
