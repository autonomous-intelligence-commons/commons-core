"""Fail-closed production startup validation (Phase 1 hardening, Task 3).

In ``AIC_ENV=production`` the application REFUSES to start unless its
security-critical configuration is complete and sane:

- ``AIC_DATABASE_URL`` must NOT be SQLite (an unprotected local file is not
  an acceptable production datastore).
- ``AIC_MASTER_KEYS`` must be present when ``require_keys=True`` (the API
  process seals/opens envelopes). The worker and management CLI never
  decrypt content (least privilege — see compose.yaml: the worker container
  deliberately receives NO keys), so they validate the database only.
- Detailed key syntax, lengths, active-kid and placeholder checks live in
  :class:`commons_core.security.cryptography.key_store.KeyStore`, which
  also refuses the ephemeral-key fallback in production.

Development and test environments keep the disposable ephemeral-key path
(fail-open for DX, loud warning logged) — the risk of losing encrypted data
after restart is documented in ``docs/CRYPTOGRAPHY.md`` and
``docs/DEPLOYMENT.md``.

This module never logs configuration VALUES (only presence/absence facts),
so key material cannot leak through startup diagnostics.
"""

from commons_core.config.settings import Settings
from commons_core.security.cryptography.errors import KeyConfigurationError

#: Environments in which the ephemeral dev-key fallback is permitted.
NON_PRODUCTION_ENVS = frozenset({"development", "test"})

#: Environments recognized by ``Settings`` (mirrors the settings validator).
KNOWN_ENVS = frozenset({"development", "test", "production"})


class ProductionConfigurationError(RuntimeError):
    """Production startup requirements are not met (fail-closed).

    Raised BEFORE the application accepts any request; uvicorn surfaces it
    as a startup failure with a non-zero exit code.
    """


def validate_production_startup(settings: Settings, *, require_keys: bool) -> None:
    """Validate security-critical configuration when running in production.

    ``require_keys=True`` (API process): ``AIC_MASTER_KEYS`` must be set —
    the ephemeral in-memory key is forbidden in production because encrypted
    messages and shared memory would become permanently unreadable after a
    process restart. ``require_keys=False`` (worker / management CLI): keys
    are NOT required — those processes never decrypt content (least
    privilege); only the database check applies.

    No-op in development/test. Never raises for missing NON-critical
    settings; never echoes configuration values.
    """
    if settings.env not in {"production"}:
        return

    if settings.database_url.strip().lower().startswith("sqlite"):
        raise ProductionConfigurationError(
            "AIC_ENV=production refuses a SQLite database (AIC_DATABASE_URL must "
            "target PostgreSQL, e.g. postgresql+psycopg://...). An unprotected "
            "local file is not an acceptable production datastore."
        )

    if require_keys and not settings.master_keys.strip():
        raise KeyConfigurationError(
            "AIC_MASTER_KEYS is REQUIRED in production (AIC_ENV=production): "
            "an ephemeral development key would make encrypted messages and "
            "shared memory permanently unreadable after restart. Configure "
            "durable key material, e.g. "
            'AIC_MASTER_KEYS="k1:<64 hex chars>" (generate with: '
            'python -c "import secrets; print(secrets.token_hex(32))").'
        )
