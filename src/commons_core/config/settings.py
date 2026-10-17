"""Application settings (Pydantic Settings, environment prefix ``AIC_``).

All configuration is injected through environment variables (or a local
``.env`` file); nothing secret is ever stored in source control (spec §10.3).

Key management note (spec §10.3): when ``AIC_MASTER_KEYS`` is empty the
cryptography layer generates an *ephemeral* in-memory key and logs a loud
warning — disposable development keys only; data encrypted with it is
unrecoverable after restart.

Contract for later agents: obtain settings via ``get_settings()`` (cached
singleton) or receive a ``Settings`` instance explicitly (tests, app factory).
Never read ``os.environ`` directly in domain modules.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for commons-core.

    Fields map to ``AIC_<FIELD>`` environment variables, e.g. ``AIC_DATABASE_URL``.
    """

    model_config = SettingsConfigDict(
        env_prefix="AIC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage -----------------------------------------------------------
    # Local development uses SQLite; production targets PostgreSQL via the
    # same SQLAlchemy layer (e.g. postgresql+psycopg://...). Honest limitation:
    # only SQLite is exercised in this sandbox.
    database_url: str = "sqlite+pysqlite:///./data/commons.db"

    # --- Cryptography (spec §10) -------------------------------------------
    # Format: "kid1:<64 hex chars>,kid2:<64 hex chars>". Empty -> ephemeral
    # in-memory dev key (data lost on restart).
    master_keys: str = ""
    active_key_id: str = "k1"

    # --- API -----------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    env: str = "development"

    # --- Quotas / limits (spec §19) ------------------------------------------
    rate_limit_per_minute: int = 120
    # Anonymous (unauthenticated) requests are throttled more tightly.
    anonymous_rate_limit_per_minute: int = 30
    # Per-agent message-send quota (durable QuotaCounter fixed window).
    max_messages_per_minute: int = 60
    max_message_bytes: int = 65536
    max_memory_entry_bytes: int = 65536
    max_pending_tasks: int = 1000
    max_task_attempts: int = 3
    task_timeout_seconds: int = 30
    retention_security_events_days: int = 14

    # --- Logging (spec §11/§20) -----------------------------------------------
    # Privacy rule: logs carry categories, handles/ids and counts only —
    # never payload, message, memory, governance content or credentials.
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the cached singleton settings (reads ``AIC_*`` env vars once)."""
    return Settings()
