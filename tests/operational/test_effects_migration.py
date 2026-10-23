"""Migration & activation-gate tests for the effect outbox (R02-B2 E1, EFF-19).

Operational acceptance slice ([O] layer):

* alembic ``0002 → 0003`` upgrade and downgrade round-trip (SQLite always;
  PostgreSQL too when ``AIC_TEST_DATABASE_URL`` points at a live server —
  the R02-B1 disposable-scratch-DB pattern);
* EFF-19(b): effects-aware code REFUSES to dispatch against a 0002-only
  schema (``verify_effects_schema`` — the fail-closed startup gate, hard,
  no warn-through);
* EFF-19(c): ``downgrade()`` REFUSES to run while effect rows/events exist
  (the freeze-not-drop rollback doctrine); an empty effect system drops
  cleanly;
* mixed-version coexistence (EFF-19a): at the 0003 schema, the legacy task
  pipeline (which knows nothing about effects) runs unchanged — the new
  tables are inert to pre-0003 binaries, and a 0003-upgraded schema does
  not alter any task-axis behavior (a task-lifecycle smoke proves it).
"""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect

from commons_core.config.settings import Settings
from commons_core.database.db import Database
from commons_core.database.enums import EffectStatus, TaskStatus
from commons_core.database.models import EffectEvent, EffectRequest, ScheduledTask
from commons_core.effects import (
    EffectSchemaError,
    FakeEffectProvider,
    request_effect,
    verify_effects_schema,
)
from commons_core.effects.dispatcher import dispatch_tick
from commons_core.scheduler import service

#: Repository root (migrations/ + alembic.ini live here).
REPO_ROOT = Path(__file__).resolve().parents[2]

T0 = datetime(2030, 1, 1, tzinfo=UTC)

PG_TEST_URL = os.environ.get("AIC_TEST_DATABASE_URL", "").strip()
IS_PG_BACKEND = bool(PG_TEST_URL) and not PG_TEST_URL.lower().startswith("sqlite")


def _run_alembic(url: str, *args: str, expect_failure: bool = False) -> None:
    """Run alembic in a SUBPROCESS against a disposable URL (isolated settings)."""
    env = {**os.environ, "AIC_DATABASE_URL": url, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if expect_failure:
        assert result.returncode != 0, (
            f"alembic {' '.join(args)} unexpectedly SUCCEEDED:\n{result.stdout}"
        )
    else:
        assert result.returncode == 0, (
            f"alembic {' '.join(args)} failed (exit {result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/effects-migration.db"


def _tables(url: str) -> set:
    engine = create_engine(url)
    try:
        return set(sa_inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _seed_effect_row(url: str) -> None:
    """Insert one minimal effect row directly (raw SQL — no ORM needed)."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agents (id, handle, status, is_admin, capabilities, "
                    "comm_formats, created_at, updated_at) "
                    "VALUES ('a1', 'mig-agent', 'active', 0, '[]', '[]', :t, :t)"
                ),
                {"t": T0.isoformat()},
            )
            conn.execute(
                text(
                    "INSERT INTO scheduled_tasks (id, kind, payload, agent_id, status, "
                    "run_at, attempts, max_attempts, lease_generation, created_at, "
                    "updated_at) VALUES ('t1', 'noop', '{}', 'a1', 'running', :t, 1, "
                    "3, 1, :t, :t)"
                ),
                {"t": T0.isoformat()},
            )
            conn.execute(
                text(
                    "INSERT INTO effect_requests (id, task_id, agent_id, "
                    "claim_generation, capability, risk_level, status, operation, "
                    "request_digest, attempts, max_attempts, claim_epoch, created_at, "
                    "updated_at) VALUES ('e1', 't1', 'a1', 1, 'fx.echo', 'low', "
                    "'authorized', '{}', :d, 0, 3, 0, :t, :t)"
                ),
                {"d": "0" * 64, "t": T0.isoformat()},
            )
    finally:
        engine.dispose()


class TestSQLiteMigrationRoundTrip:
    def test_upgrade_creates_outbox_tables(self, tmp_path) -> None:
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        tables = _tables(url)
        assert {"effect_requests", "effect_approvals", "effect_events"} <= tables

    def test_downgrade_empty_drops_tables_and_reupgrade(self, tmp_path) -> None:
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        _run_alembic(url, "downgrade", "0002")
        tables = _tables(url)
        assert "effect_requests" not in tables
        assert "effect_approvals" not in tables
        assert "effect_events" not in tables
        # Scheduled tasks survive untouched (the task axis is orthogonal).
        assert "scheduled_tasks" in tables
        _run_alembic(url, "upgrade", "head")  # idempotent re-upgrade
        assert {"effect_requests", "effect_events"} <= _tables(url)

    def test_downgrade_refuses_non_empty_outbox(self, tmp_path) -> None:
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        _seed_effect_row(url)
        # EFF-19(c): freeze-not-drop doctrine — downgrade FAILS CLOSED.
        _run_alembic(url, "downgrade", "0002", expect_failure=True)
        # The row is still there (the refusal wrote nothing).
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                count = conn.execute(text("SELECT COUNT(*) FROM effect_requests")).scalar()
        finally:
            engine.dispose()
        assert count == 1


class TestActivationGates:
    def test_eff19b_new_code_refuses_0002_schema(self, tmp_path) -> None:
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "0002")  # effects-aware binary vs 0002 schema
        database = Database(url)
        try:
            with database.session() as session:
                with pytest.raises(EffectSchemaError):
                    verify_effects_schema(session)
        finally:
            database.dispose()

    def test_activation_gate_passes_at_0003(self, tmp_path) -> None:
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        database = Database(url)
        try:
            with database.session() as session:
                verify_effects_schema(session)  # no raise — the gate passes
        finally:
            database.dispose()

    def test_eff19a_legacy_task_pipeline_on_0003_schema(self, tmp_path) -> None:
        # Mixed-version coexistence: at the 0003 schema the LEGACY task
        # pipeline (pre-effects binary equivalent: scheduler service only)
        # runs its full lifecycle unchanged — new tables are inert.
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        from commons_core.agents.service import provision_agent

        database = Database(url)
        try:
            with database.session() as session:
                agent = provision_agent(session, "mixed-version-agent")
                task, _created = service.create_task(
                    session,
                    agent,
                    kind="noop",
                    payload={},
                    run_at=T0,
                    max_pending_tasks=1000,
                    max_task_attempts=10,
                )
                session.commit()
                task_id = task.id
            with database.session() as session:
                claimed = service.claim_next(session, T0, lease_seconds=30)
                assert claimed is not None and claimed.id == task_id
                generation = int(claimed.lease_generation)
                session.commit()
                executed = service.execute_claimed(
                    session, claimed, T0, timeout_seconds=0.0, lease_generation=generation
                )
                session.commit()
                assert executed.status == TaskStatus.SUCCEEDED
            # The effects tables exist but the legacy path never touched them.
            with database.session() as session:
                assert session.query(EffectRequest).count() == 0
                assert session.query(EffectEvent).count() == 0
        finally:
            database.dispose()

    def test_full_smoke_at_0003_schema(self, tmp_path) -> None:
        # End-to-end: schema via ALEMBIC (not create_all) + real gateway
        # + dispatcher flow — proves the migration-produced schema exactly
        # matches what the ORM expects (no drift).
        url = _sqlite_url(tmp_path)
        _run_alembic(url, "upgrade", "head")
        database = Database(url)
        cfg = Settings(effects_enabled=True, env="test", master_keys="", log_level="WARNING")
        try:
            from commons_core.agents.service import provision_agent

            with database.session() as session:
                agent = provision_agent(session, "mig-smoke-agent")
                task, _created = service.create_task(
                    session,
                    agent,
                    kind="noop",
                    payload={},
                    run_at=T0,
                    max_pending_tasks=1000,
                    max_task_attempts=10,
                )
                session.commit()
                task_id = task.id
            with database.session() as session:
                task = session.get(ScheduledTask, task_id)
                task.status = TaskStatus.RUNNING
                task.lease_expires_at = T0 + timedelta(seconds=3600)
                task.lease_generation = 1
                row = request_effect(
                    session,
                    task,
                    "fx.echo",
                    {
                        "schema_version": 1,
                        "capability": "fx.echo",
                        "parameters": {"message": "mig"},
                    },
                    now=T0,
                    settings=cfg,
                )
                session.commit()
                effect_id = row.id
            provider = FakeEffectProvider()
            result = dispatch_tick(
                database.session,
                provider,
                now=T0 + timedelta(seconds=1),
                owner="mig",
                settings=cfg,
            )
            assert result.acked_success == 1
            with database.session() as session:
                assert session.get(EffectRequest, effect_id).status == EffectStatus.SUCCEEDED
        finally:
            database.dispose()


@pytest.mark.skipif(not IS_PG_BACKEND, reason="PostgreSQL scratch-DB round-trip (R02-B1 precedent)")
class TestPostgreSQLScratchRoundTrip:
    def test_pg_scratch_upgrade_smoke_downgrade(self) -> None:
        # R02-B1 precedent: an AUTOCOMMIT-created disposable scratch database
        # on the shared test server, fully dropped at the end.
        admin = create_engine(PG_TEST_URL, isolation_level="AUTOCOMMIT")
        scratch = "commons_e1_mig_scratch"
        try:
            with admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
                conn.execute(text(f"CREATE DATABASE {scratch}"))
        except Exception as exc:  # pragma: no cover - environmental
            # e.g. the configured test role lacks CREATEDB (CI setups differ);
            # the PG round-trip is then explicitly identified as an env skip.
            admin.dispose()
            pytest.skip(f"scratch-database creation denied on this server: {exc}")
        finally:
            admin.dispose()
        scratch_url = PG_TEST_URL.rsplit("/", 1)[0] + f"/{scratch}"
        try:
            _run_alembic(scratch_url, "upgrade", "head")
            assert {"effect_requests", "effect_approvals", "effect_events"} <= _tables(scratch_url)
            # Functional smoke on the real PG schema.
            database = Database(scratch_url)
            cfg = Settings(effects_enabled=True, env="test", master_keys="", log_level="WARNING")
            try:
                from commons_core.agents.service import provision_agent

                with database.session() as session:
                    agent = provision_agent(session, "pg-mig-agent")
                    task, _created = service.create_task(
                        session,
                        agent,
                        kind="noop",
                        payload={},
                        run_at=T0,
                        max_pending_tasks=1000,
                        max_task_attempts=10,
                    )
                    session.commit()
                with database.session() as session:
                    task = session.get(ScheduledTask, task.id)
                    task.status = TaskStatus.RUNNING
                    task.lease_expires_at = T0 + timedelta(seconds=3600)
                    task.lease_generation = 1
                    row = request_effect(
                        session,
                        task,
                        "fx.echo",
                        {
                            "schema_version": 1,
                            "capability": "fx.echo",
                            "parameters": {"message": "pg"},
                        },
                        now=T0,
                        settings=cfg,
                    )
                    session.commit()
                    effect_id = row.id
                provider = FakeEffectProvider()
                result = dispatch_tick(
                    database.session,
                    provider,
                    now=T0 + timedelta(seconds=1),
                    owner="pg-mig",
                    settings=cfg,
                )
                assert result.acked_success == 1
                with database.session() as session:
                    assert session.get(EffectRequest, effect_id).status == EffectStatus.SUCCEEDED
            finally:
                database.dispose()
            # Non-empty downgrade refuses (EFF-19c) on PG too.
            _run_alembic(scratch_url, "downgrade", "0002", expect_failure=True)
            # Empty-outbox downgrade works: drain the row, then drop cleanly.
            engine = create_engine(scratch_url)
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM effect_events"))
                conn.execute(text("DELETE FROM effect_requests"))
            engine.dispose()
            _run_alembic(scratch_url, "downgrade", "0002")
            assert "effect_requests" not in _tables(scratch_url)
        finally:
            admin = create_engine(PG_TEST_URL, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
            admin.dispose()
