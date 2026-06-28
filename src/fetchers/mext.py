"""Ministry of Education, Culture, Sports, Science and Technology (MEXT) RSS fetcher.

Parses the MEXT procurement RSS feed.

SSRF protection: only ALLOWED_HOST is contacted via httpx; feedparser
receives pre-fetched bytes (resp.content) to avoid its internal URL/file
dispatch logic.
HTTP safety: 30 s timeout, 3 retries with exponential back-off,
follow_redirects=False to prevent redirect-based SSRF.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from urllib.parse import urlparse

import feedparser
import httpx

from src.models import Tender

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_HOST = "www.mext.go.jp"
ALLOWED_HOSTS: frozenset[str] = frozenset({ALLOWED_HOST})
# General news RSS (RDF/RSS 1.0).  Note: this feed contains all news, not
# only public tenders — filter.py's classify() step is responsible for
# discarding non-tender entries (e.g. keeping only titles with "公募"/"公示").
RSS_URL = "https://www.mext.go.jp/b_menu/news/index.rdf"

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


def _fetch_rss_bytes(url: str) -> bytes:
    """Fetch raw RSS XML bytes with retry. Returns raw bytes."""
    _assert_allowed_url(url)
    last_exc: Exception | None = None
    with httpx.Client() as client:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = client.get(
                    url, timeout=_TIMEOUT_SECONDS, follow_redirects=False
                )
                if 400 <= resp.status_code < 500:
                    resp.raise_for_status()
                if resp.status_code >= 500:
                    logger.warning(
                        "mext: HTTP %d on attempt %d/%d",
                        resp.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_BASE ** attempt)
                        continue
                    resp.raise_for_status()
                return resp.content
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "mext: timeout on attempt %d/%d", attempt + 1, _MAX_RETRIES
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_BASE ** attempt)
    raise RuntimeError(f"mext: all {_MAX_RETRIES} attempts failed") from last_exc


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _struct_time_to_date(st) -> date | None:
    """Convert a feedparser time_struct to date, or return None."""
    if st is None:
        return None
    try:
        return datetime(*st[:3]).date()
    except (TypeError, ValueError):
        return None


def _parse_feed(xml_content: bytes | str) -> list[Tender]:
    """Parse RSS XML bytes and return a list of Tender objects.

    ``xml_content`` should be ``bytes`` (resp.content) so feedparser uses its
    bytes dispatch path and avoids treating the value as a URL or file path.
    Passing ``str`` is still accepted for backward-compat with existing tests
    that supply raw XML strings directly.
    """
    feed = feedparser.parse(xml_content)
    if feed.bozo:
        logger.warning(
            "%s: malformed feed (bozo=True): %s", __name__, feed.bozo_exception
        )
    tenders: list[Tender] = []

    for entry in feed.entries:
        title = (getattr(entry, "title", None) or "").strip()
        link = (getattr(entry, "link", None) or "").strip()
        description = (getattr(entry, "summary", None) or "").strip() or None

        if not title or not link:
            continue

        # Validate link is an HTTPS URL on the allowed host with no custom port
        parsed_link = urlparse(link)
        if (
            parsed_link.scheme != "https"
            or parsed_link.hostname not in ALLOWED_HOSTS
            or parsed_link.port is not None
        ):
            logger.warning("mext: skipping URL outside allowlist: %s", link)
            continue

        published = _struct_time_to_date(getattr(entry, "published_parsed", None))
        updated = _struct_time_to_date(getattr(entry, "updated_parsed", None))
        posted_date = published or updated

        tenders.append(
            Tender(
                source="mext",
                external_id=getattr(entry, "id", None) or None,
                title=title,
                url=link,
                description=description,
                posted_date=posted_date,
                deadline=None,
            )
        )

    return tenders


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_recent() -> list[Tender]:
    """Fetch and parse the MEXT procurement RSS feed.

    Returns
    -------
    list[Tender]
        All entries from the feed, most recent first (feedparser order).
    """
    xml_content = _fetch_rss_bytes(RSS_URL)
    return _parse_feed(xml_content)
