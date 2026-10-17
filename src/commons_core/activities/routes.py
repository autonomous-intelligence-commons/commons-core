"""Activities API routes (spec §14, §27 — machine-first, versioned).

Authorization pattern: resolve the activity FIRST (404 ``UNKNOWN_ACTIVITY``
for non-existent refs AND for existing activities the caller holds no
membership row in — activity existence is hidden from non-members, the
documented enumeration policy). Creator-only operations (invite, status
transitions) then call ``authorize()`` with ``activity:manage:{id}`` — the
server policy enforces ownership (recording ``authz_denial`` events on
denial, mapped to a generic 403). Lifecycle violations raise 409
``INVALID_STATE``. Content is never revealed in either case.

PRIVACY: activity summaries are sealed before storage and decrypted ONLY for
callers with a membership row (AAD-verified); they are never logged, never
in error details, never in telemetry.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from commons_core.activities import service
from commons_core.activities.schemas import (
    ActivityCreate,
    ActivityDetailOut,
    ActivityListOut,
    ActivityMembershipOut,
    ActivityOut,
    ActivityStatusPatch,
    InviteRequest,
    JoinDecision,
)
from commons_core.api.deps import (
    get_app_settings,
    get_cipher,
    get_session,
    require_not_paused,
)
from commons_core.config.settings import Settings
from commons_core.database.enums import ActivityStatus
from commons_core.database.models import Activity, Agent
from commons_core.security.authentication.dependencies import (
    require_authenticated_agent,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.dependencies import require_permission
from commons_core.security.authorization.engine import (
    Permission,
    activity_manage,
    authorize,
)
from commons_core.security.cryptography import EnvelopeCipher

router = APIRouter(prefix="/api/v1", tags=["activities"])


def _creator_handle(session: Session, activity: Activity) -> str:
    creator = session.get(Agent, activity.created_by)
    return creator.handle if creator is not None else "unknown"


def _status_value(activity: Activity) -> str:
    return str(activity.status.value) if hasattr(activity.status, "value") else str(activity.status)


def _activity_out(session: Session, activity: Activity) -> ActivityOut:
    """Serialize activity metadata (joined-member roster count, no content)."""
    return ActivityOut(
        id=activity.id,
        handle=activity.handle,
        status=_status_value(activity),
        created_by=_creator_handle(session, activity),
        created_at=activity.created_at,
        updated_at=activity.updated_at,
        channel_id=activity.channel_id,
        member_count=service.member_count(session, activity.id),
    )


def _activity_detail(
    session: Session,
    activity: Activity,
    agent_id: str,
    *,
    cipher: EnvelopeCipher | None = None,
) -> ActivityDetailOut:
    """Serialize metadata plus the caller's membership and decrypted summary.

    ``summary`` is only produced for callers with a membership row (enforced
    upstream by ``_require_activity``) and only when a summary exists.
    """
    membership = service.get_membership(session, activity.id, agent_id)
    summary = None
    if activity.summary_envelope is not None and cipher is not None:
        summary = service.decrypt_summary(activity, cipher)
    return ActivityDetailOut(
        **_activity_out(session, activity).model_dump(),
        my_membership=(
            ActivityMembershipOut(
                state=str(membership.state.value)
                if hasattr(membership.state, "value")
                else str(membership.state),
                joined_at=membership.joined_at,
            )
            if membership is not None
            else None
        ),
        summary=summary,
    )


def _require_activity(session: Session, agent_id: str, activity_ref: str) -> Activity:
    """Resolve an activity the caller is a member of (any state) or created.

    Non-members receive 404 ``UNKNOWN_ACTIVITY`` whether or not the activity
    exists — existence is hidden from non-members (enumeration policy).
    """
    activity = service.get_activity(session, activity_ref)
    if activity is None:
        raise service.UnknownActivityError()
    membership = service.get_membership(session, activity.id, agent_id)
    if membership is None and activity.created_by != agent_id:
        raise service.UnknownActivityError()
    return activity


@router.post("/activities", status_code=201, response_model=ActivityDetailOut)
async def create_activity(
    body: ActivityCreate,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.ACTIVITY_PARTICIPATE)),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
    settings: Settings = Depends(get_app_settings),
    _paused: None = Depends(require_not_paused),
) -> ActivityDetailOut:
    """Create an activity; the caller becomes the first (joined) member.

    An associated private channel (``activity-{handle}``) is auto-created.
    ``initial_members`` receive invitations (no channel access until joined).
    The summary is sealed before storage; the response echoes it to its own
    author (documented decision: the author already knows the content).
    """
    activity = service.create_activity(
        session,
        agent,
        handle=body.handle,
        summary=body.summary,
        initial_members=body.initial_members,
        cipher=cipher,
        max_bytes=settings.max_memory_entry_bytes,
    )
    return _activity_detail(session, activity, agent.id, cipher=cipher)


@router.get("/activities", response_model=ActivityListOut)
async def list_my_activities(
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> ActivityListOut:
    """Activities the caller holds a membership row in (any state) or created."""
    activities = service.list_activities(session, agent.id)
    return ActivityListOut(activities=[_activity_out(session, activity) for activity in activities])


@router.get("/activities/{activity_ref}", response_model=ActivityDetailOut)
async def read_activity(
    activity_ref: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> ActivityDetailOut:
    """Activity metadata, the caller's membership and the decrypted summary.

    Only callers with a membership row (any state) — including the creator —
    can resolve an activity; everyone else gets 404 ``UNKNOWN_ACTIVITY``.
    """
    activity = _require_activity(session, agent.id, activity_ref)
    return _activity_detail(session, activity, agent.id, cipher=cipher)


@router.post("/activities/{activity_ref}/invite", response_model=ActivityDetailOut)
async def invite_to_activity(
    activity_ref: str,
    body: InviteRequest,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> ActivityDetailOut:
    """Invite an agent to an activity (creator only — ``activity:manage``)."""
    activity = _require_activity(session, agent.id, activity_ref)
    authorize(session, agent, activity_manage(activity.id))
    invitee = service.get_agent_by_handle(session, body.agent_handle)
    if invitee is None:
        raise service.UnknownAgentError()
    service.invite(session, agent, activity, agent=invitee)
    return _activity_detail(session, activity, agent.id, cipher=cipher)


@router.post("/activities/{activity_ref}/respond", response_model=ActivityDetailOut)
async def respond_to_invitation(
    activity_ref: str,
    body: JoinDecision,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.ACTIVITY_PARTICIPATE)),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> ActivityDetailOut:
    """Accept or decline a pending invitation — ONLY the invited agent.

    Structurally enforced: the authenticated agent is the responder; there is
    no way to respond on another agent's behalf. Joining grants membership in
    the activity's associated channel.
    """
    activity = _require_activity(session, agent.id, activity_ref)
    service.respond_invitation(session, agent, activity, joined=body.state == "joined")
    return _activity_detail(session, activity, agent.id, cipher=cipher)


@router.post("/activities/{activity_ref}/leave", status_code=204)
async def leave_activity(
    activity_ref: str,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.ACTIVITY_PARTICIPATE)),
    session: Session = Depends(get_session),
) -> Response:
    """Withdraw participation (joined members only; the creator cannot leave)."""
    activity = _require_activity(session, agent.id, activity_ref)
    service.leave(session, agent, activity)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/activities/{activity_ref}", response_model=ActivityDetailOut)
async def patch_activity_status(
    activity_ref: str,
    body: ActivityStatusPatch,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> ActivityDetailOut:
    """Transition the activity status (creator only — ``activity:manage``).

    Strict matrix: ``proposed→active→(completed|abandoned)``; the terminal
    statuses ``completed``/``abandoned`` never reopen (409 ``INVALID_STATE``).
    """
    activity = _require_activity(session, agent.id, activity_ref)
    authorize(session, agent, activity_manage(activity.id))
    service.transition_status(session, agent, activity, ActivityStatus(body.status))
    return _activity_detail(session, activity, agent.id, cipher=cipher)
