"""Authentication subpackage: hashed token credentials (spec §7, §8).

Contract for later agents: protect routes with
``require_authenticated_agent``; never accept or store raw tokens anywhere
else. All failures are recorded as ``auth_failure`` security events and
surface as a generic 401 (no reason details leaked to callers).
"""

from commons_core.security.authentication.dependencies import require_authenticated_agent
from commons_core.security.authentication.errors import AuthenticationError
from commons_core.security.authentication.identity import AuthenticatedAgent
from commons_core.security.authentication.tokens import (
    IssuedToken,
    authenticate,
    issue_token,
    verify_token,
)

__all__ = [
    "AuthenticatedAgent",
    "AuthenticationError",
    "IssuedToken",
    "authenticate",
    "issue_token",
    "require_authenticated_agent",
    "verify_token",
]
