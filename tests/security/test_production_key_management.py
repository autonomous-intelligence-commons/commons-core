"""Production fail-closed key management & rotation (Phase 1 hardening, Task 3).

Proves, against the REAL application factory and cipher stack:
- production startup FAILS without AIC_MASTER_KEYS (no ephemeral fallback);
- production startup SUCCEEDS with valid keys (active kid honored);
- invalid/unknown active key ids are rejected;
- placeholder-pattern keys are rejected in production (allowed in dev);
- production refuses a SQLite datastore (API, worker and CLI paths);
- development/test keep the disposable ephemeral-key path;
- key rotation: old envelopes stay decryptable while the old kid is
  retained; removing it produces a typed UnknownKeyError (explicit, not
  silent); new seals always use the ACTIVE kid;
- key material and plaintext NEVER appear in captured logs.
"""

import logging
import re
import secrets

import pytest
from pydantic import ValidationError

from commons_core.api.app import create_app
from commons_core.config.settings import Settings
from commons_core.config.validation import (
    ProductionConfigurationError,
    validate_production_startup,
)
from commons_core.database.db import Database
from commons_core.security.cryptography import EnvelopeCipher, KeyStore, UnknownKeyError
from commons_core.security.cryptography.errors import KeyConfigurationError

#: A PostgreSQL URL is never connected during these tests — the app factory
#: and validation are lazy; only configuration correctness is asserted.
PG_URL = "postgresql+psycopg://commons:commons-ci@localhost:5432/commons"
SQLITE_URL = "sqlite+pysqlite:////tmp/never-used.db"

HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def _key_hex() -> str:
    return secrets.token_hex(32)


def _production_settings(**overrides) -> Settings:
    kwargs = {
        "database_url": PG_URL,
        "master_keys": f"k1:{_key_hex()}",
        "active_key_id": "k1",
        "env": "production",
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


class TestProductionStartupFailClosed:
    def test_missing_keys_fail_startup(self) -> None:
        settings = _production_settings(master_keys="")
        with pytest.raises(KeyConfigurationError, match="AIC_MASTER_KEYS is REQUIRED"):
            create_app(settings=settings, database=Database(PG_URL))

    def test_valid_keys_startup_succeeds_and_active_kid_honored(self) -> None:
        settings = _production_settings()
        app = create_app(settings=settings, database=Database(PG_URL))
        cipher = app.state.cipher
        assert cipher.active_kid == "k1"
        assert cipher.list_key_ids() == ["k1"]
        assert "dev-ephemeral" not in cipher.list_key_ids()

    def test_unknown_active_key_id_rejected(self) -> None:
        settings = _production_settings(active_key_id="kX")
        with pytest.raises(KeyConfigurationError, match="active key id"):
            create_app(settings=settings, database=Database(PG_URL))

    def test_empty_active_key_id_rejected_in_production(self) -> None:
        with pytest.raises(KeyConfigurationError, match="AIC_ACTIVE_KEY_ID"):
            KeyStore(f"k1:{_key_hex()}", "   ", env="production")

    @pytest.mark.parametrize(
        "placeholder_hex",
        [
            pytest.param("f" * 64, id="all-f"),
            pytest.param("deadbeef" * 8, id="deadbeef-repeat"),
            pytest.param("0123456789abcdef" * 4, id="sequential-hex"),
        ],
    )
    def test_placeholder_keys_rejected_in_production(self, placeholder_hex: str) -> None:
        settings = _production_settings(master_keys=f"k1:{placeholder_hex}")
        with pytest.raises(KeyConfigurationError, match="placeholder"):
            create_app(settings=settings, database=Database(PG_URL))

    def test_sqlite_datastore_refused_for_api_in_production(self) -> None:
        settings = _production_settings(database_url=SQLITE_URL)
        with pytest.raises(ProductionConfigurationError, match="SQLite"):
            create_app(settings=settings, database=Database(SQLITE_URL))

    def test_sqlite_datastore_refused_for_worker_in_production(self) -> None:
        settings = _production_settings(database_url=SQLITE_URL, master_keys="")
        with pytest.raises(ProductionConfigurationError, match="SQLite"):
            validate_production_startup(settings, require_keys=False)

    def test_worker_and_cli_do_not_require_keys_in_production(self) -> None:
        # Least privilege: the worker/CLI never decrypt content — keys are
        # NOT required for them (compose deliberately withholds keys there).
        settings = _production_settings(master_keys="")
        validate_production_startup(settings, require_keys=False)  # no raise

    def test_production_check_is_noop_in_development(self) -> None:
        settings = Settings(database_url=SQLITE_URL, master_keys="", env="development")
        validate_production_startup(settings, require_keys=True)  # no raise


class TestEnvironmentNormalization:
    def test_env_is_normalized_case_and_whitespace(self) -> None:
        assert Settings(env=" Production ").env == "production"
        assert Settings(env="TEST").env == "test"

    def test_unknown_env_fails_fast(self) -> None:
        with pytest.raises(ValidationError, match="AIC_ENV"):
            Settings(env="prodction")  # typo → clear startup error


class TestDevelopmentEphemeralFallback:
    def test_development_keeps_ephemeral_key_path(self) -> None:
        settings = Settings(database_url=PG_URL, master_keys="", env="development")
        app = create_app(settings=settings, database=Database(PG_URL))
        assert app.state.cipher.active_kid == "dev-ephemeral"

    def test_test_env_keeps_ephemeral_key_path(self) -> None:
        store = KeyStore("", "k1", env="test")
        assert store.active_kid == "dev-ephemeral"

    def test_production_env_rejects_ephemeral_fallback_directly(self) -> None:
        with pytest.raises(KeyConfigurationError, match="required in production"):
            KeyStore("", "k1", env="production")

    def test_development_allows_placeholder_pattern_keys(self) -> None:
        # Disposables are fine locally — only production rejects them.
        store = KeyStore(f"k1:{'0123456789abcdef' * 4}", "k1", env="development")
        assert store.active_kid == "k1"


class TestKeyRotation:
    def test_old_envelopes_decryptable_after_rotation_with_retained_key(self) -> None:
        old_hex, new_hex = _key_hex(), _key_hex()
        cipher_v1 = EnvelopeCipher(KeyStore(f"k1:{old_hex}", "k1"))
        envelope = cipher_v1.seal("rotation-message", "message:chan-1:agent-1")
        assert envelope.kid == "k1"

        # Rotate: k2 becomes active; k1 is RETAINED so history stays readable.
        cipher_v2 = EnvelopeCipher(KeyStore(f"k1:{old_hex},k2:{new_hex}", "k2"))
        assert cipher_v2.active_kid == "k2"
        fresh = cipher_v2.seal("post-rotation", "message:chan-1:agent-1")
        assert fresh.kid == "k2"  # new encryption uses the ACTIVE key
        assert cipher_v2.open(envelope, "message:chan-1:agent-1") == "rotation-message"

    def test_removing_required_historical_key_is_explicit_not_silent(self) -> None:
        old_hex, new_hex = _key_hex(), _key_hex()
        envelope = EnvelopeCipher(KeyStore(f"k1:{old_hex}", "k1")).seal(
            "historic", "memory:ns-1:key"
        )
        without_old = EnvelopeCipher(KeyStore(f"k2:{new_hex}", "k2"))
        with pytest.raises(UnknownKeyError, match="k1"):
            without_old.open(envelope, "memory:ns-1:key")

    def test_production_rotation_flow_via_app_factory(self) -> None:
        old_hex, new_hex = _key_hex(), _key_hex()
        settings = _production_settings(
            master_keys=f"k1:{old_hex},k2:{new_hex}", active_key_id="k2"
        )
        app = create_app(settings=settings, database=Database(PG_URL))
        assert app.state.cipher.active_kid == "k2"
        assert app.state.cipher.list_key_ids() == ["k1", "k2"]


class TestKeyMaterialNeverLogged:
    def test_seal_open_and_configuration_leave_no_key_or_plaintext_in_logs(self, caplog) -> None:
        key_hex = _key_hex()
        with caplog.at_level(logging.DEBUG, logger="commons_core"):
            store = KeyStore(f"k1:{key_hex}", "k1", env="test")
            cipher = EnvelopeCipher(store)
            envelope = cipher.seal("secret-plaintext-marker", "message:c:a")
            cipher.open(envelope, "message:c:a")
        text = "\n".join(str(record.getMessage()) for record in caplog.records)
        assert key_hex.lower() not in text.lower()
        assert "secret-plaintext-marker" not in text
        assert not HEX64.search(text)

    def test_ephemeral_warning_names_kid_but_never_material(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="commons_core"):
            KeyStore("", "k1", env="test")
        text = "\n".join(str(record.getMessage()) for record in caplog.records)
        assert "EPHEMERAL" in text  # the loud warning is present...
        assert "dev-ephemeral" in text
        assert not HEX64.search(text)  # ...without any key material
