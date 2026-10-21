# Testing — AIC Phase 1

How to run the suite, what it proves, and — honestly — what it does not.

## 1. How to run

```bash
cd /path/to/commons-core          # your local clone

# 1) environment (first time)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,postgres]"  # dev extras + pinned psycopg; the postgres
                              # extra is required for the full suite
                              # (CI installs the same; see ci.yml)

# 2) full deterministic suite (no network, injected clocks, ~35 s)
.venv/bin/python -m pytest -q

# 3) static checks
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# 4) migrations (canonical schema path; uses AIC_DATABASE_URL)
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head   # round-trip
```

Notes: the `dev` extra is defined in `pyproject.toml`
(`httpx==0.28.1`, `pytest==9.1.1`, `ruff==0.17.1`). Tests do not require
`.env` — `tests/conftest.py` builds isolated settings with disposable dev
keys. Local server smoke (optional): `.venv/bin/python -m commons_core`.

## 2. Results — Phase 1 record and current baseline

**Phase 1 record** (executed for this document, sandbox environment,
2026-10-17; preserved as a dated historical record):

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | **342 passed, 2 skipped** (1 warning: starlette/httpx deprecation notice from `fastapi.testclient`, cosmetic) |
| `.venv/bin/ruff check .` | clean (no findings) |
| `.venv/bin/ruff format --check .` | clean (no files would be reformatted) |
| `.venv/bin/alembic upgrade head` (dev SQLite) | OK |

**The 2 skipped tests** are documented skips, not missing coverage:
`tests/integration/test_activities.py` skips invalid identity-transition
combinations that are out of the transition matrix *by construction*
(parametrized cases that assert the matrix has no such edge).

**Current baseline** (repository hygiene audit, 2026-10-20, commit `491ac5c`,
SQLite backend, Python 3.12.15, age v1.3.2): **617 passed, 42 skipped,
0 failed** (≈67 s; the same single known deprecation warning). The 42 skips
are PostgreSQL-gated suites self-skipping by design (see §6); `ruff check`
and `ruff format --check` are clean. GitHub CI was green at the same commit
with all eight jobs, including the PostgreSQL 16 job (migration round-trip,
schema verification, full suite) and the PostgreSQL 18.6 backup/recovery
job — those PostgreSQL results are GitHub-reported CI, not local audit
executions.

## 3. Test inventory (what each file proves)

Suite at the Phase 1 baseline (2026-10-17) = 344 collected (342 passed + 2
skipped); the suite has since grown — 617 passed / 42 skipped at the
2026-10-20 audit baseline (see §2). The inventory below is the Phase 1 record;
categories map to spec §29.

| File | Tests | Category (spec §29) | What it proves |
|---|---|---|---|
| `tests/unit/test_crypto.py` | 26 | Cryptography | Round-trip seal/open; envelope shape; tamper detection (ciphertext/tag flip, truncation); AAD mismatch; wrong/unknown key rejection; nonce uniqueness; multi-kid rotation; ephemeral dev key; strict key-store validation; error messages never contain key material |
| `tests/unit/test_tokens.py` | 12 | Functional + Cryptography | Token issuance/verification; wrong/truncated token rejection; hash format; uniqueness; revoked/expired credentials fail; future expiry OK; rotation linkage; suspended/revoked agents fail; raw token never stored in DB |
| `tests/unit/test_authorization.py` | 21 | Authorization | Deny-by-default (no membership/unknown permission/unknown channel/suspended agent denied); channel read/write semantics; memory owner/permission semantics; admin wildcard ONLY admin:* (no private channel access); governance/task permissions; **engine canary** (source+imports reference only permitted models; content rows cannot grant permissions); denial event recording |
| `tests/unit/test_agents_service.py` | 23 | Functional | Provisioning, handle validation (4–64 chars, pattern), duplicate handles, lifecycle transitions (suspend/reactivate/revoke), admin flag only via service |
| `tests/unit/test_rate_limit.py` | 8 | Operational | Per-agent limits allow-then-deny; window reset; principal isolation; invalid limits rejected; anonymous bucket with separate limit; host isolation; bounded memory (pruning); quota persistence |
| `tests/unit/test_memory_versioning.py` | 6 | Functional | Version increment, supersede semantics, history chains, concurrent-writer arbitration |
| `tests/unit/test_scheduler_unit.py` | 36 | Operational + AI-specific | Backoff (`min(2^attempts,60)`) caps; state transitions; forbidden payload keys at depth; payload size; nesting depth; unknown kind; claim/lease semantics; cancel rules |
| `tests/integration/test_app_foundation.py` | 18 | Functional + Operational | App boots; `/health`, `/openapi.json`, `/docs`; `/agents/me` (auth + 401 paths); structured error envelope; `X-Request-Id`; rate limiting via HTTP |
| `tests/integration/test_communication.py` | 19 | Functional | Channel create/list/members (direct cardinality, add-member rules); message send/read (sequence, cursor, has_more); idempotent acks; quota 429; 413/422 validation |
| `tests/integration/test_memory.py` | 17 | Functional | Namespace create/list; owner-only grants (both-false revoke); entry write/read; key listing; history; key pattern & `/history` shadow rejection |
| `tests/integration/test_activities.py` | 34 (2 skipped) | Functional | Creation (+auto channel); invitation flow (join/decline/re-invite); lifecycle matrix incl. terminal states; creator-only transitions; 404 for non-members; 413 summary |
| `tests/integration/test_governance.py` | 14 | Functional | Record create (versioning, supersedes rules, 409 conflicts); list/detail/history; privilege lint 422 |
| `tests/integration/test_scheduler.py` | 29 | Functional | Task create (idempotency replay/conflict); queue quotas (429); worker claim/execute; retry/backoff to terminal failure; cancel; owner scoping |
| `tests/security/test_communication_isolation.py` | 10 | Authorization + AI-specific | Membership enforcement (non-member read/send/ack denied; read-only member cannot send/add); IDOR/404 policy; **prompt-injection payload is inert data** |
| `tests/security/test_memory_isolation.py` | 15 | Authorization + AI-specific | Unpermitted read/write/list 403; outsider cannot see metadata; non-owner/admin cannot grant; **admin cannot read entries**; validation set; **no update/delete endpoints exist** (OpenAPI assertion); **poisoned entry cannot grant privileges** |
| `tests/security/test_governance_boundary.py` | 11 | AI-specific | Privilege-claim language rejected (422) + `policy_violation` event without content; nothing stored when rejected; **lint-bypassing record stored but grants nothing**; limits unchanged |
| `tests/security/test_emergency_stop.py` | 16 | Operational | Admin engages (state + event); unauthenticated/non-admin denied (both verbs); paused writes 503 (message/memory/task/activity); paused reads 200; **worker does not claim while paused, resumes after release**; telemetry shows stop; writes work after release |
| `tests/operational/test_quotas.py` | 6 | Operational | Message quota 429 via API + event; per-agent isolation; window reset with injected clock; quota consumed only by successful sends |
| `tests/operational/test_task_quotas.py` | 6 | Operational | Per-agent (100) and global in-queue caps → 429 `TASK_QUEUE_FULL` + events |
| `tests/privacy/test_no_plaintext_at_rest.py` | 8 | Privacy (spec §20/§29) | **Sentinel scan of every DB column** (raw SQL) for planted plaintext; envelope columns hold hex only; **sentinel absent from captured logs**; authorized reader still sees plaintext (reads work); non-member gets no payload; telemetry contains no content or handles; non-admin/unauthenticated telemetry access denied |
| `tests/recovery/test_scheduler_recovery.py` | 9 | Recovery | **Persistence across engine teardown** (new engine/process on same file); idempotency replay after restart; **expired lease returns to pending without new attempt**; worker recovers expired lease end-to-end; final-attempt lease expiry fails terminally; **sequential double-worker run executes each task exactly once**; graceful shutdown (stop event between tasks; in-flight completes; signal install main-thread only) |

## 4. Deterministic testing notes

- **Injected clocks:** quota windows, backoff, lease expiry, telemetry
  timestamps all use injectable `now`/`clock` parameters — tests advance
  time without sleeping.
- **No network:** nothing in the suite contacts any external service; no
  model APIs; the worker tests run in-process against a temp SQLite file.
- **Disposable dev keys:** tests construct `KeyStore`/`EnvelopeCipher` with
  random in-memory keys (or the ephemeral path) — no real keys in tests,
  no production data in tests (spec §11).
- **Synthetic content only:** every message/memory/governance payload in
  the suite is disposable fixture text (sentinel markers deliberately do
  NOT match anything real).

## 5. Untested / untestable areas — Phase 1 record, updated with current status (honest)

| Area | Status | Why |
|---|---|---|
| Docker build & compose run | **Executed in CI since publication** | *(Historical, Phase 1: Docker unavailable in the sandbox.)* The `docker` CI job builds the image and runs the compose stack (non-root check, fail-closed startup, authenticated smoke, clean teardown) on every push/PR — green at the 2026-10-20 audit baseline. |
| GitHub Actions CI | **Executed since publication** | *(Historical, Phase 1: workflows were prepared but had not yet run when this table was written.)* `ci.yml` documents successful runs since publication; all eight jobs were green at the 2026-10-20 audit baseline (`491ac5c`). |
| PostgreSQL-only behaviors | **Executed since the Phase 1 hardening pass** | *(Historical, Phase 1: all tests ran on SQLite only.)* §6 records the PostgreSQL integration run (full suite, alembic round-trip, `FOR UPDATE SKIP LOCKED` proven); CI replicates it on every push. |
| TLS / production transport | **Not executed** | No deployment; localhost dev is plain HTTP. |
| Railway deployment | **Not executed** *(historical Phase 1 record; current status not established)* | *(Historical, Phase 1: deployment was explicitly forbidden without authorization (spec §31) and no Railway resources were created or modified in the Phase 1 sandbox.)* Per the maintainer-provided milestone record (`docs/milestones/2026-10-19-phase-1.md` §4–§5; see also `docs/RAILWAY.md`, status "DEPLOYED 2026-10-19"), a Railway deployment has since occurred — three services reported Online, `/health` returned HTTP 200, and the `noop`/`heartbeat` smoke tasks passed (2026-10-18). Deployment activity is not equivalent to deployment testing: no systematic Railway-specific validation or disaster-recovery testing is established by the available records, and production backup-and-restore remains unverified. The current status of Railway deployment testing has **not been established by the available evidence**. |
| `pg_dump` backup + restore | **Implemented and tested since Stage 1 (ADR-0009)** | *(Historical, Phase 1: a documented gap.)* `docs/BACKUP.md` documents the streaming `pg_dump → age` pipeline, atomic publication, and the synthetic restore drill; CI runs the backup/recovery suites against real PostgreSQL 16 and 18.6. A real-data production drill remains future, separately authorized work. |
| Real model resistance to prompt injection | **Not applicable in Phase 1** | No LLM connected; injection tests verify infrastructure enforcement only (spec §13) — NOT universal model resistance. |
| `AIC_TASK_TIMEOUT_SECONDS` enforcement | **Implemented and tested since the Phase 1 hardening pass** | Enforced via `execute_claimed(timeout_seconds)` + the revocable session guard, with lease/timeout coherence at worker startup (see SC-34 in `docs/SECURITY_CONTROLS.md`); covered by `tests/operational/test_task_timeout.py` (10 tests). *(Historical: inert at Phase 1.)* |

**No fabricated coverage numbers or guarantees:** every number above traces to
a dated, cited record — the §2 runs, the referenced CI configuration and its
recorded outcomes, or the named test files.

## 6. PostgreSQL integration (Phase 1 hardening — EXECUTED)

The full suite runs unchanged against a real PostgreSQL server via
`AIC_TEST_DATABASE_URL` (per-test schema isolation: `drop_all` →
`create_all` on the shared test database):

```bash
pip install -e ".[dev,postgres]"   # psycopg[binary]==3.2.13 (Dockerfile pin)
AIC_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/commons \
    python -m pytest -q
```

Fail-loud guarantees (`tests/conftest.py::_backend_guard`, session-scoped):
a `sqlite` value in `AIC_TEST_DATABASE_URL` aborts the session; an
unreachable/invalid URL aborts with the driver error; the live dialect is
asserted to be PostgreSQL. There is NO silent SQLite fallback.

**Results executed locally against PostgreSQL 16.2** (embedded server via
the pgserver binaries; psycopg 3.2.13):

- `python -m pytest -q` → **382 passed, 2 skipped** (the documented
  identity-matrix skips), 1 cosmetic warning (fastapi TestClient
  deprecation — same as on SQLite).
- SQLite suite (same revision): **374 passed, 10 skipped** (8
  PostgreSQL-only tests skip without `AIC_TEST_DATABASE_URL`).
- Alembic on PostgreSQL: `upgrade head` → schema inspection (17 tables
  incl. `alembic_version`; JSON columns; `timestamptz`; BIGINT
  `security_events.id`; unique index `ix_scheduled_tasks_idempotency_key`)
  → `downgrade base` → `upgrade head`. Round-trip verified.
- Durable-scheduler concurrency on PostgreSQL: the multi-worker recovery
  tests run against PostgreSQL (recovery fixtures are backend-aware);
  `claim_next` emits `FOR UPDATE SKIP LOCKED` (proven by
  `tests/integration/test_postgres_schema.py::
  TestPostgreSQLSchedulerClaim::test_claim_emits_for_update_skip_locked`).
- Live E2E on production PostgreSQL (`AIC_ENV=production`, real key,
  alembic-migrated DB): health 200 → CLI provisioning → channel →
  AES-256-GCM encrypted message → cross-agent retrieve + decrypt → admin
  non-member read 403 → telemetry counts-only → task submit →
  `run_worker.py --once` → `succeeded`.
- Fail-closed startup matrix (live subprocesses, 8/8 pass): production
  without keys REFUSED; with valid keys STARTED; with SQLite REFUSED;
  placeholder key REFUSED; unknown active kid REFUSED; development without
  keys STARTED (ephemeral); typo `AIC_ENV` REFUSED; worker with
  production+SQLite REFUSED.

New/changed test files (this hardening pass):

| File | Proves |
| --- | --- |
| `tests/integration/test_postgres_schema.py` (PG-only) | 16 tables; VARCHAR enums (no native PG ENUM); timestamptz; JSON; BIGINT id; unique constraints+indexes; FKs; `FOR UPDATE SKIP LOCKED` |
| `tests/security/test_production_key_management.py` (24) | fail-closed matrix; env normalization; ephemeral fallback only in dev/test; rotation (retained-kid decryptability, removal → typed `UnknownKeyError`, active-kid sealing); key material never in logs |
| `tests/operational/test_task_timeout.py` (10) | completes-within-budget; exceeds-budget → retry/backoff; exhausted retries → terminal `failed`; abandoned handler cannot use the session (guard raises `TaskExecutionRevokedError`); payload content never in `last_error`; legacy inline mode; lease/timeout coherence; worker E2E; concurrent workers; emergency stop |
| `tests/conftest.py` + `tests/recovery/*` | backend selection + backend-aware recovery (the multi-worker tests now execute on PostgreSQL) |

## 7. Honest scope of the hardening verification

- The `docker` and `postgres` CI jobs in `.github/workflows/ci.yml` were
  authored for GitHub Actions. The `postgres` job replicates locally what
  was executed above. *(Historical, hardening pass: the `docker` job could
  not be executed in that environment — no Docker runtime in the sandbox;
  first execution was expected on GitHub Actions after push.)* Since
  publication both jobs have executed on GitHub on every push and pull
  request; all eight jobs were green at the 2026-10-20 audit baseline.
