"""Shared-memory domain service (spec §11, §12, §10.2).

POISONING CONTROLS (spec §12) — read this before extending:
- Memory entries are UNTRUSTED DATA. They never become authority: the
  authorization engine structurally never queries ``memory_entries`` (canary
  test in ``tests/unit/test_authorization.py``), and this module never feeds
  entry content into any policy, prompt or control decision.
- Provenance: every version records ``author_id`` (an immutable agent id) and
  ``created_at``; entries are sealed with AAD ``memory:{namespace_id}:{key}``
  so a row cannot silently move between namespaces or keys (GCM verification
  fails on mismatch).
- Versioning: updates NEVER overwrite or delete — a new row is inserted with
  ``version = max(version) + 1`` and the previous ACTIVE row's
  ``superseded_by`` is pointed at the new row (immutable history). The
  ``UNIQUE(namespace_id, key, version)`` constraint arbitrates concurrent
  writers with up to 3 retries.
- Size limits (``max_memory_entry_bytes``, UTF-8 bytes) and content-type
  validation (JSON must parse) are enforced BEFORE sealing.
- PRIVACY: payloads are sealed with :class:`EnvelopeCipher` BEFORE storage;
  never logged, never echoed in errors, never stored in plaintext columns.

Authorization model: the caller (route layer) authorizes through
``authorize(session, agent, memory:read|write|namespace:manage:{ref})`` —
the server policy implements owner + ``memory_permissions`` checks and
records ``authz_denial`` events. Granting permissions is OWNER-ONLY and
deliberately NOT available to infrastructure admins (spec §3.C privacy:
admins maintain the platform, they do not read or re-permission private
collaboration data). Service functions add DEFENSIVE permission checks that
raise :class:`AuthorizationError` without recording events.

Enumeration policy (consistent with communication): non-existent namespace
→ 404; existing namespace without permissions → 403; entry keys the caller
cannot read are never listed. Content is never revealed in error responses.
"""

import json
import uuid as uuidlib

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from commons_core.agents.service import HANDLE_PATTERN, get_agent_by_handle
from commons_core.api.errors import DomainError, InvalidPayloadError, PayloadTooLargeError
from commons_core.database.models import (
    Agent,
    MemoryEntry,
    MemoryNamespace,
    MemoryPermission,
    new_uuid,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.cryptography import Envelope, EnvelopeCipher

#: Retries for the (namespace, key, version) race.
_VERSION_ATTEMPTS = 3


class UnknownNamespaceError(DomainError):
    """Namespace referenced by id or handle does not exist (404)."""

    status_code = 404
    code = "UNKNOWN_NAMESPACE"

    def __init__(self) -> None:
        super().__init__("Namespace not found.")


class UnknownAgentError(DomainError):
    """Agent referenced by handle does not exist (404, generic message)."""

    status_code = 404
    code = "UNKNOWN_AGENT"

    def __init__(self) -> None:
        super().__init__("Referenced agent does not exist.")


class UnknownEntryError(DomainError):
    """No (active) entry exists for the key (404)."""

    status_code = 404
    code = "UNKNOWN_ENTRY"

    def __init__(self) -> None:
        super().__init__("Entry not found.")


class NamespaceHandleInvalidError(DomainError):
    """Namespace handle does not match the handle pattern (422)."""

    status_code = 422
    code = "INVALID_NAMESPACE_HANDLE"

    def __init__(self) -> None:
        super().__init__("Namespace handle is invalid (4-64 chars, ^[a-z][a-z0-9-]{3,63}$).")


class NamespaceExistsError(DomainError):
    """Namespace handle already in use (409)."""

    status_code = 409
    code = "NAMESPACE_EXISTS"

    def __init__(self) -> None:
        super().__init__("Namespace handle is already in use.")


class VersionConflictError(DomainError):
    """Version could not be assigned after retries (409)."""

    status_code = 409
    code = "VERSION_CONFLICT"

    def __init__(self) -> None:
        super().__init__("Could not assign an entry version; retry the write.")


def _resolve_uuid(ref: str) -> str | None:
    try:
        return str(uuidlib.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


def get_namespace(session: Session, ref: str) -> MemoryNamespace | None:
    """Look up a namespace by id (UUID) or by handle."""
    namespace_id = _resolve_uuid(ref)
    if namespace_id is not None:
        namespace = session.get(MemoryNamespace, namespace_id)
        if namespace is not None:
            return namespace
    return session.scalar(select(MemoryNamespace).where(MemoryNamespace.handle == ref))


def effective_permissions(
    session: Session, namespace: MemoryNamespace, agent_id: str
) -> tuple[bool, bool, bool]:
    """Return ``(is_owner, can_read, can_write)`` for an agent on a namespace."""
    if namespace.created_by == agent_id:
        return True, True, True
    permission = session.get(MemoryPermission, (namespace.id, agent_id))
    if permission is None:
        return False, False, False
    return False, bool(permission.can_read), bool(permission.can_write)


def create_namespace(
    session: Session,
    creator: Agent | AuthenticatedAgent,
    *,
    handle: str,
    description: str | None = None,
) -> MemoryNamespace:
    """Create a namespace; the creator becomes the owner (all rights).

    Ownership is derived from ``namespace.created_by`` by the server policy —
    no permission row is written for the owner. The caller owns the
    transaction (this function flushes).
    """
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise NamespaceHandleInvalidError()
    if get_namespace(session, handle) is not None:
        raise NamespaceExistsError()
    namespace = MemoryNamespace(
        id=new_uuid(), handle=handle, description=description, created_by=creator.id
    )
    session.add(namespace)
    try:
        session.flush()
    except IntegrityError as exc:  # race on the unique handle index
        session.rollback()
        raise NamespaceExistsError() from exc
    return namespace


def grant_permission(
    session: Session,
    actor: Agent | AuthenticatedAgent,
    namespace: MemoryNamespace,
    *,
    agent_handle: str,
    can_read: bool,
    can_write: bool,
) -> str:
    """Grant/update an agent's read/write permission on a namespace.

    OWNER-ONLY (spec §3.C): infrastructure admins deliberately cannot grant
    memory permissions — they maintain the platform and must not re-permission
    private collaboration data. Upsert semantics: an existing row is updated.
    Passing both flags ``False`` REVOKES (the permission row is removed, so
    the agent no longer sees the namespace at all). Returns the grantee's
    agent id.
    """
    if namespace.created_by != actor.id:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(f"memory:namespace:manage:{namespace.id}")

    target = get_agent_by_handle(session, agent_handle)
    if target is None:
        raise UnknownAgentError()

    permission = session.get(MemoryPermission, (namespace.id, target.id))
    if can_read or can_write:
        if permission is None:
            permission = MemoryPermission(
                namespace_id=namespace.id,
                agent_id=target.id,
                can_read=can_read,
                can_write=can_write,
            )
            session.add(permission)
        else:
            permission.can_read = can_read
            permission.can_write = can_write
        session.flush()
    elif permission is not None:
        # Revoke: remove the row entirely (no lingering "no-access" grants).
        session.delete(permission)
        session.flush()
    return target.id


def list_permissions(
    session: Session, namespace: MemoryNamespace
) -> list[tuple[MemoryPermission, str]]:
    """All granted permissions with grantee handles (owner view)."""
    stmt = (
        select(MemoryPermission, Agent.handle)
        .join(Agent, Agent.id == MemoryPermission.agent_id)
        .where(MemoryPermission.namespace_id == namespace.id)
        .order_by(Agent.handle)
    )
    return list(session.execute(stmt).all())


def namespaces_for_agent(session: Session, agent_id: str) -> list[MemoryNamespace]:
    """Namespaces the agent owns or holds any permission on (by handle)."""
    stmt = (
        select(MemoryNamespace)
        .outerjoin(MemoryPermission, MemoryPermission.namespace_id == MemoryNamespace.id)
        .where((MemoryNamespace.created_by == agent_id) | (MemoryPermission.agent_id == agent_id))
        .order_by(MemoryNamespace.handle)
        .distinct()
    )
    return list(session.scalars(stmt))


def entry_aad(namespace_id: str, key: str) -> str:
    """AAD binding an entry envelope to its namespace and key (spec §10.2)."""
    return f"memory:{namespace_id}:{key}"


def _active_entry(
    session: Session, namespace_id: str, key: str, *, exclude_id: str | None = None
) -> MemoryEntry | None:
    """The latest non-superseded entry for a key, or ``None``.

    ``exclude_id`` lets :func:`write_entry` find the PREVIOUS active row
    right after inserting the new one (both are momentarily unsuperseded
    inside the same transaction).
    """
    stmt = (
        select(MemoryEntry)
        .where(
            MemoryEntry.namespace_id == namespace_id,
            MemoryEntry.key == key,
            MemoryEntry.superseded_by.is_(None),
        )
        .order_by(MemoryEntry.version.desc())
        .limit(1)
    )
    if exclude_id is not None:
        stmt = stmt.where(MemoryEntry.id != exclude_id)
    return session.scalar(stmt)


def write_entry(
    session: Session,
    author: Agent | AuthenticatedAgent,
    namespace: MemoryNamespace,
    *,
    key: str,
    payload: str,
    content_type: str,
    cipher: EnvelopeCipher,
    max_bytes: int,
) -> MemoryEntry:
    """Write a NEW version of an entry (immutable history, spec §12).

    Requires write permission (defensive check; routes authorize first).
    The previous active version — if any — is superseded (its row is never
    modified in content, only linked forward). Returns the new version row.
    """
    _, _, can_write = effective_permissions(session, namespace, author.id)
    if not can_write:
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(f"memory:write:{namespace.id}")

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

    aad = entry_aad(namespace.id, key)
    envelope = cipher.seal(payload, aad)
    namespace_id, author_id = namespace.id, author.id

    # Two-phase flush: the new version row is INSERTed first (its
    # UNIQUE(namespace, key, version) constraint arbitrates concurrent
    # writers), and only afterwards is the previous ACTIVE row superseded.
    # A single flush would let SQLAlchemy emit the self-referential UPDATE
    # before the INSERT and fail the FK check.
    entry: MemoryEntry | None = None
    for attempt in range(_VERSION_ATTEMPTS):
        max_version = session.scalar(
            select(func.max(MemoryEntry.version)).where(
                MemoryEntry.namespace_id == namespace_id, MemoryEntry.key == key
            )
        )
        entry = MemoryEntry(
            id=new_uuid(),
            namespace_id=namespace_id,
            key=key,
            envelope=envelope.model_dump(),
            author_id=author_id,
            version=int(max_version or 0) + 1,
            superseded_by=None,
            size_bytes=size_bytes,
        )
        session.add(entry)
        try:
            session.flush()
            break
        except IntegrityError:
            # Concurrent writer took this (namespace, key, version) slot:
            # rollback and recompute the version.
            session.rollback()
            entry = None
            if attempt == _VERSION_ATTEMPTS - 1:
                raise VersionConflictError() from None
    if entry is None:  # pragma: no cover - loop always sets entry or raises
        raise VersionConflictError()

    # Supersede the previous active row (re-read AFTER our insert so a
    # concurrent writer's newer active row is not clobbered by a stale copy;
    # the freshly inserted row itself is excluded from the lookup).
    active = _active_entry(session, namespace_id, key, exclude_id=entry.id)
    if active is not None:
        active.superseded_by = entry.id
        session.flush()
    return entry


def read_entry(
    session: Session,
    reader: Agent | AuthenticatedAgent,
    namespace: MemoryNamespace,
    key: str,
) -> MemoryEntry:
    """Return the LATEST active entry for ``key`` (404 when absent)."""
    is_owner, can_read, _ = effective_permissions(session, namespace, reader.id)
    if not (is_owner or can_read):
        # Defensive re-check; the route layer authorizes first (with events).
        raise AuthorizationError(f"memory:read:{namespace.id}")
    entry = _active_entry(session, namespace.id, key)
    if entry is None:
        raise UnknownEntryError()
    return entry


def read_history(
    session: Session,
    reader: Agent | AuthenticatedAgent,
    namespace: MemoryNamespace,
    key: str,
) -> list[MemoryEntry]:
    """All versions of ``key`` ascending (immutable provenance chain)."""
    is_owner, can_read, _ = effective_permissions(session, namespace, reader.id)
    if not (is_owner or can_read):
        raise AuthorizationError(f"memory:read:{namespace.id}")
    stmt = (
        select(MemoryEntry)
        .where(MemoryEntry.namespace_id == namespace.id, MemoryEntry.key == key)
        .order_by(MemoryEntry.version)
    )
    return list(session.scalars(stmt))


def list_keys(
    session: Session,
    reader: Agent | AuthenticatedAgent,
    namespace: MemoryNamespace,
) -> list[tuple[str, int, object]]:
    """Keys with their latest version numbers (NO payloads)."""
    is_owner, can_read, _ = effective_permissions(session, namespace, reader.id)
    if not (is_owner or can_read):
        raise AuthorizationError(f"memory:read:{namespace.id}")
    stmt = (
        select(
            MemoryEntry.key,
            func.max(MemoryEntry.version).label("latest_version"),
            func.max(MemoryEntry.created_at).label("updated_at"),
        )
        .where(MemoryEntry.namespace_id == namespace.id)
        .group_by(MemoryEntry.key)
        .order_by(MemoryEntry.key)
    )
    return [
        (key, int(version or 0), updated_at) for key, version, updated_at in session.execute(stmt)
    ]


def decrypt_entry(entry: MemoryEntry, cipher: EnvelopeCipher) -> str:
    """Decrypt an entry payload with its AAD (verification included)."""
    return cipher.open(Envelope(**entry.envelope), entry_aad(entry.namespace_id, entry.key))
