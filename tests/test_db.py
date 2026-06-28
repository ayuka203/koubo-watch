"""Tests for src/db.py — SQLAlchemy ORM and upsert logic."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.models import Tender as TenderSchema


# ---------------------------------------------------------------------------
# Fixtures: use an in-memory SQLite DB for isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_engine(tmp_path, monkeypatch):
    """Each test gets a fresh DB engine.

    KOUBO_DB_PATH is set to a file inside the project's data/ directory so
    that the path-traversal guard in _get_db_path() accepts it.  A unique
    filename derived from tmp_path avoids collisions between parallel tests.

    The DB file is removed BEFORE the test starts to avoid stale state from
    previous failed runs (on Windows, WAL files may not clean up on teardown).
    """
    import src.db as _db_mod_early
    project_root = Path(_db_mod_early.__file__).resolve().parent.parent
    data_dir = project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Use the last two components of tmp_path as a unique suffix
    unique = tmp_path.name
    db_file = data_dir / f"test_{unique}.sqlite"

    # Pre-test cleanup: remove stale DB files from previous failed runs
    for suffix in ("", "-wal", "-shm"):
        p = db_file.with_suffix(db_file.suffix + suffix) if suffix else db_file
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    monkeypatch.setenv("KOUBO_DB_PATH", str(db_file))

    # Reset the singleton so the env var takes effect
    import src.db as db_module

    db_module._engine = None
    db_module._SessionLocal = None

    yield

    # Teardown: reset singleton and remove the per-test DB file
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


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_creates_table(session):
    from src.db import TenderORM

    # If the table doesn't exist, a query would raise; here it should work.
    count = session.query(TenderORM).count()
    assert count == 0


# ---------------------------------------------------------------------------
# upsert_tender — insert
# ---------------------------------------------------------------------------


def _make_tender(**kwargs) -> TenderSchema:
    defaults = dict(
        source="jgrants",
        external_id="JG-001",
        title="廃炉技術開発",
        url="https://api.jgrants-portal.go.jp/subsidies/JG-001",
        description="燃料デブリ取り出し",
        posted_date=date(2024, 4, 1),
        deadline=date(2024, 5, 31),
    )
    defaults.update(kwargs)
    return TenderSchema(**defaults)


def test_upsert_insert_new(session):
    from src.db import TenderORM, upsert_tender

    tender = _make_tender()
    categories = {"原子力": ["廃炉・廃棄物"]}
    row = upsert_tender(session, tender, categories)

    assert session.query(TenderORM).count() == 1
    assert row.title == "廃炉技術開発"
    assert row.category_nuclear is True
    assert row.category_radiation is False
    assert row.category_grid is False
    hits = json.loads(row.keyword_hits)
    assert "廃炉・廃棄物" in hits


def test_upsert_sets_first_seen_and_last_checked(session):
    from src.db import upsert_tender

    before = datetime.now(tz=timezone.utc)
    tender = _make_tender()
    row = upsert_tender(session, tender, {})
    after = datetime.now(tz=timezone.utc)

    # Both timestamps should be within the test window
    assert before <= row.first_seen <= after
    assert before <= row.last_checked <= after


# ---------------------------------------------------------------------------
# upsert_tender — update (same URL)
# ---------------------------------------------------------------------------


def test_upsert_update_existing(session):
    from src.db import TenderORM, upsert_tender

    tender = _make_tender(title="旧タイトル")
    upsert_tender(session, tender, {})

    # Update with new title but same URL
    updated = _make_tender(title="新タイトル")
    upsert_tender(session, updated, {"原子力": ["次世代炉・核融合"]})

    rows = session.query(TenderORM).all()
    assert len(rows) == 1
    assert rows[0].title == "新タイトル"
    assert rows[0].category_nuclear is True


def test_upsert_update_preserves_first_seen(session):
    from src.db import upsert_tender

    tender = _make_tender()
    first_row = upsert_tender(session, tender, {})
    original_first_seen = first_row.first_seen

    # Upsert again (update)
    upsert_tender(session, tender, {})
    assert first_row.first_seen == original_first_seen


# ---------------------------------------------------------------------------
# upsert_tender — category flags
# ---------------------------------------------------------------------------


def test_upsert_all_category_flags(session):
    from src.db import upsert_tender

    tender = _make_tender()
    categories = {
        "原子力": ["廃炉・廃棄物"],
        "放射線": ["環境・除染"],
        "送配電": ["系統運用・市場"],
    }
    row = upsert_tender(session, tender, categories)
    assert row.category_nuclear is True
    assert row.category_radiation is True
    assert row.category_grid is True


def test_upsert_no_category(session):
    from src.db import upsert_tender

    tender = _make_tender()
    row = upsert_tender(session, tender, {})
    assert row.category_nuclear is False
    assert row.category_radiation is False
    assert row.category_grid is False
    assert row.keyword_hits is None


# ---------------------------------------------------------------------------
# upsert_tender — keyword_hits JSON
# ---------------------------------------------------------------------------


def test_upsert_keyword_hits_multiple(session):
    from src.db import upsert_tender

    tender = _make_tender()
    categories = {
        "原子力": ["廃炉・廃棄物", "次世代炉・核融合"],
        "送配電": ["系統運用・市場"],
    }
    row = upsert_tender(session, tender, categories)
    hits = json.loads(row.keyword_hits)
    assert set(hits) == {"廃炉・廃棄物", "次世代炉・核融合", "系統運用・市場"}


# ---------------------------------------------------------------------------
# Multiple distinct tenders
# ---------------------------------------------------------------------------


def test_two_distinct_tenders(session):
    from src.db import TenderORM, upsert_tender

    t1 = _make_tender(url="https://api.jgrants-portal.go.jp/subsidies/001", external_id="001")
    t2 = _make_tender(url="https://api.jgrants-portal.go.jp/subsidies/002", external_id="002")
    upsert_tender(session, t1, {})
    upsert_tender(session, t2, {})

    assert session.query(TenderORM).count() == 2


# ---------------------------------------------------------------------------
# get_session context manager
# ---------------------------------------------------------------------------


def test_get_session_commits_on_success(reset_engine):
    from src.db import TenderORM, get_session, init_db, upsert_tender

    init_db()
    tender = _make_tender()

    with get_session() as sess:
        upsert_tender(sess, tender, {})

    # Verify the row was committed in a new session
    with get_session() as sess2:
        count = sess2.query(TenderORM).count()
    assert count == 1


def test_get_session_rollback_on_exception(reset_engine):
    from src.db import TenderORM, get_session, init_db, upsert_tender

    init_db()
    tender = _make_tender()

    with pytest.raises(RuntimeError, match="test rollback"):
        with get_session() as sess:
            upsert_tender(sess, tender, {})
            raise RuntimeError("test rollback")

    # Row should not have been committed
    with get_session() as sess2:
        count = sess2.query(TenderORM).count()
    assert count == 0


# ---------------------------------------------------------------------------
# _get_db_path — path traversal prevention
# ---------------------------------------------------------------------------


def test_db_path_traversal_rejected(tmp_path, monkeypatch):
    """KOUBO_DB_PATH outside data/ must raise ValueError."""
    import src.db as db_module

    db_module._engine = None
    db_module._SessionLocal = None

    evil_path = tmp_path / "evil.sqlite"
    monkeypatch.setenv("KOUBO_DB_PATH", str(evil_path))

    with pytest.raises(ValueError, match="KOUBO_DB_PATH must be within"):
        db_module._get_db_path()

    # Restore
    db_module._engine = None
    db_module._SessionLocal = None


def test_db_path_within_data_allowed(tmp_path, monkeypatch):
    """KOUBO_DB_PATH inside project data/ resolves without error."""
    import src.db as db_module
    from pathlib import Path

    db_module._engine = None
    db_module._SessionLocal = None

    # Compute the real allowed_root the same way db.py does
    project_root = Path(db_module.__file__).resolve().parent.parent
    allowed_root = (project_root / "data").resolve()
    allowed_root.mkdir(parents=True, exist_ok=True)
    valid_path = allowed_root / "test_valid.sqlite"

    monkeypatch.setenv("KOUBO_DB_PATH", str(valid_path))
    path = db_module._get_db_path()
    assert path == valid_path

    db_module._engine = None
    db_module._SessionLocal = None
