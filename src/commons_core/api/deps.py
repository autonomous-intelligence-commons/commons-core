"""Request-scoped dependencies: database session, cipher, settings, pause check.

CONTRACT for domain routers: obtain the per-request session via
``Depends(get_session)`` and the application-scoped cipher/settings via
``Depends(get_cipher)`` / ``Depends(get_app_settings)`` — never reach into
module-level singletons.

``Depends(require_not_paused)`` guards NEW-WRITE endpoints (message send,
memory write, task create, activity create): while the infrastructure
emergency stop is engaged it raises 503 ``SERVICE_PAUSED`` (spec §16/§21 —
pause halts new writes and worker claims, preserves reads). FastAPI caches
dependencies per request, so the system-state row is read once per request
through the same shared session.
"""

from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from commons_core.api.errors import ServicePausedError
from commons_core.config.settings import Settings
from commons_core.infrastructure.system_state import emergency_stop_active
from commons_core.security.cryptography import EnvelopeCipher


async def get_session(request: Request) -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on error."""
    database = request.app.state.database
    session = database.sessionmaker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_cipher(request: Request) -> EnvelopeCipher:
    """Return the application's envelope cipher (spec §10.2)."""
    return request.app.state.cipher


def get_app_settings(request: Request) -> Settings:
    """Return the application's runtime settings."""
    return request.app.state.settings


async def require_not_paused(session: Session = Depends(get_session)) -> None:
    """Reject new-write requests while the emergency stop is engaged (503).

    Reads are NOT guarded — the pause preserves read access (documented
    semantics, see ``commons_core.api.errors.ServicePausedError``). This
    dependency is request-cached by FastAPI: the system-state row is read
    at most once per request, through the same shared session the route
    itself uses.
    """
    if emergency_stop_active(session):
        raise ServicePausedError()
