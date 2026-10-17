"""HTTP middleware: request IDs and rate limiting (spec §19, §21).

Request flow: ``RequestIDMiddleware`` (outer) → ``RateLimitMiddleware`` →
application. The rate limiter pre-authenticates bearer tokens (stashing the
result in ``request.state``) so token verification — including its PBKDF2
hashing — happens exactly once per request, and per-agent limiting works
before route dependencies run.

Exempt paths (never rate limited): ``/health``, ``/docs``, ``/redoc``,
``/openapi.json`` — infrastructure availability endpoints.

429 responses are structured JSON and include ``Retry-After``; a
``rate_limited`` security event is recorded (principal = agent id for
authenticated callers, ``anonymous`` otherwise — never an IP address).
"""

import logging
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from commons_core.database.enums import SecurityEventCategory
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.tokens import authenticate
from commons_core.security.events import ANONYMOUS, record_event
from commons_core.security.rate_limit import persist_request_count

_LOGGER = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique ``X-Request-Id`` to every request/response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limiting; anonymous traffic is bucketed by client host.

    The in-memory limiter is authoritative (see
    ``commons_core.security.rate_limit``); authenticated counts are mirrored
    into ``QuotaCounter`` best-effort.
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
            except Exception as exc:  # noqa: BLE001 — fail safe, still rate-limit
                _LOGGER.warning("pre-authentication error class=%s", type(exc).__name__)
                agent = None
            request.state.authenticated_agent = agent

        if agent is not None:
            principal = agent.id
        else:
            host = request.client.host if request.client else "unknown"
            principal = f"anonymous:{host}"

        decision = limiter.check(principal)

        if decision.allowed:
            if agent is not None:
                try:
                    with database.session() as session:
                        persist_request_count(session, agent.id, decision.window_start)
                except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                    _LOGGER.warning("quota counter update failed class=%s", type(exc).__name__)
            return await call_next(request)

        event_principal = agent.id if agent is not None else ANONYMOUS
        with database.session() as session:
            record_event(
                session,
                SecurityEventCategory.RATE_LIMITED,
                event_principal,
                {
                    "limit": decision.limit,
                    "window_start": decision.window_start.isoformat(),
                    "authenticated": agent is not None,
                },
            )
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "Request rate limit exceeded.",
                    "retry_after_seconds": decision.retry_after_seconds,
                    "limit": decision.limit,
                }
            },
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
