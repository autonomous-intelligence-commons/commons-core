# ADR-0007: Production fail-closed key management (environment-aware startup validation)

- **Status:** Accepted (Phase 1 Final Security Hardening, Task 3)
- **Context:** Phase 1 allowed an ephemeral in-memory encryption key when
  `AIC_MASTER_KEYS` is absent. This is acceptable for disposable
  development/test runs (spec §10.3 permits automatically generated
  disposable development keys), but as a SILENT fallback it is unacceptable
  in production: encrypted messages and shared memory would become
  permanently unreadable after a process restart. The original review also
  noted an unsafe-defaults risk (`.env.example` placeholders) and an
  unvalidated `AIC_ENV` value. The deployment target (Railway) injects
  plain environment variables; no external key-management service may be
  introduced (cost/dependency restrictions).
- **Decision:** Make startup validation environment-aware and fail-closed:
  1. `AIC_ENV` is normalized and validated (`development` / `test` /
     `production`; typos fail fast at settings load).
  2. In `production`: `AIC_MASTER_KEYS` is REQUIRED (empty →
     `KeyConfigurationError` — the ephemeral path is forbidden),
     `AIC_ACTIVE_KEY_ID` must be non-empty and present among the kids, and
     placeholder-pattern keys (all-f, repeated `deadbeef`, sequential-hex,
     all-zero) are rejected.
  3. In `production`: a SQLite `AIC_DATABASE_URL` is refused
     (`config/validation.py::ProductionConfigurationError`) for the API,
     the worker and the management CLI — an unprotected local file is not
     an acceptable production datastore.
  4. Least privilege is preserved: the worker and CLI validate the
     database but do NOT require keys (they never decrypt content; the
     compose worker container deliberately receives no keys).
  5. `create_app` runs the validation BEFORE wiring anything — invalid
     configuration means uvicorn exits non-zero before accepting a
     request.
- **Consequences:**
  - (+) The "restart loses all encrypted data" failure mode is
    structurally impossible in production; misconfiguration surfaces as
    an explicit, actionable startup error (never as silent data loss).
  - (+) Automated proof: 24 dedicated tests
    (`tests/security/test_production_key_management.py`) plus a live
    8-case subprocess matrix (production+no-keys REFUSED, valid keys
    STARTED, sqlite REFUSED, placeholder REFUSED, unknown kid REFUSED,
    dev+no-keys STARTED, typo env REFUSED, worker prod+sqlite REFUSED).
  - (+) Railway-compatible: plain env vars / sealed secrets; no external
    KMS, no paid service.
  - (−) Key material remains required to decrypt backups — losing all
    copies of a kid is unrecoverable by design (rotation and recovery
    procedures are documented in CRYPTOGRAPHY.md §6 / DEPLOYMENT.md §12).
  - (−) This remains application-layer encryption, NOT E2EE — host/Railway
    administrators and future AI API providers can still exceed these
    guarantees in some scenarios (honest limitation, unchanged).
