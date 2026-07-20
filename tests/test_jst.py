"""Tests for src/fetchers/jst.py — offline using respx.

The JST fetcher was rewritten (2026-07-20) from RSS parsing to HTML
scraping, since choutatsu.jst.go.jp's RSS feed is defunct (robots.txt
disallows /rss/, the real feed URL 404s). It now scrapes
koukoku_link.html for a listing, then fetches each detail page directly by
constructing its clean URL from the listing href.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from src.fetchers.jst import (
    ALLOWED_HOST,
    BASE_URL,
    LISTING_PATH,
    _assert_allowed_url,
    _clean_detail_url,
    _extract_dd_text,
    _parse_detail,
    _parse_listing,
    _parse_wareki_date,
    fetch_recent,
)
from bs4 import BeautifulSoup

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LISTING_FIXTURE = FIXTURES_DIR / "jst_listing_sample.html"
DETAIL_FIXTURE = FIXTURES_DIR / "jst_detail_sample.html"
DETAIL_NO_DATES_FIXTURE = FIXTURES_DIR / "jst_detail_no_dates_sample.html"


# ---------------------------------------------------------------------------
# _assert_allowed_url
# ---------------------------------------------------------------------------


def test_allowed_url_passes():
    _assert_allowed_url(f"https://{ALLOWED_HOST}/koukoku_link.html")


def test_disallowed_url_raises():
    with pytest.raises(ValueError, match="SSRF guard"):
        _assert_allowed_url("https://evil.example.com/koukoku_link.html")


def test_http_scheme_rejected():
    with pytest.raises(ValueError, match="Non-HTTPS scheme rejected"):
        _assert_allowed_url(f"http://{ALLOWED_HOST}/koukoku_link.html")


def test_non_default_port_rejected():
    with pytest.raises(ValueError, match="Non-default port rejected"):
        _assert_allowed_url(f"https://{ALLOWED_HOST}:8443/koukoku_link.html")


# ---------------------------------------------------------------------------
# _clean_detail_url
# ---------------------------------------------------------------------------


def test_clean_detail_url_basic():
    assert (
        _clean_detail_url("kankouju/47_2026_16.html")
        == f"{BASE_URL}/47-2026-16"
    )


def test_clean_detail_url_different_numbers():
    assert (
        _clean_detail_url("kankouju/22_2026_11.html")
        == f"{BASE_URL}/22-2026-11"
    )


def test_clean_detail_url_no_match_returns_none():
    assert _clean_detail_url("/") is None
    assert _clean_detail_url("https://external.example.com/koukoku") is None
    assert _clean_detail_url("mailmaga/index.html") is None


# ---------------------------------------------------------------------------
# _parse_wareki_date
# ---------------------------------------------------------------------------


def test_parse_wareki_date_reiwa_fullwidth():
    assert _parse_wareki_date("令和８年７月１７日(金)") == date(2026, 7, 17)


def test_parse_wareki_date_reiwa_with_time_suffix():
    assert _parse_wareki_date("令和８年８月６日(木)　１３時００分　まで") == date(
        2026, 8, 6
    )


def test_parse_wareki_date_heisei():
    assert _parse_wareki_date("平成３１年４月１日") == date(2019, 4, 1)


def test_parse_wareki_date_no_match_returns_none():
    assert _parse_wareki_date("実施しない") is None


def test_parse_wareki_date_empty_returns_none():
    assert _parse_wareki_date("") is None
    assert _parse_wareki_date(None) is None


def test_parse_wareki_date_invalid_day_returns_none():
    # There is no 令和8年2月30日
    assert _parse_wareki_date("令和８年２月３０日") is None


# ---------------------------------------------------------------------------
# _parse_listing
# ---------------------------------------------------------------------------


@pytest.fixture
def listing_html():
    return LISTING_FIXTURE.read_text(encoding="utf-8")


def test_parse_listing_returns_items(listing_html):
    items = _parse_listing(listing_html)
    assert len(items) == 3


def test_parse_listing_urls_are_clean(listing_html):
    items = _parse_listing(listing_html)
    urls = [url for url, _ in items]
    assert f"{BASE_URL}/47-2026-16" in urls
    assert f"{BASE_URL}/47-2026-17" in urls
    assert f"{BASE_URL}/46-2026-81" in urls


def test_parse_listing_titles_not_empty(listing_html):
    items = _parse_listing(listing_html)
    for _, title in items:
        assert title.strip() != ""


def test_parse_listing_ignores_non_kankouju_links(listing_html):
    items = _parse_listing(listing_html)
    urls = [url for url, _ in items]
    assert not any("external.example.com" in u for u in urls)
    assert len(urls) == 3  # "/" and the external link are excluded


def test_parse_listing_no_duplicate_urls(listing_html):
    items = _parse_listing(listing_html)
    urls = [url for url, _ in items]
    assert len(urls) == len(set(urls))


def test_parse_listing_empty_html():
    assert _parse_listing("<html><body></body></html>") == []


# ---------------------------------------------------------------------------
# _extract_dd_text
# ---------------------------------------------------------------------------


@pytest.fixture
def detail_soup():
    return BeautifulSoup(DETAIL_FIXTURE.read_text(encoding="utf-8"), "html.parser")


def test_extract_dd_text_first_match(detail_soup):
    assert _extract_dd_text(detail_soup, "公告日") == "令和８年７月１７日(金)"


def test_extract_dd_text_last_match_of_multiple(detail_soup):
    # There are three "期限" dt/dd pairs; last=True should return the final one.
    text = _extract_dd_text(detail_soup, "期限", last=True)
    assert "令和８年８月６日" in text


def test_extract_dd_text_first_match_of_multiple(detail_soup):
    # Without last=True, the first "期限" (質問書提出期限) is returned.
    text = _extract_dd_text(detail_soup, "期限", last=False)
    assert "令和８年７月２７日" in text


def test_extract_dd_text_no_match_returns_none(detail_soup):
    assert _extract_dd_text(detail_soup, "存在しない項目") is None


# ---------------------------------------------------------------------------
# _parse_detail
# ---------------------------------------------------------------------------


def test_parse_detail_basic():
    html = DETAIL_FIXTURE.read_text(encoding="utf-8")
    url = f"{BASE_URL}/47-2026-16"
    tender = _parse_detail(html, url, "外国人研究者宿舎「二の宮ハウス」居住者管理システム等のWindows11対応に伴う更新　一式")

    assert tender.source == "jst"
    assert tender.url == url
    assert tender.external_id == "47-2026-16"
    assert tender.posted_date == date(2026, 7, 17)
    # deadline should be the LAST 期限 (応募資料提出期限), not the first
    assert tender.deadline == date(2026, 8, 6)
    assert tender.description is None


def test_parse_detail_no_dates_fields_are_none():
    html = DETAIL_NO_DATES_FIXTURE.read_text(encoding="utf-8")
    url = f"{BASE_URL}/22-2026-11"
    tender = _parse_detail(html, url, "日付情報のない案件")

    assert tender.posted_date is None
    assert tender.deadline is None
    assert tender.title == "日付情報のない案件"


# ---------------------------------------------------------------------------
# fetch_recent — full pipeline (listing + N detail fetches), mocked HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_recent_returns_tenders():
    listing_html = LISTING_FIXTURE.read_text(encoding="utf-8")
    detail_html = DETAIL_FIXTURE.read_text(encoding="utf-8")

    respx.get(BASE_URL + LISTING_PATH).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
    respx.get(url__regex=rf"{BASE_URL}/\d+-\d+-\d+").mock(
        return_value=httpx.Response(200, text=detail_html)
    )

    tenders = fetch_recent()
    assert len(tenders) == 3
    assert all(t.source == "jst" for t in tenders)


@respx.mock
def test_fetch_recent_skips_failed_detail_page():
    """One detail page failing (e.g. 500 after retries) must not abort the run."""
    listing_html = LISTING_FIXTURE.read_text(encoding="utf-8")
    detail_html = DETAIL_FIXTURE.read_text(encoding="utf-8")

    respx.get(BASE_URL + LISTING_PATH).mock(
        return_value=httpx.Response(200, text=listing_html)
    )
    respx.get(f"{BASE_URL}/47-2026-16").mock(return_value=httpx.Response(500))
    respx.get(f"{BASE_URL}/47-2026-17").mock(
        return_value=httpx.Response(200, text=detail_html)
    )
    respx.get(f"{BASE_URL}/46-2026-81").mock(
        return_value=httpx.Response(200, text=detail_html)
    )

    tenders = fetch_recent()
    # 3 listing items, 1 fails after retries -> 2 succeed
    assert len(tenders) == 2


@respx.mock
def test_fetch_recent_listing_4xx_raises():
    respx.get(BASE_URL + LISTING_PATH).mock(return_value=httpx.Response(404))
    with pytest.raises(Exception):
        fetch_recent()


@respx.mock
def test_fetch_recent_listing_5xx_raises_after_retries():
    respx.get(BASE_URL + LISTING_PATH).mock(return_value=httpx.Response(500))
    with pytest.raises(Exception):
        fetch_recent()


@respx.mock
def test_fetch_recent_empty_listing_returns_empty_list():
    respx.get(BASE_URL + LISTING_PATH).mock(
        return_value=httpx.Response(200, text="<html><body></body></html>")
    )
    tenders = fetch_recent()
    assert tenders == []
