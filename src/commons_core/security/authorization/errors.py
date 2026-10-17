"""Authorization errors.

Raised on denial (deny-by-default). The API layer maps this to a generic
HTTP 403 ``{"error": {"code": "FORBIDDEN", ...}}`` without leaking which
resource was involved.
"""


class AuthorizationError(Exception):
    """Raised when an operation is not authorized (generic 403)."""

    def __init__(self, permission: str) -> None:
        super().__init__("operation not authorized")
        self.permission = permission
