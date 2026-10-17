"""Service health endpoint (no authentication — availability only, spec §20).

Deliberately does NOT report database or subsystem state: it answers "is the
API process alive" for infrastructure health checks without exposing
operational detail publicly.
"""

from fastapi import APIRouter

from commons_core import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness check: service availability only."""
    return {"status": "ok", "service": "commons-core", "version": __version__}
