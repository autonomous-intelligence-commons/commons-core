"""Migration & activation-gate tests for lease fencing (R02-B1, matrix R02B-18).

Covers the operational acceptance slice of the revised R02-B test matrix that
can be proven by automated tests:

* alembic ``0001 → 0002`` upgrade and ``0002 → 0001`` downgrade round-trip
  (SQLite batch mode ALWAYS; PostgreSQL too when ``AIC_TEST_DATABASE_URL``
  points at a live server whose test role may create scratch databases);
* the column lands as ``NOT NULL DEFAULT 0`` — pre-existing rows classify to
  generation 0 and legacy (unmapped-ORM) INSERTs that omit the column keep
  working against an upgraded schema (deployment coexistence, design §5.1);
* generation-zero semantics (INV-9): a generation-0 pending row claims to
  generation 1; there is no authorized execution path at generation 0;
* API surface freeze: ``TaskOut``/OpenAPI gain NO new field.

The full six-class row taxonomy, drain-completion queries and the deployment
gates (DG-1..8) are operational procedures beyond unit-test reach; they are
exercised and archived by the R02-B1 harness evidence instead.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect

from commons_core.scheduler import service

#: Repository root (migrations/ + alembic.ini live here).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The fencing column under test.
COLUMN = "lease_generation"

T0 = datetime(2030, 1, 1, tzinfo=UTC)

PG_TEST_URL = os.environ.get("AIC_TEST_DATABASE_URL", "").strip()
IS_PG_BACKEND = bool(PG_TEST_URL) and not PG_TEST_URL.lower().startswith("sqlite")


def _run_alembic(url: str, *args: str) -> None:
    """Run alembic in a SUBPROCESS against a disposable URL (isolated settings)."""
    env = {**os.environ, "AIC_DATABASE_URL": url, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/migration-fencing.db"


def _columns(url: str, table: str = "scheduled_tasks") -> set:
    engine = create_engine(url)
    try:
        return {c["name"] for c in sa_inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# SQLite round-trip (always runs)
# ---------------------------------------------------------------------------


class TestSQLiteMigrationRoundTrip:
    def test_upgrade_downgrade_upgrade_round_trip_with_default_zero(self, tmp_path) -> None:
        url = _sqlite_url(tmp_path)

        # 0001 → 0002 (upgrade head from scratch)
        _run_alembic(url, "upgrade", "head")
        cols = _columns(url)
        assert COLUMN in cols

        # legacy INSERT (unmapped ORM / baseline binary) omits the column —
        # the server-side DEFAULT 0 keeps it working against 0002. A valid
        # agents row is inserted first: batch-mode downgrade re-validates FK
        # constraints while rebuilding the table.
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agents (id, handle, status, is_admin, model_provider,"
                    " capabilities, comm_formats, created_at, updated_at)"
                    " VALUES ('a-mig', 'agent-migration-probe', 'active', 0, NULL, '{}',"
                    " '[]', '2030-01-01 00:00:00', '2030-01-01 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO scheduled_tasks (id, kind, payload, agent_id, status,"
                    " run_at, idempotency_key, attempts, max_attempts, lease_expires_at,"
                    " last_error, created_at, updated_at, finished_at)"
                    " VALUES ('t-mig-1', 'noop', '{}', 'a-mig', 'pending',"
                    " '2030-01-01 00:00:00', NULL, 0, 3, NULL, NULL,"
                    " '2030-01-01 00:00:00', '2030-01-01 00:00:00', NULL)"
                )
            )
            gen = conn.execute(
                text(f"SELECT {COLUMN} FROM scheduled_tasks WHERE id = 't-mig-1'")
            ).scalar()
        engine.dispose()
        assert gen == 0  # classified, not backfilled: never-claimed ⇒ epoch 0

        # 0002 → 0001 (batch-mode column drop on SQLite)
        _run_alembic(url, "downgrade", "-1")
        assert COLUMN not in _columns(url)

        # 0001 → 0002 again (idempotent re-upgrade; existing row classified)
        _run_alembic(url, "upgrade", "head")
        engine = create_engine(url)
        with engine.begin() as conn:
            gen = conn.execute(
                text(f"SELECT {COLUMN} FROM scheduled_tasks WHERE id = 't-mig-1'")
            ).scalar()
        engine.dispose()
        assert gen == 0
        assert COLUMN in _columns(url)


# ---------------------------------------------------------------------------
# PostgreSQL round-trip (runs when the PG backend is configured)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not IS_PG_BACKEND,
    reason="PostgreSQL migration round-trip needs AIC_TEST_DATABASE_URL (live PG)",
)
class TestPostgresMigrationRoundTrip:
    def test_pg_upgrade_downgrade_upgrade_on_scratch_database(self, tmp_path) -> None:
        """Round-trip on a DISPOSABLE scratch database created for this test
        (never the shared test database, whose schema is rebuilt per-test)."""
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(PG_TEST_URL)
        # admin-ish base: same server, scratch DB; created & dropped here.
        scratch_db = f"{parts.path.lstrip('/').split('?')[0]}_mig_r02b1_scratch"
        admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", parts.fragment))
        scratch_url = urlunsplit((parts.scheme, parts.netloc, f"/{scratch_db}", "", parts.fragment))

        from sqlalchemy import create_engine as _ce

        # CREATE/DROP DATABASE cannot run inside a transaction block — the
        # admin side must run in AUTOCOMMIT (disposable scratch DB only).
        admin = _ce(admin_url, isolation_level="AUTOCOMMIT")
        try:
            admin.connect().close()  # connectivity probe
        except Exception as exc:  # pragma: no cover - environmental
            admin.dispose()
            pytest.skip(f"scratch-database creation unavailable on this server: {exc}")
        try:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
                conn.execute(text(f'CREATE DATABASE "{scratch_db}"'))
        except Exception as exc:  # pragma: no cover - environmental
            # e.g. the configured test role lacks CREATEDB (CI setups differ);
            # the PG round-trip is then explicitly identified as an env skip.
            admin.dispose()
            pytest.skip(f"scratch-database creation denied on this server: {exc}")
        admin.dispose()

        try:
            _run_alembic(scratch_url, "upgrade", "head")
            assert COLUMN in _columns(scratch_url)
            # native (non-batch) downgrade on PostgreSQL
            _run_alembic(scratch_url, "downgrade", "-1")
            assert COLUMN not in _columns(scratch_url)
            _run_alembic(scratch_url, "upgrade", "head")
            assert COLUMN in _columns(scratch_url)
        finally:
            admin = _ce(admin_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": scratch_db},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            admin.dispose()


# ---------------------------------------------------------------------------
# Generation-zero semantics against the migration-produced schema
# ---------------------------------------------------------------------------


class TestGenerationZeroSemantics:
    def test_migrated_pending_row_claims_to_epoch_one_and_executes(self, tmp_path) -> None:
        """A generation-0 row created by LEGACY DDL paths classifies to epoch 0
        and the first fencing-aware claim raises it to 1 (INV-9) — generation 0
        never authorizes execution."""
        import uuid

        from commons_core.agents.service import provision_agent
        from commons_core.database.db import Database
        from commons_core.scheduler import registry
        from commons_core.scheduler.service import LeaseSupersededError

        calls = {"n": 0}

        def _noop(session, task) -> None:
            calls["n"] += 1

        registry.register("mig-noop", _noop)
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        task_id = str(uuid.uuid4())  # task ids must be resolvable UUIDs

        db = Database(url)
        try:
            with db.session() as s:
                agent = provision_agent(s, "agent-migration-zero")
                s.commit()
                # raw legacy-style insert (column omitted → DEFAULT 0)
                s.execute(
                    text(
                        "INSERT INTO scheduled_tasks (id, kind, payload, agent_id, status,"
                        " run_at, idempotency_key, attempts, max_attempts,"
                        " lease_expires_at, last_error, created_at, updated_at, finished_at)"
                        " VALUES (:tid, 'mig-noop', '{}', :agent, 'pending',"
                        " '2030-01-01 00:00:00', NULL, 0, 3, NULL, NULL,"
                        " '2030-01-01 00:00:00', '2030-01-01 00:00:00', NULL)"
                    ),
                    {"agent": str(agent.id), "tid": task_id},
                )
                s.commit()

            with db.session() as s:
                claimed = service.claim_next(s, T0, lease_seconds=30)
                assert claimed is not None and str(claimed.id) == task_id
                assert int(claimed.lease_generation) == 1  # 0 → 1 at claim
                # generation 0 can never authorize a task-owned transition
                with pytest.raises(LeaseSupersededError):
                    service.execute_claimed(s, claimed, T0, lease_generation=0)

            with db.session() as s:
                fresh = service.get_task(s, task_id)
                service.execute_claimed(s, fresh, T0, lease_generation=1)
            assert calls["n"] == 1
            with db.session() as s:
                row = s.execute(
                    text(
                        "SELECT status, attempts, lease_generation FROM scheduled_tasks"
                        " WHERE id = :tid"
                    ),
                    {"tid": task_id},
                ).first()
            assert row[0] == "succeeded"
            assert row[1] == 1
            assert row[2] == 1
        finally:
            registry.unregister("mig-noop")
            db.dispose()


# ---------------------------------------------------------------------------
# API surface freeze (R02B-18 assertion 6)
# ---------------------------------------------------------------------------


class TestAPIFreeze:
    def test_taskout_and_openapi_gain_no_fencing_field(self, client: TestClient) -> None:
        """``lease_generation`` is a worker-facing fencing epoch, NOT API data:
        ``TaskOut`` and the OpenAPI schema must not grow a field for it."""
        from commons_core.scheduler.schemas import TaskOut

        assert COLUMN not in TaskOut.model_fields

        schema = client.get("/openapi.json").json()
        task_schemas = json.dumps(schema.get("components", {}).get("schemas", {}))
        assert "lease_generation" not in task_schemas

    def test_task_api_behaviour_unchanged(self, client: TestClient) -> None:
        """End-to-end: authenticated create → (worker executes) → the owner's
        task list carries no new fields and the documented status vocabulary
        is intact."""
        from conftest import auth_headers, provision_and_login

        from commons_core.scheduler import registry

        registry.register("api-freeze-noop", lambda s, t: None)
        try:
            token = provision_and_login(client, "agent-api-freeze")
            headers = auth_headers(token)
            created = client.post(
                "/api/v1/tasks",
                json={"kind": "api-freeze-noop", "payload": {}},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert "lease_generation" not in body  # TaskOut freeze holds end-to-end
            task_id = body["id"]

            from commons_core.scheduler import service as sched_service

            db = client.app.state.database
            with db.session() as s:
                claimed = sched_service.claim_next(
                    s, datetime(2030, 1, 1, tzinfo=UTC), lease_seconds=30
                )
                assert claimed is not None and str(claimed.id) == task_id
                sched_service.execute_claimed(
                    s,
                    claimed,
                    datetime(2030, 1, 1, tzinfo=UTC),
                    lease_generation=int(claimed.lease_generation),
                )

            listed = client.get("/api/v1/tasks", headers=headers)
            assert listed.status_code == 200
            tasks = listed.json()["tasks"]
            mine = [t for t in tasks if t["id"] == task_id]
            assert len(mine) == 1
            assert mine[0]["status"] == "succeeded"
            assert "lease_generation" not in mine[0]  # API freeze holds end-to-end
        finally:
            registry.unregister("api-freeze-noop")
