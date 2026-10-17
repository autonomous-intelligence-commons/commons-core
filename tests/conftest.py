"""Shared pytest fixtures: settings, database, app, client, auth helpers."""

import pytest
from fastapi.testclient import TestClient

from commons_core.agents.service import provision_agent
from commons_core.api.app import create_app
from commons_core.config.settings import Settings
from commons_core.database.db import Database
from commons_core.security.authentication.tokens import issue_token


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Hermetic settings: a per-test SQLite file and ephemeral dev keys."""
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path}/commons-test.db",
        master_keys="",
        env="test",
        log_level="WARNING",
    )


@pytest.fixture()
def database(settings) -> Database:
    """A database with the full schema created (create_all — tests only)."""
    db = Database(settings.database_url)
    db.init()
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
