"""JST (Japan Science and Technology Agency) 調達情報 HTML スクレイパー。

旧 RSS (``rss.php``) は廃止されており（robots.txt が ``/rss/`` 配下を
Disallow、実 URL ``rss/new_j.rdf`` も 404）、現在の一覧ページは
``koukoku_link.html`` の静的 HTML。各案件へのリンクは
``kankouju/NN_YYYY_NN.html`` 形式で、実体は
``<meta http-equiv="refresh">`` で ``NN-YYYY-NN`` 形式のクリーン URL に
リダイレクトするだけの薄いページ。クリーン URL は href から機械的に
組み立てられるため、中間ページの fetch は行わず詳細ページを直接叩く
（一覧 1 回 + 詳細 N 回、で完結する）。

投稿日・締切日は一覧ページには無く、詳細ページ内の dl/dt/dd 構造からのみ
取得できる（NEDO と異なる点）。詳細ページの日付は和暦表記
（例: 令和８年７月１７日）なので、NFKC 正規化で全角数字を半角化してから
和暦→西暦変換する。

SSRF protection: only ALLOWED_HOST is contacted; both the listing URL and
each constructed detail URL are validated before fetching.
HTTP safety: 30 s timeout, 3 retries with exponential back-off,
follow_redirects=False to prevent redirect-based SSRF.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import date
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.models import Tender

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_HOST = "choutatsu.jst.go.jp"
ALLOWED_HOSTS: frozenset[str] = frozenset({ALLOWED_HOST})
BASE_URL = f"https://{ALLOWED_HOST}"
LISTING_PATH = "/koukoku_link.html"
# Legacy constant kept for backward-compat with anything importing it;
# the RSS feed itself is defunct (see module docstring).
RSS_URL = f"https://{ALLOWED_HOST}/rss.php"

_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0

# Matches href values like "kankouju/47_2026_16.html" and captures the three
# numeric components used to build the clean detail URL "47-2026-16".
_KANKOUJU_HREF_RE = re.compile(r"kankouju/(\d+)_(\d+)_(\d+)\.html$")

# Japanese era -> Gregorian year offset (Gregorian year = offset + era year).
# Only eras plausible for tenders on a currently-operating site are listed.
_ERA_YEAR_OFFSET: dict[str, int] = {
    "令和": 2018,  # 令和1年 = 2019
    "平成": 1988,  # 平成1年 = 1989
    "昭和": 1925,  # 昭和1年 = 1926
}
_WAREKI_DATE_RE = re.compile(
    r"(令和|平成|昭和)(\d+)年(\d+)月(\d+)日"
)


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
                    "jst: HTTP %d on attempt %d/%d",
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
            logger.warning("jst: timeout on attempt %d/%d", attempt + 1, _MAX_RETRIES)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_BACKOFF_BASE ** attempt)
    raise RuntimeError(f"jst: all {_MAX_RETRIES} attempts failed") from last_exc


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def _clean_detail_url(href: str) -> str | None:
    """Build the clean detail-page URL from a ``kankouju/NN_YYYY_NN.html`` href.

    Returns None if *href* does not match the expected pattern.  The
    resulting URL is validated against ALLOWED_HOST by the caller (via
    ``_get_with_retry`` / ``_assert_allowed_url``) before any request is made.
    """
    match = _KANKOUJU_HREF_RE.search(href)
    if match is None:
        return None
    a, b, c = match.groups()
    return f"{BASE_URL}/{a}-{b}-{c}"


# ---------------------------------------------------------------------------
# Date parsing (和暦 -> date)
# ---------------------------------------------------------------------------


def _parse_wareki_date(text: str) -> date | None:
    """Parse a Japanese-era date string (e.g. '令和８年７月１７日(金)') to date.

    Full-width digits are normalised to ASCII via NFKC before matching.
    Returns None if no recognised era/date pattern is found or the resulting
    date is invalid (e.g. day 32).
    """
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    match = _WAREKI_DATE_RE.search(normalized)
    if match is None:
        return None
    era, year_s, month_s, day_s = match.groups()
    offset = _ERA_YEAR_OFFSET.get(era)
    if offset is None:
        return None
    try:
        return date(offset + int(year_s), int(month_s), int(day_s))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Listing page parsing
# ---------------------------------------------------------------------------


def _parse_listing(html: str) -> list[tuple[str, str]]:
    """Extract (detail_url, title) pairs from the koukoku_link.html listing.

    Only anchors whose href matches the kankouju/NN_YYYY_NN.html pattern are
    kept; duplicates (same detail_url) are dropped.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        detail_url = _clean_detail_url(a["href"])
        if detail_url is None:
            continue
        title = a.get_text(strip=True)
        if not title or detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)
        items.append((detail_url, title))

    return items


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------


def _extract_dd_text(soup: BeautifulSoup, dt_text: str, *, last: bool = False) -> str | None:
    """Return the text of the <dd> sibling of the <dt> whose text == dt_text.

    If multiple matching <dt> elements exist, the first is used unless
    ``last=True``.
    """
    candidates = soup.find_all("dt", string=lambda s: s and s.strip() == dt_text)
    if not candidates:
        return None
    dt = candidates[-1] if last else candidates[0]
    dd = dt.find_next_sibling("dd")
    if dd is None:
        return None
    return dd.get_text(" ", strip=True)


def _parse_detail(html: str, url: str, title: str) -> Tender:
    """Parse one JST detail page into a Tender.

    posted_date is taken from the "公告日" dt/dd pair (first occurrence).
    deadline is taken from the *last* "期限" dt/dd pair in the document —
    the detail page lists several successive deadlines (質問書提出期限,
    参加意思確認書提出期限, 応募資料提出期限 etc.) and the last one is the
    final, practically binding deadline for participation.
    external_id is derived from the URL's "NN-YYYY-NN" suffix rather than
    the <title> tag, since that suffix is stable and doesn't depend on the
    page's internal formatting.
    """
    soup = BeautifulSoup(html, "html.parser")

    posted_date = None
    posted_text = _extract_dd_text(soup, "公告日")
    if posted_text:
        posted_date = _parse_wareki_date(posted_text)

    deadline = None
    deadline_text = _extract_dd_text(soup, "期限", last=True)
    if deadline_text:
        deadline = _parse_wareki_date(deadline_text)

    external_id = url.rstrip("/").rsplit("/", 1)[-1] or None

    return Tender(
        source="jst",
        external_id=external_id,
        title=title,
        url=url,
        description=None,
        posted_date=posted_date,
        deadline=deadline,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_recent() -> list[Tender]:
    """Fetch the JST procurement listing and each linked detail page.

    Total request count is 1 (listing) + N (one per detail page); no
    intermediate ``kankouju/*.html`` fetch is needed since the clean detail
    URL can be built directly from the listing href. No explicit sleep is
    inserted between detail-page requests — the existing NEDO/MEXT fetchers
    do not throttle either, and JST typically posts well under 100 items at
    a time, so load is comparable to a human browsing the site in one
    sitting. If this ever needs to scale up, add a short sleep here.

    Individual detail-page fetch/parse failures are logged and skipped so
    one bad page does not abort the whole run.

    Returns
    -------
    list[Tender]
        All successfully parsed tenders from the current listing.
    """
    listing_url = BASE_URL + LISTING_PATH
    tenders: list[Tender] = []

    with httpx.Client() as client:
        listing_resp = _get_with_retry(client, listing_url)
        items = _parse_listing(listing_resp.text)

        for detail_url, title in items:
            try:
                detail_resp = _get_with_retry(client, detail_url)
            except Exception as exc:
                logger.warning("jst: detail fetch failed url=%s: %s", detail_url, exc)
                continue
            try:
                tender = _parse_detail(detail_resp.text, detail_url, title)
            except Exception as exc:
                logger.warning("jst: detail parse failed url=%s: %s", detail_url, exc)
                continue
            tenders.append(tender)

    return tenders
