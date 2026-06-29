"""Jinja2 を使った静的サイト生成。

build_site() は out_dir (例: public/) に以下を出力する:
  - index.html   : 期限未過 × AI スコア 5 以上、タブ切り替え UI
  - archive.html : 全件テーブル、月別グループ
  - .nojekyll    : GitHub Pages が Jekyll を実行しないよう抑制
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.db import TenderORM

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data sources shown on the index page
# ---------------------------------------------------------------------------

_DATA_SOURCES: list[dict[str, str]] = [
    {
        "name": "Jグランツ（補助金電子申請）",
        "url": "https://www.jgrants-portal.go.jp/",
        "note": "中央政府の統合補助金ポータル",
    },
    {
        "name": "NEDO 公募",
        "url": "https://www.nedo.go.jp/koubo/",
        "note": "エネルギー・産業技術総合開発機構",
    },
    {
        "name": "文部科学省 新着",
        "url": "https://www.mext.go.jp/b_menu/news/index.html",
        "note": "省全体の新着情報",
    },
    {
        "name": "JST 調達情報",
        "url": "https://choutatsu.jst.go.jp/",
        "note": "科学技術振興機構（一時的に自動収集を停止中）",
    },
]

# ---------------------------------------------------------------------------
# Template environment
# ---------------------------------------------------------------------------

# Templates are resolved relative to the project root (parent of src/)
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _month_label(d: date | None) -> str:
    """Return 'YYYY年MM月' for grouping, or '不明' when date is None."""
    if d is None:
        return "不明"
    return d.strftime("%Y年%m月")


def _sort_key_for_archive(t: TenderORM) -> tuple:
    """Sort descending by posted_date, then by id descending as tiebreaker."""
    d = t.posted_date or date.min
    pk = t.id or 0
    return (d, pk)


def _sort_key_for_active(t: TenderORM) -> tuple:
    """Sort ascending by deadline (soonest first), then by score descending, then id descending."""
    dl = t.deadline or date.max
    score = -(t.energy_system_score if t.energy_system_score is not None else 0)
    pk = -(t.id or 0)
    return (dl, score, pk)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_site(out_dir: Path, tenders: list[TenderORM]) -> None:
    """out_dir に静的サイトを生成する。

    Parameters
    ----------
    out_dir:
        出力ディレクトリ（存在しない場合は作成される）。
    tenders:
        全 TenderORM オブジェクトのリスト。空でも各 HTML は生成される。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    env = _get_jinja_env()

    # Write .nojekyll
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    today = date.today()
    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Active tenders: deadline not passed AND energy_system_score >= 5 ---
    def _is_active(t: TenderORM) -> bool:
        if t.deadline is not None and t.deadline < today:
            return False
        score = t.energy_system_score
        if score is None or score < 5:
            return False
        return True

    tenders_active = sorted(
        [t for t in tenders if _is_active(t)],
        key=_sort_key_for_active,
    )
    tenders_nuclear = [t for t in tenders_active if t.category_nuclear]
    tenders_radiation = [t for t in tenders_active if t.category_radiation]
    tenders_grid = [t for t in tenders_active if t.category_grid]

    # --- index.html ---
    index_tmpl = env.get_template("index.html.j2")
    index_html = index_tmpl.render(
        tenders_active=tenders_active,
        tenders_nuclear=tenders_nuclear,
        tenders_radiation=tenders_radiation,
        tenders_grid=tenders_grid,
        today=today,
        generated_at=generated_at,
        data_sources=_DATA_SOURCES,
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    logger.info("Generated %s (%d active tenders)", out_dir / "index.html", len(tenders_active))

    # --- archive.html: all tenders, grouped by month (posted_date desc) ---
    sorted_all = sorted(tenders, key=_sort_key_for_archive, reverse=True)

    # Build month groups preserving order
    groups: dict[str, list[TenderORM]] = defaultdict(list)
    month_order: list[str] = []
    for t in sorted_all:
        label = _month_label(t.posted_date)
        if label not in groups:
            month_order.append(label)
        groups[label].append(t)

    month_groups = [(label, groups[label]) for label in month_order]

    archive_tmpl = env.get_template("archive.html.j2")
    archive_html = archive_tmpl.render(
        month_groups=month_groups,
        total_count=len(tenders),
        today=today,
        generated_at=generated_at,
    )
    (out_dir / "archive.html").write_text(archive_html, encoding="utf-8")
    logger.info("Generated %s (%d total tenders)", out_dir / "archive.html", len(tenders))

    logger.info("Site build complete -> %s", out_dir)
