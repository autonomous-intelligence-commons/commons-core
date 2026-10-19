"""Synthetic source-database generator for backup/restore testing.

Populates a MIGRATED, EMPTY database through the application's own service
functions so every encrypted envelope (memory chain, messages, activity
summary, governance records) is produced by the production code path —
AES-256-GCM with AAD binding via the real ``EnvelopeCipher``.

All content is SYNTHETIC (Stage 1 rule: no real Agent data anywhere).

The returned *expected facts* dict is the controlled synthetic expectation
set the restore battery validates against. It is deliberately kept OUT of
the backup manifest (manifest-consistency correction 2): expectations
describe what the fixture wrote, the manifest describes what the pipeline
measured — different concerns, different lifetimes.
"""

from __future__ import annotations

import secrets

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from commons_core.activities.service import create_activity
from commons_core.agents.service import provision_agent
from commons_core.communication.service import create_channel, send_message
from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import (
    Activity,
    ActivityMember,
    Agent,
    AgentCredential,
    Channel,
    ChannelMember,
    GovernanceRecord,
    MemoryEntry,
    MemoryNamespace,
    MemoryPermission,
    Message,
    MessageAck,
    QuotaCounter,
    ScheduledTask,
    SecurityEvent,
    SystemState,
)
from commons_core.governance.service import create_record as governance_create_record
from commons_core.infrastructure.system_state import EMERGENCY_STOP_KEY, write_system_state
from commons_core.memory.service import write_entry as memory_write_entry
from commons_core.scheduler.service import create_task
from commons_core.security.authentication.tokens import issue_token
from commons_core.security.cryptography.key_store import KeyStore
from commons_core.security.cryptography.service import EnvelopeCipher
from commons_core.security.events import record_event

MAX_BYTES = 65536


def build_cipher(master_keys: str, active_key_id: str = "k1") -> EnvelopeCipher:
    """The application cipher built from EXPLICIT key material (test-only)."""
    store = KeyStore(master_keys, active_key_id, env="test")
    return EnvelopeCipher(store)


def populate_synthetic_source(
    database_url: str,
    master_keys: str,
    active_key_id: str = "k1",
) -> dict:
    """Populate an empty migrated database and return the expected facts."""
    cipher = build_cipher(master_keys, active_key_id)
    engine = create_engine(database_url)

    memory_plaintexts: dict[str, list[str]] = {}
    message_plaintexts: list[str] = []
    governance_plaintexts: list[str] = []
    activity_summary: str | None = None

    with Session(engine) as session:
        admin = provision_agent(session, "backup-synth-admin", is_admin=True)
        agent_b = provision_agent(session, "backup-synth-b")
        agent_c = provision_agent(session, "backup-synth-c")
        issue_token(session, admin.id)
        issue_token(session, agent_b.id)
        issue_token(session, agent_c.id, rotated_from=None)

        ns = MemoryNamespace(
            handle="backup-synth-namespace",
            description="synthetic backup-test namespace",
            created_by=admin.id,
        )
        session.add(ns)
        session.flush()
        chain = [
            "BACKUP-SYNTH plan v1",
            "BACKUP-SYNTH plan v2",
            "BACKUP-SYNTH plan v3 (final)",
        ]
        for payload in chain:
            memory_write_entry(
                session,
                admin,
                ns,
                key="backup-plan",
                payload=payload,
                content_type="text/plain",
                cipher=cipher,
                max_bytes=MAX_BYTES,
            )
        obs = '{"v": 1, "note": "synthetic backup observations"}'
        memory_write_entry(
            session,
            admin,
            ns,
            key="observations",
            payload=obs,
            content_type="application/json",
            cipher=cipher,
            max_bytes=MAX_BYTES,
        )
        memory_plaintexts["backup-plan"] = chain
        memory_plaintexts["observations"] = [obs]

        channel = create_channel(
            session,
            admin,
            handle="backup-synth-channel",
            kind="group",
            initial_members=["backup-synth-b", "backup-synth-c"],
        )
        message_plaintexts = [
            '{"msg": "backup-synth hello 1"}',
            "backup-synth second message",
            "backup-synth reply from b",
        ]
        for payload in message_plaintexts:
            send_message(
                session,
                admin,
                channel,
                content_type="text/plain",
                payload=payload,
                cipher=cipher,
                max_bytes=MAX_BYTES,
            )
        first_message = session.scalars(
            select(Message).order_by(Message.sequence, Message.created_at)
        ).first()
        session.add(MessageAck(agent_id=agent_b.id, message_id=first_message.id))

        activity_summary = "backup-synth activity summary"
        create_activity(
            session,
            admin,
            handle="backup-synth-activity",
            summary=activity_summary,
            initial_members=["backup-synth-b"],
            cipher=cipher,
            max_bytes=MAX_BYTES,
        )

        governance_plaintexts = [
            "backup-synth governance v1",
            "backup-synth governance v2",
        ]
        governance_create_record(
            session,
            admin,
            handle="backup-synth-convention",
            kind="convention",
            content=governance_plaintexts[0],
            cipher=cipher,
            max_bytes=MAX_BYTES,
        )
        governance_create_record(
            session,
            admin,
            handle="backup-synth-convention",
            kind="convention",
            content=governance_plaintexts[1],
            supersedes="backup-synth-convention",
            cipher=cipher,
            max_bytes=MAX_BYTES,
        )

        create_task(
            session,
            admin,
            kind="heartbeat",
            payload={},
            idempotency_key="backup-synth-heartbeat-001",
            max_pending_tasks=1000,
            max_task_attempts=3,
        )
        create_task(
            session,
            agent_b,
            kind="noop",
            payload={},
            idempotency_key="backup-synth-noop-001",
            max_pending_tasks=1000,
            max_task_attempts=3,
        )

        write_system_state(
            session,
            EMERGENCY_STOP_KEY,
            {
                "active": False,
                "set_at": "2026-10-19T00:00:00+00:00",
                "set_by": admin.id,
            },
        )
        record_event(
            session,
            SecurityEventCategory.AUTH_FAILURE,
            admin.id,
            {"count": 1, "source": "backup-synth"},
        )
        import datetime

        session.add(
            QuotaCounter(
                agent_id=admin.id,
                window_start=datetime.datetime(2026, 10, 19, tzinfo=datetime.UTC),
                requests=5,
                messages=3,
            )
        )
        session.commit()

        counts: dict[str, int] = {}
        for model in (
            Agent,
            AgentCredential,
            Channel,
            ChannelMember,
            Message,
            MessageAck,
            MemoryNamespace,
            MemoryPermission,
            MemoryEntry,
            Activity,
            ActivityMember,
            GovernanceRecord,
            ScheduledTask,
            SecurityEvent,
            SystemState,
            QuotaCounter,
        ):
            counts[model.__tablename__] = session.scalar(select(func.count()).select_from(model))
        alembic_version = session.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

        entry = session.scalars(select(MemoryEntry).order_by(MemoryEntry.created_at)).first()
        assert entry.envelope["alg"] == "AES-256-GCM", entry.envelope["alg"]

        facts = {
            "alembic_version": alembic_version,
            "table_counts": counts,
            "memory_plaintexts": memory_plaintexts,
            "namespace_handle": ns.handle,
            "message_plaintexts": message_plaintexts,
            "channel_handle": channel.handle,
            "activity_summary": activity_summary,
            "governance_plaintexts": governance_plaintexts,
            "governance_handle": "backup-synth-convention",
            "emergency_stop": {"active": False},
        }
    engine.dispose()
    return facts


def random_master_key() -> str:
    """A fresh synthetic application master key (never committed, per run)."""
    return f"k1:{secrets.token_hex(32)}"
