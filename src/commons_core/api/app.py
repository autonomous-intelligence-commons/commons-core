"""FastAPI application factory (spec §27).

Wires configuration, database, cryptography, rate limiting, structured error
handling and the foundation routes. Machine-first: OpenAPI at ``/docs`` and
``/openapi.json``; every error body is ``{"error": {"code", "message"}}``.

Privacy in error handling: unexpected exceptions are logged with their class
name and request context only (no tracebacks / driver messages, which could
embed content), and 500 responses reveal nothing beyond ``INTERNAL``.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from commons_core import __version__
from commons_core.activities.routes import router as activities_router
from commons_core.api.errors import DomainError
from commons_core.api.middleware import RateLimitMiddleware, RequestIDMiddleware
from commons_core.api.routes.admin import router as admin_router
from commons_core.api.routes.agents import router as agents_router
from commons_core.api.routes.health import router as health_router
from commons_core.communication.routes import router as communication_router
from commons_core.config.settings import Settings, get_settings
from commons_core.config.validation import validate_production_startup
from commons_core.database.db import Database
from commons_core.governance.routes import router as governance_router
from commons_core.infrastructure.logging import configure_logging
from commons_core.infrastructure.time import utc_now
from commons_core.memory.routes import router as memory_router
from commons_core.scheduler.routes import router as scheduler_router
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authorization.errors import AuthorizationError
from commons_core.security.cryptography import (
    CryptographyError,
    EnvelopeCipher,
    EnvelopeFormatError,
    KeyStore,
    TamperDetectedError,
    UnknownKeyError,
)
from commons_core.security.rate_limit import RateLimiter

_LOGGER = logging.getLogger(__name__)

_HTTP_CODE_BY_STATUS = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def _error_body(code: str, message: str, **extra) -> dict:
    """Build a structured machine-readable error body."""
    error: dict = {"code": code, "message": message}
    error.update(extra)
    return {"error": error}


def _register_exception_handlers(app: FastAPI) -> None:
    """Register global structured-JSON exception handlers."""

    @app.exception_handler(AuthenticationError)
    async def _authentication_error(_: Request, exc: AuthenticationError) -> JSONResponse:
        # Generic message: never leak the failure reason to callers.
        return JSONResponse(
            status_code=401,
            content=_error_body(
                "UNAUTHENTICATED", "Authentication required or invalid credentials."
            ),
        )

    @app.exception_handler(AuthorizationError)
    async def _authorization_error(_: Request, exc: AuthorizationError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content=_error_body("FORBIDDEN", "Not authorized for this operation."),
        )

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        # Domain errors carry machine-readable codes and counts/limits only —
        # never content (enforced by construction, see api/errors.py).
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, **exc.details),
        )

    @app.exception_handler(UnknownKeyError)
    async def _unknown_key(_: Request, exc: UnknownKeyError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_error_body("UNKNOWN_KEY", str(exc)))

    @app.exception_handler(TamperDetectedError)
    async def _tamper_detected(_: Request, exc: TamperDetectedError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_error_body("TAMPER_DETECTED", str(exc)))

    @app.exception_handler(EnvelopeFormatError)
    async def _envelope_format(_: Request, exc: EnvelopeFormatError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_error_body("ENVELOPE_FORMAT", str(exc)))

    @app.exception_handler(CryptographyError)
    async def _cryptography_error(_: Request, exc: CryptographyError) -> JSONResponse:
        return JSONResponse(status_code=400, content=_error_body("CRYPTOGRAPHY_ERROR", str(exc)))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Strip pydantic error context (which can echo request input values)
        # and keep only machine-readable locations/types/messages.
        details = [
            {"loc": list(error.get("loc", [])), "type": error.get("type"), "msg": error.get("msg")}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body("VALIDATION_ERROR", "Request validation failed.", details=details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODE_BY_STATUS.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # PRIVACY: log the exception class and request context only —
        # full tracebacks/driver messages could embed content values.
        _LOGGER.error(
            "unhandled exception class=%s path=%s request_id=%s",
            type(exc).__name__,
            request.url.path,
            getattr(request.state, "request_id", "-"),
        )
        return JSONResponse(
            status_code=500,
            content=_error_body("INTERNAL", "Internal server error."),
        )


def create_app(
    settings: Settings | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Build the commons-core FastAPI application.

    ``settings``/``database`` can be injected (tests); defaults come from
    ``get_settings()`` and a fresh :class:`Database`.

    Fail-closed startup (Phase 1 hardening): in ``AIC_ENV=production`` the
    factory validates security-critical configuration BEFORE wiring anything
    (SQLite datastore refused; ``AIC_MASTER_KEYS`` required) — an invalid
    configuration raises and the server never accepts a request.
    """
    settings = settings or get_settings()
    configure_logging(settings)
    validate_production_startup(settings, require_keys=True)
    database = database or Database(settings.database_url)
    key_store = KeyStore(settings.master_keys, settings.active_key_id, env=settings.env)

    app = FastAPI(
        title="Autonomous Intelligence Commons — Core",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.database = database
    app.state.cipher = EnvelopeCipher(key_store)
    app.state.started_at = utc_now()
    app.state.rate_limiter = RateLimiter(
        per_agent_limit=settings.rate_limit_per_minute,
        anonymous_limit=settings.anonymous_rate_limit_per_minute,
    )

    _register_exception_handlers(app)

    # Middleware: added in reverse execution order — RequestID runs first
    # (outermost), then the rate limiter, then the app/exception handlers.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestIDMiddleware)

    app.include_router(health_router)
    app.include_router(agents_router)
    app.include_router(communication_router)
    app.include_router(memory_router)
    app.include_router(activities_router)
    app.include_router(governance_router)
    app.include_router(scheduler_router)
    app.include_router(admin_router)
    return app


#: Module-level ASGI app for `uvicorn commons_core.api.app:app`.
#: The factory remains available for tests that need isolated settings.
app = create_app()
