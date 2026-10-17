"""Authentication errors.

Carriers carry a private ``reason`` for operational logging (category-level
only); the HTTP layer always returns a GENERIC 401 so callers learn nothing
beyond "not authenticated" (spec §8, no detail leaks).
"""


class AuthenticationError(Exception):
    """Raised when a request cannot be authenticated (generic 401)."""

    def __init__(self, reason: str) -> None:
        super().__init__("authentication failed")
        self.reason = reason
