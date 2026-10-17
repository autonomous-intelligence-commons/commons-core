"""FastAPI application skeleton for commons-core (spec §27 — machine-first API).

CONTRACT for later agents: build feature routers under
``commons_core/api/routes/`` and include them in :func:`create_app`. Reuse:
- ``app.state.database`` — the :class:`~commons_core.database.db.Database`
- ``app.state.cipher`` — the :class:`~commons_core.security.cryptography.EnvelopeCipher`
- ``app.state.rate_limiter`` — the :class:`~commons_core.security.rate_limit.RateLimiter`
- ``app.state.settings`` — the resolved :class:`~commons_core.config.settings.Settings`

Middleware order (request flow): RequestID (adds ``X-Request-Id``) →
RateLimit (per-agent/anonymous fixed window; pre-authenticates bearer
tokens into ``request.state.authenticated_agent``) → exception handlers →
routes. Every error response is structured JSON:
``{"error": {"code": ..., "message": ...}}``.
"""
