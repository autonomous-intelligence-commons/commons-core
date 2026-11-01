"""HTTP middleware: request IDs and rate limiting (spec §19, §21).

Request flow: ``RequestIDMiddleware`` (outer) → ``RateLimitMiddleware`` →
application. The rate limiter pre-authenticates bearer tokens (stashing the
result in ``request.state``) so token verification — including its PBKDF2
hashing — happens exactly once per request, and per-agent limiting works
before route dependencies run.

Exempt paths (never rate limited): ``/health``, ``/docs``, ``/redoc``,
``/openapi.json`` — infrastructure availability endpoints.

Admission authority (R06-B F.2 — owner decisions D1/D2/D3):

- AUTHENTICATED Agent requests are admitted by the database-atomic
  conditional UPSERT (``admit_request``) inside a dedicated short
  transaction, committed BEFORE the request is forwarded downstream: one
  authoritative increment per admitted request, shared across all workers
  pointing at the same database (B1 remediated). The former best-effort
  telemetry call (``persist_request_count``) is retired from this path
  (D3) — the admission statement IS the durable request counter.
- Failure policy is FAIL-CLOSED (D2): if the authoritative admission
  cannot be completed (database unavailable, statement/lock timeout,
  rollback, ...), the request is NOT admitted through any in-memory
  fallback and receives HTTP 503 ``SERVICE_UNAVAILABLE`` with a
  conservative ``Retry-After``. A quota denial (shared limit reached) is a
  different condition and keeps the ordinary HTTP 429 contract below. An
  authentication-infrastructure failure is likewise surfaced as 503, not
  disguised as an invalid-credential 401.
- ANONYMOUS requests keep the in-memory per-process limiter (privacy: no
  IP-keyed persistent quota rows) and never depend on the database for
  admission.

429 responses are structured JSON and include ``Retry-After``; a
``rate_limited`` security event is recorded (principal = agent id for
authenticated callers, ``anonymous`` otherwise — never an IP address). If
that event cannot be recorded, the denial itself stands.
"""

import logging
import uuid
from datetime import datetime

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from commons_core.database.enums import SecurityEventCategory
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.tokens import authenticate
from commons_core.security.events import ANONYMOUS, record_event
from commons_core.security.rate_limit import admit_request

_LOGGER = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})

#: Conservative retry hint for fail-closed 503 responses (seconds).
_SERVICE_UNAVAILABLE_RETRY_AFTER_S = 5


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _service_unavailable_response() -> JSONResponse:
    """Fail-closed HTTP 503 (D2). Privacy-safe by construction: no SQL,
    driver message, hostname, connection string, token or traceback ever
    leaves the process boundary."""
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": "Service temporarily unavailable. Please retry later.",
                "retry_after_seconds": _SERVICE_UNAVAILABLE_RETRY_AFTER_S,
            }
        },
        headers={"Retry-After": str(_SERVICE_UNAVAILABLE_RETRY_AFTER_S)},
    )


def _rate_limited_response(
    database,
    *,
    event_principal: str,
    limit: int,
    window_start: datetime,
    retry_after_seconds: int,
    authenticated: bool,
) -> JSONResponse:
    """HTTP 429 denial with its security event (best-effort).

    The denial itself must stand even when the event cannot be recorded
    (e.g. degraded database): an event-persistence failure must never
    accidentally admit the request.
    """
    try:
        with database.session() as session:
            record_event(
                session,
                SecurityEventCategory.RATE_LIMITED,
                event_principal,
                {
                    "limit": limit,
                    "window_start": window_start.isoformat(),
                    "authenticated": authenticated,
                },
            )
    except Exception as exc:  # noqa: BLE001 — preserve the denial regardless
        _LOGGER.warning("rate-limit event recording failed class=%s", type(exc).__name__)
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMITED",
                "message": "Request rate limit exceeded.",
                "retry_after_seconds": retry_after_seconds,
                "limit": limit,
            }
        },
        headers={"Retry-After": str(retry_after_seconds)},
    )


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique ``X-Request-Id`` to every request/response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting with database-atomic shared admission.

    Authenticated Agent requests are admitted by the conditional UPSERT
    (``admit_request``) — the authoritative, cross-worker decision (R06-B
    F.2). Failure of that admission is FAIL-CLOSED (HTTP 503, never an
    in-memory fallback); quota denial keeps the HTTP 429 contract.
    Anonymous traffic keeps the in-memory per-process limiter and is never
    persisted.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        database = request.app.state.database
        limiter = request.app.state.rate_limiter

        agent = None
        raw_token = _bearer_token(request)
        if raw_token is not None:
            request.state.auth_attempted = True
            try:
                with database.session() as session:
                    agent = authenticate(session, raw_token)
            except AuthenticationError:
                agent = None  # the auth dependency will report the failure
            except Exception as exc:  # noqa: BLE001 — auth infrastructure failure
                # Fail-closed (D2): a database/authentication-infrastructure
                # failure must not be disguised as an invalid credential
                # (401) nor silently degrade into the anonymous bucket.
                _LOGGER.warning("pre-authentication error class=%s", type(exc).__name__)
                request.state.authenticated_agent = None
                return _service_unavailable_response()
            request.state.authenticated_agent = agent

        if agent is not None:
            return await self._admit_authenticated(request, call_next, database, limiter, agent)

        # Anonymous principal (missing or invalid credentials): the
        # in-memory per-process limiter — no database dependency, nothing
        # persisted (privacy invariant).
        host = request.client.host if request.client else "unknown"
        principal = f"anonymous:{host}"
        decision = limiter.check(principal)
        if decision.allowed:
            return await call_next(request)
        return _rate_limited_response(
            database,
            event_principal=ANONYMOUS,
            limit=decision.limit,
            window_start=decision.window_start,
            retry_after_seconds=decision.retry_after_seconds,
            authenticated=False,
        )

    async def _admit_authenticated(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
        database,
        limiter,
        agent,
    ) -> Response:
        """Authoritative shared admission for an authenticated Agent.

        One conditional UPSERT inside a dedicated short transaction,
        committed before the request is forwarded downstream. One admitted
        request produces exactly one authoritative request-counter
        increment (the legacy telemetry call is retired — D3).
        """
        limit = request.app.state.settings.rate_limit_per_minute
        window, retry_after = limiter.current_window_and_retry()
        try:
            with database.session() as session:
                new_count = admit_request(session, agent.id, window, limit=limit)
        except Exception as exc:  # noqa: BLE001 — fail-closed (D2)
            # The authoritative admission could not be completed: the
            # request must NOT proceed downstream and must NOT fall back to
            # any worker-local limiter (RateLimiter or otherwise).
            _LOGGER.warning("authoritative admission failed class=%s", type(exc).__name__)
            return _service_unavailable_response()
        if new_count is None:
            # Shared limit reached: the ordinary 429 contract (distinct
            # from an admission-infrastructure failure).
            return _rate_limited_response(
                database,
                event_principal=agent.id,
                limit=limit,
                window_start=window,
                retry_after_seconds=retry_after,
                authenticated=True,
            )
        return await call_next(request)
