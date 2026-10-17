"""Shared-memory API routes (spec §11, §12, §27 — machine-first, versioned).

Authorization pattern: resolve the namespace FIRST (404
``UNKNOWN_NAMESPACE`` for non-existent refs), then ``authorize()`` with the
namespace-scoped permission — the server policy enforces ownership /
``memory_permissions`` (recording ``authz_denial`` events on denial, mapped
to a generic 403). Enumeration policy matches communication: non-existent
resource → 404, existing resource without permission → 403, content never
revealed in either case.

KEY ROUTING DECISION (documented): entry keys contain ``/`` and ``.``, so
keys are addressed with the FastAPI ``{key:path}`` converter, e.g.
``GET /api/v1/memory/namespaces/ns/entries/notes/task-1`` with
``key="notes/task-1"``. Consequences, handled explicitly:
- the ``/history`` route is registered BEFORE the plain key route (the greedy
  path converter would otherwise swallow it);
- keys ending in the literal ``/history`` are rejected at write time
  (``EntryWrite`` validator) because the history endpoint shadows them.

Namespace creation requires authentication only: the policy engine has no
``memory:namespace:create`` capability and creating an EMPTY namespace grants
the creator nothing but ownership of it (deny-by-default still applies to
everything inside). Documented decision — consistent with ``channel:create``
being granted to every active agent.

PRIVACY: entry payloads are sealed before storage and decrypted only for
readers with ``memory:read``; errors and security events never contain
content. Permission grants are OWNER-ONLY (admins deliberately excluded,
spec §3.C).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from commons_core.api.deps import (
    get_app_settings,
    get_cipher,
    get_session,
    require_not_paused,
)
from commons_core.config.settings import Settings
from commons_core.database.models import Agent, MemoryEntry, MemoryNamespace
from commons_core.memory import service
from commons_core.memory.schemas import (
    EntryHistoryOut,
    EntryOut,
    EntryWrite,
    KeyListOut,
    KeySummaryOut,
    NamespaceCreate,
    NamespaceListOut,
    NamespaceOut,
    NamespacePermissionsOut,
    PermissionGrant,
    PermissionOut,
)
from commons_core.security.authentication.dependencies import (
    require_authenticated_agent,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.engine import (
    authorize,
    memory_namespace_manage,
    memory_read,
    memory_write,
)
from commons_core.security.cryptography import EnvelopeCipher

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])


def _namespace_out(
    session: Session,
    namespace: MemoryNamespace,
    agent_id: str,
    *,
    include_grants: bool = False,
) -> NamespaceOut:
    """Serialize namespace metadata plus the caller's permissions."""
    is_owner, can_read, can_write = service.effective_permissions(session, namespace, agent_id)
    grants: list[PermissionOut] | None = None
    if include_grants and is_owner:
        grants = [
            PermissionOut(agent_handle=handle, can_read=row.can_read, can_write=row.can_write)
            for row, handle in service.list_permissions(session, namespace)
        ]
    creator = session.get(Agent, namespace.created_by)
    return NamespaceOut(
        id=namespace.id,
        handle=namespace.handle,
        description=namespace.description,
        created_by_handle=creator.handle if creator is not None else "unknown",
        created_at=namespace.created_at,
        my_permissions=NamespacePermissionsOut(
            is_owner=is_owner, can_read=can_read, can_write=can_write
        ),
        grants=grants,
    )


def _entry_out(entry: MemoryEntry, author_handle: str, payload: str) -> EntryOut:
    return EntryOut(
        key=entry.key,
        payload=payload,
        version=entry.version,
        author=author_handle,
        created_at=entry.created_at,
        superseded=entry.superseded_by is not None,
        size_bytes=entry.size_bytes,
    )


def _author_handle(session: Session, entry: MemoryEntry) -> str:
    author = session.get(Agent, entry.author_id)
    return author.handle if author is not None else "unknown"


def _require_namespace(session: Session, namespace_ref: str) -> MemoryNamespace:
    namespace = service.get_namespace(session, namespace_ref)
    if namespace is None:
        raise service.UnknownNamespaceError()
    return namespace


@router.post("/namespaces", status_code=201, response_model=NamespaceOut)
async def create_namespace(
    body: NamespaceCreate,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> NamespaceOut:
    """Create a namespace; the caller becomes its owner (see module notes)."""
    namespace = service.create_namespace(
        session, agent, handle=body.handle, description=body.description
    )
    return _namespace_out(session, namespace, agent.id, include_grants=True)


@router.get("/namespaces", response_model=NamespaceListOut)
async def list_my_namespaces(
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> NamespaceListOut:
    """List namespaces the caller owns or holds any permission on."""
    namespaces = service.namespaces_for_agent(session, agent.id)
    return NamespaceListOut(
        namespaces=[_namespace_out(session, namespace, agent.id) for namespace in namespaces]
    )


@router.get("/namespaces/{namespace_ref}", response_model=NamespaceOut)
async def read_namespace(
    namespace_ref: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> NamespaceOut:
    """Namespace metadata with the caller's permissions (readers and above)."""
    namespace = _require_namespace(session, namespace_ref)
    authorize(session, agent, memory_read(namespace.handle))
    return _namespace_out(session, namespace, agent.id, include_grants=True)


@router.put("/namespaces/{namespace_ref}/permissions", response_model=PermissionOut)
async def grant_namespace_permission(
    namespace_ref: str,
    body: PermissionGrant,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> PermissionOut:
    """Grant/update an agent's permissions on a namespace (OWNER-ONLY).

    Infrastructure admins are deliberately excluded (spec §3.C privacy).
    Upsert semantics: re-granting replaces the agent's previous grant;
    passing both flags false REVOKES the grant (row removed).
    """
    namespace = _require_namespace(session, namespace_ref)
    authorize(session, agent, memory_namespace_manage(namespace.handle))
    grantee_id = service.grant_permission(
        session,
        agent,
        namespace,
        agent_handle=body.agent_handle,
        can_read=body.can_read,
        can_write=body.can_write,
    )
    grantee = session.get(Agent, grantee_id)
    return PermissionOut(
        agent_handle=grantee.handle if grantee is not None else "unknown",
        can_read=body.can_read,
        can_write=body.can_write,
    )


@router.post("/namespaces/{namespace_ref}/entries", status_code=201, response_model=EntryOut)
async def write_namespace_entry(
    namespace_ref: str,
    body: EntryWrite,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
    settings: Settings = Depends(get_app_settings),
    _paused: None = Depends(require_not_paused),
) -> EntryOut:
    """Write a NEW immutable version of an entry (writers only).

    The response echoes the payload to its own author (documented decision:
    the author already knows the content; no other reader can reach this
    response). While the emergency stop is engaged, writes are rejected with
    503 ``SERVICE_PAUSED`` (reads continue).
    """
    namespace = _require_namespace(session, namespace_ref)
    authorize(session, agent, memory_write(namespace.handle))
    entry = service.write_entry(
        session,
        agent,
        namespace,
        key=body.key,
        payload=body.payload,
        content_type=body.content_type,
        cipher=cipher,
        max_bytes=settings.max_memory_entry_bytes,
    )
    return _entry_out(entry, agent.handle, body.payload)


@router.get("/namespaces/{namespace_ref}/entries", response_model=KeyListOut)
async def list_namespace_keys(
    namespace_ref: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
) -> KeyListOut:
    """List keys with latest version numbers (readers only; no payloads)."""
    namespace = _require_namespace(session, namespace_ref)
    authorize(session, agent, memory_read(namespace.handle))
    rows = service.list_keys(session, agent, namespace)
    return KeyListOut(
        keys=[
            KeySummaryOut(key=key, latest_version=version, updated_at=updated)
            for key, version, updated in rows
        ]
    )


# NOTE: registered BEFORE the plain ``{key:path}`` route — the greedy path
# converter would otherwise swallow ``.../entries/{key}/history``.
@router.get(
    "/namespaces/{namespace_ref}/entries/{key:path}/history", response_model=EntryHistoryOut
)
async def read_entry_history(
    namespace_ref: str,
    key: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> EntryHistoryOut:
    """Full version chain of a key (readers only), ascending by version."""
    namespace = _require_namespace(session, namespace_ref)
    authorize(session, agent, memory_read(namespace.handle))
    entries = service.read_history(session, agent, namespace, key)
    if not entries:
        raise service.UnknownEntryError()
    versions = [
        _entry_out(
            entry,
            _author_handle(session, entry),
            service.decrypt_entry(entry, cipher),
        )
        for entry in entries
    ]
    return EntryHistoryOut(key=key, versions=versions)


@router.get("/namespaces/{namespace_ref}/entries/{key:path}", response_model=EntryOut)
async def read_namespace_entry(
    namespace_ref: str,
    key: str,
    agent: AuthenticatedAgent = Depends(require_authenticated_agent),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> EntryOut:
    """Read the LATEST active version of a key (readers only), decrypted."""
    namespace = _require_namespace(session, namespace_ref)
    authorize(session, agent, memory_read(namespace.handle))
    entry = service.read_entry(session, agent, namespace, key)
    return _entry_out(entry, _author_handle(session, entry), service.decrypt_entry(entry, cipher))
