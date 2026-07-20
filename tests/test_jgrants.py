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
    PUBLIC_HOST,
    _DEFAULT_KEYWORDS,
    _SUBSIDIES_PATH,
    _assert_allowed_url,
    _fetch_recent_impl,
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


def test_parse_date_iso8601_with_millis_and_z_suffix():
    """Real API dates look like '2026-07-16T03:00:00.000Z'."""
    assert _parse_date("2026-07-16T03:00:00.000Z") == date(2026, 7, 16)


# ---------------------------------------------------------------------------
# _item_to_tender
# ---------------------------------------------------------------------------


def test_item_to_tender_basic():
    item = {
        "id": "JG-001",
        "title": "廃炉技術開発",
        "acceptance_start_datetime": "2024-04-01T00:00:00.000Z",
        "acceptance_end_datetime": "2024-05-31T15:00:00.000Z",
    }
    tender = _item_to_tender(item)
    assert tender is not None
    assert tender.source == "jgrants"
    assert tender.external_id == "JG-001"
    assert tender.title == "廃炉技術開発"
    assert tender.posted_date == date(2024, 4, 1)
    assert tender.deadline == date(2024, 5, 31)


def test_item_to_tender_builds_public_portal_url():
    """URL is constructed as www.jgrants-portal.go.jp/subsidy/{id} — the
    human-facing detail page, which lives on a different domain from the
    API host (ALLOWED_HOST)."""
    item = {
        "id": "a0WJ200000TEST123",
        "title": "テスト公募",
        "acceptance_start_datetime": "2026-01-01T00:00:00.000Z",
        "acceptance_end_datetime": "2026-12-31T23:59:59.000Z",
    }
    tender = _item_to_tender(item)
    assert tender is not None
    assert str(tender.url) == "https://www.jgrants-portal.go.jp/subsidy/a0WJ200000TEST123"


def test_item_to_tender_missing_id_returns_none():
    item = {"title": "IDなし公募"}
    assert _item_to_tender(item) is None


def test_item_to_tender_missing_title_returns_none():
    item = {"id": "a0WJ200000TEST456"}
    assert _item_to_tender(item) is None


def test_item_to_tender_missing_both_returns_none():
    assert _item_to_tender({}) is None


def test_item_to_tender_parses_snake_case_dates():
    item = {
        "id": "a0WJ200000TEST789",
        "title": "日付テスト",
        "acceptance_start_datetime": "2026-06-25T05:00:00.000Z",
        "acceptance_end_datetime": "2026-07-16T03:00:00.000Z",
    }
    tender = _item_to_tender(item)
    assert tender is not None
    assert tender.posted_date is not None
    assert tender.deadline is not None


def test_item_to_tender_description_is_none():
    """The list API has no description-like field; description must stay None
    rather than guessing at an unrelated field."""
    item = {
        "id": "JG-100",
        "title": "概要なし案件",
        "institution_name": "経済産業省",
        "target_area_search": "全国",
    }
    tender = _item_to_tender(item)
    assert tender is not None
    assert tender.description is None


# ---------------------------------------------------------------------------
# fetch_recent — required parameters in every request
# ---------------------------------------------------------------------------


@respx.mock
def test_fetch_recent_required_params_present():
    """Every request must include keyword, sort, order, acceptance."""
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"result": []})

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    _fetch_recent_impl(keywords=["電力"])

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
        return httpx.Response(200, json={"result": []})

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    keywords = ["電力", "原子力", "水素"]
    _fetch_recent_impl(keywords=keywords)

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
    tenders = _fetch_recent_impl(keywords=["電力", "原子力", "水素"])

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
            "result": [
                {**item, "id": f"{item['id']}-{i}"}
                for item in sample["result"]
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
    tenders = _fetch_recent_impl(keywords=["電力", "原子力", "水素"], limit=4)

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
                "result": [
                    {
                        "id": f"JG-kw{call_count}",
                        "title": f"案件{call_count}",
                        "acceptance_start_datetime": "2024-04-01T00:00:00.000Z",
                    }
                ]
            },
        )

    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(side_effect=handler)
    tenders = _fetch_recent_impl(keywords=["エラーキーワード", "電力"])

    # The second keyword should yield 1 result; the first was skipped
    assert len(tenders) >= 1


@respx.mock
def test_fetch_recent_all_keywords_fail_returns_empty():
    """If all keyword requests fail, return empty list (no exception raised)."""
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(return_value=httpx.Response(400))
    tenders = _fetch_recent_impl(keywords=["電力"])
    assert tenders == []


@respx.mock
def test_fetch_recent_parses_all_items():
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = _fetch_recent_impl(keywords=["電力"], limit=100)
    assert len(tenders) == 3
    sources = {t.source for t in tenders}
    assert sources == {"jgrants"}


@respx.mock
def test_fetch_recent_returns_tender_objects():
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = _fetch_recent_impl(keywords=["電力"], limit=100)
    from src.models import Tender

    assert all(isinstance(t, Tender) for t in tenders)


@respx.mock
def test_fetch_recent_urls_point_to_public_portal():
    """Regression test for the production bug: detail URLs must resolve to
    the public portal domain, not the API domain, and must not be empty."""
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = _fetch_recent_impl(keywords=["電力"], limit=100)
    assert len(tenders) == 3
    for t in tenders:
        assert str(t.url).startswith(f"https://{PUBLIC_HOST}/subsidy/")


@respx.mock
def test_fetch_recent_respects_limit():
    sample = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample)
    )
    tenders = _fetch_recent_impl(keywords=["電力"], limit=1)
    assert len(tenders) <= 1


@respx.mock
def test_fetch_recent_5xx_warns_and_skips_keyword():
    """5xx errors (after retries) for a keyword are logged and skipped."""
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(return_value=httpx.Response(503))
    # Should not raise; all keywords will fail and be skipped
    tenders = _fetch_recent_impl(keywords=["電力"])
    assert tenders == []


@respx.mock
def test_fetch_recent_empty_response():
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    tenders = _fetch_recent_impl(keywords=["電力"])
    assert tenders == []


def test_fetch_recent_invalid_limit():
    with pytest.raises(ValueError, match="limit must be > 0"):
        _fetch_recent_impl(limit=0)


def test_fetch_recent_negative_limit():
    with pytest.raises(ValueError, match="limit must be > 0"):
        _fetch_recent_impl(limit=-5)


def test_default_keywords_non_empty():
    """_DEFAULT_KEYWORDS must be non-empty so fetch_recent has something to query."""
    assert len(_DEFAULT_KEYWORDS) > 0


# ---------------------------------------------------------------------------
# fetch_recent — disabled (2026-07-20 Fable裁定: 補助金専用APIのため停止)
# ---------------------------------------------------------------------------


def test_fetch_recent_disabled_returns_empty_list():
    """fetch_recent() is disabled and must always return [] without any HTTP call."""
    assert fetch_recent() == []


def test_fetch_recent_disabled_ignores_arguments():
    """Even with keywords/limit/since supplied, the disabled fetcher returns []."""
    assert fetch_recent(keywords=["電力"], limit=5) == []


def test_fetch_recent_disabled_emits_warning(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="src.fetchers.jgrants"):
        fetch_recent()

    assert any(
        "無効化" in r.message for r in caplog.records
    ), "Expected a warning log containing '無効化'"


def test_fetch_recent_disabled_makes_no_http_request():
    """Regression: no network call must occur — respx with no routes registered
    will raise on any unmocked request, so a clean call proves this."""
    with respx.mock:
        # No routes registered; any HTTP request would raise.
        result = fetch_recent()
    assert result == []
