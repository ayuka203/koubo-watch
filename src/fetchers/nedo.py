"""NEDO public tender HTML scraper.

Scrapes the NEDO tender listing page and extracts individual tender items.

SSRF protection: only ALLOWED_HOST is contacted.
HTTP safety: 30 s timeout, 3 retries with exponential back-off.
4xx responses raise immediately; 5xx are retried.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.models import Tender

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_HOST = "www.nedo.go.jp"
BASE_URL = f"https://{ALLOWED_HOST}"
INDEX_PATH = "/koubo/"

_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def _assert_allowed_url(url: str) -> None:
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


# ---------------------------------------------------------------------------
# HTTP with retry
# ---------------------------------------------------------------------------


def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response:
    _assert_allowed_url(url)
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = client.get(url, timeout=_TIMEOUT_SECONDS, follow_redirects=False)
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()
            if resp.status_code >= 500:
                logger.warning(
                    "nedo: HTTP %d on attempt %d/%d",
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
            logger.warning("nedo: timeout on attempt %d/%d", attempt + 1, _MAX_RETRIES)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_BASE ** attempt)
    raise RuntimeError(f"nedo: all {_MAX_RETRIES} attempts failed") from last_exc


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return dateutil_parser.parse(str(value)).date()
    except (ValueError, OverflowError):
        return None


def _resolve_url(href: str) -> str | None:
    """Resolve a relative href to an absolute URL on the allowed host.

    Returns None if the resolved host would be outside ALLOWED_HOST.
    """
    if not href:
        return None
    resolved = urljoin(BASE_URL, href)
    parsed = urlparse(resolved)
    if parsed.hostname != ALLOWED_HOST:
        return None
    return resolved


def _parse_listing(html: str) -> list[Tender]:
    """Extract tender items from the NEDO koubo index HTML."""
    soup = BeautifulSoup(html, "html.parser")
    tenders: list[Tender] = []

    # NEDO's listing page typically uses a table or a list of <li>/<div> items.
    # The selectors below are best-effort; adjust after inspecting the live page.
    # Strategy: look for links inside a common listing container.
    # Try table rows first, then generic list items.

    rows = soup.select("table.koubo-list tr, ul.koubo-list li, .koubo-item")
    if not rows:
        # Fallback: any <a> tag whose href points to /koubo/ subdirectories
        rows = soup.find_all("a", href=True)

    seen_urls: set[str] = set()

    for element in rows:
        # Handle plain <a> fallback
        if element.name == "a":
            href = element.get("href", "")
            if "/koubo/" not in href and "/research/" not in href:
                continue
            abs_url = _resolve_url(href)
            if abs_url is None or abs_url in seen_urls:
                continue
            title = element.get_text(strip=True)
            if not title:
                continue
            seen_urls.add(abs_url)
            tenders.append(
                Tender(
                    source="nedo",
                    external_id=None,
                    title=title,
                    url=abs_url,
                    description=None,
                    posted_date=None,
                    deadline=None,
                )
            )
            continue

        # Handle table row / list item
        link = element.find("a", href=True)
        if link is None:
            continue
        abs_url = _resolve_url(link["href"])
        if abs_url is None or abs_url in seen_urls:
            continue
        title = link.get_text(strip=True)
        if not title:
            continue

        # Try to extract dates from surrounding text
        text_content = element.get_text(separator=" ")
        posted_date = None
        deadline = None

        # Look for typical Japanese date patterns in the text
        import re

        date_matches = re.findall(r"\d{4}[/\-年]\d{1,2}[/\-月]\d{1,2}", text_content)
        if len(date_matches) >= 1:
            posted_date = _parse_date(date_matches[0])
        if len(date_matches) >= 2:
            deadline = _parse_date(date_matches[1])

        seen_urls.add(abs_url)
        tenders.append(
            Tender(
                source="nedo",
                external_id=None,
                title=title,
                url=abs_url,
                description=None,
                posted_date=posted_date,
                deadline=deadline,
            )
        )

    return tenders


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_recent() -> list[Tender]:
    """Fetch the current NEDO public tender listing.

    Returns
    -------
    list[Tender]
        All tenders found on the listing page.
    """
    index_url = BASE_URL + INDEX_PATH
    with httpx.Client() as client:
        resp = _get_with_retry(client, index_url)
    return _parse_listing(resp.text)
