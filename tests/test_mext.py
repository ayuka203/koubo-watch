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

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mext_sample.xml"


# ---------------------------------------------------------------------------
# _assert_allowed_url
# ---------------------------------------------------------------------------


def test_allowed_url_passes():
    _assert_allowed_url(RSS_URL)


def test_disallowed_url_raises():
    with pytest.raises(ValueError, match="SSRF guard"):
        _assert_allowed_url("https://evil.example.com/rss")


def test_http_scheme_rejected():
    with pytest.raises(ValueError, match="Non-HTTPS scheme rejected"):
        _assert_allowed_url(f"http://{ALLOWED_HOST}/b_menu/offer/index.xml")


def test_non_default_port_rejected():
    with pytest.raises(ValueError, match="Non-default port rejected"):
        _assert_allowed_url(f"https://{ALLOWED_HOST}:8443/b_menu/offer/index.xml")


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
# _parse_feed — accepts bytes
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_bytes():
    return FIXTURE_PATH.read_bytes()


@pytest.fixture
def fixture_xml():
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_feed_returns_list(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    assert isinstance(tenders, list)


def test_parse_feed_returns_list_bytes(fixture_bytes):
    tenders = _parse_feed(fixture_bytes)
    assert isinstance(tenders, list)


def test_parse_feed_count(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    assert len(tenders) == 3


def test_parse_feed_count_bytes(fixture_bytes):
    tenders = _parse_feed(fixture_bytes)
    assert len(tenders) == 3


def test_parse_feed_source(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    assert all(t.source == "mext" for t in tenders)


def test_parse_feed_titles_non_empty(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    for t in tenders:
        assert t.title.strip() != ""


def test_parse_feed_urls_are_https(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    for t in tenders:
        assert t.url.startswith("https://")


def test_parse_feed_posted_dates_present(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    assert all(t.posted_date is not None for t in tenders)


def test_parse_feed_tru_found(fixture_xml):
    tenders = _parse_feed(fixture_xml)
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
# fetch_recent — mocked HTTP
# ---------------------------------------------------------------------------


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
