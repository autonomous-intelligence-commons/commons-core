"""Private communication domain service (spec §9, §10.2, §8).

Machine-first messaging between agents over membership-controlled channels.
All payloads are sealed with :class:`EnvelopeCipher` BEFORE storage and are
never logged, never echoed in error details, and never stored in plaintext
columns (spec §11/§20).

Authorization model (documented decision):
- Channel-level, enforced by the caller through
  ``authorize(session, agent, channel_read/write(channel_id))`` (the server
  policy implements membership checks and records ``authz_denial`` events).
  Service functions additionally perform DEFENSIVE membership checks that
  raise :class:`AuthorizationError` without recording events, so programmatic
  callers cannot bypass policy by skipping the route layer.
- Send requires ``can_write`` membership; retrieval and acknowledgment require
  plain membership (read is implied by membership).
- Adding members requires ``can_write`` membership (the creator is always a
  ``can_write`` member) — a conservative, deny-by-default choice: only agents
  who may speak in a channel may widen its audience. Documented trade-off:
  read-only members cannot add members.

Ordering & delivery guarantees (honest, spec §9):
- Per-channel ``sequence`` is monotonically increasing, assigned as
  ``max(sequence) + 1`` and protected by the ``UNIQUE(channel_id, sequence)``
  constraint with up to 3 retries on conflict (SQLite-safe; no
  ``SELECT ... FOR UPDATE``).
- Retrieval is cursor-based (``sequence > after``) and therefore
  AT-LEAST-ONCE from the reader's perspective; duplicates are impossible
  within a single ordered scan, but re-reads overlap. NO exactly-once
  delivery claim is made.
- Acks are idempotent upserts.

Enumeration policy (documented decision): a non-existent channel/message
returns 404; an EXISTING channel the caller is not a member of returns 403
(authorization denial, event recorded). This is an existence oracle for
callers who already know a channel id or handle — accepted because channel
ids are unguessable UUIDs and handles are an intentionally public namespace;
message/channel CONTENT is never revealed in either case.
"""

import json
import uuid as uuidlib

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from commons_core.agents.service import HANDLE_PATTERN, get_agent_by_handle
from commons_core.api.errors import (
    DomainError,
    InvalidPayloadError,
    PayloadTooLargeError,
)
from commons_core.database.enums import ChannelKind
from commons_core.database.models import (
    Agent,
    Channel,
    ChannelMember,
    Message,
    MessageAck,
    new_uuid,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.engine import channel_read, channel_write
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.cryptography import Envelope, EnvelopeCipher

#: Maximum page size for message retrieval.
MAX_PAGE_SIZE = 100
#: Retries for the per-channel sequence race (UNIQUE constraint arbitration).
_SEQUENCE_ATTEMPTS = 3


class UnknownChannelError(DomainError):
    """Channel referenced by id or handle does not exist (404)."""

    status_code = 404
    code = "UNKNOWN_CHANNEL"

    def __init__(self) -> None:
        super().__init__("Channel not found.")


class UnknownAgentError(DomainError):
    """Agent referenced by handle does not exist (404, generic message)."""

    status_code = 404
    code = "UNKNOWN_AGENT"

    def __init__(self) -> None:
        super().__init__("Referenced agent does not exist.")


class UnknownMessageError(DomainError):
    """Message referenced by id does not exist (404)."""

    status_code = 404
    code = "UNKNOWN_MESSAGE"

    def __init__(self) -> None:
        super().__init__("Message not found.")


class ChannelHandleInvalidError(DomainError):
    """Channel handle does not match the handle pattern (422)."""

    status_code = 422
    code = "INVALID_CHANNEL_HANDLE"

    def __init__(self) -> None:
        super().__init__("Channel handle is invalid (4-64 chars, ^[a-z][a-z0-9-]{3,63}$).")


class ChannelExistsError(DomainError):
    """Channel handle already in use (409)."""

    status_code = 409
    code = "CHANNEL_EXISTS"

    def __init__(self) -> None:
        super().__init__("Channel handle is already in use.")


class InvalidMembersError(DomainError):
    """Membership change violates the channel kind's cardinality (422)."""

    status_code = 422
    code = "INVALID_MEMBERS"

    def __init__(
        self, message: str = "Membership request is invalid for this channel kind."
    ) -> None:
        super().__init__(message)


class MemberExistsError(DomainError):
    """Agent is already a member of the channel (409)."""

    status_code = 409
    code = "MEMBER_EXISTS"

    def __init__(self) -> None:
        super().__init__("Agent is already a member of this channel.")


class SequenceConflictError(DomainError):
    """Per-channel sequence could not be assigned after retries (409)."""

    status_code = 409
    code = "SEQUENCE_CONFLICT"

    def __init__(self) -> None:
        super().__init__("Could not assign a message sequence; retry the send.")


def _resolve_uuid(ref: str) -> str | None:
    try:
        return str(uuidlib.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


def get_channel(session: Session, ref: str) -> Channel | None:
    """Look up a channel by id (UUID) or by handle."""
    channel_id = _resolve_uuid(ref)
    if channel_id is not None:
        channel = session.get(Channel, channel_id)
        if channel is not None:
            return channel
    return session.scalar(select(Channel).where(Channel.handle == ref))


def get_message(session: Session, message_id: str) -> Message | None:
    """Look up a message by id (UUID only)."""
    message_uuid = _resolve_uuid(message_id)
    if message_uuid is None:
        return None
    return session.get(Message, message_uuid)


def get_membership(session: Session, channel_id: str, agent_id: str) -> ChannelMember | None:
    """Return the membership row for an agent in a channel, or ``None``."""
    return session.get(ChannelMember, (channel_id, agent_id))


def list_memberships(session: Session, channel_id: str) -> list[ChannelMember]:
    """All memberships of a channel ordered by member handle join."""
    stmt = (
        select(ChannelMember)
        .where(ChannelMember.channel_id == channel_id)
        .order_by(ChannelMember.created_at)
    )
    return list(session.scalars(stmt))


def channels_for_agent(session: Session, agent_id: str) -> list[Channel]:
    """Channels the agent is a member of, ordered by handle."""
    stmt = (
        select(Channel)
        .join(ChannelMember, ChannelMember.channel_id == Channel.id)
        .where(ChannelMember.agent_id == agent_id)
        .order_by(Channel.handle)
    )
    return list(session.scalars(stmt))


def create_channel(
    session: Session,
    creator: Agent | AuthenticatedAgent,
    *,
    handle: str,
    kind: str,
    description: str | None = None,
    initial_members: list[str] | None = None,
) -> Channel:
    """Create a channel; the creator becomes a ``can_write`` member.

    ``creator`` may be an ``Agent`` row or an ``AuthenticatedAgent`` snapshot
    (only ``id``/``handle`` are used). ``kind == "direct"`` requires exactly
    one *other* member (2 members total); ``kind == "group"`` accepts any
    number. All referenced handles must exist. The caller owns the
    transaction (this function flushes).
    """
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise ChannelHandleInvalidError()
    if get_channel(session, handle) is not None:
        raise ChannelExistsError()
    try:
        kind_value = ChannelKind(kind)
    except ValueError as exc:
        raise InvalidMembersError(f"unsupported channel kind {kind!r}") from exc

    # Normalize: unique handles, creator excluded (creator is added below).
    other_handles: list[str] = []
    for member_handle in initial_members or []:
        if member_handle == creator.handle:
            continue
        if member_handle not in other_handles:
            other_handles.append(member_handle)

    if kind_value == ChannelKind.DIRECT and len(other_handles) != 1:
        raise InvalidMembersError(
            "a direct channel requires exactly one other member (two members total)"
        )

    member_agents = []
    for member_handle in other_handles:
        member_agent = get_agent_by_handle(session, member_handle)
        if member_agent is None:
            raise UnknownAgentError()
        member_agents.append(member_agent)

    channel = Channel(
        id=new_uuid(),
        handle=handle,
        kind=kind_value,
        created_by=creator.id,
        description=description,
    )
    session.add(channel)
    session.add(ChannelMember(channel_id=channel.id, agent_id=creator.id, can_write=True))
    for member_agent in member_agents:
        session.add(ChannelMember(channel_id=channel.id, agent_id=member_agent.id, can_write=True))
    try:
        session.flush()
    except IntegrityError as exc:  # race on the unique handle index
        session.rollback()
        raise ChannelExistsError() from exc
    return channel


def add_member(
    session: Session,
    actor: Agent | AuthenticatedAgent,
    channel: Channel,
    *,
    handle: str,
    can_write: bool = True,
) -> ChannelMember:
    """Add an agent to a channel (actor must be a ``can_write`` member).

    Policy (documented above): only ``can_write`` members — which always
    includes the creator — may widen the channel audience. Deny-by-default
    otherwise (the route layer records the authorization denial).
    """
    actor_membership = get_membership(session, channel.id, actor.id)
    if actor_membership is None or not actor_membership.can_write:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(channel_write(channel.id))

    member_agent = get_agent_by_handle(session, handle)
    if member_agent is None:
        raise UnknownAgentError()
    if get_membership(session, channel.id, member_agent.id) is not None:
        raise MemberExistsError()
    if channel.kind == ChannelKind.DIRECT:
        raise InvalidMembersError("a direct channel is limited to two members")

    membership = ChannelMember(channel_id=channel.id, agent_id=member_agent.id, can_write=can_write)
    session.add(membership)
    session.flush()
    return membership


def message_aad(channel_id: str, sender_id: str) -> str:
    """AAD binding a message envelope to its channel and sender (spec §10.2)."""
    return f"message:{channel_id}:{sender_id}"


def send_message(
    session: Session,
    sender: Agent | AuthenticatedAgent,
    channel: Channel,
    *,
    content_type: str,
    payload: str,
    correlation_id: str | None = None,
    cipher: EnvelopeCipher,
    max_bytes: int,
) -> Message:
    """Seal and persist one message (the caller owns the transaction).

    Checks: sender membership with ``can_write``; payload size in UTF-8 bytes
    (413 on exceed); JSON validity for ``application/json`` (422 on failure).
    The payload is sealed BEFORE any storage attempt and the envelope's AAD
    binds ``message:{channel_id}:{sender_id}``.
    """
    membership = get_membership(session, channel.id, sender.id)
    if membership is None or not membership.can_write:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(channel_write(channel.id))

    size_bytes = len(payload.encode("utf-8"))
    if size_bytes > max_bytes:
        raise PayloadTooLargeError(size_bytes, max_bytes)
    if size_bytes == 0:
        raise InvalidPayloadError("Payload must not be empty.")

    if content_type == "application/json":
        try:
            json.loads(payload)
        except ValueError as exc:
            raise InvalidPayloadError(
                "Payload is not valid JSON for content type 'application/json'."
            ) from exc

    aad = message_aad(channel.id, sender.id)
    envelope = cipher.seal(payload, aad)

    channel_id, sender_id = channel.id, sender.id
    for attempt in range(_SEQUENCE_ATTEMPTS):
        current_max = session.scalar(
            select(func.max(Message.sequence)).where(Message.channel_id == channel_id)
        )
        sequence = int(current_max or 0) + 1
        message = Message(
            id=new_uuid(),
            channel_id=channel_id,
            sender_id=sender_id,
            content_type=content_type,
            envelope=envelope.model_dump(),
            correlation_id=correlation_id,
            sequence=sequence,
            size_bytes=size_bytes,
        )
        session.add(message)
        try:
            session.flush()
            return message
        except IntegrityError:
            # Concurrent writer won this (channel_id, sequence) slot: rollback
            # (also discards any pending writes of THIS transaction — the
            # route calls send_message before other mutations) and retry with
            # the recomputed maximum.
            session.rollback()
            if attempt == _SEQUENCE_ATTEMPTS - 1:
                raise SequenceConflictError() from None
    raise SequenceConflictError()  # pragma: no cover


def decrypt_message(message: Message, cipher: EnvelopeCipher) -> str:
    """Decrypt a message payload with its AAD (verification included)."""
    aad = message_aad(message.channel_id, message.sender_id)
    return cipher.open(Envelope(**message.envelope), aad)


def list_messages(
    session: Session,
    reader: Agent | AuthenticatedAgent,
    channel: Channel,
    *,
    after_sequence: int = 0,
    limit: int = 50,
    cipher: EnvelopeCipher,
) -> tuple[list[tuple[Message, str, str]], bool]:
    """Cursor page of messages with sender handles and decrypted payloads.

    Returns ``(rows, has_more)`` where ``rows`` holds up to ``limit`` tuples
    ``(message, sender_handle, payload)`` with ``sequence > after_sequence``
    ordered by ``sequence`` ascending; ``has_more`` is True when additional
    messages remain beyond the page (the client advances its cursor to the
    last returned sequence). ``reader`` must be a member (defensive; routes
    authorize first).
    """
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if get_membership(session, channel.id, reader.id) is None:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(channel_read(channel.id))

    stmt = (
        select(Message)
        .where(Message.channel_id == channel.id, Message.sequence > after_sequence)
        .order_by(Message.sequence)
        .limit(limit + 1)
    )
    page = list(session.scalars(stmt))
    has_more = len(page) > limit
    messages = page[:limit]
    if not messages:
        return [], has_more

    sender_ids = {message.sender_id for message in messages}
    handle_by_id = dict(
        session.execute(select(Agent.id, Agent.handle).where(Agent.id.in_(sender_ids))).all()
    )
    rows = [
        (
            message,
            handle_by_id.get(message.sender_id, "unknown"),
            decrypt_message(message, cipher),
        )
        for message in messages
    ]
    return rows, has_more


def ack_message(
    session: Session,
    reader: Agent | AuthenticatedAgent,
    message: Message,
) -> tuple[MessageAck, bool]:
    """Acknowledge a message (idempotent); returns ``(ack_row, already_acked)``.

    When the ack already exists the original row and timestamp are returned
    unchanged and ``already_acked`` is True (no second row is written).
    """
    if get_membership(session, message.channel_id, reader.id) is None:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(channel_read(message.channel_id))

    existing = session.get(MessageAck, (reader.id, message.id))
    if existing is not None:
        return existing, True
    ack = MessageAck(agent_id=reader.id, message_id=message.id)
    session.add(ack)
    session.flush()
    return ack, False
