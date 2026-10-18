"""Shared pytest fixtures: settings, database, app, client, auth helpers.

Backend selection (Phase 1 hardening, Task 1):

- Default: per-test SQLite files (lightweight local development).
- ``AIC_TEST_DATABASE_URL`` (e.g. a ``postgresql+psycopg://...`` URL):
  the SAME suite runs against that PostgreSQL server instead — the schema is
  dropped and re-created for every test. Set it in CI (see the
  ``postgres`` job in .github/workflows/ci.yml) or locally:

      AIC_TEST_DATABASE_URL=postgresql+psycopg://commons:pass@localhost:5432/commons \
          python -m pytest -q

  Fail-loud guarantees (never a silent SQLite fallback):
  - a ``sqlite`` value in ``AIC_TEST_DATABASE_URL`` aborts the session;
  - an unreachable/invalid URL aborts the session with the driver error;
  - a session-scoped guard asserts the live dialect really is PostgreSQL.
"""

import os

import pytest
from fastapi.testclient import TestClient

from commons_core.agents.service import provision_agent
from commons_core.api.app import create_app
from commons_core.config.settings import Settings
from commons_core.database.base import Base
from commons_core.database.db import Database
from commons_core.security.authentication.tokens import issue_token

#: Explicit backend override for the whole suite (empty → SQLite default).
PG_TEST_URL = os.environ.get("AIC_TEST_DATABASE_URL", "").strip()


def backend_database_url(tmp_path, name: str = "test.db") -> str:
    """The configured backend URL, or a fresh per-test SQLite file path."""
    return PG_TEST_URL or f"sqlite+pysqlite:///{tmp_path}/{name}"


def reset_schema(database: Database) -> None:
    """Isolate the schema for the next test (any backend).

    SQLite: create_all on the fresh file. PostgreSQL (shared test database):
    drop_all + create_all so tests never observe each other's rows.
    """
    if database.url.startswith("sqlite"):
        database.init()
    else:
        Base.metadata.drop_all(database.engine)
        Base.metadata.create_all(database.engine)


@pytest.fixture(scope="session", autouse=True)
def _backend_guard() -> None:
    """Fail the SESSION (not single tests) on a misconfigured backend."""
    if not PG_TEST_URL:
        return
    if PG_TEST_URL.lower().startswith("sqlite"):
        raise pytest.UsageError(
            "AIC_TEST_DATABASE_URL is set to a SQLite URL — refusing to run the "
            "PostgreSQL integration suite against SQLite (no silent fallback). "
            "Unset the variable for local SQLite tests."
        )
    from sqlalchemy import create_engine, text

    engine = create_engine(PG_TEST_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1
            dialect = engine.dialect.name
    except Exception as exc:  # noqa: BLE001 — re-reported as a session error
        engine.dispose()
        raise pytest.UsageError(
            "AIC_TEST_DATABASE_URL is set but the PostgreSQL server is "
            f"unreachable ({exc.__class__.__name__}: {exc}) — the suite will NOT "
            "silently fall back to SQLite."
        ) from exc
    engine.dispose()
    if dialect != "postgresql":
        raise pytest.UsageError(
            f"AIC_TEST_DATABASE_URL must be a PostgreSQL URL (dialect={dialect!r})."
        )


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Hermetic settings: ephemeral dev keys; SQLite file or the configured PG URL."""
    database_url = PG_TEST_URL if PG_TEST_URL else f"sqlite+pysqlite:///{tmp_path}/commons-test.db"
    return Settings(
        database_url=database_url,
        master_keys="",
        env="test",
        log_level="WARNING",
    )


@pytest.fixture()
def database(settings) -> Database:
    """A database with the full schema for this test (isolated from other tests).

    SQLite: a brand-new file per test (tmp_path). PostgreSQL: the shared test
    database with the schema DROPPED and re-created per test (drop_all →
    create_all), so tests never observe each other's rows.
    """
    db = Database(settings.database_url)
    reset_schema(db)
    yield db
    db.dispose()


@pytest.fixture()
def session(database):
    """A committed-on-exit session for direct data setup."""
    with database.session() as db_session:
        yield db_session


@pytest.fixture()
def app(settings, database):
    """The full application wired to the test database."""
    application = create_app(settings=settings, database=database)
    yield application


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def session_agent(session):
    """A provisioned ordinary agent for unit-level tests."""
    return provision_agent(session, "agent-fixture")


def provision_and_login(client: TestClient, handle: str, **kwargs) -> str:
    """Provision an agent with a credential; return the raw bearer token."""
    database = client.app.state.database
    with database.session() as db_session:
        agent = provision_agent(db_session, handle, **kwargs)
        issued = issue_token(db_session, agent.id)
    return issued.raw_token


def auth_headers(token: str) -> dict:
    """Authorization headers for a raw token."""
    return {"Authorization": f"Bearer {token}"}
