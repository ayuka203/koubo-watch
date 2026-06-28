"""Jグランツ (J-Grants) public API client.

The public endpoint is documented at https://api.jgrants-portal.go.jp/
No API key is required for public grant information.

The /exp/v1/public/subsidies endpoint requires ``keyword``, ``sort``,
``order``, and ``acceptance`` parameters for every request.  fetch_recent
iterates over a configurable keyword list and unions the results, deduplicating
by ``external_id``.

SSRF protection: only the ALLOWED_HOST is contacted; scheme and port are
validated; response-derived URLs are also validated before use.
HTTP safety: 30 s timeout, 3 retries with exponential back-off,
follow_redirects=False to prevent redirect-based SSRF.
4xx responses raise immediately; 5xx are retried.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from dateutil import parser as dateutil_parser

from src.models import Tender

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_HOST = "api.jgrants-portal.go.jp"
BASE_URL = f"https://{ALLOWED_HOST}"

_SUBSIDIES_PATH = "/exp/v1/public/subsidies"

_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds

# Default keywords used when the caller does not supply a custom list.
# Covers the main domains of interest (nuclear, grid, renewable energy, GX).
_DEFAULT_KEYWORDS: list[str] = [
    "原子力",
    "放射線",
    "送配電",
    "電力",
    "送電",
    "配電",
    "再エネ",
    "蓄電",
    "水素",
    "エネルギー",
    "脱炭素",
    "GX",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_allowed_url(url: str) -> None:
    """Raise ValueError if *url* does not target the allowed host."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Non-HTTPS scheme rejected: {url}")
    if parsed.hostname != ALLOWED_HOST:
        raise ValueError(
            f"SSRF guard: attempted request to disallowed host {parsed.hostname!r}. "
            f"Only {ALLOWED_HOST!r} is permitted."
        )
    if parsed.port is not None:
        raise ValueError(f"Non-default port rejected: {url}")


def _get_with_retry(
    client: httpx.Client, url: str, params: dict[str, Any]
) -> httpx.Response:
    """GET *url* with retry logic.

    4xx — raise immediately (no retry).
    5xx — retry up to _MAX_RETRIES times with exponential back-off.
    """
    _assert_allowed_url(url)
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = client.get(
                url, params=params, timeout=_TIMEOUT_SECONDS, follow_redirects=False
            )
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()
            if resp.status_code >= 500:
                logger.warning(
                    "jgrants: HTTP %d on attempt %d/%d",
                    resp.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_BASE ** attempt)
                    continue
                resp.raise_for_status()
            return resp
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "jgrants: timeout on attempt %d/%d", attempt + 1, _MAX_RETRIES
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_BASE ** attempt)
    raise RuntimeError(f"jgrants: all {_MAX_RETRIES} attempts failed") from last_exc


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    """Parse a date string or return None."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return dateutil_parser.parse(str(value)).date()
    except (ValueError, OverflowError):
        return None


def _item_to_tender(item: dict) -> Tender | None:
    """Convert one API response item to a Tender.  Returns None on bad data."""
    # Field names are assumptions based on common J-Grants API patterns.
    # Adjust once real API documentation is confirmed.
    subsidy_id = str(item.get("subsidyId") or item.get("id") or "")
    title = str(item.get("subsidyName") or item.get("title") or "").strip()
    if not title:
        return None

    detail_url = str(item.get("url") or item.get("detailUrl") or "").strip()
    if not detail_url:
        # Construct from id if no URL provided
        if not subsidy_id:
            return None
        detail_url = f"{BASE_URL}/subsidies/{subsidy_id}"

    if not detail_url.startswith(("http://", "https://")):
        detail_url = f"https://{ALLOWED_HOST}{detail_url}"

    # Validate the response-derived URL before use
    parsed = urlparse(detail_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_HOST
        or parsed.port is not None
    ):
        logger.warning("jgrants: skipping URL outside allowlist: %s", detail_url)
        return None

    description = str(item.get("targetDescription") or item.get("description") or "") or None
    posted_raw = item.get("acceptStartDate") or item.get("postedDate")
    deadline_raw = item.get("acceptEndDate") or item.get("deadline")

    return Tender(
        source="jgrants",
        external_id=subsidy_id or None,
        title=title,
        url=detail_url,
        description=description,
        posted_date=_parse_date(posted_raw),
        deadline=_parse_date(deadline_raw),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_recent(
    since: date | None = None,
    limit: int = 100,
    keywords: list[str] | None = None,
) -> list[Tender]:
    """Fetch recently posted subsidies from the J-Grants public API.

    The /subsidies endpoint requires ``keyword``, ``sort``, ``order``, and
    ``acceptance`` for every call.  This function iterates over *keywords*
    (or _DEFAULT_KEYWORDS if None) and unions the results, deduplicating by
    ``external_id``.

    Parameters
    ----------
    since:
        Only return records with ``acceptStartDate`` on or after this date.
        If None, returns the most recent *limit* records (no date filter).
    limit:
        Maximum total records to return (across all keywords).
    keywords:
        List of keyword strings to query.  Defaults to _DEFAULT_KEYWORDS.

    Returns
    -------
    list[Tender]
        Deduplicated, parsed tender objects; at most *limit* entries.
    """
    if limit <= 0:
        raise ValueError(f"limit must be > 0, got {limit}")

    queries = keywords if keywords is not None else _DEFAULT_KEYWORDS
    per_keyword_limit = max(10, limit // max(1, len(queries)))

    seen: set[str] = set()
    results: list[Tender] = []

    with httpx.Client() as client:
        for kw in queries:
            params: dict[str, Any] = {
                "keyword": kw,
                "sort": "created_date",
                "order": "DESC",
                "acceptance": 1,  # 1 = 応募受付中のみ
                "limit": per_keyword_limit,
            }
            if since is not None:
                params["acceptStartDate"] = since.isoformat()

            url = BASE_URL + _SUBSIDIES_PATH
            try:
                resp = _get_with_retry(client, url, params)
            except Exception as exc:
                logger.warning("jgrants: keyword=%s で取得失敗: %s", kw, exc)
                continue

            body = resp.json()
            items = body.get("result") or body.get("results") or body.get("subsidies") or body.get("data") or (body if isinstance(body, list) else [])

            for item in items:
                tender = _item_to_tender(item)
                if tender is None:
                    continue
                key = tender.external_id or str(tender.url)
                if key in seen:
                    continue
                seen.add(key)
                results.append(tender)
                if len(results) >= limit:
                    return results

    return results
