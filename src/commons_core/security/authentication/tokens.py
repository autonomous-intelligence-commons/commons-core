"""Hashed API-token credentials (spec §7).

Verification design:
- Raw tokens are ``secrets.token_urlsafe(32)`` (~256-bit), shown ONCE at
  provisioning and never stored or logged.
- Storage: PBKDF2-HMAC-SHA256(raw, salt, 200_000 iterations) hex hash + hex
  salt + 8-character prefix used as a non-secret lookup hint.
- Lookup by ``token_prefix``, then constant-time comparison
  (``hmac.compare_digest``) against every candidate hash.
- Verification enforces credential status, expiry, and agent status
  (suspended/revoked agents fail authentication).

PRIVACY RULE (spec §11/§20): raw tokens, hashes and salts are never logged.
"""

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from commons_core.database.enums import AgentStatus, CredentialStatus
from commons_core.database.models import Agent, AgentCredential, new_uuid
from commons_core.infrastructure.time import as_utc, utc_now
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.identity import AuthenticatedAgent

#: PBKDF2 iteration count (task spec; OWASP-class parameterization).
PBKDF2_ITERATIONS = 200_000
#: Non-secret prefix used to narrow the credential lookup.
TOKEN_PREFIX_LENGTH = 8
_SALT_BYTES = 16
_HASH_BYTES = 32


class IssuedToken:  # noqa: D101 — documented via attributes
    """Result of issuing a credential: carries the raw token ONCE."""

    __slots__ = ("raw_token", "credential_id", "agent_id", "token_prefix")

    def __init__(
        self, raw_token: str, credential_id: str, agent_id: str, token_prefix: str
    ) -> None:
        self.raw_token = raw_token
        self.credential_id = credential_id
        self.agent_id = agent_id
        self.token_prefix = token_prefix

    def __repr__(self) -> str:  # privacy-safe: never the raw token
        return (
            f"IssuedToken(credential_id={self.credential_id!r}, "
            f"agent_id={self.agent_id!r}, token_prefix={self.token_prefix!r})"
        )


def _hash_token(raw_token: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", raw_token.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def issue_token(
    session: Session,
    agent_id: str,
    *,
    expires_at=None,
    rotated_from: str | None = None,
) -> IssuedToken:
    """Issue a new credential for ``agent_id`` and return the raw token once.

    The caller owns the transaction (this function flushes). ``rotated_from``
    links the credential to the one it replaces during rotation.
    """
    raw_token = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(_SALT_BYTES)
    credential = AgentCredential(
        id=new_uuid(),
        agent_id=agent_id,
        token_hash=_hash_token(raw_token, salt),
        token_prefix=raw_token[:TOKEN_PREFIX_LENGTH],
        salt=salt.hex(),
        status=CredentialStatus.ACTIVE,
        expires_at=expires_at,
        rotated_from=rotated_from,
    )
    session.add(credential)
    session.flush()
    return IssuedToken(
        raw_token=raw_token,
        credential_id=credential.id,
        agent_id=agent_id,
        token_prefix=credential.token_prefix,
    )


def revoke_credential(session: Session, credential: AgentCredential) -> AgentCredential:
    """Mark a credential revoked (used by rotation and revocation flows)."""
    credential.status = CredentialStatus.REVOKED
    session.flush()
    return credential


def verify_token(session: Session, raw_token: str) -> str:
    """Verify a raw bearer token and return the authenticated ``agent_id``.

    Raises :class:`AuthenticationError` for unknown, malformed, revoked,
    expired credentials or suspended/revoked agents. The error reason is for
    internal use only; callers receive a generic 401.
    """
    raw_token = raw_token.strip()
    if len(raw_token) <= TOKEN_PREFIX_LENGTH:
        raise AuthenticationError("malformed token")

    prefix = raw_token[:TOKEN_PREFIX_LENGTH]
    candidates = list(
        session.scalars(select(AgentCredential).where(AgentCredential.token_prefix == prefix))
    )

    for credential in candidates:
        computed_hash = _hash_token(raw_token, bytes.fromhex(credential.salt))
        if not hmac.compare_digest(computed_hash, credential.token_hash):
            continue
        # Token matches this credential — now enforce its state.
        if credential.status != CredentialStatus.ACTIVE:
            raise AuthenticationError("credential revoked")
        if credential.expires_at is not None and as_utc(credential.expires_at) <= utc_now():
            raise AuthenticationError("credential expired")
        agent = session.get(Agent, credential.agent_id)
        if agent is None:
            raise AuthenticationError("agent missing")
        if agent.status == AgentStatus.SUSPENDED:
            raise AuthenticationError("agent suspended")
        if agent.status == AgentStatus.REVOKED:
            raise AuthenticationError("agent revoked")
        return agent.id

    raise AuthenticationError("invalid token")


def authenticate(session: Session, raw_token: str) -> AuthenticatedAgent:
    """Verify a bearer token and return an identity snapshot.

    Single entry point shared by the rate-limit middleware and the
    ``require_authenticated_agent`` dependency, so each request performs
    exactly one token verification.
    """
    agent_id = verify_token(session, raw_token)
    agent = session.get(Agent, agent_id)
    if agent is None:  # defensive; verify_token already ensured existence
        raise AuthenticationError("agent missing")
    return AuthenticatedAgent.from_agent(agent)
