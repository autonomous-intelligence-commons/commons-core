"""Alembic migration environment for commons-core.

The database URL comes from application settings (``AIC_DATABASE_URL`` env
var / ``.env`` file), never from the checked-in ini file. All models are
imported so ``Base.metadata`` is complete for autogeneration support.

``compare_type=True`` is enabled so future autogenerate runs detect column
type changes; ``render_as_batch=True`` is enabled because SQLite requires
batch mode for table alteration.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make the src layout importable regardless of the working directory.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from commons_core.config.settings import get_settings  # noqa: E402
from commons_core.database import models  # noqa: E402,F401  (registers all tables)
from commons_core.database.base import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the URL from application settings (AIC_DATABASE_URL).
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
        dialect_name="sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with an application-style engine."""
    from commons_core.database.db import create_engine_for

    connectable = create_engine_for(settings.database_url)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
