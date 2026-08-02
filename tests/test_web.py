import asyncio

from three_loop import (
    SearchResult,
    StaticSearchProvider,
    intersect_sources,
    triangulate_sources,
)


def test_intersection_keeps_exact_links_and_common_domains() -> None:
    results = {
        "heuristic": [
            SearchResult("https://www.example.org/guide?utm_source=a", "Guide"),
            SearchResult("https://papers.example.net/one"),
        ],
        "critic": [
            SearchResult("https://example.org/guide", "Guide shared"),
            SearchResult("https://papers.example.net/two"),
        ],
        "writer": [SearchResult("https://unrelated.test/page")],
    }

    matches = intersect_sources(results)

    assert any(match.match_type == "url" and match.domain == "example.org" for match in matches)
    assert any(match.match_type == "domain" and match.domain == "papers.example.net" for match in matches)
    assert all("unrelated.test" not in match.domain for match in matches)


def test_triangulate_sources_searches_independently() -> None:
    shared = SearchResult("https://docs.example.org/reference")
    provider = StaticSearchProvider(
        {
            "q1": [shared, SearchResult("https://one.test")],
            "q2": [shared],
            "q3": [SearchResult("https://three.test")],
        }
    )

    result = asyncio.run(
        triangulate_sources(
            {"heuristic": "q1", "critic": "q2", "writer": "q3"},
            provider,
        )
    )

    assert provider.queries == ["q1", "q2", "q3"]
    assert len(result.sources) == 1
    assert result.sources[0].url == "https://docs.example.org/reference"
    assert set(result.sources[0].agent_ids) == {"heuristic", "critic"}
