"""Database engine/session helpers. SQLite for development and tests, PostgreSQL in production.

All timestamps are stored as naive UTC datetimes (see utcnow)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_factory: sessionmaker | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_database_url(url: str) -> str:
    """Hosting platforms (Render, Scaleway, Heroku-style) hand out postgres:// or postgresql:// URLs; SQLAlchemy
    needs the driver named explicitly for psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine():
    global _engine, _factory
    if _engine is None:
        s = get_settings()
        url = normalize_database_url(s.database_url)
        kwargs: dict = {"future": True}
        if url.startswith("sqlite"):
            s.var_dir.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 10
        _engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def SessionLocal() -> Session:
    get_engine()
    assert _factory is not None
    return _factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Columns added after the first release. create_all() only creates missing tables, so new columns on existing
# tables are added here (SQLite and PostgreSQL both support ADD COLUMN with a default). Existing projects get
# terms_version 'v1': everything they bought keeps its original terms (grandfathering).
ADDED_COLUMNS = [
    ("projects", "terms_version", "VARCHAR(10) DEFAULT 'v1'"),
    ("projects", "dismissed_offers", "JSON"),
    ("entitlements", "family_id", "VARCHAR(32)"),
    ("entitlements", "group_id", "VARCHAR(32)"),
    ("entitlements", "tier", "VARCHAR(20) DEFAULT 'standard'"),
    ("entitlements", "meta", "JSON"),
    ("print_orders", "credit_copies", "INTEGER DEFAULT 0"),
    ("print_orders", "paid_copies", "INTEGER DEFAULT 0"),
    ("print_orders", "page_tier", "INTEGER DEFAULT 0"),
    ("print_orders", "payment_status", "VARCHAR(20) DEFAULT 'prepaid'"),
    ("print_orders", "source", "VARCHAR(20) DEFAULT 'legacy'"),
    ("print_orders", "family_link_id", "VARCHAR(32)"),
    ("print_orders", "shop_order_ids", "JSON"),
    ("print_orders", "meta", "JSON"),
]


def migrate_columns(engine) -> list[str]:
    from sqlalchemy import inspect, text

    added = []
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added.append(f"{table}.{column}")
    return added


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    engine = get_engine()
    migrate_columns(engine)
    Base.metadata.create_all(engine)


def reset_engine() -> None:
    """Used by tests after changing settings."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
