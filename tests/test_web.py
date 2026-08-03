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


def test_duckduckgo_search_posts_form_data_with_a_browser_user_agent() -> None:
    """A GET with a non-browser UA silently returns DDG's homepage (no
    results, no error) instead of the results page - measured directly.
    POSTing form data with an ordinary browser UA is what actually works.
    """

    import urllib.request

    from three_loop.web import DuckDuckGoSearchProvider

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'<a class="result__a" href="https://example.org">Titre</a>'

    def fake_urlopen(request, timeout=None):
        captured["method"] = request.get_method()
        captured["data"] = request.data
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return _FakeResponse()

    import three_loop.web as web_module

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        import asyncio

        asyncio.run(DuckDuckGoSearchProvider().search("test query"))
    finally:
        urllib.request.urlopen = original

    assert captured["method"] == "POST"
    assert captured["data"] == b"q=test+query"
    assert "mozilla" in captured["headers"].get("user-agent", "").lower()
