"""Privacy-preserving security event recording (spec §20, §21).

CONTRACT for later agents: call :func:`record_event` for every security-relevant
occurrence — auth failures, authz denials, rate limiting, quota violations,
policy violations, emergency stops, containment.

PRIVACY RULE: ``details`` must carry counts/limits only (e.g. ``{"limit": 120}``).
NEVER put message, memory or governance content — or credentials — into
events. ``principal`` is an agent id or the literal ``"anonymous"``.

Failure semantics: recording must NEVER break the request path — failures are
swallowed and logged (category only, never details).
"""

import logging

from sqlalchemy.orm import Session

from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import SecurityEvent
from commons_core.infrastructure.time import utc_now

_LOGGER = logging.getLogger(__name__)

#: Principal used when the acting identity is unknown/unauthenticated.
ANONYMOUS = "anonymous"


def record_event(
    session: Session,
    category: SecurityEventCategory | str,
    principal: str,
    details: dict | None = None,
) -> SecurityEvent | None:
    """Record a security event, committing the given session defensively.

    Returns the persisted event, or ``None`` when recording failed (the
    failure is logged with the category only and swallowed).

    Note: this function COMMITS the session it is given. Pass a session
    without pending domain writes (authorization-denial and authentication
    paths satisfy this because they run before any domain mutation).
    """
    try:
        category_value = SecurityEventCategory(category)
    except ValueError:
        _LOGGER.warning(
            "unrecognized security event category=%s; event dropped",
            category if isinstance(category, str) else "invalid",
        )
        return None

    try:
        event = SecurityEvent(
            category=category_value,
            principal=principal[:64],
            occurred_at=utc_now(),
            details=dict(details or {}),
        )
        session.add(event)
        session.commit()
        return event
    except Exception:  # noqa: BLE001 — recording must never fail the request path
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        _LOGGER.warning("failed to record security event category=%s", category_value.value)
        return None
