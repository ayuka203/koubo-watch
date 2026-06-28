"""Tests for src/fetchers/mext.py — offline using respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from src.fetchers.mext import (
    ALLOWED_HOST,
    RSS_URL,
    _assert_allowed_url,
    _parse_feed,
    _struct_time_to_date,
    fetch_recent,
)

# Use the RDF/RSS 1.0 fixture that matches the live index.rdf URL.
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mext_rdf_sample.xml"
# Keep backward-compatible reference for a few tests that still use RSS 2.0 inline XML.
_RSS2_FIXTURE = Path(__file__).parent / "fixtures" / "mext_sample.xml"


# ---------------------------------------------------------------------------
# _assert_allowed_url
# ---------------------------------------------------------------------------


def test_allowed_url_passes():
    _assert_allowed_url(RSS_URL)


def test_rss_url_points_to_rdf():
    """The canonical URL must be the general-news RDF feed, not the old offer XML."""
    assert RSS_URL == "https://www.mext.go.jp/b_menu/news/index.rdf"


def test_disallowed_url_raises():
    with pytest.raises(ValueError, match="SSRF guard"):
        _assert_allowed_url("https://evil.example.com/rss")


def test_http_scheme_rejected():
    with pytest.raises(ValueError, match="Non-HTTPS scheme rejected"):
        _assert_allowed_url(f"http://{ALLOWED_HOST}/b_menu/news/index.rdf")


def test_non_default_port_rejected():
    with pytest.raises(ValueError, match="Non-default port rejected"):
        _assert_allowed_url(f"https://{ALLOWED_HOST}:8443/b_menu/news/index.rdf")


# ---------------------------------------------------------------------------
# _struct_time_to_date
# ---------------------------------------------------------------------------


def test_struct_time_to_date_none():
    assert _struct_time_to_date(None) is None


def test_struct_time_to_date_valid():
    from datetime import date

    st = (2024, 4, 2, 10, 0, 0, 0, 92, 0)
    assert _struct_time_to_date(st) == date(2024, 4, 2)


def test_struct_time_to_date_bad_input():
    assert _struct_time_to_date("garbage") is None


# ---------------------------------------------------------------------------
# _parse_feed — RDF/RSS 1.0 fixture (bytes)
# ---------------------------------------------------------------------------


@pytest.fixture
def rdf_fixture_bytes():
    return FIXTURE_PATH.read_bytes()


@pytest.fixture
def rdf_fixture_xml():
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_feed_rdf_returns_list(rdf_fixture_xml):
    tenders = _parse_feed(rdf_fixture_xml)
    assert isinstance(tenders, list)


def test_parse_feed_rdf_returns_list_bytes(rdf_fixture_bytes):
    tenders = _parse_feed(rdf_fixture_bytes)
    assert isinstance(tenders, list)


def test_parse_feed_rdf_count(rdf_fixture_xml):
    tenders = _parse_feed(rdf_fixture_xml)
    assert len(tenders) == 3


def test_parse_feed_rdf_count_bytes(rdf_fixture_bytes):
    tenders = _parse_feed(rdf_fixture_bytes)
    assert len(tenders) == 3


def test_parse_feed_rdf_source(rdf_fixture_xml):
    tenders = _parse_feed(rdf_fixture_xml)
    assert all(t.source == "mext" for t in tenders)


def test_parse_feed_rdf_titles_non_empty(rdf_fixture_xml):
    tenders = _parse_feed(rdf_fixture_xml)
    for t in tenders:
        assert t.title.strip() != ""


def test_parse_feed_rdf_urls_are_https(rdf_fixture_xml):
    tenders = _parse_feed(rdf_fixture_xml)
    for t in tenders:
        assert t.url.startswith("https://")


def test_parse_feed_rdf_urls_on_allowed_host(rdf_fixture_xml):
    """All parsed links must point to the ALLOWED_HOST."""
    tenders = _parse_feed(rdf_fixture_xml)
    for t in tenders:
        from urllib.parse import urlparse

        assert urlparse(t.url).hostname == ALLOWED_HOST


def test_parse_feed_rdf_tru_found(rdf_fixture_xml):
    tenders = _parse_feed(rdf_fixture_xml)
    descriptions = [t.description or "" for t in tenders]
    assert any("TRU" in d or "地層処分" in d for d in descriptions)


# ---------------------------------------------------------------------------
# _parse_feed — edge cases (inline RSS 2.0 XML still accepted)
# ---------------------------------------------------------------------------


def test_parse_feed_returns_list():
    xml = _RSS2_FIXTURE.read_text(encoding="utf-8")
    tenders = _parse_feed(xml)
    assert isinstance(tenders, list)


def test_parse_feed_returns_list_bytes():
    tenders = _parse_feed(_RSS2_FIXTURE.read_bytes())
    assert isinstance(tenders, list)


def test_parse_feed_count():
    tenders = _parse_feed(_RSS2_FIXTURE.read_text(encoding="utf-8"))
    assert len(tenders) == 3


def test_parse_feed_count_bytes():
    tenders = _parse_feed(_RSS2_FIXTURE.read_bytes())
    assert len(tenders) == 3


def test_parse_feed_source():
    tenders = _parse_feed(_RSS2_FIXTURE.read_text(encoding="utf-8"))
    assert all(t.source == "mext" for t in tenders)


def test_parse_feed_titles_non_empty():
    tenders = _parse_feed(_RSS2_FIXTURE.read_text(encoding="utf-8"))
    for t in tenders:
        assert t.title.strip() != ""


def test_parse_feed_urls_are_https():
    tenders = _parse_feed(_RSS2_FIXTURE.read_text(encoding="utf-8"))
    for t in tenders:
        assert t.url.startswith("https://")


def test_parse_feed_posted_dates_present():
    tenders = _parse_feed(_RSS2_FIXTURE.read_text(encoding="utf-8"))
    assert all(t.posted_date is not None for t in tenders)


def test_parse_feed_tru_found():
    tenders = _parse_feed(_RSS2_FIXTURE.read_text(encoding="utf-8"))
    descriptions = [t.description or "" for t in tenders]
    assert any("TRU" in d or "地層処分" in d for d in descriptions)


def test_parse_feed_empty_xml():
    tenders = _parse_feed("<rss version='2.0'><channel></channel></rss>")
    assert tenders == []


def test_parse_feed_entry_without_link_skipped():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>リンクなし</title></item>
    </channel></rss>"""
    assert _parse_feed(xml) == []


def test_parse_feed_entry_without_title_skipped():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><link>https://www.mext.go.jp/offer/999.html</link></item>
    </channel></rss>"""
    assert _parse_feed(xml) == []


def test_parse_feed_external_link_skipped():
    """Links pointing outside ALLOWED_HOST must be dropped."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>外部リンク案件</title>
        <link>https://evil.example.com/page</link>
        <pubDate>Tue, 02 Apr 2024 10:00:00 +0900</pubDate>
      </item>
    </channel></rss>"""
    tenders = _parse_feed(xml)
    assert tenders == []


def test_parse_feed_http_link_skipped():
    """HTTP (non-HTTPS) links in RSS entries must be dropped."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>HTTP案件</title>
        <link>http://www.mext.go.jp/offer/999.html</link>
        <pubDate>Tue, 02 Apr 2024 10:00:00 +0900</pubDate>
      </item>
    </channel></rss>"""
    tenders = _parse_feed(xml)
    assert tenders == []


# ---------------------------------------------------------------------------
# fetch_recent — mocked HTTP (uses new RDF URL)
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_recent_uses_new_rdf_url():
    """fetch_recent must request the new index.rdf URL, not the old offer XML."""
    fixture_bytes = FIXTURE_PATH.read_bytes()
    mock_route = respx.get(RSS_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes)
    )
    tenders = fetch_recent()
    assert mock_route.called, f"Expected request to {RSS_URL!r} but it was not called"
    assert len(tenders) == 3


@respx.mock
def test_fetch_recent_returns_tenders():
    fixture_bytes = FIXTURE_PATH.read_bytes()
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=fixture_bytes))
    tenders = fetch_recent()
    assert len(tenders) == 3


@respx.mock
def test_fetch_recent_4xx_raises():
    respx.get(RSS_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(Exception):
        fetch_recent()


@respx.mock
def test_fetch_recent_5xx_raises_after_retries():
    respx.get(RSS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(Exception):
        fetch_recent()
