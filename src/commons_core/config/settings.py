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

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Environments recognized by the application. ``production`` activates
#: fail-closed startup validation (commons_core.config.validation);
#: ``development``/``test`` permit the ephemeral dev-key fallback.
KNOWN_ENVS = frozenset({"development", "test", "production"})


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

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """Normalize the database URL dialect prefix.

        Platform-provided connection URLs (Railway ``${{Postgres.DATABASE_URL}}``,
        Heroku-style ``DATABASE_URL``) use the bare ``postgresql://`` (or legacy
        ``postgres://``) scheme, which SQLAlchemy maps to the psycopg2 dialect.
        The production image ships ONLY psycopg 3 (``psycopg[binary]``) — a
        bare scheme would fail at engine creation with ModuleNotFoundError.
        Rewrite the bare scheme to the explicit psycopg 3 dialect; the rest of
        the URL (including any percent-encoded userinfo) is preserved
        byte-for-byte. Explicit dialect suffixes (``postgresql+psycopg://``,
        ``postgresql+psycopg2://``) and SQLite URLs are never rewritten.
        """
        normalized = value.strip()
        for bare in ("postgresql://", "postgres://"):
            if normalized.startswith(bare):
                return "postgresql+psycopg://" + normalized[len(bare) :]
        return normalized

    # --- Cryptography (spec §10) -------------------------------------------
    # Format: "kid1:<64 hex chars>,kid2:<64 hex chars>". Empty -> ephemeral
    # in-memory dev key (data lost on restart).
    master_keys: str = ""
    active_key_id: str = "k1"

    # --- API -----------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8010
    # "development" | "test" | "production" — production enables fail-closed
    # startup checks (keys required, SQLite refused; see config/validation.py).
    env: str = "development"

    @field_validator("env")
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        """Normalize and validate the environment name (fail early on typos)."""
        normalized = value.strip().lower()
        if normalized not in KNOWN_ENVS:
            raise ValueError(f"AIC_ENV must be one of {sorted(KNOWN_ENVS)} (got {value!r})")
        return normalized

    @field_validator("effects_claim_deadline_seconds")
    @classmethod
    def _validate_claim_deadline(cls, value: int) -> int:
        if not 1 <= value <= 600:
            raise ValueError("effects_claim_deadline_seconds must be within 1..600")
        return value

    @field_validator("effects_ack_window_seconds")
    @classmethod
    def _validate_ack_window(cls, value: int) -> int:
        if not 1 <= value <= 3600:
            raise ValueError("effects_ack_window_seconds must be within 1..3600")
        return value

    @field_validator("effects_authorization_ttl_seconds")
    @classmethod
    def _validate_auth_ttl(cls, value: int) -> int:
        if not 10 <= value <= 86400:
            raise ValueError("effects_authorization_ttl_seconds must be within 10..86400")
        return value

    @field_validator("effects_max_in_flight")
    @classmethod
    def _validate_max_in_flight(cls, value: int) -> int:
        if not 1 <= value <= 10000:
            raise ValueError("effects_max_in_flight must be within 1..10000")
        return value

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

    # --- External effects (R02-B2 E1 — FAKE-ONLY; REAL EFFECTS PROHIBITED) -----
    # Master switch for the SYNTHETIC effect gateway. Fail-closed default:
    # False means every gateway request gets 403 EFFECTS_DISABLED and the
    # dispatcher scans NOTHING (existing rows are frozen, never cleared —
    # INV-EF-R11). Enabling this via environment exposes ONLY the synthetic
    # fx.* allowlist (code constants); real effects remain structurally
    # impossible until a separately reviewed E2 change (prompt §6.6).
    effects_enabled: bool = False
    # Dispatcher claim deadline (CLAIMED rows older than this are requeued).
    effects_claim_deadline_seconds: int = 30
    # Ack window: SUBMITTED rows unanswered past this window become UNKNOWN
    # (NEVER FAILED — "no local record" is not "did not happen").
    effects_ack_window_seconds: int = 60
    # Authorization TTL: AUTHORIZED rows never claimed past this age EXPIRE.
    effects_authorization_ttl_seconds: int = 300
    # Global bound on non-terminal effect rows (bounded outbox → 429).
    effects_max_in_flight: int = 1000

    # --- Logging (spec §11/§20) -----------------------------------------------
    # Privacy rule: logs carry categories, handles/ids and counts only —
    # never payload, message, memory, governance content or credentials.
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the cached singleton settings (reads ``AIC_*`` env vars once)."""
    return Settings()
