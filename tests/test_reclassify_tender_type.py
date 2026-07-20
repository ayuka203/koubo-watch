"""Tests for scripts/reclassify_tender_type.py.

Focus: the "unknown での上書き禁止" 不変条件が、この移行スクリプト単体でも
守られていること（src.main._run_ai_for_id と同じガード）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.classifier import TenderAssessment
from src.models import Tender as TenderSchema


# ---------------------------------------------------------------------------
# Fixtures: reuse the same in-memory-DB-per-test pattern as tests/test_db.py
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_engine(tmp_path, monkeypatch):
    import src.db as _db_mod_early

    project_root = Path(_db_mod_early.__file__).resolve().parent.parent
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    unique = tmp_path.name
    db_file = data_dir / f"test_{unique}.sqlite"

    for suffix in ("", "-wal", "-shm"):
        p = db_file.with_suffix(db_file.suffix + suffix) if suffix else db_file
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    monkeypatch.setenv("KOUBO_DB_PATH", str(db_file))

    import src.db as db_module

    db_module._engine = None
    db_module._SessionLocal = None

    yield

    db_module._engine = None
    db_module._SessionLocal = None
    for suffix in ("", "-wal", "-shm"):
        p = db_file.with_suffix(db_file.suffix + suffix) if suffix else db_file
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


@pytest.fixture()
def session(reset_engine):
    from src.db import get_session, init_db

    init_db()
    with get_session() as sess:
        yield sess


def _make_tender(**kwargs) -> TenderSchema:
    defaults = dict(
        source="jgrants",
        external_id="JG-001",
        title="廃炉技術開発",
        url="https://api.jgrants-portal.go.jp/subsidies/JG-001",
        description="燃料デブリ取り出し",
        posted_date=date(2024, 4, 1),
        deadline=date.today() + timedelta(days=30),
    )
    defaults.update(kwargs)
    return TenderSchema(**defaults)


# ---------------------------------------------------------------------------
# _select_candidate_ids
# ---------------------------------------------------------------------------


def test_select_candidate_ids_only_unknown(session):
    """tender_type=unknown のレコードのみが対象になり、確定済みは除外される。"""
    from scripts.reclassify_tender_type import _select_candidate_ids
    from src.db import upsert_tender

    unknown_row = upsert_tender(
        session,
        _make_tender(external_id="JG-001", url="https://api.jgrants-portal.go.jp/subsidies/001"),
        {},
        tender_type="unknown",
    )
    commissioned_row = upsert_tender(
        session,
        _make_tender(external_id="JG-002", url="https://api.jgrants-portal.go.jp/subsidies/002"),
        {},
        tender_type="commissioned",
    )
    subsidy_row = upsert_tender(
        session,
        _make_tender(external_id="JG-003", url="https://api.jgrants-portal.go.jp/subsidies/003"),
        {},
        tender_type="subsidy",
    )
    session.commit()

    candidate_ids = _select_candidate_ids()

    assert unknown_row.id in candidate_ids
    assert commissioned_row.id not in candidate_ids
    assert subsidy_row.id not in candidate_ids


def test_select_candidate_ids_excludes_expired_deadline(session):
    """締切超過分は unknown であっても対象外のまま（既存ロジックとの併存確認）。"""
    from scripts.reclassify_tender_type import _select_candidate_ids
    from src.db import upsert_tender

    expired_row = upsert_tender(
        session,
        _make_tender(
            external_id="JG-004",
            url="https://api.jgrants-portal.go.jp/subsidies/004",
            deadline=date.today() - timedelta(days=1),
        ),
        {},
        tender_type="unknown",
    )
    active_row = upsert_tender(
        session,
        _make_tender(
            external_id="JG-005",
            url="https://api.jgrants-portal.go.jp/subsidies/005",
            deadline=date.today() + timedelta(days=1),
        ),
        {},
        tender_type="unknown",
    )
    session.commit()

    candidate_ids = _select_candidate_ids()

    assert expired_row.id not in candidate_ids
    assert active_row.id in candidate_ids


def test_select_candidate_ids_null_deadline_included(session):
    """締切 NULL は unknown であれば対象に含まれる。"""
    from scripts.reclassify_tender_type import _select_candidate_ids
    from src.db import upsert_tender

    row = upsert_tender(
        session,
        _make_tender(
            external_id="JG-006",
            url="https://api.jgrants-portal.go.jp/subsidies/006",
            deadline=None,
        ),
        {},
        tender_type="unknown",
    )
    session.commit()

    assert row.id in _select_candidate_ids()


# ---------------------------------------------------------------------------
# _reclassify_one
# ---------------------------------------------------------------------------


def test_reclassify_one_does_not_overwrite_with_unknown(session):
    """AI 判定が unknown の場合、既存の確定値 (commissioned) を上書きしない。"""
    from scripts.reclassify_tender_type import _reclassify_one
    from src.db import TenderORM, upsert_tender

    row = upsert_tender(session, _make_tender(), {}, tender_type="commissioned")
    session.commit()
    tender_id = row.id

    mock_assessment = TenderAssessment(
        energy_system_score=3, reason="判定不能", is_research=False, tender_type="unknown"
    )
    with patch(
        "scripts.reclassify_tender_type.classify_tender", return_value=mock_assessment
    ):
        result = _reclassify_one(tender_id)

    assert result == "unknown"

    from src.db import get_session

    with get_session() as sess:
        persisted = sess.query(TenderORM).filter_by(id=tender_id).first()
        assert persisted.tender_type == "commissioned"


def test_reclassify_one_writes_confirmed_value(session):
    """AI 判定が commissioned/subsidy の場合は書き込まれる。"""
    from scripts.reclassify_tender_type import _reclassify_one
    from src.db import TenderORM, upsert_tender

    row = upsert_tender(session, _make_tender(), {}, tender_type="unknown")
    session.commit()
    tender_id = row.id

    mock_assessment = TenderAssessment(
        energy_system_score=8, reason="受注型案件", is_research=False, tender_type="commissioned"
    )
    with patch(
        "scripts.reclassify_tender_type.classify_tender", return_value=mock_assessment
    ):
        result = _reclassify_one(tender_id)

    assert result == "commissioned"

    from src.db import get_session

    with get_session() as sess:
        persisted = sess.query(TenderORM).filter_by(id=tender_id).first()
        assert persisted.tender_type == "commissioned"
