from three_loop.scrape import extract_readable_text


def test_strips_script_style_nav_and_footer() -> None:
    html = (
        "<html><head><title>T</title><style>.x{color:red}</style>"
        "<script>var a=1;</script></head><body>"
        "<nav>Accueil Contact</nav><h1>Bonjour</h1>"
        "<p>Un paragraphe utile.</p><footer>copyright 2026</footer>"
        "</body></html>"
    )

    title, text = extract_readable_text(html)

    assert title == "T"
    assert "Bonjour" in text
    assert "Un paragraphe utile." in text
    for chrome in ("Accueil", "copyright", "color:red", "var a=1"):
        assert chrome not in text


def test_block_elements_get_line_breaks_not_run_together() -> None:
    html = "<p>Premiere phrase.</p><p>Deuxieme phrase.</p>"

    _, text = extract_readable_text(html)

    assert "Premiere phrase.\nDeuxieme phrase." in text


def test_collapses_internal_whitespace() -> None:
    html = "<p>Texte   avec\n\n  des   espaces</p>"

    _, text = extract_readable_text(html)

    assert text == "Texte avec des espaces"


def test_empty_html_yields_empty_text() -> None:
    title, text = extract_readable_text("<html><body></body></html>")

    assert title == ""
    assert text == ""


def test_fetch_page_rejects_non_http_schemes() -> None:
    from three_loop.scrape import fetch_page

    try:
        fetch_page("file:///etc/passwd")
        assert False, "should have raised"
    except ValueError as exc:
        assert "invalide" in str(exc)
