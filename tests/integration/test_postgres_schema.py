"""PostgreSQL schema & dialect verification (Phase 1 hardening, Task 1).

Runs ONLY when ``AIC_TEST_DATABASE_URL`` points at a live PostgreSQL server
(the CI ``postgres`` job and local embedded-PG runs). Verifies on the REAL
PostgreSQL information schema (not SQLite assumptions):

- all 16 tables exist;
- enum columns are portable VARCHAR (``native_enum=False`` — no native PG
  ENUM types that would complicate migrations);
- timestamps are ``timestamptz`` (``TIMESTAMP WITH TIME ZONE``);
- JSON columns render as PG ``JSON``;
- ``security_events.id`` is a 64-bit identity (BigInteger variant);
- the critical uniqueness constraints exist (message sequencing, memory
  versioning, governance versioning, task idempotency, handle uniqueness);
- foreign keys are present and enforced;
- the durable-scheduler claim emits ``FOR UPDATE SKIP LOCKED`` on PostgreSQL
  (concurrent workers skip, rather than block on, each other's claims).
"""

import os

import pytest
from sqlalchemy import event, inspect

from commons_core.agents.service import provision_agent
from commons_core.infrastructure.time import utc_now
from commons_core.scheduler import service

PG_URL = os.environ.get("AIC_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL or PG_URL.lower().startswith("sqlite"),
    reason="PostgreSQL-only verification: set AIC_TEST_DATABASE_URL to a live PG URL",
)

EXPECTED_TABLES = {
    "agents",
    "agent_credentials",
    "channels",
    "channel_members",
    "messages",
    "message_acks",
    "memory_namespaces",
    "memory_permissions",
    "memory_entries",
    "activities",
    "activity_members",
    "governance_records",
    "scheduled_tasks",
    "security_events",
    "system_state",
    "quota_counters",
}


def _columns(inspector, table: str) -> dict:
    return {column["name"]: column for column in inspector.get_columns(table)}


def _type_name(column) -> str:
    return str(column["type"]).upper()


class TestPostgreSQLSchema:
    def test_all_tables_exist(self, database) -> None:
        inspector = inspect(database.engine)
        present = set(inspector.get_table_names())
        missing = EXPECTED_TABLES - present
        assert not missing, f"missing tables on PostgreSQL: {sorted(missing)}"

    def test_enum_columns_are_portable_varchar_not_native_enums(self, database) -> None:
        inspector = inspect(database.engine)
        for table, column_name in [
            ("agents", "status"),
            ("agent_credentials", "status"),
            ("channels", "kind"),
            ("activities", "status"),
            ("activity_members", "state"),
            ("governance_records", "kind"),
            ("governance_records", "status"),
            ("scheduled_tasks", "status"),
            ("security_events", "category"),
        ]:
            column = _columns(inspector, table)[column_name]
            assert _type_name(column).startswith("VARCHAR"), (
                f"{table}.{column_name} should be VARCHAR, got {_type_name(column)}"
            )

    def test_timestamps_are_timestamptz(self, database) -> None:
        inspector = inspect(database.engine)
        timestamp_columns = [
            ("messages", "created_at"),
            ("scheduled_tasks", "run_at"),
            ("scheduled_tasks", "lease_expires_at"),
            ("security_events", "occurred_at"),
            ("agents", "created_at"),
        ]
        for table, column_name in timestamp_columns:
            column = _columns(inspector, table)[column_name]
            # SQLAlchemy renders PG timestamptz as TIMESTAMP(timezone=True);
            # str() alone is just "TIMESTAMP", so assert the timezone flag.
            timezone_flag = bool(getattr(column["type"], "timezone", False))
            assert timezone_flag, f"{table}.{column_name} should be timestamptz"

    def test_json_columns_render_as_postgres_json(self, database) -> None:
        inspector = inspect(database.engine)
        assert "JSON" in _type_name(_columns(inspector, "messages")["envelope"])
        assert "JSON" in _type_name(_columns(inspector, "scheduled_tasks")["payload"])
        assert "JSON" in _type_name(_columns(inspector, "system_state")["value"])

    def test_security_event_id_is_64bit(self, database) -> None:
        inspector = inspect(database.engine)
        id_column = _columns(inspector, "security_events")["id"]
        assert "BIGINT" in _type_name(id_column), _type_name(id_column)

    def test_critical_unique_constraints_exist(self, database) -> None:
        inspector = inspect(database.engine)

        def _unique_column_sets(table: str) -> set[tuple[str, ...]]:
            # Column-level ``unique=True, index=True`` renders as a UNIQUE
            # INDEX on PostgreSQL (not a constraint) — union both sources.
            columns = {
                tuple(constraint.get("column_names", []))
                for constraint in inspector.get_unique_constraints(table)
            }
            columns |= {
                tuple(index.get("column_names", []))
                for index in inspector.get_indexes(table)
                if index.get("unique")
            }
            return columns

        assert ("channel_id", "sequence") in _unique_column_sets("messages")
        assert ("namespace_id", "key", "version") in _unique_column_sets("memory_entries")
        assert ("kind", "handle", "version") in _unique_column_sets("governance_records")
        assert ("idempotency_key",) in _unique_column_sets("scheduled_tasks")
        assert ("handle",) in _unique_column_sets("agents")

    def test_foreign_keys_are_declared(self, database) -> None:
        inspector = inspect(database.engine)
        task_fks = {
            fk["constrained_columns"][0]: (fk["referred_table"], fk["referred_columns"][0])
            for fk in inspector.get_foreign_keys("scheduled_tasks")
        }
        assert task_fks.get("agent_id") == ("agents", "id")
        message_fks = {
            fk["constrained_columns"][0] for fk in inspector.get_foreign_keys("messages")
        }
        assert {"channel_id", "sender_id"} <= message_fks


class TestPostgreSQLSchedulerClaim:
    def test_claim_emits_for_update_skip_locked(self, database, session) -> None:
        agent = provision_agent(session, "pg-claim-agent")
        task, _ = service.create_task(
            session,
            agent,
            kind="noop",
            payload={},
            max_pending_tasks=1000,
            max_task_attempts=3,
        )
        statements: list[str] = []

        def _capture(connection, cursor, statement, parameters, context, executemany) -> None:
            statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", _capture)
        try:
            claimed = service.claim_next(session, utc_now(), lease_seconds=60)
        finally:
            event.remove(database.engine, "before_cursor_execute", _capture)

        assert claimed is not None and claimed.id == task.id
        assert any("FOR UPDATE SKIP LOCKED" in statement.upper() for statement in statements), (
            "claim_next must lock candidate rows with FOR UPDATE SKIP LOCKED on PostgreSQL"
        )
