"""Privacy-preserving operational telemetry (spec §20).

``collect_telemetry`` aggregates infrastructure state and COUNTERS ONLY.
What it must never contain (enforced by test
``tests/privacy/test_no_plaintext_at_rest.py`` and review):
message bodies, memory content, deliberations, credentials, encryption keys,
agent handles (counts are fine — handles are identifying metadata and are
excluded), channel handles, memory keys, activity handles, task payloads.

Access: admin-only endpoint ``GET /api/v1/admin/telemetry`` (spec §20
"restrict operational-log access"). This module only *reads* the
``system_state`` rows (``emergency_stop`` managed by the admin emergency-
stop endpoints, ``last_heartbeat`` written by the deterministic heartbeat
handler).
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from commons_core import __version__
from commons_core.database.enums import AgentStatus, SecurityEventCategory, TaskStatus
from commons_core.database.models import (
    Agent,
    Channel,
    MemoryEntry,
    Message,
    ScheduledTask,
    SecurityEvent,
)
from commons_core.infrastructure.system_state import (
    LAST_HEARTBEAT_KEY,
    read_system_state,
)
from commons_core.infrastructure.time import as_utc, utc_now

#: Included verbatim in every telemetry payload (spec §20).
CONTENT_POLICY = (
    "Telemetry contains counters and infrastructure state only — never message "
    "bodies, memory content, deliberations, or credentials."
)

_EMERGENCY_STOP_KEY = "emergency_stop"


def _count(session: Session, entity, *conditions) -> int:
    """Count rows of ``entity`` matching optional ``conditions``."""
    stmt = select(func.count()).select_from(entity)
    if conditions:
        stmt = stmt.where(*conditions)
    return int(session.scalar(stmt) or 0)


def _events_last_24h(session: Session, category: SecurityEventCategory, now: datetime) -> int:
    cutoff = now - timedelta(hours=24)
    return _count(
        session,
        SecurityEvent,
        SecurityEvent.category == category,
        SecurityEvent.occurred_at >= cutoff,
    )


def _emergency_stop_active(session: Session) -> bool:
    value = read_system_state(session, _EMERGENCY_STOP_KEY)
    return bool((value or {}).get("active"))


def _tasks_by_status(session: Session) -> dict[str, int]:
    """Task counters per status (operational counts only — never payloads)."""
    counts = {status.value: 0 for status in TaskStatus}
    rows = session.execute(
        select(ScheduledTask.status, func.count()).group_by(ScheduledTask.status)
    ).all()
    for status, count in rows:
        value = status.value if hasattr(status, "value") else str(status)
        counts[value] = int(count)
    return counts


def _last_worker_heartbeat(session: Session) -> str | None:
    """ISO timestamp of the last processed heartbeat task (or ``None``)."""
    value = read_system_state(session, LAST_HEARTBEAT_KEY)
    if not value:
        return None
    stamp = value.get("at")
    return str(stamp) if stamp is not None else None


def collect_telemetry(session: Session, app_state, *, now: datetime | None = None) -> dict:
    """Collect a privacy-preserving telemetry snapshot (spec §20).

    ``app_state`` is the FastAPI application state (needs ``started_at`` and
    ``settings``); ``now`` is injectable for deterministic tests.
    """
    now = as_utc(now or utc_now())
    started_at = getattr(app_state, "started_at", None)
    uptime_seconds = (
        max(0.0, round((now - as_utc(started_at)).total_seconds(), 3))
        if started_at is not None
        else None
    )
    settings = getattr(app_state, "settings", None)
    tasks_by_status = _tasks_by_status(session)

    return {
        "service": "commons-core",
        "version": __version__,
        "env": getattr(settings, "env", None),
        "status": "ok",
        "uptime_seconds": uptime_seconds,
        "counts": {
            "agents_active": _count(session, Agent, Agent.status == AgentStatus.ACTIVE),
            "channels": _count(session, Channel),
            "messages": _count(session, Message),
            "memory_entries": _count(session, MemoryEntry),
            "pending_tasks": tasks_by_status.get(TaskStatus.PENDING.value, 0),
            "tasks_by_status": tasks_by_status,
        },
        "security_events_24h": {
            "auth_failures": _events_last_24h(session, SecurityEventCategory.AUTH_FAILURE, now),
            "authz_denials": _events_last_24h(session, SecurityEventCategory.AUTHZ_DENIAL, now),
            "rate_limited": _events_last_24h(session, SecurityEventCategory.RATE_LIMITED, now),
        },
        "emergency_stop": {"active": _emergency_stop_active(session)},
        "last_worker_heartbeat": _last_worker_heartbeat(session),
        "limits": {
            "max_message_bytes": getattr(settings, "max_message_bytes", None),
            "max_memory_entry_bytes": getattr(settings, "max_memory_entry_bytes", None),
            "max_messages_per_minute": getattr(settings, "max_messages_per_minute", None),
            "rate_limit_per_minute": getattr(settings, "rate_limit_per_minute", None),
            "max_pending_tasks": getattr(settings, "max_pending_tasks", None),
            "max_task_attempts": getattr(settings, "max_task_attempts", None),
        },
        "content_policy": CONTENT_POLICY,
    }
