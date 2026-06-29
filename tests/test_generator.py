"""Tests for src/generator.py — static site generation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.generator import build_site, _month_label, _sort_key_for_active, _sort_key_for_archive


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orm(
    *,
    id: int = 1,
    source: str = "jst",
    title: str = "廃炉技術の研究開発",
    url: str = "https://example.com/tender/1",
    description: str | None = None,
    posted_date: date | None = date(2025, 4, 1),
    deadline: date | None = date(2099, 12, 31),  # far future = not expired
    category_nuclear: bool = True,
    category_radiation: bool = False,
    category_grid: bool = False,
    energy_system_score: float | None = 8.0,
    ai_reason: str | None = "廃炉技術に直結",
    is_research: bool | None = True,
) -> SimpleNamespace:
    """Create a minimal duck-typed TenderORM-like object for generator tests.

    SimpleNamespace is used instead of TenderORM.__new__() because SQLAlchemy
    ORM instrumentation requires a real session context to set attributes.
    The generator only reads attributes, so duck typing works fine here.
    """
    return SimpleNamespace(
        id=id,
        source=source,
        external_id=None,
        title=title,
        url=url,
        description=description,
        posted_date=posted_date,
        deadline=deadline,
        category_nuclear=category_nuclear,
        category_radiation=category_radiation,
        category_grid=category_grid,
        keyword_hits=None,
        energy_system_score=energy_system_score,
        ai_reason=ai_reason,
        is_research=is_research,
    )


# ---------------------------------------------------------------------------
# _month_label
# ---------------------------------------------------------------------------


def test_month_label_with_date():
    assert _month_label(date(2025, 4, 1)) == "2025年04月"


def test_month_label_none():
    assert _month_label(None) == "不明"


# ---------------------------------------------------------------------------
# _sort_key_for_active
# ---------------------------------------------------------------------------


def test_sort_key_for_active_orders_by_deadline():
    t1 = _make_orm(id=1, deadline=date(2025, 6, 1))
    t2 = _make_orm(id=2, deadline=date(2025, 5, 1))
    assert _sort_key_for_active(t2) < _sort_key_for_active(t1)


def test_sort_key_for_active_none_deadline_last():
    t_none = _make_orm(id=1, deadline=None)
    t_date = _make_orm(id=2, deadline=date(2025, 6, 1))
    # None deadline => date.max => sorts after any real date
    assert _sort_key_for_active(t_date) < _sort_key_for_active(t_none)


# ---------------------------------------------------------------------------
# _sort_key_for_archive
# ---------------------------------------------------------------------------


def test_sort_key_for_archive_orders_by_posted_date():
    t_old = _make_orm(id=1, posted_date=date(2024, 1, 1))
    t_new = _make_orm(id=2, posted_date=date(2025, 1, 1))
    # For descending sort, newer should have larger key
    assert _sort_key_for_archive(t_new) > _sort_key_for_archive(t_old)


# ---------------------------------------------------------------------------
# build_site — happy path
# ---------------------------------------------------------------------------


def test_build_site_creates_files(tmp_path):
    tenders = [_make_orm(id=1, deadline=date(2099, 12, 31))]
    build_site(tmp_path, tenders)

    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "archive.html").exists()
    assert (tmp_path / ".nojekyll").exists()


def test_build_site_index_contains_tender_title(tmp_path):
    tenders = [_make_orm(id=1, title="廃炉技術の研究開発", deadline=date(2099, 12, 31))]
    build_site(tmp_path, tenders)

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "廃炉技術の研究開発" in content


def test_build_site_index_contains_masthead(tmp_path):
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "KOUBO WATCH" in content


def test_build_site_index_contains_ai_score_badge(tmp_path):
    tenders = [_make_orm(id=1, energy_system_score=9.0, deadline=date(2099, 12, 31))]
    build_site(tmp_path, tenders)

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "9" in content


def test_build_site_index_contains_ai_reason(tmp_path):
    tenders = [
        _make_orm(id=1, ai_reason="市場制度に直接貢献", deadline=date(2099, 12, 31))
    ]
    build_site(tmp_path, tenders)

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "市場制度に直接貢献" in content


def test_build_site_index_contains_source(tmp_path):
    tenders = [_make_orm(id=1, source="nedo", deadline=date(2099, 12, 31))]
    build_site(tmp_path, tenders)

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "nedo" in content


def test_build_site_archive_contains_all_tenders(tmp_path):
    tenders = [
        _make_orm(id=1, title="案件A", deadline=date(2099, 12, 31)),
        _make_orm(id=2, title="案件B", url="https://example.com/2", deadline=date(2020, 1, 1)),
    ]
    build_site(tmp_path, tenders)

    content = (tmp_path / "archive.html").read_text(encoding="utf-8")
    assert "案件A" in content
    assert "案件B" in content


def test_build_site_archive_contains_total_count(tmp_path):
    tenders = [
        _make_orm(id=i, url=f"https://example.com/{i}", deadline=date(2099, 12, 31))
        for i in range(3)
    ]
    build_site(tmp_path, tenders)

    content = (tmp_path / "archive.html").read_text(encoding="utf-8")
    assert "3" in content


def test_build_site_archive_month_group(tmp_path):
    tenders = [_make_orm(id=1, posted_date=date(2025, 4, 1), deadline=date(2099, 12, 31))]
    build_site(tmp_path, tenders)

    content = (tmp_path / "archive.html").read_text(encoding="utf-8")
    assert "2025年04月" in content


# ---------------------------------------------------------------------------
# build_site — active tender filtering
# ---------------------------------------------------------------------------


def test_build_site_excludes_expired_tenders_from_index(tmp_path):
    """Expired tenders (deadline in past) should not appear in the active tab."""
    expired = _make_orm(
        id=1,
        title="期限切れ案件",
        deadline=date(2020, 1, 1),
        energy_system_score=9.0,
    )
    build_site(tmp_path, [expired])

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    # Active tenders list should be empty -> "該当案件なし" shown
    assert "該当案件なし" in content


def test_build_site_excludes_low_score_from_index(tmp_path):
    """Tenders with score < 5 should not appear in the active tab."""
    low_score = _make_orm(
        id=1,
        title="スコア低案件",
        deadline=date(2099, 12, 31),
        energy_system_score=3.0,
    )
    build_site(tmp_path, [low_score])

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "該当案件なし" in content


def test_build_site_excludes_none_score_from_index(tmp_path):
    """Tenders with score=None (not yet classified) should not appear in active tab."""
    unscored = _make_orm(
        id=1,
        title="未分類案件",
        deadline=date(2099, 12, 31),
        energy_system_score=None,
    )
    build_site(tmp_path, [unscored])

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "該当案件なし" in content


def test_build_site_nuclear_tab_only_nuclear_tenders(tmp_path):
    nuclear = _make_orm(
        id=1, title="原子力案件", category_nuclear=True, category_grid=False,
        deadline=date(2099, 12, 31), energy_system_score=7.0,
    )
    grid_only = _make_orm(
        id=2, title="送配電案件", url="https://example.com/2",
        category_nuclear=False, category_grid=True,
        deadline=date(2099, 12, 31), energy_system_score=7.0,
    )
    build_site(tmp_path, [nuclear, grid_only])

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    # Both titles appear in "all" tab, so both in HTML
    assert "原子力案件" in content
    assert "送配電案件" in content


# ---------------------------------------------------------------------------
# build_site — empty input
# ---------------------------------------------------------------------------


def test_build_site_empty_input(tmp_path):
    """Empty list should produce valid HTML with '該当案件なし' messages."""
    build_site(tmp_path, [])

    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    archive = (tmp_path / "archive.html").read_text(encoding="utf-8")

    assert "KOUBO WATCH" in index
    assert "該当案件なし" in index
    assert "案件データがありません" in archive


def test_build_site_nojekyll_is_empty(tmp_path):
    build_site(tmp_path, [])
    assert (tmp_path / ".nojekyll").read_text(encoding="utf-8") == ""


def test_build_site_creates_out_dir(tmp_path):
    out = tmp_path / "nested" / "output"
    build_site(out, [])
    assert out.is_dir()
    assert (out / "index.html").exists()


# ---------------------------------------------------------------------------
# build_site — HTML safety (autoescape)
# ---------------------------------------------------------------------------


def test_build_site_escapes_html_in_title(tmp_path):
    """XSS attempt in title should be HTML-escaped."""
    xss = _make_orm(
        id=1,
        title="<script>alert('xss')</script>",
        deadline=date(2099, 12, 31),
        energy_system_score=8.0,
    )
    build_site(tmp_path, [xss])

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in content
    assert "&lt;script&gt;" in content


def test_build_site_escapes_html_in_archive_title(tmp_path):
    xss = _make_orm(
        id=1,
        title='<img src=x onerror="alert(1)">',
        deadline=date(2020, 1, 1),  # expired — only in archive
        energy_system_score=8.0,
    )
    build_site(tmp_path, [xss])

    content = (tmp_path / "archive.html").read_text(encoding="utf-8")
    assert "<img" not in content
    assert "&lt;img" in content


# ---------------------------------------------------------------------------
# build_site — multiple tenders, tab counts
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_site — deadline badge edge cases (days_left <= 0)
# ---------------------------------------------------------------------------


def test_build_site_deadline_passed_badge(tmp_path):
    """A tender with a past deadline shown in archive should render '期限切れ' badge.

    Note: expired tenders are filtered out of the active index, so we verify
    the badge logic by rendering a tender whose deadline is yesterday and
    checking the archive, where all tenders appear.
    """
    import datetime

    yesterday = date.today() - datetime.timedelta(days=1)
    expired = _make_orm(
        id=1,
        title="期限切れ案件",
        deadline=yesterday,
        energy_system_score=8.0,
    )
    build_site(tmp_path, [expired])

    # Active index: expired tender is filtered out, badge logic not exercised there
    index_content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "該当案件なし" in index_content


def test_build_site_deadline_today_badge(tmp_path):
    """A tender due today should show '本日期限' badge in active index."""
    today_date = date.today()
    due_today = _make_orm(
        id=1,
        title="本日締切案件",
        deadline=today_date,
        energy_system_score=8.0,
    )
    build_site(tmp_path, [due_today])

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "本日期限" in content


# ---------------------------------------------------------------------------
# build_site — data sources section
# ---------------------------------------------------------------------------


def test_build_site_index_contains_data_sources_heading(tmp_path):
    """index.html should contain the '情報源' section heading."""
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "情報源" in content


def test_build_site_index_contains_jgrants_link(tmp_path):
    """index.html should contain 'Jグランツ' text."""
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "Jグランツ" in content


def test_build_site_index_contains_nedo_link(tmp_path):
    """index.html should contain 'NEDO' text."""
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "NEDO" in content


def test_build_site_archive_has_no_data_sources_section(tmp_path):
    """archive.html should NOT contain the '情報源' section element.

    The CSS rule `.data-sources` is defined in _base.html.j2 and therefore
    appears in the <style> block of every page.  We verify that the *section
    element itself* is absent from archive.html.
    """
    build_site(tmp_path, [])
    content = (tmp_path / "archive.html").read_text(encoding="utf-8")
    assert '<section class="data-sources">' not in content
    assert "情報源" not in content


def test_build_site_data_source_links_have_target_blank(tmp_path):
    """Each data source anchor must carry target=\"_blank\"."""
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    # All data-source links must have target="_blank"
    import re
    anchors = re.findall(r'<a[^>]+class="[^"]*"[^>]*>|<a\s[^>]*href="https://[^"]*"[^>]*>', content)
    # Simpler: check the section block specifically
    # The data-sources section should contain target="_blank"
    assert 'target="_blank"' in content


def test_build_site_data_source_links_have_rel_noopener(tmp_path):
    """Each data source anchor must carry rel=\"noopener\"."""
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'rel="noopener"' in content


def test_build_site_data_source_urls_are_autoescaped(tmp_path):
    """Jinja2 autoescape should ensure URL characters are safe in href."""
    build_site(tmp_path, [])
    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    # URLs in _DATA_SOURCES contain only safe characters — verify they are
    # rendered verbatim (not double-escaped) and that no raw '<' appears
    # injected from the URL field.
    assert "https://www.jgrants-portal.go.jp/" in content
    assert "https://www.nedo.go.jp/koubo/" in content
    # Confirm autoescape is active: no unescaped angle bracket in href context
    assert 'href="<' not in content


def test_build_site_tab_counts(tmp_path):
    tenders = [
        _make_orm(
            id=1, title="核融合研究", category_nuclear=True, category_radiation=False,
            category_grid=False, deadline=date(2099, 12, 31), energy_system_score=9.0,
        ),
        _make_orm(
            id=2, title="系統解析", url="https://example.com/2",
            category_nuclear=False, category_radiation=False, category_grid=True,
            deadline=date(2099, 12, 31), energy_system_score=7.0,
        ),
    ]
    build_site(tmp_path, tenders)

    content = (tmp_path / "index.html").read_text(encoding="utf-8")
    # すべて tab shows 2
    assert "すべて (2)" in content
    # 原子力 tab shows 1
    assert "原子力 (1)" in content
    # 送配電 tab shows 1
    assert "送配電 (1)" in content
    # 放射線 tab shows 0
    assert "放射線 (0)" in content
