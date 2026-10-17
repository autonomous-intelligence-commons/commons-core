"""FastAPI authentication dependency (spec §7, §8).

CONTRACT for later agents: protect every agent-facing route with

    ``agent: AuthenticatedAgent = Depends(require_authenticated_agent)``

Failure behavior: a ``SecurityEvent(auth_failure, principal="anonymous")`` is
recorded and the request fails with HTTP 401 ``{"error": {"code":
"UNAUTHENTICATED", ...}}``. Reasons are never leaked to the caller.

Cooperation with the rate-limit middleware: the middleware pre-authenticates
requests that carry a ``Bearer`` token and stores the snapshot in
``request.state.authenticated_agent`` (with ``request.state.auth_attempted``
set); the dependency reuses that snapshot so each request performs exactly
one PBKDF2 verification. When the middleware did not attempt authentication
(no token header) the dependency records the failure itself.
"""

import logging

from fastapi import Request

from commons_core.database.enums import SecurityEventCategory
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authentication.tokens import authenticate
from commons_core.security.events import ANONYMOUS, record_event

_LOGGER = logging.getLogger(__name__)


def _bearer_token(request: Request) -> str | None:
    """Extract the raw bearer token from the Authorization header, if any."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _record_auth_failure(request: Request, reason: str) -> None:
    """Record an auth_failure event on a fresh session (never breaks the path)."""
    database = request.app.state.database
    try:
        with database.session() as session:
            record_event(session, SecurityEventCategory.AUTH_FAILURE, ANONYMOUS, {})
    except Exception:  # noqa: BLE001 — event recording is best-effort
        _LOGGER.warning("failed to persist auth_failure security event")
    _LOGGER.info("authentication failed principal=%s reason=%s", ANONYMOUS, reason)


async def require_authenticated_agent(request: Request) -> AuthenticatedAgent:
    """FastAPI dependency: require a valid bearer token, return identity snapshot."""
    agent = getattr(request.state, "authenticated_agent", None)
    if agent is not None:
        return agent

    if getattr(request.state, "auth_attempted", False):
        # The middleware already attempted verification and it failed.
        _record_auth_failure(request, "rejected by pre-check")
        raise AuthenticationError("invalid or revoked credentials")

    raw_token = _bearer_token(request)
    if raw_token is None:
        _record_auth_failure(request, "missing bearer token")
        raise AuthenticationError("missing bearer token")

    database = request.app.state.database
    try:
        with database.session() as session:
            verified = authenticate(session, raw_token)
    except AuthenticationError as exc:
        _record_auth_failure(request, exc.reason)
        raise
    except Exception as exc:  # noqa: BLE001 — fail closed on unexpected errors
        _LOGGER.warning("authentication backend error class=%s", type(exc).__name__)
        _record_auth_failure(request, "backend error")
        raise AuthenticationError("authentication backend error") from exc

    request.state.authenticated_agent = verified
    return verified
