"""Smoke tests for src/main.py CLI modes.

All external dependencies (fetchers, classifier, generator) are mocked to
avoid network calls and API usage.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_engine(tmp_path, monkeypatch):
    """Give each test a fresh in-project DB so path-traversal guard passes.

    The DB file is removed BEFORE the test starts to avoid stale state from
    previous failed runs (on Windows, WAL files may not clean up on teardown).
    """
    import src.db as db_mod

    project_root = Path(db_mod.__file__).resolve().parent.parent
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    unique = tmp_path.name
    db_file = data_dir / f"test_main_{unique}.sqlite"

    # Pre-test cleanup: remove stale DB files from previous failed runs
    for suffix in ("", "-wal", "-shm"):
        p = db_file.with_suffix(db_file.suffix + suffix) if suffix else db_file
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    monkeypatch.setenv("KOUBO_DB_PATH", str(db_file))

    db_mod._engine = None
    db_mod._SessionLocal = None

    yield

    db_mod._engine = None
    db_mod._SessionLocal = None
    for suffix in ("", "-wal", "-shm"):
        p = db_file.with_suffix(db_file.suffix + suffix) if suffix else db_file
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _make_ns(
    *,
    id: int = 1,
    source: str = "jst",
    title: str = "廃炉技術の研究開発",
    url: str = "https://example.com/tender/1",
    description: str | None = None,
    posted_date: date | None = date(2025, 4, 1),
    deadline: date | None = date(2099, 12, 31),
    category_nuclear: bool = True,
    category_radiation: bool = False,
    category_grid: bool = False,
    energy_system_score: float | None = 8.0,
    ai_reason: str | None = "根拠",
    is_research: bool | None = True,
) -> SimpleNamespace:
    """Create a duck-typed TenderORM-like object for mocking upsert_tender returns."""
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
# --rebuild-site-only
# ---------------------------------------------------------------------------


def test_rebuild_site_only_skips_fetching(tmp_path):
    """--rebuild-site-only should not call fetchers but should call build_site."""
    from src.db import init_db

    init_db()

    with patch("src.main.build_site") as mock_build, \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"), \
         patch("src.main.jst_fetch") as mock_jst:

        sys.argv = ["main", "--rebuild-site-only"]
        from src.main import main
        result = main()

    assert result == 0
    mock_jst.assert_not_called()
    mock_build.assert_called_once()


def test_rebuild_site_only_passes_db_tenders_to_generator(tmp_path):
    """build_site receives TenderORM rows from the DB."""
    from src.db import init_db, get_session, upsert_tender
    from src.models import Tender as TenderSchema

    init_db()

    t = TenderSchema(
        source="jst",
        title="テスト案件",
        url="https://choutatsu.jst.go.jp/test/1",
    )
    with get_session() as sess:
        upsert_tender(sess, t, {"原子力": ["廃炉・廃棄物"]})

    with patch("src.main.build_site") as mock_build, \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main", "--rebuild-site-only"]
        from src.main import main
        main()

    called_tenders = mock_build.call_args[0][1]
    assert len(called_tenders) == 1
    assert called_tenders[0].title == "テスト案件"


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_skips_ai_classification(tmp_path):
    """--dry-run should insert to DB but skip AI classify_tender call."""
    from src.models import Tender as TenderSchema

    tender = TenderSchema(
        source="jst",
        title="廃炉技術",
        url="https://choutatsu.jst.go.jp/test/dry",
    )

    mock_row = _make_ns(id=99, url=tender.url)

    with patch("src.main.jst_fetch", return_value=[tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", return_value={"原子力": ["廃炉・廃棄物"]}), \
         patch("src.main.upsert_tender", return_value=mock_row) as mock_upsert, \
         patch("src.classifier.classify_tender") as mock_ai, \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main", "--dry-run"]
        from src.main import main
        result = main()

    assert result == 0
    mock_upsert.assert_called_once()
    mock_ai.assert_not_called()


# ---------------------------------------------------------------------------
# --classify-pending
# ---------------------------------------------------------------------------


def test_classify_pending_calls_ai_for_null_score(tmp_path):
    """--classify-pending should call classify_tender for rows with NULL score."""
    from src.db import init_db, get_session, upsert_tender as db_upsert
    from src.models import Tender as TenderSchema
    from src.classifier import TenderAssessment

    init_db()

    t = TenderSchema(
        source="jst",
        title="未分類案件",
        url="https://choutatsu.jst.go.jp/test/pending",
    )
    with get_session() as sess:
        db_upsert(sess, t, {"原子力": ["廃炉・廃棄物"]})

    mock_assessment = TenderAssessment(
        energy_system_score=7, reason="関連あり", is_research=False, tender_type="commissioned"
    )

    with patch("src.classifier.classify_tender", return_value=mock_assessment) as mock_ai, \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main", "--classify-pending"]
        from src.main import main
        result = main()

    assert result == 0
    mock_ai.assert_called_once()


def test_classify_pending_skips_already_scored(tmp_path):
    """--classify-pending should skip rows that already have a score."""
    from src.db import init_db, get_session, TenderORM
    from src.models import Tender as TenderSchema
    from src.db import upsert_tender as db_upsert

    init_db()

    t = TenderSchema(
        source="nedo",
        title="既分類案件",
        url="https://www.nedo.go.jp/test/scored",
    )
    with get_session() as sess:
        db_upsert(sess, t, {"送配電": ["系統運用・市場"]})

    with get_session() as sess:
        r = sess.query(TenderORM).filter_by(url=t.url).first()
        r.energy_system_score = 6.0
        r.ai_reason = "既に分類済み"
        r.is_research = False

    with patch("src.classifier.classify_tender") as mock_ai, \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main", "--classify-pending"]
        from src.main import main
        result = main()

    assert result == 0
    mock_ai.assert_not_called()


# ---------------------------------------------------------------------------
# --since validation
# ---------------------------------------------------------------------------


def test_since_invalid_format_returns_1(capsys):
    """Invalid --since format should return exit code 1."""
    sys.argv = ["main", "--backfill", "--since", "not-a-date"]
    from src.main import main
    result = main()
    assert result == 1
    captured = capsys.readouterr()
    assert "YYYY-MM-DD" in captured.err


def test_since_valid_format_accepted(tmp_path):
    """Valid --since format should not cause an error on its own."""
    from src.db import init_db

    init_db()

    with patch("src.main.jst_fetch", return_value=[]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main", "--backfill", "--since", "2025-01-01", "--skip-ai"]
        from src.main import main
        result = main()

    assert result == 0


# ---------------------------------------------------------------------------
# --max-tenders
# ---------------------------------------------------------------------------


def test_max_tenders_limits_processing(tmp_path):
    """--max-tenders should cap the number of tenders processed."""
    from src.models import Tender as TenderSchema

    tenders = [
        TenderSchema(
            source="jst",
            title=f"案件{i}",
            url=f"https://choutatsu.jst.go.jp/test/{i}",
        )
        for i in range(10)
    ]

    processed_urls = []

    def mock_upsert(sess, tender, cats, tender_type=None):
        processed_urls.append(tender.url)
        return _make_ns(id=len(processed_urls), url=tender.url)

    with patch("src.main.jst_fetch", return_value=tenders), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", return_value={"原子力": ["廃炉・廃棄物"]}), \
         patch("src.main.upsert_tender", side_effect=mock_upsert), \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"), \
         patch("src.classifier.classify_tender"):

        sys.argv = ["main", "--max-tenders", "3", "--skip-ai"]
        from src.main import main
        result = main()

    assert result == 0
    assert len(processed_urls) <= 3


# ---------------------------------------------------------------------------
# AI error handling: individual failures should not abort the run
# ---------------------------------------------------------------------------


def test_ai_failure_does_not_abort_run(tmp_path):
    """If classify_tender raises RuntimeError, the run continues and build_site is called."""
    from src.db import init_db
    from src.models import Tender as TenderSchema

    init_db()

    tender = TenderSchema(
        source="jst",
        title="廃炉技術",
        url="https://choutatsu.jst.go.jp/test/ai-fail",
    )

    # Use real upsert_tender so the row is actually in the DB (so _run_ai_for_id
    # can query it), but patch classify_tender to simulate API failure.
    with patch("src.main.jst_fetch", return_value=[tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", return_value={"原子力": ["廃炉・廃棄物"]}), \
         patch("src.classifier.classify_tender",
               side_effect=RuntimeError("API down")), \
         patch("src.main.build_site") as mock_build, \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main"]
        from src.main import main
        result = main()

    # build_site must still be called even when AI fails
    mock_build.assert_called_once()
    assert result == 1, "AI 失敗時は終了コード 1 が返るはず"


# ---------------------------------------------------------------------------
# Excluded tenders are not inserted into DB
# ---------------------------------------------------------------------------


def test_excluded_tenders_not_upserted(tmp_path):
    """Excluded tenders (is_excluded=True) should not call upsert_tender."""
    from src.models import Tender as TenderSchema

    tender = TenderSchema(
        source="jst",
        title="庁舎清掃業務委託",
        url="https://choutatsu.jst.go.jp/test/excluded",
    )

    with patch("src.main.jst_fetch", return_value=[tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": ["清掃"]}), \
         patch("src.main.is_excluded", return_value=True), \
         patch("src.main.upsert_tender") as mock_upsert, \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main", "--dry-run"]
        from src.main import main
        result = main()

    assert result == 0
    mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# no_category stats counter
# ---------------------------------------------------------------------------


def test_no_category_counter_increments(tmp_path, capsys):
    """Tenders that match no category must increment stats['no_category']
    and their title must appear in the stderr summary examples."""
    from src.models import Tender as TenderSchema

    # One tender with no category match, one with a match
    no_cat_tender = TenderSchema(
        source="mext",
        title="庁舎什器の調達",
        url="https://www.mext.go.jp/test/nocat",
    )
    cat_tender = TenderSchema(
        source="jst",
        title="廃炉技術の研究開発",
        url="https://choutatsu.jst.go.jp/test/cat",
    )

    mock_row = _make_ns(id=1, url=cat_tender.url)

    def mock_classify(title, description, keywords):
        # Return empty dict for the no-category tender
        if "庁舎" in title:
            return {}
        return {"原子力": ["廃炉・廃棄物"]}

    with patch("src.main.jst_fetch", return_value=[no_cat_tender, cat_tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", side_effect=mock_classify), \
         patch("src.main.upsert_tender", return_value=mock_row), \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"), \
         patch("src.classifier.classify_tender"):

        sys.argv = ["main", "--skip-ai"]
        from src.main import main
        result = main()

    assert result == 0
    captured = capsys.readouterr()
    # Stats output goes to stderr
    assert "カテゴリなし:     1" in captured.err
    # The title example should appear in stderr output
    assert "庁舎什器" in captured.err


def test_no_category_counter_zero_when_all_match(tmp_path, capsys):
    """When every tender matches a category, no_category should be 0
    and the examples block must not appear."""
    from src.models import Tender as TenderSchema

    tender = TenderSchema(
        source="jst",
        title="廃炉技術の研究開発",
        url="https://choutatsu.jst.go.jp/test/all-match",
    )
    mock_row = _make_ns(id=1, url=tender.url)

    with patch("src.main.jst_fetch", return_value=[tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", return_value={"原子力": ["廃炉・廃棄物"]}), \
         patch("src.main.upsert_tender", return_value=mock_row), \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"), \
         patch("src.classifier.classify_tender"):

        sys.argv = ["main", "--skip-ai"]
        from src.main import main
        result = main()

    assert result == 0
    captured = capsys.readouterr()
    assert "カテゴリなし:     0" in captured.err
    # No examples block should appear
    assert "例:" not in captured.err


# ---------------------------------------------------------------------------
# pre-label subsidy must not skip AI (security監査 MEDIUM対応、案A)
# ---------------------------------------------------------------------------


def test_pre_label_subsidy_still_calls_ai(tmp_path):
    """pre_label が 'subsidy' でも、AI 判定(classify_tender)は呼び出される。

    以前は pre_label=='subsidy' の場合 AI 呼び出し自体をスキップしていたが、
    pre-label はキーワード単一シグナルの仮判定に過ぎず誤判定の訂正機会が
    無くなるため(security監査 MEDIUM)、AI による再確認を必ず行うよう変更した。
    """
    from src.db import init_db
    from src.models import Tender as TenderSchema

    init_db()

    tender = TenderSchema(
        source="jst",
        title="支援金交付事務局運営委託",
        url="https://choutatsu.jst.go.jp/test/pre-label-subsidy",
    )

    from src.classifier import TenderAssessment

    mock_assessment = TenderAssessment(
        energy_system_score=5, reason="確認済み", is_research=False, tender_type="subsidy"
    )

    with patch("src.main.jst_fetch", return_value=[tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", return_value={"原子力": ["廃炉・廃棄物"]}), \
         patch("src.main.pre_label_tender_type", return_value="subsidy"), \
         patch("src.classifier.classify_tender", return_value=mock_assessment) as mock_ai, \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main"]
        from src.main import main
        result = main()

    assert result == 0
    mock_ai.assert_called_once()


def test_pre_label_subsidy_ai_can_override_to_commissioned(tmp_path):
    """pre_label='subsidy' でも AI が commissioned と判定すれば tender_type は上書きされる。

    pre-label 段階の誤判定(subsidy固定化による永久非表示)から自己修正できることを
    確認する回帰テスト。
    """
    from src.db import TenderORM, get_session, init_db
    from src.models import Tender as TenderSchema

    init_db()

    tender = TenderSchema(
        source="jst",
        title="廃炉技術の研究開発（支援金申請事務あり）",
        url="https://choutatsu.jst.go.jp/test/pre-label-override",
    )

    from src.classifier import TenderAssessment

    mock_assessment = TenderAssessment(
        energy_system_score=8, reason="実際は受注型案件", is_research=False,
        tender_type="commissioned",
    )

    with patch("src.main.jst_fetch", return_value=[tender]), \
         patch("src.main.mext_fetch", return_value=[]), \
         patch("src.main.nedo_fetch", return_value=[]), \
         patch("src.main.jgrants_fetch", return_value=[]), \
         patch("src.main.load_keywords",
               return_value={"原子力": {"廃炉・廃棄物": ["廃炉"]}, "exclude": []}), \
         patch("src.main.is_excluded", return_value=False), \
         patch("src.main.classify", return_value={"原子力": ["廃炉・廃棄物"]}), \
         patch("src.main.pre_label_tender_type", return_value="subsidy"), \
         patch("src.classifier.classify_tender", return_value=mock_assessment), \
         patch("src.main.build_site"), \
         patch("src.main.PUBLIC_DIR", tmp_path / "public"):

        sys.argv = ["main"]
        from src.main import main
        result = main()

    assert result == 0

    with get_session() as sess:
        row = sess.query(TenderORM).filter_by(url=tender.url).first()
        assert row.tender_type == "commissioned"
