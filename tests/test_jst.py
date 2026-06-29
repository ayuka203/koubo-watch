"""Tests for src/fetchers/jst.py — offline using respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from src.fetchers.jst import (
    ALLOWED_HOST,
    RSS_URL,
    _assert_allowed_url,
    _parse_feed,
    _struct_time_to_date,
    fetch_recent,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "jst_sample.xml"


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
        _assert_allowed_url(f"http://{ALLOWED_HOST}/rss.php")


def test_non_default_port_rejected():
    with pytest.raises(ValueError, match="Non-default port rejected"):
        _assert_allowed_url(f"https://{ALLOWED_HOST}:8443/rss.php")


# ---------------------------------------------------------------------------
# _struct_time_to_date
# ---------------------------------------------------------------------------


def test_struct_time_to_date_none():
    assert _struct_time_to_date(None) is None


def test_struct_time_to_date_valid():
    from datetime import date

    st = (2024, 4, 1, 9, 0, 0, 0, 91, 0)
    assert _struct_time_to_date(st) == date(2024, 4, 1)


def test_struct_time_to_date_invalid():
    assert _struct_time_to_date("not-a-struct") is None


# ---------------------------------------------------------------------------
# _parse_feed — accepts bytes
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_bytes():
    return FIXTURE_PATH.read_bytes()


@pytest.fixture
def fixture_xml():
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_feed_returns_list_bytes(fixture_bytes):
    tenders = _parse_feed(fixture_bytes)
    assert isinstance(tenders, list)


def test_parse_feed_returns_list(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    assert isinstance(tenders, list)


def test_parse_feed_counts(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    # 3 items in fixture
    assert len(tenders) == 3


def test_parse_feed_counts_bytes(fixture_bytes):
    tenders = _parse_feed(fixture_bytes)
    assert len(tenders) == 3


def test_parse_feed_source(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    assert all(t.source == "jst" for t in tenders)


def test_parse_feed_titles_not_empty(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    for t in tenders:
        assert t.title.strip() != ""


def test_parse_feed_urls_are_https(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    for t in tenders:
        assert t.url.startswith("https://")


def test_parse_feed_has_posted_date(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    # All 3 fixture items have pubDate
    assert all(t.posted_date is not None for t in tenders)


def test_parse_feed_smr_found(fixture_xml):
    tenders = _parse_feed(fixture_xml)
    titles = [t.title for t in tenders]
    assert any("SMR" in title for title in titles)


def test_parse_feed_empty_xml():
    tenders = _parse_feed("<rss version='2.0'><channel></channel></rss>")
    assert tenders == []


# ---------------------------------------------------------------------------
# bozo feed — body sample appears in warning log
# ---------------------------------------------------------------------------


def test_parse_feed_bozo_logs_body_sample_bytes(caplog):
    """When bozo=True and input is bytes, the log message must include body sample."""
    import logging

    # Truncated XML triggers bozo (unclosed token)
    malformed = b"<?xml version='1.0'?><rss version='2.0'><channel><broken"
    with caplog.at_level(logging.WARNING, logger="src.fetchers.jst"):
        _parse_feed(malformed)

    bozo_records = [r for r in caplog.records if "bozo" in r.message.lower()]
    assert bozo_records, "Expected a bozo warning log record"
    assert "body sample" in bozo_records[0].message


def test_parse_feed_bozo_logs_body_sample_str(caplog):
    """When bozo=True and input is str, the log message must include body sample."""
    import logging

    # Truncated XML triggers bozo (unclosed token)
    malformed_str = "<?xml version='1.0'?><rss version='2.0'><channel><broken"
    with caplog.at_level(logging.WARNING, logger="src.fetchers.jst"):
        _parse_feed(malformed_str)

    bozo_records = [r for r in caplog.records if "bozo" in r.message.lower()]
    assert bozo_records, "Expected a bozo warning log record"
    assert "body sample" in bozo_records[0].message


def test_parse_feed_missing_link_skipped():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>タイトルのみ</title></item>
    </channel></rss>"""
    tenders = _parse_feed(xml)
    assert tenders == []


def test_parse_feed_missing_title_skipped():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><link>https://choutatsu.jst.go.jp/bid/999</link></item>
    </channel></rss>"""
    tenders = _parse_feed(xml)
    assert tenders == []


def test_parse_feed_external_link_skipped():
    """Links pointing outside ALLOWED_HOST must be dropped."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>外部リンク案件</title>
        <link>https://evil.example.com/page</link>
        <pubDate>Mon, 01 Apr 2024 09:00:00 +0900</pubDate>
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
        <link>http://choutatsu.jst.go.jp/bid/999</link>
        <pubDate>Mon, 01 Apr 2024 09:00:00 +0900</pubDate>
      </item>
    </channel></rss>"""
    tenders = _parse_feed(xml)
    assert tenders == []


# ---------------------------------------------------------------------------
# fetch_recent — disabled fetcher (RSS URL under re-investigation)
# ---------------------------------------------------------------------------


def test_fetch_recent_returns_empty_list():
    """fetch_recent returns [] while the JST RSS URL is under re-investigation."""
    tenders = fetch_recent()
    assert tenders == []


def test_fetch_recent_emits_warning(caplog):
    """fetch_recent must emit a WARNING log when it is disabled."""
    import logging

    with caplog.at_level(logging.WARNING, logger="src.fetchers.jst"):
        fetch_recent()

    assert any(
        "無効化" in r.message for r in caplog.records
    ), "Expected a warning log containing '無効化'"


# The tests below exercise the HTTP/retry behaviour of the old live fetcher.
# They are skipped until the real RSS URL is identified and the fetcher is re-enabled.


@pytest.mark.skip(reason="JST フェッチャーは RSS URL 再調査までの間、無効化済み")
@respx.mock
def test_fetch_recent_returns_tenders():
    fixture_bytes = FIXTURE_PATH.read_bytes()
    respx.get(RSS_URL).mock(return_value=httpx.Response(200, content=fixture_bytes))
    tenders = fetch_recent()
    assert len(tenders) == 3


@pytest.mark.skip(reason="JST フェッチャーは RSS URL 再調査までの間、無効化済み")
@respx.mock
def test_fetch_recent_4xx_raises():
    respx.get(RSS_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(Exception):
        fetch_recent()


@pytest.mark.skip(reason="JST フェッチャーは RSS URL 再調査までの間、無効化済み")
@respx.mock
def test_fetch_recent_5xx_raises_after_retries():
    respx.get(RSS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(Exception):
        fetch_recent()
