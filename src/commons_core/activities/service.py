"""Activities and collaboration infrastructure service (spec §14).

Agent-created activities with an invitation-based membership flow, a strict
status lifecycle and an associated private communication channel. Phase 1
supports SIMULATED collaboration only: the infrastructure never requires an
agent to produce messages, outputs or human-beneficial results (spec §14 —
no forced participation, no mandated field of inquiry).

Lifecycle (strict, documented transition matrix)::

    proposed -> active -> (completed | abandoned)

``completed`` and ``abandoned`` are TERMINAL (no resurrection). Any other
transition is rejected with 409 ``INVALID_STATE``.

Membership states: ``invited`` → (``joined`` | ``declined``). ``declined``
members may be re-invited (the row resets to ``invited`` — documented
decision). Leaving deletes the membership row (there is no ``left`` state);
the CREATOR cannot leave their own activity (they manage it — abandoning the
activity is the creator's exit path). Joining (or declining an invitation)
requires the activity to be ``proposed`` or ``active`` — the invitation flow
is closed on terminal statuses (409 ``INVALID_STATE``).

Associated channel (documented decision): creating an activity auto-creates
a private group channel with handle ``activity-{handle}`` (truncated to the
64-char handle limit — two activities whose handles share the first 55
characters collide and the second create fails with 409 ``CHANNEL_EXISTS``,
an honest, detectable failure). JOINED members receive channel membership
(``can_write``); leaving an activity removes it. Invited (not yet joined)
agents get no channel access.

PRIVACY (spec §14/§20): the activity ``summary`` is sealed with
:class:`EnvelopeCipher` (AAD ``activity:{id}:summary``) BEFORE storage, is
never logged, and never appears in error details or telemetry. Membership
changes are operational events: they may be counted, never enumerated in
telemetry.

Enumeration policy (stronger than channels, per the Phase 1 task contract):
any caller without a membership row (any state) in an existing activity gets
404 ``UNKNOWN_ACTIVITY`` — activity existence is hidden from non-members.
State violations by MEMBERS surface as 403 (authorization, event recorded)
or 409 ``INVALID_STATE`` (lifecycle). Content is never revealed either way.
"""

import uuid as uuidlib
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from commons_core.agents.service import HANDLE_PATTERN, get_agent_by_handle
from commons_core.api.errors import DomainError, InvalidStateError, PayloadTooLargeError
from commons_core.communication import service as communication_service
from commons_core.database.enums import ActivityMemberState, ActivityStatus
from commons_core.database.models import (
    Activity,
    ActivityMember,
    Agent,
    Channel,
    ChannelMember,
    new_uuid,
)
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.engine import activity_manage
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.cryptography import Envelope, EnvelopeCipher

#: Statuses from which joining/responding is still allowed.
OPEN_STATUSES = frozenset({ActivityStatus.PROPOSED, ActivityStatus.ACTIVE})

#: The strict transition matrix (spec §14): proposed→active→(completed|abandoned).
_ACTIVITY_TRANSITIONS: dict[ActivityStatus, frozenset[ActivityStatus]] = {
    ActivityStatus.PROPOSED: frozenset({ActivityStatus.ACTIVE}),
    ActivityStatus.ACTIVE: frozenset({ActivityStatus.COMPLETED, ActivityStatus.ABANDONED}),
    ActivityStatus.COMPLETED: frozenset(),
    ActivityStatus.ABANDONED: frozenset(),
}

#: Prefix (and 64-char truncation) for the activity's associated channel handle.
_CHANNEL_HANDLE_PREFIX = "activity-"


class UnknownActivityError(DomainError):
    """Activity referenced by id or handle does not exist (404).

    Also raised for EXISTING activities when the caller holds no membership
    row — activity existence is hidden from non-members (enumeration policy).
    """

    status_code = 404
    code = "UNKNOWN_ACTIVITY"

    def __init__(self) -> None:
        super().__init__("Activity not found.")


class UnknownAgentError(DomainError):
    """Agent referenced by handle does not exist (404, generic message)."""

    status_code = 404
    code = "UNKNOWN_AGENT"

    def __init__(self) -> None:
        super().__init__("Referenced agent does not exist.")


class ActivityHandleInvalidError(DomainError):
    """Activity handle does not match the handle pattern (422)."""

    status_code = 422
    code = "INVALID_ACTIVITY_HANDLE"

    def __init__(self) -> None:
        super().__init__("Activity handle is invalid (4-64 chars, ^[a-z][a-z0-9-]{3,63}$).")


class ActivityExistsError(DomainError):
    """Activity handle already in use (409)."""

    status_code = 409
    code = "ACTIVITY_EXISTS"

    def __init__(self) -> None:
        super().__init__("Activity handle is already in use.")


class ActivityMemberExistsError(DomainError):
    """Agent already has an invited/joined membership row (409)."""

    status_code = 409
    code = "MEMBER_EXISTS"

    def __init__(self) -> None:
        super().__init__("Agent is already invited or joined in this activity.")


def _resolve_uuid(ref: str) -> str | None:
    try:
        return str(uuidlib.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


def get_activity(session: Session, ref: str) -> Activity | None:
    """Look up an activity by id (UUID) or by handle."""
    activity_id = _resolve_uuid(ref)
    if activity_id is not None:
        activity = session.get(Activity, activity_id)
        if activity is not None:
            return activity
    return session.scalar(select(Activity).where(Activity.handle == ref))


def get_membership(session: Session, activity_id: str, agent_id: str) -> ActivityMember | None:
    """Return the membership row for an agent in an activity, or ``None``."""
    return session.get(ActivityMember, (activity_id, agent_id))


def member_count(session: Session, activity_id: str) -> int:
    """Number of members in the ``joined`` state (the operational roster)."""
    stmt = (
        select(func.count())
        .select_from(ActivityMember)
        .where(
            ActivityMember.activity_id == activity_id,
            ActivityMember.state == ActivityMemberState.JOINED,
        )
    )
    return int(session.scalar(stmt) or 0)


def activity_aad(activity_id: str) -> str:
    """AAD binding an activity summary envelope to its activity (spec §10.2)."""
    return f"activity:{activity_id}:summary"


def decrypt_summary(activity: Activity, cipher: EnvelopeCipher) -> str:
    """Decrypt an activity summary with its AAD (verification included)."""
    return cipher.open(Envelope(**activity.summary_envelope), activity_aad(activity.id))


def _channel_handle_for(handle: str) -> str:
    """Derive the associated channel handle (truncated to 64 chars)."""
    return (_CHANNEL_HANDLE_PREFIX + handle)[:64]


def _sync_channel_membership(
    session: Session, activity: Activity, agent_id: str, *, member: bool
) -> None:
    """Add/remove the agent's membership in the activity's channel."""
    if activity.channel_id is None:
        return
    existing = session.get(ChannelMember, (activity.channel_id, agent_id))
    if member and existing is None:
        session.add(
            ChannelMember(channel_id=activity.channel_id, agent_id=agent_id, can_write=True)
        )
    elif not member and existing is not None:
        session.delete(existing)
    session.flush()


def create_activity(
    session: Session,
    creator: Agent | AuthenticatedAgent,
    *,
    handle: str,
    summary: str | None = None,
    initial_members: list[str] | None = None,
    cipher: EnvelopeCipher,
    max_bytes: int,
    now: datetime | None = None,
) -> Activity:
    """Create an activity; the creator becomes the first (joined) member.

    An associated private group channel (handle ``activity-{handle}``) is
    auto-created and linked; the creator is its ``can_write`` member.
    ``initial_members`` (agent handles) receive ``invited`` membership rows
    but no channel access until they join. The summary is sealed with AAD
    ``activity:{id}:summary`` BEFORE storage. The caller owns the transaction
    (this function flushes).
    """
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise ActivityHandleInvalidError()
    if get_activity(session, handle) is not None:
        raise ActivityExistsError()
    if summary is not None:
        size_bytes = len(summary.encode("utf-8"))
        if size_bytes > max_bytes:
            raise PayloadTooLargeError(size_bytes, max_bytes)

    now = as_utc(now or utc_now())

    # Normalize: unique handles, creator excluded (creator joins as author).
    invitee_handles: list[str] = []
    for member_handle in initial_members or []:
        if member_handle == creator.handle:
            continue
        if member_handle not in invitee_handles:
            invitee_handles.append(member_handle)
    invitees = []
    for member_handle in invitee_handles:
        invitee = get_agent_by_handle(session, member_handle)
        if invitee is None:
            raise UnknownAgentError()
        invitees.append(invitee)

    activity = Activity(id=new_uuid(), handle=handle, created_by=creator.id)
    session.add(activity)
    session.add(
        ActivityMember(
            activity_id=activity.id,
            agent_id=creator.id,
            state=ActivityMemberState.JOINED,
            joined_at=now,
        )
    )
    for invitee in invitees:
        session.add(
            ActivityMember(
                activity_id=activity.id,
                agent_id=invitee.id,
                state=ActivityMemberState.INVITED,
            )
        )

    # Associated channel: created through the communication service so all
    # channel invariants (handle pattern, membership, description) hold.
    channel = communication_service.create_channel(
        session,
        creator,
        handle=_channel_handle_for(handle),
        kind="group",
        description=f"Channel for activity {handle}",
    )
    activity.channel_id = channel.id

    if summary is not None:
        activity.summary_envelope = cipher.seal(summary, activity_aad(activity.id)).model_dump()

    try:
        session.flush()
    except IntegrityError as exc:  # race on the unique activity handle index
        session.rollback()
        raise ActivityExistsError() from exc
    return activity


def invite(
    session: Session,
    actor: Agent | AuthenticatedAgent,
    activity: Activity,
    *,
    agent: Agent,
) -> ActivityMember:
    """Invite an agent to an activity (creator only — ``activity:manage``).

    Defensive creator re-check; the route layer authorizes first (recording
    ``authz_denial`` on denial). An existing ``invited``/``joined`` row is a
    409 ``MEMBER_EXISTS``; a ``declined`` row resets to ``invited``
    (re-invitation is allowed — documented decision).
    """
    if activity.created_by != actor.id:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(activity_manage(activity.id))

    if agent.id == activity.created_by:
        raise ActivityMemberExistsError()

    membership = get_membership(session, activity.id, agent.id)
    if membership is not None:
        if membership.state == ActivityMemberState.DECLINED:
            membership.state = ActivityMemberState.INVITED
            session.flush()
            return membership
        raise ActivityMemberExistsError()

    membership = ActivityMember(
        activity_id=activity.id, agent_id=agent.id, state=ActivityMemberState.INVITED
    )
    session.add(membership)
    session.flush()
    return membership


def respond_invitation(
    session: Session,
    agent: Agent | AuthenticatedAgent,
    activity: Activity,
    *,
    joined: bool,
    now: datetime | None = None,
) -> ActivityMember:
    """Respond to an invitation — ONLY the invited agent themselves.

    Structurally enforced: the route layer always passes the AUTHENTICATED
    agent, so no agent can respond on another's behalf. Requires a pending
    ``invited`` row for the caller and a non-terminal activity status;
    joining grants channel membership, declining does not.
    """
    membership = get_membership(session, activity.id, agent.id)
    if membership is None or membership.state != ActivityMemberState.INVITED:
        raise InvalidStateError("No pending invitation for this agent and activity.")

    if activity.status not in OPEN_STATUSES:
        raise InvalidStateError(
            "Activity is not open for participation (terminal or inactive status)."
        )

    now = as_utc(now or utc_now())
    if joined:
        membership.state = ActivityMemberState.JOINED
        membership.joined_at = now
        session.flush()
        _sync_channel_membership(session, activity, agent.id, member=True)
    else:
        membership.state = ActivityMemberState.DECLINED
        session.flush()
    return membership


def leave(session: Session, agent: Agent | AuthenticatedAgent, activity: Activity) -> None:
    """Withdraw participation: deletes the membership row and channel access.

    Only ``joined`` members can leave; the CREATOR cannot leave their own
    activity (they own its lifecycle — abandoning it is the exit path).
    """
    if activity.created_by == agent.id:
        raise InvalidStateError(
            "The activity creator cannot leave their own activity; abandon it instead."
        )
    membership = get_membership(session, activity.id, agent.id)
    if membership is None or membership.state != ActivityMemberState.JOINED:
        raise InvalidStateError("Only joined members can leave an activity.")

    session.delete(membership)
    session.flush()
    _sync_channel_membership(session, activity, agent.id, member=False)


def transition_status(
    session: Session,
    actor: Agent | AuthenticatedAgent,
    activity: Activity,
    new_status: ActivityStatus,
) -> Activity:
    """Transition the activity status (creator only — ``activity:manage``).

    Strict matrix: ``proposed→active→(completed|abandoned)``; the terminal
    states never transition again. Invalid transitions raise 409
    ``INVALID_STATE``.
    """
    if activity.created_by != actor.id:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(activity_manage(activity.id))

    allowed = _ACTIVITY_TRANSITIONS.get(activity.status, frozenset())
    if new_status not in allowed:
        raise InvalidStateError(
            "Invalid activity status transition "
            f"(from {activity.status.value!r} to {new_status.value!r}); "
            "allowed: proposed→active→(completed|abandoned), terminal states never reopen."
        )
    activity.status = new_status
    activity.updated_at = utc_now()
    session.flush()
    return activity


def list_activities(session: Session, agent_id: str) -> list[Activity]:
    """Activities the agent holds a membership row in (any state) or created."""
    stmt = (
        select(Activity)
        .outerjoin(ActivityMember, ActivityMember.activity_id == Activity.id)
        .where((ActivityMember.agent_id == agent_id) | (Activity.created_by == agent_id))
        .order_by(Activity.handle)
    )
    # Python-side deduplication (identity map) instead of SQL DISTINCT:
    # PostgreSQL has no equality operator for the `json` type, and entity
    # results are unique-ified by primary-key identity anyway.
    return list(session.scalars(stmt).unique())


def activities_channel(session: Session, activity: Activity) -> Channel | None:
    """The activity's associated channel, when linked (operational metadata)."""
    if activity.channel_id is None:
        return None
    return session.get(Channel, activity.channel_id)
