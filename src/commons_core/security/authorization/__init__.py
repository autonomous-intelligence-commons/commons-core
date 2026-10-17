"""Authorization subpackage: deny-by-default capability model (spec §8, §15).

CONTRACT for later agents:
- Protect routes with ``Depends(require_permission(permission))`` or
  ``Depends(require_admin)``.
- Call ``authorize(session, agent, permission)`` from domain services for
  every protected operation (in addition to route-level checks).

Structural rule: authorization decisions are derived ONLY from database
state (agent status, memberships, permissions, is_admin flag). Governance
records, messages and memory entries are agent-generated content and can
NEVER influence authorization — see
``commons_core.security.authorization.policies.server_policy``.
"""

from commons_core.security.authorization.dependencies import require_admin, require_permission
from commons_core.security.authorization.engine import Permission, authorize
from commons_core.security.authorization.errors import AuthorizationError

__all__ = [
    "AuthorizationError",
    "Permission",
    "authorize",
    "require_admin",
    "require_permission",
]
