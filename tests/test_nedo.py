"""Tests for src/fetchers/nedo.py — offline using respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from src.fetchers.nedo import (
    ALLOWED_HOST,
    BASE_URL,
    INDEX_PATH,
    _assert_allowed_url,
    _parse_listing,
    _resolve_url,
    fetch_recent,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nedo_index_sample.html"


# ---------------------------------------------------------------------------
# _assert_allowed_url
# ---------------------------------------------------------------------------


def test_allowed_url_passes():
    _assert_allowed_url(f"https://{ALLOWED_HOST}/koubo/")


def test_disallowed_url_raises():
    with pytest.raises(ValueError, match="SSRF guard"):
        _assert_allowed_url("https://evil.example.com/path")


def test_http_scheme_rejected():
    with pytest.raises(ValueError, match="Non-HTTPS scheme rejected"):
        _assert_allowed_url(f"http://{ALLOWED_HOST}/koubo/")


def test_non_default_port_rejected():
    with pytest.raises(ValueError, match="Non-default port rejected"):
        _assert_allowed_url(f"https://{ALLOWED_HOST}:8080/koubo/")


# ---------------------------------------------------------------------------
# _resolve_url
# ---------------------------------------------------------------------------


def test_resolve_relative_href():
    url = _resolve_url("/koubo/2024/001.html")
    assert url == f"https://{ALLOWED_HOST}/koubo/2024/001.html"


def test_resolve_absolute_same_host():
    url = _resolve_url(f"https://{ALLOWED_HOST}/koubo/001.html")
    assert url is not None
    assert ALLOWED_HOST in url


def test_resolve_external_returns_none():
    url = _resolve_url("https://external.example.com/page")
    assert url is None


def test_resolve_empty_returns_none():
    assert _resolve_url("") is None


# ---------------------------------------------------------------------------
# _parse_listing — using fixture HTML
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_html():
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_listing_returns_list(fixture_html):
    tenders = _parse_listing(fixture_html)
    assert isinstance(tenders, list)


def test_parse_listing_finds_items(fixture_html):
    tenders = _parse_listing(fixture_html)
    assert len(tenders) >= 1


def test_parse_listing_source_is_nedo(fixture_html):
    tenders = _parse_listing(fixture_html)
    assert all(t.source == "nedo" for t in tenders)


def test_parse_listing_urls_are_absolute(fixture_html):
    tenders = _parse_listing(fixture_html)
    for t in tenders:
        assert t.url.startswith("https://")


def test_parse_listing_no_duplicate_urls(fixture_html):
    tenders = _parse_listing(fixture_html)
    urls = [t.url for t in tenders]
    assert len(urls) == len(set(urls))


def test_parse_listing_titles_not_empty(fixture_html):
    tenders = _parse_listing(fixture_html)
    for t in tenders:
        assert t.title.strip() != ""


def test_parse_listing_httr_found(fixture_html):
    tenders = _parse_listing(fixture_html)
    titles = [t.title for t in tenders]
    assert any("HTTR" in title or "高温ガス炉" in title for title in titles)


def test_parse_listing_empty_html():
    tenders = _parse_listing("<html><body></body></html>")
    assert tenders == []


# ---------------------------------------------------------------------------
# fetch_recent — mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_recent_returns_tenders():
    fixture_html = FIXTURE_PATH.read_text(encoding="utf-8")
    respx.get(BASE_URL + INDEX_PATH).mock(
        return_value=httpx.Response(200, text=fixture_html)
    )
    tenders = fetch_recent()
    assert len(tenders) >= 1


@respx.mock
def test_fetch_recent_4xx_raises():
    respx.get(BASE_URL + INDEX_PATH).mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(Exception):
        fetch_recent()


@respx.mock
def test_fetch_recent_5xx_raises_after_retries():
    respx.get(BASE_URL + INDEX_PATH).mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(Exception):
        fetch_recent()
