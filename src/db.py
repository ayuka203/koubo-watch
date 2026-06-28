"""SQLAlchemy 2.0 ORM for koubo-watch.

Engine is a singleton; WAL mode is enabled for safe concurrent reads.
DB path defaults to data/koubo.sqlite but can be overridden via KOUBO_DB_PATH.
KOUBO_DB_PATH must resolve within project_root/data/ to prevent path traversal.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Generator

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedColumn,
    Session,
    mapped_column,
    sessionmaker,
)

from src.models import Tender as TenderSchema

# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


def _get_db_path() -> Path:
    project_root = Path(__file__).resolve().parent.parent
    default = project_root / "data" / "koubo.sqlite"
    raw = os.environ.get("KOUBO_DB_PATH")
    if raw is None:
        path = default
    else:
        path = Path(raw).resolve()
        # Restrict to project_root/data/ to prevent path traversal
        allowed_root = (project_root / "data").resolve()
        try:
            path.relative_to(allowed_root)
        except ValueError:
            raise ValueError(
                f"KOUBO_DB_PATH must be within {allowed_root}, got {path}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_engine():
    """Return the singleton SQLAlchemy engine, creating it on first call."""
    global _engine, _SessionLocal
    if _engine is None:
        db_path = _get_db_path()
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        # Enable WAL mode for safe concurrent reads
        @event.listens_for(_engine, "connect")
        def _set_wal(dbapi_conn, _connection_record):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that yields a database session with automatic commit/rollback."""
    if _SessionLocal is None:
        get_engine()
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# ORM Model
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class TenderORM(Base):
    __tablename__ = "tenders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Tracking columns
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_checked: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Classification columns
    category_nuclear: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category_radiation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    category_grid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Keyword match metadata (JSON-serialised list stored as text)
    keyword_hits: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Stage 2 fields (nullable at this stage)
    energy_system_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_research: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create all tables if they do not already exist."""
    engine = get_engine()
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Upsert helper
# ---------------------------------------------------------------------------


def upsert_tender(
    session: Session,
    tender_in: TenderSchema,
    categories: dict[str, list[str]],
) -> TenderORM:
    """Insert a new tender or update an existing one (matched by URL).

    ``categories`` is the output of ``filter.classify()``, mapping top-level
    category name to a list of matched sub-categories.

    Returns the ORM instance (either new or updated).
    """
    import json

    now = datetime.now(tz=timezone.utc)

    # Derive boolean category flags
    category_nuclear = "原子力" in categories
    category_radiation = "放射線" in categories
    category_grid = "送配電" in categories

    # Flatten all matched sub-categories for keyword_hits
    hits: list[str] = []
    for sub_list in categories.values():
        hits.extend(sub_list)
    keyword_hits_json = json.dumps(hits, ensure_ascii=False) if hits else None

    existing = session.query(TenderORM).filter_by(url=tender_in.url).first()

    if existing is not None:
        # Update mutable fields only
        existing.title = tender_in.title
        existing.description = tender_in.description
        existing.posted_date = tender_in.posted_date
        existing.deadline = tender_in.deadline
        existing.last_checked = now
        existing.category_nuclear = category_nuclear
        existing.category_radiation = category_radiation
        existing.category_grid = category_grid
        existing.keyword_hits = keyword_hits_json
        return existing

    new_row = TenderORM(
        source=tender_in.source,
        external_id=tender_in.external_id,
        title=tender_in.title,
        url=tender_in.url,
        description=tender_in.description,
        posted_date=tender_in.posted_date,
        deadline=tender_in.deadline,
        first_seen=now,
        last_checked=now,
        category_nuclear=category_nuclear,
        category_radiation=category_radiation,
        category_grid=category_grid,
        keyword_hits=keyword_hits_json,
    )
    session.add(new_row)
    session.flush()  # make row visible to subsequent queries in the same session
    return new_row
