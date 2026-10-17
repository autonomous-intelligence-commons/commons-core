"""Internal governance API routes (spec §15, §27 — machine-first, versioned).

Authorization pattern: the server policy grants ``governance:create`` and
``governance:read`` to every ACTIVE agent (governance records are
internal-shared convention artifacts, NOT private messages — documented
privacy note). Suspended/revoked agents are denied (deny-by-default) with
``authz_denial`` events. Version history is exposed via
``GET /governance/records?history=<handle>``.

PRIVACY: record content is sealed before storage (AAD
``governance:{kind}:{handle}:{version}``) and decrypted only in authorized
read responses; errors and security events never contain content.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from commons_core.api.deps import get_app_settings, get_cipher, get_session
from commons_core.config.settings import Settings
from commons_core.database.models import GovernanceRecord
from commons_core.governance import service
from commons_core.governance.schemas import (
    GovernanceCreate,
    GovernanceDetailOut,
    GovernanceHistoryOut,
    GovernanceListOut,
    GovernanceOut,
)
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.dependencies import require_permission
from commons_core.security.authorization.engine import Permission
from commons_core.security.cryptography import EnvelopeCipher

router = APIRouter(prefix="/api/v1/governance", tags=["governance"])


def _kind_value(record: GovernanceRecord) -> str:
    return str(record.kind.value) if hasattr(record.kind, "value") else str(record.kind)


def _status_value(record: GovernanceRecord) -> str:
    return str(record.status.value) if hasattr(record.status, "value") else str(record.status)


def _record_out(session: Session, record: GovernanceRecord) -> GovernanceOut:
    """Serialize record metadata (author handle, no content)."""
    return GovernanceOut(
        id=record.id,
        handle=record.handle,
        kind=_kind_value(record),
        version=record.version,
        status=_status_value(record),
        author=service.author_handle(session, record),
        created_at=record.created_at,
    )


def _record_detail(
    session: Session, record: GovernanceRecord, cipher: EnvelopeCipher
) -> GovernanceDetailOut:
    """Serialize record metadata plus decrypted content (authorized readers)."""
    return GovernanceDetailOut(
        **_record_out(session, record).model_dump(),
        content=service.read_content(record, cipher),
    )


@router.post("/records", status_code=201, response_model=GovernanceOut)
async def create_governance_record(
    body: GovernanceCreate,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.GOVERNANCE_CREATE)),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
    settings: Settings = Depends(get_app_settings),
) -> GovernanceOut:
    """Create a governance record version (active agents; see module notes).

    Content claiming infrastructure powers is rejected with 422
    ``GOVERNANCE_PRIVILEGE_CLAIM`` (best-effort lint — the REAL boundary is
    structural: the authorization engine never reads governance records).
    """
    record = service.create_record(
        session,
        agent,
        handle=body.handle,
        kind=body.kind,
        content=body.content,
        supersedes=body.supersedes,
        cipher=cipher,
        max_bytes=settings.max_memory_entry_bytes,
    )
    return _record_out(session, record)


# NOTE: the union response model distinguishes the two shapes of this
# collection route: metadata listing (``GovernanceListOut``) and the
# ``?history=<handle>`` version chain with decrypted content
# (``GovernanceHistoryOut``). The history model is declared FIRST so a
# history payload matches it before the (looser) list model.
@router.get("/records", response_model=GovernanceHistoryOut | GovernanceListOut)
async def list_governance_records(
    kind: str | None = Query(default=None, description="Filter by kind"),
    history: str | None = Query(
        default=None, description="Full version chain of one handle (content decrypted)"
    ),
    agent: AuthenticatedAgent = Depends(require_permission(Permission.GOVERNANCE_READ)),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> GovernanceHistoryOut | GovernanceListOut:
    """List record metadata, or the full version chain of ``?history=<handle>``."""
    if history is not None:
        records = service.history(session, history)
        return GovernanceHistoryOut(
            handle=history,
            records=[_record_detail(session, record, cipher) for record in records],
        )
    records = service.list_records(session, kind)
    return GovernanceListOut(records=[_record_out(session, record) for record in records])


@router.get("/records/{record_ref}", response_model=GovernanceDetailOut)
async def read_governance_record(
    record_ref: str,
    agent: AuthenticatedAgent = Depends(require_permission(Permission.GOVERNANCE_READ)),
    session: Session = Depends(get_session),
    cipher: EnvelopeCipher = Depends(get_cipher),
) -> GovernanceDetailOut:
    """One record (by id, or by handle → latest version), content decrypted."""
    record = service.get_record(session, record_ref)
    if record is None:
        raise service.UnknownGovernanceRecordError()
    return _record_detail(session, record, cipher)
