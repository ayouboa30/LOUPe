"""Asynchronous web search and multi-agent source triangulation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, quote_plus, unquote, urlparse, urlunparse

from .models import SearchResult, SourceMatch, WebResearchResult


class SearchProvider(Protocol):
    """Minimal async interface required by the triangulation layer."""

    async def search(self, query: str, *, max_results: int = 5) -> Sequence[SearchResult]:
        """Return search results for one independently generated query."""


def canonicalize_url(url: str) -> str:
    """Normalize a URL for equality checks while retaining its useful path."""

    candidate = url.strip()
    if not candidate:
        return ""
    if not urlparse(candidate).scheme:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    port = parsed.port
    netloc = hostname
    if port and not ((parsed.scheme.lower() == "https" and port == 443) or port == 80):
        netloc = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(("https", netloc, path, "", "", ""))


def domain_from_url(url: str) -> str:
    """Return a lower-case host without a leading ``www.``."""

    parsed = urlparse(url if urlparse(url).scheme else f"https://{url}")
    hostname = (parsed.hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def intersect_sources(
    results_by_agent: Mapping[str, Iterable[SearchResult | str]]
    | Sequence[Iterable[SearchResult | str]],
    *,
    min_agents: int = 2,
) -> tuple[SourceMatch, ...]:
    """Keep links or domains independently found by at least ``min_agents``.

    Exact canonical links are preferred.  If agents cite different pages on
    the same domain, one representative page is retained as a domain-level
    match.  A source is counted at most once per agent, which prevents one
    provider returning duplicate results from inflating the consensus.
    """

    if min_agents < 1:
        raise ValueError("min_agents must be at least one")
    normalized = _normalize_agent_results(results_by_agent)
    url_agents: dict[str, set[str]] = {}
    domain_agents: dict[str, set[str]] = {}
    first_by_url: dict[str, SearchResult] = {}
    first_by_domain: dict[str, SearchResult] = {}

    for agent_id, raw_results in normalized.items():
        seen_urls: set[str] = set()
        seen_domains: set[str] = set()
        for raw_result in raw_results:
            result = _coerce_result(raw_result)
            canonical_url = canonicalize_url(result.url)
            domain = domain_from_url(canonical_url or result.url)
            if canonical_url and canonical_url not in seen_urls:
                seen_urls.add(canonical_url)
                url_agents.setdefault(canonical_url, set()).add(agent_id)
                first_by_url.setdefault(canonical_url, result)
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                domain_agents.setdefault(domain, set()).add(agent_id)
                first_by_domain.setdefault(domain, result)

    matches: list[SourceMatch] = []
    matched_domains: set[str] = set()
    for canonical_url, agent_ids in url_agents.items():
        if len(agent_ids) < min_agents:
            continue
        result = first_by_url[canonical_url]
        domain = domain_from_url(canonical_url)
        matched_domains.add(domain)
        matches.append(
            SourceMatch(
                url=canonical_url,
                domain=domain,
                title=result.title,
                snippet=result.snippet,
                agent_ids=tuple(sorted(agent_ids)),
                match_type="url",
            )
        )

    for domain, agent_ids in domain_agents.items():
        if len(agent_ids) < min_agents or domain in matched_domains:
            continue
        result = first_by_domain[domain]
        matches.append(
            SourceMatch(
                url=canonicalize_url(result.url),
                domain=domain,
                title=result.title,
                snippet=result.snippet,
                agent_ids=tuple(sorted(agent_ids)),
                match_type="domain",
            )
        )
    return tuple(matches)


async def triangulate_sources(
    queries: Mapping[str, str],
    provider: SearchProvider,
    *,
    max_results: int = 5,
    min_agents: int = 2,
) -> WebResearchResult:
    """Search all independent queries concurrently and intersect the results."""

    if not queries:
        return WebResearchResult()
    if max_results < 1:
        raise ValueError("max_results must be at least one")

    async def fetch(agent_id: str, query: str) -> tuple[str, tuple[SearchResult, ...], str | None]:
        try:
            raw_results = await provider.search(query, max_results=max_results)
            results = tuple(_coerce_result(result) for result in raw_results)
            return agent_id, results, None
        except Exception as exc:  # search failures should not kill the debate
            return agent_id, (), f"{type(exc).__name__}: {exc}"

    fetched = await asyncio.gather(
        *(fetch(agent_id, query) for agent_id, query in queries.items())
    )
    results_by_agent = {agent_id: results for agent_id, results, _ in fetched}
    errors = {
        agent_id: error
        for agent_id, _, error in fetched
        if error is not None
    }
    return WebResearchResult(
        queries=dict(queries),
        results_by_agent=results_by_agent,
        sources=intersect_sources(results_by_agent, min_agents=min_agents),
        errors=errors,
    )


triangulate_web_sources = triangulate_sources


class DuckDuckGoSearchProvider:
    """Dependency-free provider backed by DuckDuckGo HTML search.

    Uses only the standard library (``urllib``) so it works unmodified inside
    a frozen executable, with no extra HTTP client to install or bundle.
    """

    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout

    async def search(self, query: str, *, max_results: int = 5) -> Sequence[SearchResult]:
        """Fetch and parse a public HTML results page.

        Two things measured necessary to get real results instead of a
        silent empty page: DuckDuckGo's HTML endpoint serves the plain
        homepage (no results, no error) to a GET request carrying a
        non-browser User-Agent - it just looks like a bot query and gets
        the safe default response. POSTing the query as form data with an
        ordinary browser User-Agent returns the actual results page.
        """

        import urllib.error
        import urllib.request

        def fetch() -> str:
            request = urllib.request.Request(
                "https://html.duckduckgo.com/html/",
                data=f"q={quote_plus(query)}".encode("ascii"),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    ),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Recherche web indisponible: {exc}") from exc

        html = await asyncio.to_thread(fetch)
        parser = _DuckDuckGoParser(max_results=max_results)
        parser.feed(html)
        return tuple(parser.results)


class StaticSearchProvider:
    """In-memory provider useful for examples and deterministic tests."""

    def __init__(self, results_by_query: Mapping[str, Sequence[SearchResult]]) -> None:
        self.results_by_query = dict(results_by_query)
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int = 5) -> Sequence[SearchResult]:
        """Return configured results and remember the query."""

        self.queries.append(query)
        return tuple(self.results_by_query.get(query, ()))[:max_results]


class _DuckDuckGoParser(HTMLParser):
    """Parse only the title, URL, and snippet classes used by DDG HTML."""

    def __init__(self, *, max_results: int) -> None:
        super().__init__()
        self.max_results = max_results
        self.results: list[SearchResult] = []
        self._current_url = ""
        self._current_title = ""
        self._current_snippet = ""
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._current_url = _unwrap_search_url(attributes.get("href") or "")
            self._current_title = ""
            self._current_snippet = ""
            self._capture = "title"
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._capture = "snippet"

    def handle_data(self, data: str) -> None:
        if self._capture == "title":
            self._current_title += data
        elif self._capture == "snippet":
            self._current_snippet += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture == "title":
            self._capture = None
        elif self._capture == "snippet" and tag in {"a", "div"}:
            self._capture = None
            if self._current_url and len(self.results) < self.max_results:
                self.results.append(
                    SearchResult(
                        url=self._current_url,
                        title=" ".join(self._current_title.split()),
                        snippet=" ".join(self._current_snippet.split()),
                    )
                )


def _normalize_agent_results(
    results_by_agent: Mapping[str, Iterable[SearchResult | str]]
    | Sequence[Iterable[SearchResult | str]],
) -> dict[str, tuple[SearchResult | str, ...]]:
    if isinstance(results_by_agent, Mapping):
        return {str(key): tuple(value) for key, value in results_by_agent.items()}
    return {
        f"agent-{index + 1}": tuple(value)
        for index, value in enumerate(results_by_agent)
    }


def _coerce_result(result: SearchResult | str) -> SearchResult:
    if isinstance(result, SearchResult):
        return result
    return SearchResult(url=str(result))


def _unwrap_search_url(url: str) -> str:
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "uddg" in query and query["uddg"]:
        return unquote(query["uddg"][0])
    return url
