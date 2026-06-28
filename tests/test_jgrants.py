"""Tests for src/fetchers/jgrants.py — offline using respx."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from src.fetchers.jgrants import (
    ALLOWED_HOST,
    BASE_URL,
    _DEFAULT_KEYWORDS,
    _SUBSIDIES_PATH,
    _assert_allowed_url,
    _item_to_tender,
    _parse_date,
    fetch_recent,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "jgrants_sample.json"


# ---------------------------------------------------------------------------
# _assert_allowed_url
# ---------------------------------------------------------------------------


def test_allowed_url_passes():
    _assert_allowed_url(f"https://{ALLOWED_HOST}/test")


def test_disallowed_url_raises():
    with pytest.raises(ValueError, match="SSRF guard"):
        _assert_allowed_url("https://evil.example.com/path")


def test_disallowed_http_url_raises():
    with pytest.raises(ValueError, match="Non-HTTPS scheme rejected"):
        _assert_allowed_url(f"http://{ALLOWED_HOST}/path")


def test_non_default_port_rejected():
    with pytest.raises(ValueError, match="Non-default port rejected"):
        _assert_allowed_url(f"https://{ALLOWED_HOST}:8443/path")


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------


def test_parse_date_iso():
    assert _parse_date("2024-04-01") == date(2024, 4, 1)


def test_parse_date_none():
    assert _parse_date(None) is None


def test_parse_date_empty():
    assert _parse_date("") is None


def test_parse_date_invalid():
    assert _parse_date("not-a-date") is None


def test_parse_date_already_date():
    d = date(2024, 5, 1)
    assert _parse_date(d) == d


# ---------------------------------------------------------------------------
# _item_to_tender
# ---------------------------------------------------------------------------


def test_item_to_tender_basic():
    item = {
        "subsidyId": "JG-001",
        "subsidyName": "廃炉技術開発",
        "url": "https://api.jgrants-portal.go.jp/subsidies/JG-001",
        "targetDescription": "燃料デブリ取り出し",
        "acceptStartDate": "2024-04-01",
        "acceptEndDate": "2024-05-31",
    }
    tender = _item_to_tender(item)
    assert tender is not None
    assert tender.source == "jgrants"
    assert tender.external_id == "JG-001"
    assert tender.title == "廃炉技術開発"
    assert tender.posted_date == date(2024, 4, 1)
    assert tender.deadline == date(2024, 5, 31)


def test_item_to_tender_no_title_returns_none():
    item = {"subsidyId": "JG-002", "url": "https://api.jgrants-portal.go.jp/s/2"}
    assert _item_to_tender(item) is None


def test_item_to_tender_no_url_uses_id():
    item = {
        "subsidyId": "JG-003",
        "subsidyName": "テスト公募",
    }
    tender = _item_to_tender(item)
    assert tender is not None
    assert "JG-003" in tender.url


def test_item_to_tender_no_url_no_id_returns_none():
    item = {"subsidyName": "タイトルのみ"}
    assert _item_to_tender(item) is None


def test_item_to_tender_off_host_url_returns_none():
    """Response-supplied URLs pointing off-host must be rejected."""
    item = {
        "subsidyId": "JG-999",
        "subsidyName": "外部リンク案件",
        "url": "https://evil.example.com/malicious",
    }
    assert _item_to_tender(item) is None


def test_item_to_tender_http_url_returns_none():
    """Response-supplied HTTP (non-HTTPS) URLs must be rejected."""
    item = {
        "subsidyId": "JG-998",
        "subsidyName": "HTTP案件",
        "url": f"http://{ALLOWED_HOST}/subsidies/JG-998",
    }
    assert _item_to_tender(item) is None


def test_item_to_tender_port_url_returns_none():
    """Response-supplied URLs with non-default port must be rejected."""
    item = {
        "subsidyId": "JG-997",
        "subsidyName": "ポート付き案件",
        "url": f"https://{ALLOWED_HOST}:9000/subsidies/JG-997",
    }
    assert _item_to_tender(item) is None


# ---------------------------------------------------------------------------
# fetch_recent — required parameters in every request
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_recent_required_params_present():
    """Every request must include keyword, sort, order, acceptance."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"subsidies": [], "total": 0})

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    fetch_recent(keywords=["電力"])

    assert len(captured_requests) == 1
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(str(captured_requests[0].url)).query)
    assert "keyword" in qs, "keyword param missing"
    assert "sort" in qs, "sort param missing"
    assert "order" in qs, "order param missing"
    assert "acceptance" in qs, "acceptance param missing"
    assert qs["keyword"] == ["電力"]


@respx.mock
def test_fetch_recent_keyword_sent_per_query():
    """Each keyword in the list triggers a separate request."""
    captured_keywords: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(str(request.url)).query)
        captured_keywords.extend(qs.get("keyword", []))
        return httpx.Response(200, json={"subsidies": [], "total": 0})

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    keywords = ["電力", "原子力", "水素"]
    fetch_recent(keywords=keywords)

    assert set(captured_keywords) == set(keywords)


@respx.mock
def test_fetch_recent_union_deduplication():
    """Items with the same external_id returned by different keyword queries
    must appear only once in the result."""
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        # Return the same 3-item payload for every keyword
        return httpx.Response(200, json=sample)

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    tenders = fetch_recent(keywords=["電力", "原子力", "水素"])

    # Only 3 unique items despite 3 queries each returning 3 items
    assert len(tenders) == 3
    ids = [t.external_id for t in tenders]
    assert len(ids) == len(set(ids)), "Duplicate external_ids found"


@respx.mock
def test_fetch_recent_limit_respected():
    """fetch_recent must return at most limit items across all keyword queries."""
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # Assign unique IDs so dedup doesn't interfere
    unique_samples = []
    for i, kw in enumerate(["電力", "原子力", "水素"]):
        payload = {
            "subsidies": [
                {**item, "subsidyId": f"{item['subsidyId']}-{i}"}
                for item in sample["subsidies"]
            ]
        }
        unique_samples.append(payload)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        resp = httpx.Response(200, json=unique_samples[min(call_count, len(unique_samples) - 1)])
        call_count += 1
        return resp

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    tenders = fetch_recent(keywords=["電力", "原子力", "水素"], limit=4)

    assert len(tenders) <= 4


@respx.mock
def test_fetch_recent_http400_skips_keyword_continues():
    """A 400 error for one keyword should be skipped; other keywords continue."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(400)  # First keyword fails
        return httpx.Response(
            200,
            json={
                "subsidies": [
                    {
                        "subsidyId": f"JG-kw{call_count}",
                        "subsidyName": f"案件{call_count}",
                        "url": f"https://{ALLOWED_HOST}/subsidies/kw{call_count}",
                        "acceptStartDate": "2024-04-01",
                    }
                ]
            },
        )

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    tenders = fetch_recent(keywords=["エラーキーワード", "電力"])

    # The second keyword should yield 1 result; the first was skipped
    assert len(tenders) >= 1


@respx.mock
def test_fetch_recent_all_keywords_fail_returns_empty():
    """If all keyword requests fail, return empty list (no exception raised)."""
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(return_value=httpx.Response(400))
    tenders = fetch_recent(keywords=["電力"])
    assert tenders == []


@respx.mock
def test_fetch_recent_parses_all_items():
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = fetch_recent(keywords=["電力"], limit=100)
    assert len(tenders) == 3
    sources = {t.source for t in tenders}
    assert sources == {"jgrants"}


@respx.mock
def test_fetch_recent_returns_tender_objects():
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = fetch_recent(keywords=["電力"], limit=100)
    from src.models import Tender

    assert all(isinstance(t, Tender) for t in tenders)


@respx.mock
def test_fetch_recent_respects_limit():
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = fetch_recent(keywords=["電力"], limit=1)
    assert len(tenders) <= 1


@respx.mock
def test_fetch_recent_5xx_warns_and_skips_keyword():
    """5xx errors (after retries) for a keyword are logged and skipped."""
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(return_value=httpx.Response(503))
    # Should not raise; all keywords will fail and be skipped
    tenders = fetch_recent(keywords=["電力"])
    assert tenders == []


@respx.mock
def test_fetch_recent_empty_response():
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json={"total": 0, "subsidies": []})
    )
    tenders = fetch_recent(keywords=["電力"])
    assert tenders == []


def test_fetch_recent_invalid_limit():
    with pytest.raises(ValueError, match="limit must be > 0"):
        fetch_recent(limit=0)


def test_fetch_recent_negative_limit():
    with pytest.raises(ValueError, match="limit must be > 0"):
        fetch_recent(limit=-5)


def test_default_keywords_non_empty():
    """_DEFAULT_KEYWORDS must be non-empty so fetch_recent has something to query."""
    assert len(_DEFAULT_KEYWORDS) > 0
