"""Database URL dialect normalization (Phase 1 Railway preparation).

Proves, against the REAL Settings model and SQLAlchemy URL parser:
- platform-style bare ``postgresql://`` / ``postgres://`` schemes are
  rewritten to the explicit psycopg 3 dialect (``postgresql+psycopg://``)
  while the rest of the URL is preserved byte-for-byte;
- explicit dialect suffixes and SQLite URLs are never touched;
- the rewrite actually resolves to the psycopg 3 dialect at engine-parse
  time (no psycopg2 import required);
- production fail-closed startup still accepts a platform-style URL AFTER
  normalization (and still refuses SQLite — no weakening).
"""

import secrets

import pytest
from sqlalchemy.engine import make_url

from commons_core.config.settings import Settings
from commons_core.config.validation import validate_production_startup

HEX64 = secrets.token_hex(32)


class TestSchemeNormalization:
    def test_bare_postgresql_scheme_normalized(self) -> None:
        url = "postgresql://user:pass@host:5432/dbname"
        assert Settings(database_url=url).database_url == (
            "postgresql+psycopg://user:pass@host:5432/dbname"
        )

    def test_legacy_post_scheme_normalized(self) -> None:
        url = "postgres://user:pass@host:5432/dbname"
        assert Settings(database_url=url).database_url == (
            "postgresql+psycopg://user:pass@host:5432/dbname"
        )

    def test_percent_encoded_userinfo_preserved_byte_for_byte(self) -> None:
        # Railway-style URL with percent-encoded special characters — the
        # rewrite must not re-encode or decode anything.
        url = "postgresql://user:p%40ss%3Aword@host:5432/db%20name?sslmode=require"
        assert Settings(database_url=url).database_url == (
            "postgresql+psycopg://user:p%40ss%3Aword@host:5432/db%20name?sslmode=require"
        )

    def test_explicit_psycopg_scheme_unchanged(self) -> None:
        url = "postgresql+psycopg://user:pass@host:5432/dbname"
        assert Settings(database_url=url).database_url == url

    def test_explicit_psycopg2_scheme_unchanged(self) -> None:
        # An explicit driver choice is respected, never silently rewritten.
        url = "postgresql+psycopg2://user:pass@host:5432/dbname"
        assert Settings(database_url=url).database_url == url

    def test_sqlite_urls_unchanged(self) -> None:
        for url in (
            "sqlite+pysqlite:///./data/commons.db",
            "sqlite+pysqlite:///:memory:",
        ):
            assert Settings(database_url=url).database_url == url

    def test_whitespace_stripped(self) -> None:
        assert (
            Settings(database_url="  postgresql://user:pass@host/db  ").database_url
            == "postgresql+psycopg://user:pass@host/db"
        )


class TestDialectResolution:
    def test_normalized_url_resolves_psycopg3_dialect(self) -> None:
        pytest.importorskip("psycopg")  # dialect import needs the driver
        url = Settings(database_url="postgresql://user:pass@host:5432/db").database_url
        parsed = make_url(url)
        assert parsed.drivername == "postgresql+psycopg"
        dialect_cls = parsed.get_dialect()
        assert dialect_cls.driver == "psycopg"
        assert dialect_cls is not None


class TestAlembicConfigInterpolation:
    def test_percent_encoded_url_round_trips_through_alembic_config(self) -> None:
        # migrations/env.py escapes '%' before set_main_option (alembic's
        # config layer is a ConfigParser with %-interpolation — a raw
        # percent-encoded userinfo raises "invalid interpolation syntax").
        # This test locks in the escape/unescape round-trip contract.
        from alembic.config import Config

        pytest.importorskip("alembic")
        url = "postgresql+psycopg://sp_char:p%40ss%3Aword@host:5432/db"
        cfg = Config()
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        assert cfg.get_main_option("sqlalchemy.url") == url

    def test_plain_url_round_trips_through_alembic_config(self) -> None:
        from alembic.config import Config

        url = "postgresql+psycopg://user:pass@host:5432/db"
        cfg = Config()
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        assert cfg.get_main_option("sqlalchemy.url") == url


class TestProductionFailClosedStillHolds:
    def test_platform_style_url_accepted_after_normalization(self) -> None:
        # A Railway-style bare-scheme URL must satisfy the production
        # PostgreSQL-only fail-closed check AFTER normalization.
        settings = Settings(
            database_url="postgresql://postgres:secret@postgres.railway.internal:5432/railway",
            master_keys=f"k1:{HEX64}",
            active_key_id="k1",
            env="production",
        )
        validate_production_startup(settings, require_keys=True)  # no raise
        assert settings.database_url.startswith("postgresql+psycopg://")

    def test_sqlite_still_refused_in_production(self) -> None:
        settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            master_keys=f"k1:{HEX64}",
            active_key_id="k1",
            env="production",
        )
        with pytest.raises(Exception, match="SQLite"):
            validate_production_startup(settings, require_keys=True)
