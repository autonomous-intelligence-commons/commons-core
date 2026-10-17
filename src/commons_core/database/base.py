"""Declarative base with a stable constraint naming convention.

Explicit naming is required for future Alembic migrations (especially SQLite
batch operations, which address constraints by name).
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s",
    "fk": "fk_%(table_0_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Common declarative base for every commons-core model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
