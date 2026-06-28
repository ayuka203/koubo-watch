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
# fetch_recent — mocked HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_payload():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@respx.mock
def test_fetch_recent_parses_all_items(sample_payload):
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample_payload)
    )
    tenders = fetch_recent(limit=100)
    # 3 items in fixture; one has no matching content but should still parse
    assert len(tenders) == 3
    sources = {t.source for t in tenders}
    assert sources == {"jgrants"}


@respx.mock
def test_fetch_recent_returns_tender_objects(sample_payload):
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample_payload)
    )
    tenders = fetch_recent(limit=100)
    from src.models import Tender

    assert all(isinstance(t, Tender) for t in tenders)


@respx.mock
def test_fetch_recent_respects_limit(sample_payload):
    # Fixture has 3 items; limit to 1
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json=sample_payload)
    )
    tenders = fetch_recent(limit=1)
    assert len(tenders) <= 1


@respx.mock
def test_fetch_recent_4xx_raises():
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(403)
    )
    with pytest.raises(Exception):
        fetch_recent()


@respx.mock
def test_fetch_recent_5xx_retries_and_raises():
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(Exception):
        fetch_recent()


def test_fetch_recent_invalid_limit():
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        fetch_recent(limit=0)


def test_fetch_recent_negative_limit():
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        fetch_recent(limit=-5)


@respx.mock
def test_fetch_recent_empty_response():
    respx.get(BASE_URL + _SUBSIDIES_PATH).mock(
        return_value=httpx.Response(200, json={"total": 0, "subsidies": []})
    )
    tenders = fetch_recent()
    assert tenders == []
