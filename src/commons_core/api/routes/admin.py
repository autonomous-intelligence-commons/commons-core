"""Infrastructure-admin endpoints (spec §3.C, §20, §21).

``GET /api/v1/admin/telemetry`` — privacy-preserving operational telemetry
(counters and infrastructure state ONLY). Requires the ``admin:*``
capability, which only the provisioning service's ``is_admin`` flag grants.

Emergency stop (spec §16/§21): ``POST /api/v1/admin/emergency-stop`` engages
and ``DELETE`` releases the infrastructure-wide pause. Admin-only (denials
record ``authz_denial`` events). EFFECT (documented pause semantics):
- the worker refuses to claim/execute new work;
- NEW-WRITE agent endpoints reject with 503 ``SERVICE_PAUSED`` (message
  send, memory write, task create, activity create);
- READS continue to be served; governance record creation remains available
  (records are inert versioned artifacts — spec §15 — and do not create
  execution work);
- releasing the stop resumes normal operation; nothing is destroyed.

Deliberate exclusions (spec §3.C): administrators maintain the platform;
they do NOT receive channel/memory content, handles, or permission-granting
powers over private namespaces.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from commons_core.api.deps import get_session
from commons_core.api.errors import EmergencyStopUnconfirmedError
from commons_core.infrastructure.system_state import (
    EmergencyStopStateError,
    set_emergency_stop,
)
from commons_core.infrastructure.telemetry import collect_telemetry
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authorization.dependencies import require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/telemetry")
async def read_telemetry(
    request: Request,
    agent: AuthenticatedAgent = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Operational telemetry snapshot (admin only; counters only)."""
    return collect_telemetry(session, request.app.state)


@router.post("/emergency-stop")
async def engage_emergency_stop(
    agent: AuthenticatedAgent = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Engage the infrastructure emergency stop (admin only, spec §21).

    New writes and worker claims are rejected (503 ``SERVICE_PAUSED`` /
    worker idles); reads continue. An ``emergency_stop`` security event is
    recorded (action only). Idempotent: engaging while engaged refreshes
    the stored state.

    R04 durability: a 200 response confirms the stop is durably committed
    and attributed to this administrator's command; if the required state
    transition cannot be confirmed the endpoint responds 503
    ``EMERGENCY_STOP_UNCONFIRMED`` — never a false success (R04-INV-01).
    Success is a state-convergence contract (R04-REV-01): an idempotent
    re-issuance is equivalent to the original command, and a concurrent
    command from another administrator may supersede the state after the
    response (last-writer-wins; admin telemetry is authoritative).
    """
    try:
        state = set_emergency_stop(session, active=True, principal=agent.id)
    except EmergencyStopStateError:
        # Required transition unconfirmed — report non-success without
        # leaking internal database details (R04-INV-01/03).
        raise EmergencyStopUnconfirmedError() from None
    return {"emergency_stop": state}


@router.delete("/emergency-stop")
async def release_emergency_stop(
    agent: AuthenticatedAgent = Depends(require_admin),
    session: Session = Depends(get_session),
) -> dict:
    """Release the emergency stop (admin only); normal operation resumes.

    R04 durability: a 200 response confirms the release is durably
    committed and attributed to this administrator's command; an
    unconfirmable transition responds 503 ``EMERGENCY_STOP_UNCONFIRMED``
    — a failed release is never reported as a successful release.
    State-convergence semantics apply symmetrically (R04-REV-01).
    """
    try:
        state = set_emergency_stop(session, active=False, principal=agent.id)
    except EmergencyStopStateError:
        raise EmergencyStopUnconfirmedError() from None
    return {"emergency_stop": state}
