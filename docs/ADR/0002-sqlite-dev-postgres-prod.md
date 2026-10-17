# ADR-0002: SQLite for local development and tests; PostgreSQL as the production target

- **Status:** Accepted (Phase 1) — with a documented verification gap
- **Context:** Spec §5/§11 require PostgreSQL persistence in production and
  Alembic migrations. The Phase 1 sandbox has NO PostgreSQL server and NO
  Docker (verified during setup — Task 4 worklog). Python 3.12 + PyPI are
  available.
- **Decision:** Use SQLAlchemy 2.0 ORM + Alembic as the single data-access
  layer. Develop and test against SQLite
  (`sqlite+pysqlite:///./data/commons.db`); target PostgreSQL in production
  via `AIC_DATABASE_URL` (`postgresql+psycopg://…`) with no code changes.
  Migration `0001_initial_schema` is dialect-portable (VARCHAR-backed
  enums via `native_enum=False`, batch mode enabled for SQLite DDL).
  Document everywhere that only SQLite was exercised.
- **Consequences:**
  - (+) The full test suite (342 passed) runs with zero external services;
    CI can run without a database container (see
    `.github/workflows/ci.yml` migrate-check job, which uses SQLite).
  - (+) Honest, spec-compliant local developer flow (spec §28 steps 1–10).
  - (−) **PostgreSQL behavior is unverified**: the migration round-trip,
    `timestamptz` fidelity, index creation, and the locking upgrades
    recommended for quota/claim hot paths (`SELECT … FOR UPDATE`) are
    documented in code but never executed. First production deployment
    must run migrations manually and verify (see `docs/DEPLOYMENT.md`).
  - (−) SQLite serializes writers — concurrency paths that are safe here
    may behave differently under PostgreSQL concurrency; the code uses
    conditional updates + unique-constraint retries to stay portable.
