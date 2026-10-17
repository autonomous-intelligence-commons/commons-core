"""Engine and session management (SQLAlchemy 2.0, sync).

Local development uses SQLite (no PostgreSQL/Docker in this sandbox);
the same code path targets PostgreSQL in production via ``AIC_DATABASE_URL``.
Honest limitation: only SQLite is exercised locally — PostgreSQL behavior
(e.g. ``timestamptz``, native index creation) is inherited from SQLAlchemy
but not verified here.

SQLite specifics:
- ``PRAGMA foreign_keys=ON`` on every new connection (SQLite defaults to OFF).
- ``check_same_thread=False`` so TestClient threads can share the engine.
- in-memory databases use a ``StaticPool`` so one connection backs the whole
  process (otherwise each connection would get a fresh empty database).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from commons_core.config.settings import get_settings
from commons_core.database import models  # noqa: F401  (registers all tables on Base)
from commons_core.database.base import Base


def create_engine_for(url: str) -> Engine:
    """Create an engine with dialect-appropriate pooling and SQLite pragmas."""
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)

    is_memory = ":memory:" in url or "mode=memory" in url
    connect_args = {"check_same_thread": False}
    if is_memory:
        engine = create_engine(url, poolclass=StaticPool, connect_args=connect_args)
    else:
        engine = create_engine(url, connect_args=connect_args)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - trivial
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


class Database:
    """Owns an engine and session factory bound to one database URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_engine_for(url)
        self.sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session; commit on success, roll back on error, always close."""
        session = self.sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def init(self) -> None:
        """Create all tables (dev/test convenience). Canonical path is Alembic."""
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        """Dispose the engine's connection pool."""
        self.engine.dispose()


@lru_cache
def get_database() -> Database:
    """Return the process-wide default database (from settings)."""
    return Database(get_settings().database_url)


def init_db() -> None:
    """Create tables on the default database (dev/test convenience only)."""
    get_database().init()
