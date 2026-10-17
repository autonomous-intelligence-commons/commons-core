"""Communication API routes (spec §9, §27 — machine-first, versioned).

Authorization pattern: every route resolves the referenced resource FIRST
(404 ``UNKNOWN_CHANNEL``/``UNKNOWN_MESSAGE`` for non-existent ids), then calls
``authorize()`` with the channel-scoped permission — the server policy
enforces membership (recording ``authz_denial`` security events on denial,
mapped to a generic 403). Enumeration policy: non-existent resource → 404;
existing resource without membership → 403 (content is never revealed in
either case; see ``commons_core.communication.service`` for the documented
trade-off).

PRIVACY: message payloads are sealed before storage and only decrypted for
authorized members; errors and security events never contain content.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from commons_core.api.deps import (
    get_app_settings,
    get_cipher,
    get_session,
    require_not_paused,
)
from commons_core.communication import service
from commons_core.communication.schemas import (
    AckOut,
    ChannelCreate,
    ChannelListOut,
    ChannelOut,
    MemberAdd,
    MemberOut,
    MessageCreate,
    MessageListOut,
    MessageOut,
)
from commons_core.config.settings import Settings
from commons_core.database.models import Agent, Channel, Message
from commons_core.infrastructure.quotas import check_message_quota
from commons_core.security.authentication.dependencies import (
    require_authenticated_agent,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.dependencies import require_permission
from commons_core.security.authorization.engine import (
    Permission,
    authorize,
    channel_read,
    channel_write,
)
from commons_core.security.cryptography import EnvelopeCipher

router = APIRouter(prefix="/api/v1", tags=["communication"])


def _channel_out(session: Session, channel: Channel) -> ChannelOut:
    """Serialize channel metadata (members as handles; no message content)."""
    memberships = service.list_memberships(session, channel.id)
    agent_ids = [membership.agent_id for membership in memberships]
    handle_by_id = (
        dict(session.execute(select(Agent.id, Agent.handle).where(Agent.id.in_(agent_ids))).all())
        if agent_ids
        else {}
    )
    members = [
        MemberOut(
            agent_handle=handle_by_id.get(membership.agent_id, "unknown"),
            can_write=membership.can_write,
            joined_at=membership.created_at,
        )
        for membership in memberships
    ]
    creator_handle = handle_by_id.get(channel.created_by, "unknown")
    return ChannelOut(
        id=channel.id,
        handle=channel.handle,
        kind=str(channel.kind.value) if hasattr(channel.kind, "value") else str(channel.kind),
        description=channel.description,
        created_by_handle=creator_handle,
        created_at=channel.created_at,
        members=members,
    )


def _message_out(message: Message, sender_handle: str, payload: str) -> MessageOut:
    return MessageOut(
        id=message.id,
        channel_id=message.channel_id,
        sender=sender_handle,
        content_type=message.content_type,
        payload=payload,
        correlation_id=message.correlation_id,
        sequence=message.sequence,
        created_at=message.created_at,
        size_bytes=message.size_bytes,
    )


def _require_channel(session: Session, channel_ref: str) -> Channel:
    channel = service.get_channel(session, channel_ref)
    if channel is None:
        raise service.UnknownChannelError()
    return channel


@router.post("/channels", status_code=201, response_model=ChannelOut)
async def create_channel(
    body: ChannelCreate,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.CHANNEL_CREATE)),
    session: Session = Depends(get_session),
) -> ChannelOut:
    """Create a channel; the caller becomes a ``can_write`` member."""
    channel = service.create_channel(
        session,
        agent,
        handle=body.handle,
        kind=body.kind,
        description=body.description,
        initial_members=body.initial_members,
    )
    return _channel_out(session, channel)


@router.get("/channels", response_model=ChannelListOut)
async def list_my_channels(
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> ChannelListOut:
    """List channels the caller is a member of (metadata only)."""
    channels = service.channels_for_agent(session, agent.id)
    return ChannelListOut(channels=[_channel_out(session, channel) for channel in channels])


@router.get("/channels/{channel_id}", response_model=ChannelOut)
async def read_channel(
    channel_id: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> ChannelOut:
    """Channel metadata with member handles and roles (members only)."""
    channel = _require_channel(session, channel_id)
    authorize(session, agent, channel_read(channel.id))
    return _channel_out(session, channel)


@router.post("/channels/{channel_id}/members", status_code=201, response_model=ChannelOut)
async def add_channel_member(
    channel_id: str,
    body: MemberAdd,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> ChannelOut:
    """Add a member to a group channel (requires ``can_write`` membership)."""
    channel = _require_channel(session, channel_id)
    authorize(session, agent, channel_write(channel.id))
    service.add_member(session, agent, channel, handle=body.agent_handle, can_write=body.can_write)
    return _channel_out(session, channel)


@router.post("/channels/{channel_id}/messages", status_code=201, response_model=MessageOut)
async def send_channel_message(
    channel_id: str,
    body: MessageCreate,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
    settings: Settings = Depends(get_app_settings),
    _paused: None = Depends(require_not_paused),
) -> MessageOut:
    """Send a sealed message to a channel (member with ``can_write`` only).

    Quota (per-agent, per-minute) is enforced BEFORE the payload is processed.
    The response echoes the payload back to its own sender (documented
    decision: the sender already knows the content; no other reader can reach
    this response). While the emergency stop is engaged, sends are rejected
    with 503 ``SERVICE_PAUSED`` (reads continue).
    """
    channel = _require_channel(session, channel_id)
    authorize(session, agent, channel_write(channel.id))
    check_message_quota(session, agent.id, limit=settings.max_messages_per_minute)
    message = service.send_message(
        session,
        agent,
        channel,
        content_type=body.content_type,
        payload=body.payload,
        correlation_id=body.correlation_id,
        cipher=cipher,
        max_bytes=settings.max_message_bytes,
    )
    return _message_out(message, agent.handle, body.payload)


@router.get("/channels/{channel_id}/messages", response_model=MessageListOut)
async def list_channel_messages(
    channel_id: str,
    after: int = Query(default=0, ge=0, description="Return messages with sequence > after"),
    limit: int = Query(default=50, ge=1, le=service.MAX_PAGE_SIZE),
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> MessageListOut:
    """Cursor retrieval (members only): decrypted payloads, ascending sequence."""
    channel = _require_channel(session, channel_id)
    authorize(session, agent, channel_read(channel.id))
    rows, has_more = service.list_messages(
        session,
        agent,
        channel,
        after_sequence=after,
        limit=limit,
        cipher=cipher,
    )
    return MessageListOut(
        messages=[
            _message_out(message, sender_handle, payload)
            for message, sender_handle, payload in rows
        ],
        has_more=has_more,
    )


@router.post("/messages/{message_id}/ack", response_model=AckOut)
async def ack_a_message(
    message_id: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> AckOut:
    """Acknowledge delivery (idempotent; any channel member may ack)."""
    message = service.get_message(session, message_id)
    if message is None:
        raise service.UnknownMessageError()
    authorize(session, agent, channel_read(message.channel_id))
    ack, already_acked = service.ack_message(session, agent, message)
    return AckOut(
        message_id=message.id,
        agent_id=agent.id,
        acked_at=ack.acked_at,
        already_acked=already_acked,
    )
