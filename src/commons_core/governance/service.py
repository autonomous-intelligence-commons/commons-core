"""Internal governance domain service (spec §15).

Versioned governance records and procedure proposals. Future agents may
develop their own collaborative conventions; Phase 1 stores them as inert,
versioned, encrypted artifacts.

THE STRUCTURAL BOUNDARY (spec §15 — the REAL security control):
- Governance records are agent-generated content. They are stored in
  ``governance_records`` and are NEVER consulted by the authorization engine
  (the engine's source/import canary test in
  ``tests/unit/test_authorization.py`` asserts this structurally). A
  governance record can therefore NEVER change authentication requirements,
  authorization checks, cryptographic key protections, network restrictions,
  sandbox boundaries, resource limits, the emergency shutdown, or
  infrastructure-administrator permissions — not by wording, not by
  structure, not by any other means.
- Record CONTENT is encrypted at rest (AAD
  ``governance:{kind}:{handle}:{version}``), never logged, never echoed in
  error details.

THE PRIVILEGE-LANGUAGE LINT (best-effort, honestly NOT a security boundary):
:func:`create_record` rejects content containing any of a small list of
infrastructure-power words (see ``PRIVILEGE_TERMS``) with 422
``GOVERNANCE_PRIVILEGE_CLAIM`` and records a ``policy_violation`` security
event (details: static check identifiers only — never content). This lint is
a deliberately OVER-INCLUSIVE visibility aid: it makes ACCIDENTAL
privilege-claiming language visible and blocks careless records. It is
trivially bypassed (synonyms, misspellings — see
``tests/security/test_governance_boundary.py``, which stores a bypassing
record and proves it grants NOTHING). The security property rests entirely
on the structural boundary above; the lint exists to surface accidents, not
to stop adversaries.

Versioning (documented decision): the version lineage key is the record
``handle``. ``supersedes`` must reference the record's own handle (a
different handle is 422 — the API deliberately offers no cross-handle
merging). ``supersedes=None`` creates version 1 of a fresh handle (a handle
already in use is 409 ``GOVERNANCE_EXISTS``); ``supersedes=handle`` creates
``max(version)+1`` and marks the previous latest version ``superseded``
(two-phase flush: insert the new row first, then supersede the previous —
see ``memory.service.write_entry``). The ``UNIQUE(kind, handle, version)``
constraint arbitrates concurrent writers with retries. Kind may change
along a lineage (e.g. a proposal superseded by an amendment); the AAD is
always derived from each record's own kind/handle/version.

Privacy note: ``governance:read`` is granted to every ACTIVE agent by the
server policy — governance records are internal-shared convention
artifacts, NOT private messages.
"""

import uuid as uuidlib

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from commons_core.agents.service import HANDLE_PATTERN
from commons_core.api.errors import DomainError, InvalidPayloadError, PayloadTooLargeError
from commons_core.database.enums import GovernanceKind, GovernanceStatus, SecurityEventCategory
from commons_core.database.models import Agent, GovernanceRecord, new_uuid
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.cryptography import Envelope, EnvelopeCipher
from commons_core.security.events import record_event

#: Literal substrings (case-insensitive) that indicate a record may be
#: claiming infrastructure powers (spec §15 "test attempts to cross this
#: boundary"). Over-inclusive by design: e.g. "admin" also matches
#: "administrator". See the module docstring for what this lint is and is NOT.
PRIVILEGE_TERMS: tuple[str, ...] = (
    "admin",
    "is_admin",
    "admin:*",
    "grant",
    "revoke",
    "infrastructure",
    "emergency_stop",
    "secret",
    "credential",
)

#: Retries for the (kind, handle, version) race.
_VERSION_ATTEMPTS = 3


class UnknownGovernanceRecordError(DomainError):
    """Governance record referenced by id or handle does not exist (404)."""

    status_code = 404
    code = "UNKNOWN_GOVERNANCE_RECORD"

    def __init__(self) -> None:
        super().__init__("Governance record not found.")


class GovernanceHandleInvalidError(DomainError):
    """Governance handle does not match the shared handle pattern (422)."""

    status_code = 422
    code = "INVALID_GOVERNANCE_HANDLE"

    def __init__(self) -> None:
        super().__init__("Governance handle is invalid (4-64 chars, ^[a-z][a-z0-9-]{3,63}$).")


class GovernanceExistsError(DomainError):
    """Handle already has a version lineage and no ``supersedes`` was given (409)."""

    status_code = 409
    code = "GOVERNANCE_EXISTS"

    def __init__(self) -> None:
        super().__init__(
            "Governance handle already in use; pass 'supersedes' to create the next version."
        )


class GovernancePrivilegeClaimError(DomainError):
    """Content appears to claim infrastructure powers (422, spec §15).

    Honest scope: this is the best-effort lint described in the module
    docstring — a visibility aid for accidental privilege language, NOT the
    security boundary (which is structural: the authorization engine never
    reads governance records).
    """

    status_code = 422
    code = "GOVERNANCE_PRIVILEGE_CLAIM"

    def __init__(self) -> None:
        super().__init__(
            "Governance content cannot claim infrastructure privileges "
            "(admin rights, grants/revocations, secrets, emergency-stop control); "
            "governance records are inert collaboration artifacts and never affect "
            "infrastructure security policy.",
            check="privilege_language",
        )


class VersionConflictError(DomainError):
    """Version could not be assigned after retries (409)."""

    status_code = 409
    code = "VERSION_CONFLICT"

    def __init__(self) -> None:
        super().__init__("Could not assign a governance version; retry the write.")


def _resolve_uuid(ref: str) -> str | None:
    try:
        return str(uuidlib.UUID(ref))
    except (ValueError, AttributeError, TypeError):
        return None


def governance_aad(kind: str, handle: str, version: int) -> str:
    """AAD binding a governance envelope to its kind/handle/version (spec §10.2)."""
    return f"governance:{kind}:{handle}:{version}"


def scan_privilege_language(content: str) -> str | None:
    """Return the first matching privilege term, or ``None`` (best-effort lint).

    Case-insensitive literal-substring scan of the whole content. Trivially
    bypassable — documented honestly; the real boundary is structural.
    """
    lowered = content.lower()
    for term in PRIVILEGE_TERMS:
        if term.lower() in lowered:
            return term
    return None


def latest_version_of(session: Session, handle: str) -> GovernanceRecord | None:
    """The highest-version record of a handle (the lineage head), or ``None``."""
    stmt = (
        select(GovernanceRecord)
        .where(GovernanceRecord.handle == handle)
        .order_by(GovernanceRecord.version.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def get_record(session: Session, ref: str) -> GovernanceRecord | None:
    """Look up a record by id (UUID) or by handle (resolves the latest version)."""
    record_id = _resolve_uuid(ref)
    if record_id is not None:
        record = session.get(GovernanceRecord, record_id)
        if record is not None:
            return record
    return latest_version_of(session, ref)


def create_record(
    session: Session,
    author: Agent | AuthenticatedAgent,
    *,
    handle: str,
    kind: str,
    content: str,
    supersedes: str | None = None,
    cipher: EnvelopeCipher,
    max_bytes: int,
) -> GovernanceRecord:
    """Create a governance record version (the caller owns the transaction).

    Order of checks: handle pattern → privilege lint (422 + ``policy_violation``
    event with content-free details) → supersedes/lineage resolution → content
    size (413) → seal → insert → supersede the previous latest version.
    """
    if not isinstance(handle, str) or not HANDLE_PATTERN.fullmatch(handle):
        raise GovernanceHandleInvalidError()

    matched_term = scan_privilege_language(content)
    if matched_term is not None:
        # Event details carry STATIC check identifiers only — never content,
        # never the matched fragment in context.
        record_event(
            session,
            SecurityEventCategory.POLICY_VIOLATION,
            author.id,
            {"check": "privilege_language", "domain": "governance"},
        )
        raise GovernancePrivilegeClaimError()

    if supersedes is not None:
        if supersedes != handle:
            raise InvalidPayloadError(
                "'supersedes' must reference the record's own handle "
                "(version lineages are per-handle; cross-handle merging is not offered)."
            )
        if latest_version_of(session, handle) is None:
            raise UnknownGovernanceRecordError()
    elif latest_version_of(session, handle) is not None:
        raise GovernanceExistsError()

    size_bytes = len(content.encode("utf-8"))
    if size_bytes > max_bytes:
        raise PayloadTooLargeError(size_bytes, max_bytes)
    if size_bytes == 0:
        raise InvalidPayloadError("Content must not be empty.")

    kind_value = GovernanceKind(kind)
    handle, author_id = handle, author.id

    record: GovernanceRecord | None = None
    for attempt in range(_VERSION_ATTEMPTS):
        if supersedes is not None:
            max_version = session.scalar(
                select(func.max(GovernanceRecord.version)).where(GovernanceRecord.handle == handle)
            )
            version = int(max_version or 0) + 1
        else:
            version = 1
        envelope = cipher.seal(content, governance_aad(kind_value.value, handle, version))
        record = GovernanceRecord(
            id=new_uuid(),
            handle=handle,
            kind=kind_value,
            version=version,
            envelope=envelope.model_dump(),
            author_id=author_id,
            status=GovernanceStatus.DRAFT,
            supersedes_id=None,
        )
        session.add(record)
        try:
            session.flush()
            break
        except IntegrityError:
            # Concurrent writer took this (kind, handle, version) slot:
            # rollback and recompute the version.
            session.rollback()
            record = None
            if attempt == _VERSION_ATTEMPTS - 1:
                raise VersionConflictError() from None
    if record is None:  # pragma: no cover - loop always sets record or raises
        raise VersionConflictError()

    if supersedes is not None:
        # Two-phase supersede: the new row is inserted, and only now is the
        # previous latest version marked superseded (re-read AFTER the insert;
        # the new row itself is excluded from the lookup).
        previous = session.scalar(
            select(GovernanceRecord)
            .where(GovernanceRecord.handle == handle, GovernanceRecord.id != record.id)
            .order_by(GovernanceRecord.version.desc())
            .limit(1)
        )
        if previous is not None and previous.status != GovernanceStatus.SUPERSEDED:
            previous.status = GovernanceStatus.SUPERSEDED
            session.flush()
    return record


def read_content(record: GovernanceRecord, cipher: EnvelopeCipher) -> str:
    """Decrypt a record's content with its AAD (verification included)."""
    kind_value = record.kind.value if hasattr(record.kind, "value") else str(record.kind)
    return cipher.open(
        Envelope(**record.envelope), governance_aad(kind_value, record.handle, record.version)
    )


def list_records(session: Session, kind: str | None = None) -> list[GovernanceRecord]:
    """All records (metadata order), optionally filtered by kind."""
    stmt = select(GovernanceRecord).order_by(GovernanceRecord.handle, GovernanceRecord.version)
    if kind is not None:
        stmt = stmt.where(GovernanceRecord.kind == GovernanceKind(kind))
    return list(session.scalars(stmt))


def history(session: Session, handle: str) -> list[GovernanceRecord]:
    """All versions of a handle, ascending by version."""
    stmt = (
        select(GovernanceRecord)
        .where(GovernanceRecord.handle == handle)
        .order_by(GovernanceRecord.version)
    )
    return list(session.scalars(stmt))


def author_handle(session: Session, record: GovernanceRecord) -> str:
    """The author's handle (or ``"unknown"`` defensively)."""
    author = session.get(Agent, record.author_id)
    return author.handle if author is not None else "unknown"
