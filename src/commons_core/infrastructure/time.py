"""Time helpers.

SQLAlchemy + SQLite stores ``DateTime(timezone=True)`` columns as naive UTC
strings (verified experimentally); PostgreSQL with the same column type
round-trips timezone-aware values. All comparisons therefore normalize through
:func:`as_utc` so behavior is identical on both backends.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize a datetime to aware UTC (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
