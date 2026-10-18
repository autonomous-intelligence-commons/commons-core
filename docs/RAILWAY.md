# Railway Secure Deployment Runbook — AIC Phase 1

> **Status: PREPARED, NOT DEPLOYED.** Nothing in this document was executed
> against Railway — it is the deployment-ready configuration for user review
> before any service is started (per the Railway Secure Deployment
> Preparation spec). All platform behaviors marked **[assumed]** are based on
> Railway's public documentation and MUST be confirmed in the dashboard at
> deploy time. Everything marked **[verified]** was executed locally against
> a real PostgreSQL 18.3 server with the exact repository code.

## 0. What changed in this branch (and why)

Two real deployment blockers were found by the Railway readiness audit and
fixed with the smallest secure correction:

| # | Blocker | Fix | Status |
|---|---|---|---|
| 1 | Railway's `${{Postgres.DATABASE_URL}}` uses the bare `postgresql://` scheme. SQLAlchemy maps that to the **psycopg2** dialect — the image ships only psycopg 3, so API/worker/migrate would all crash at engine creation (`ModuleNotFoundError`). | `Settings.database_url` field validator rewrites bare `postgresql://` / `postgres://` to `postgresql+psycopg://` (rest of the URL preserved byte-for-byte; explicit dialect suffixes and SQLite untouched). | **[verified]** end-to-end on PG 18.3: alembic, production app factory, authenticated round-trip, worker task. |
| 2 | Railway-generated PostgreSQL passwords contain special characters, so the injected URL carries **percent-encoded userinfo** (`%40` etc.). `migrations/env.py` passed the URL to `alembic`'s ConfigParser-based config, which treats `%` as interpolation syntax → `ValueError: invalid interpolation syntax` → the migration step dies before any service starts. | `migrations/env.py` escapes `%` → `%%` before `set_main_option` (alembic FAQ pattern; offline mode reads the unescaped value back). | **[verified]** on PG 18.3 with a percent-encoded special-character password. |

No security control was weakened: fail-closed production startup, key
validation, port isolation and rate limits are untouched (full suite green —
see §8).

## 1. Target topology

```
Railway project: autonomous-intelligence-commons  (already exists)
├── Postgres (Railway-managed, PostgreSQL 18, Online)   ← keep as-is
│     private networking only, NO public access, persistent volume
├── commons-api   (new service, from GitHub repo)
│     Docker build → uvicorn on $PORT → public domain (the ONLY public endpoint)
└── commons-worker (new service, same repo, same image)
      start command override → run_worker.py --loop, NO domain/port
```

Both app services use the SAME approved source (GitHub `main` after this PR
merges) and the SAME database reference. The worker never receives
`AIC_MASTER_KEYS` (least privilege — it never decrypts content; see
docs/DEPLOYMENT.md §11).

## 2. Environment variables (exact names — never actual secrets)

| Variable | Where | Value (Railway reference syntax) |
|---|---|---|
| `AIC_DATABASE_URL` | api + worker + migration | `${{Postgres.DATABASE_URL}}` |
| `AIC_ENV` | api + worker | `production` |
| `AIC_MASTER_KEYS` | api ONLY | `k1:<64-hex>` — generate with `python -c "import secrets; print(secrets.token_hex(32))"` locally, paste ONCE, store offline (password manager). **Never** in the repo. |
| `AIC_ACTIVE_KEY_ID` | api ONLY | `k1` |
| `AIC_LOG_LEVEL` | api + worker | `INFO` (default) |
| `AIC_TASK_TIMEOUT_SECONDS` | worker | default `30` (leave) |
| `PORT` | api | **injected by Railway automatically — do not set.** The Dockerfile CMD already binds `--port ${PORT:-8010}`. |

Notes:

- `${{Postgres.DATABASE_URL}}` is Railway's service-variable reference syntax
  (typed in the dashboard Variables tab; Railway resolves it at deploy time —
  the raw password never appears in your repo or logs). **[assumed: exact
  variable name shown in the dashboard; confirm the Postgres service exposes
  `DATABASE_URL` in its "Available variables" panel.]**
- The bare `postgresql://` scheme of that reference is now handled by the app
  (§0.1). Alternatively the private/individual forms (`PGHOST`, `PGUSER`,
  `PGPASSWORD`, `PGDATABASE`) may be composed — but manual composition risks
  URL-encoding mistakes; prefer the platform-provided URL.
- The database URL with a **private** network host must be used (Railway
  injects the private-network form when public access is disabled
  **[assumed — verify the host in the resolved URL does not route through the
  public TCP proxy]**). Public PostgreSQL access stays disabled.

## 3. Service: commons-api

| Setting | Value |
|---|---|
| Source | GitHub repo `autonomous-intelligence-commons/commons-core`, branch `main` |
| Builder | **Dockerfile** (root `Dockerfile`: python:3.12-slim, non-root `appuser`, pinned deps + `psycopg[binary]==3.2.13`) |
| Start command | (none needed — image CMD) `sh -c "uvicorn commons_core.api.app:app --host 0.0.0.0 --port ${PORT:-8010}"` |
| Port | Railway injects `PORT`; the app binds it dynamically. Generate a **domain** for the public endpoint (TLS-terminated by Railway). |
| Health check | HTTP `GET /health` → expect `200 {"status": "ok"}` (unauthenticated, rate-limit-exempt). Configure as the service's healthcheck path; also increase the healthcheck **timeout** to ≥ 60s for first-deploy image builds. **[assumed: dashboard healthcheck configuration]** |
| Resource limits | Suggest RAM limit 512 MB, CPU limit 0.5 vCPU (see §7 costs). Watch for OOM-kills in metrics. |
| Restart policy | Railway default (restart on crash). |
| Networking | Public: only this service gets a domain. The Postgres service stays private. |

**Fail-closed guarantee (existing, unchanged):** with `AIC_ENV=production`,
the app refuses to start unless `AIC_MASTER_KEYS` is valid AND the database
URL is PostgreSQL — a missing secret means the container exits non-zero
BEFORE accepting any request (no half-started state). This is enforced by
tests in `tests/security/test_production_key_management.py`.

## 4. Service: commons-worker

| Setting | Value |
|---|---|
| Source / image | Same repo, same branch, same Dockerfile as the API (identical image digest). |
| Start command override | `python scripts/run_worker.py --loop --lease-seconds 90` |
| Domain / port | **NONE — do not generate a domain.** The worker serves no HTTP; with no domain, Railway exposes nothing publicly. |
| Variables | `AIC_DATABASE_URL`, `AIC_ENV=production` only. **No `AIC_MASTER_KEYS`** (least privilege, by design). |
| Health | No HTTP healthcheck (by design — spec §16). Railway marks it healthy when the process stays up. |
| Preserved semantics | task leasing (`--lease-seconds 90` > `AIC_TASK_TIMEOUT_SECONDS=30` — the worker refuses to start otherwise), per-agent rate limits, durable quotas, emergency stop (worker idles while the stop is engaged), graceful SIGTERM (in-flight task completes before exit). |

## 5. Migration: `alembic upgrade head` exactly once

**Recommended: Railway Pre-Deploy Command on the API service only.**

- Set the API service's *Pre-deploy command* to: `alembic upgrade head`
  **[assumed: pre-deploy command support for Docker-builder services;
  confirm the setting exists in service Settings → Deploy.]**
- Semantics: the command runs in the deployment container BEFORE the start
  command, once per deployment, with the service's variables (including the
  Postgres reference) already resolved.
- "Exactly once" is guaranteed by alembic itself: `upgrade head` is
  idempotent — `alembic_version` records the applied revision and re-runs are
  no-ops. Every deploy re-checks cheaply; the migration applies exactly once
  per new revision.
- **Concurrency assessment (why the worker gets NO pre-deploy command):**
  on a push, both services may deploy simultaneously. Two concurrent
  `alembic upgrade head` runs race on DDL (alembic takes no advisory lock);
  the loser fails its pre-deploy and marks that service's deployment failed
  (Railway keeps the previous deployment running; recovery = manual redeploy
  — ugly). Keeping migration on the API service only removes the race: the
  worker has no pre-deploy step.
- **Worker startup vs. migration window:** the worker may start before the
  first migration completes. Verified behavior: `run_worker.py` exits
  non-zero on missing tables (it does NOT silently retry); Railway's
  restart-with-backoff then relaunches it — typically succeeding once the
  API's pre-deploy finishes (tens of seconds). This is noisy-but-safe on the
  FIRST deploy only; on later deploys the schema already exists and both
  start cleanly.
- **First-deploy alternative (zero race, zero noise):** run the migration
  manually BEFORE generating the worker: deploy the API first (its pre-deploy
  migrates), wait for `/health` 200, then create/deploy the worker.
- **Rollback safety:** `alembic downgrade` is NOT automated; per
  docs/DEPLOYMENT.md §5, prefer forward-fix migrations. Never create tables
  by hand (spec).

## 6. Backups, restore, and key custody

**A persistent volume is NOT a backup.** It is the live data directory: it
does not protect against logical corruption, bad migrations, accidental
`DROP`, or project/account loss. Railway-managed backups are currently NOT
enabled on the plan (spec).

Required independent backup procedure (manual, free, no public DB access):

1. **Database dump — via the Railway CLI from inside the project network:**
   `railway connect` (or the service's shell in the dashboard) into the
   `Postgres` service, then `pg_dump --format=plain "$DATABASE_URL"` and save
   the streamed output to an encrypted local file
   (e.g. `commons-YYYYMMDD.sql`, then `gpg -c`). **[assumed: pg_dump is
   available in the Railway Postgres service container — confirm via the
   shell; if absent, attach a one-off service from the same Docker image
   with a `pg_dump` start command instead.]** Local `pg_dump` from your
   machine is impossible while public access stays disabled — keep it that
   way.
2. **Master keys — offline custody:** the database dump contains only
   CIPHERTEXT (application-layer encryption). `AIC_MASTER_KEYS` lives in
   Railway Variables — a variable store is not a backup either. Store the
   key list (`k1:<hex>`) in a password manager / offline secure location.
   **Losing the keys = the backup is permanently undecryptable.**
3. **Restore (rehearse before trusting):** create/reuse a Postgres service →
   `psql "$DATABASE_URL" < commons-YYYYMMDD.sql` (or restore into a fresh
   database) → point a fresh API deployment's `AIC_DATABASE_URL` at it → set
   the SAME master keys → start → verify `GET /health` + one authorized
   read. See docs/DEPLOYMENT.md §6.
4. **Schedule:** manual dump after meaningful data changes (Trial-plan
   scale); no cron/paid service enabled.

## 7. Resource and cost plan (Trial, ~US$5 credit)

Estimates only — Railway bills metered usage; **verify live numbers in the
dashboard's usage panel** (pricing changes; do not treat these as quotes):

| Item | Estimate | Basis |
|---|---|---|
| `commons-api` | ~120–200 MB RAM, ~0.05 vCPU avg | uvicorn + FastAPI + SQLAlchemy pool; smoke-level traffic **[assumed]** |
| `commons-worker` | ~80–150 MB RAM, ~0.01 vCPU idle | single-threaded poll loop (0.05s idle sleep), bursts on tasks |
| `Postgres` (Railway-managed) | ~150–300 MB RAM | small schema, low write volume **[assumed]** |
| Egress | negligible at this scale | content-free API responses |
| **Total** | ~0.35–0.65 GB-equivalent RAM + trivial CPU | at Railway's published ~US$10/GB-RAM/month rate → roughly **US$4–7/month** → the US$5 Trial credit lasts on the order of **~1 month** of 24/7 runtime |

Cost-control measures: (a) set a **spend limit / billing alert** at the
account level; (b) keep only these three services; (c) stop the worker
service when not testing (tasks queue durably and drain on restart);
(d) if Railway offers **App Sleeping** for the API on this plan, weigh the
cold-start latency — never sleep the worker if prompt task processing
matters **[assumed: availability of App Sleeping on Trial]**. Scale by
quotas (env vars), not instance size (docs/DEPLOYMENT.md §2).

## 8. Local verification performed (this branch)

All on the REAL repository code; **PostgreSQL 18.3** server (matches
Railway's major version — NOT inferred from PG 16):

- Full test suite against PG 18.3: **394 passed / 2 skipped** (fail-loud
  backend guard; no SQLite fallback).
- Full test suite on SQLite (default): **386 passed / 10 skipped** —
  includes 12 new tests for the URL normalization + alembic interpolation
  contract; zero regressions vs. main (374/10).
- Railway-style end-to-end on PG 18.3 with a **bare `postgresql://` URL**:
  `alembic upgrade head` ✓ → production app factory (fail-closed satisfied
  with real keys) ✓ → `/health` 200 ✓ → unauthenticated 401 ✓ →
  authenticated `agents/me` + task submit ✓ → worker `--once` processed the
  task to `succeeded` ✓ → `downgrade base` + `upgrade head` round-trip ✓.
- Percent-encoded special-character password (`p%40ss`) through the same
  path: alembic migration ✓ (this is what failed before fix #2).
- `ruff check` + `ruff format --check`: clean.
- All existing CI checks continue to run unchanged on push (lint / test /
  postgres-16 integration / migrate-check / dependency-audit / docker
  compose smoke) — the compose path is untouched by this branch.

## 9. Step-by-step deployment (Railway web interface)

> Perform ONLY after this PR is reviewed and merged to `main`. Nothing below
> was executed here.

**Preparation (local, once):**

1. Generate a master key: `python -c "import secrets; print(secrets.token_hex(32))"` → keep `k1:<hex>` in your password manager NOW.
2. Confirm the Postgres service is Online and note its reference variable name (Variables tab).

**Deploy the API service:**

3. Project → New Service → **GitHub repo** → `autonomous-intelligence-commons/commons-core` → watch that it tracks **`main`** (set the branch if the UI defaults elsewhere).
4. Open the new service → Settings → rename to `commons-api`; Builder = **Dockerfile** (auto-detected).
5. Settings → **Pre-deploy command**: `alembic upgrade head`.
6. Variables tab → add (each as its own row; reference syntax typed verbatim):
   - `AIC_DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `AIC_ENV` = `production`
   - `AIC_MASTER_KEYS` = `k1:<paste your 64-hex>`
   - `AIC_ACTIVE_KEY_ID` = `k1`
   - `AIC_LOG_LEVEL` = `INFO`
7. Settings → Networking → **Generate Domain** (this creates the ONLY public endpoint; TLS is automatic). In the domain settings, confirm the target port is the injected `PORT`.
8. Settings → Healthcheck: type HTTP, path `/health`, timeout ≥ 60s (first build is slow). *(If the UI offers no healthcheck field, rely on the port listener + the Docker HEALTHCHECK — behavior to confirm **[assumed]**.)*
9. **Deploy** (click Deploy Repo / wait for auto-deploy on the merge commit). Watch the deploy logs: the pre-deploy step runs `alembic upgrade head` (first run creates ~16 tables), then uvicorn starts.
10. Verify: `curl https://<your-domain>/health` → `{"status": "ok", ...}`; `curl -i https://<your-domain>/api/v1/admin/telemetry` → `401`.
11. Provision a first agent token for smoke tests (service shell):
    `railway shell` on `commons-api` (or dashboard shell) →
    `python scripts/manage.py provision --handle ops-admin` → copy the
    one-time token. **Do NOT print it into tickets.**

**Deploy the Worker service:**

12. New Service → **GitHub repo** → same repo/branch → Settings → rename to `commons-worker`; Builder = Dockerfile.
13. Variables: `AIC_DATABASE_URL` = `${{Postgres.DATABASE_URL}}`, `AIC_ENV` = `production`. **No keys. No domain. No pre-deploy command.**
14. Settings → **Start command override** (or "Custom start command"):
    `python scripts/run_worker.py --loop --lease-seconds 90`
15. Deploy. Verify: service stays Up (no HTTP), logs show the worker loop;
    submit a `noop` task via the API (authenticated `POST /api/v1/tasks`)
    and watch it reach `succeeded` in `GET /api/v1/tasks`.
16. Confirm the worker has NO domain: Settings → Networking shows none.

**Operational checks after first deploy:**

17. Emergency-stop drill (read-only): review `docs/INCIDENT_RESPONSE.md`; the stop engages via `POST /api/v1/admin/emergency-stop` with the admin token.
18. Take the first backup (§6) once you consider the data non-disposable.
19. Set the account spend limit/alert (§7) and check the usage panel against the estimates.

## 10. Remaining limitations (honest)

- Nothing here was deployed: **Railway-side behaviors marked [assumed]** must
  be confirmed during the first deploy (pre-deploy command support, exact
  reference variable names, healthcheck UI, pg_dump availability, App
  Sleeping, pricing).
- PostgreSQL 18 compatibility is **verified locally at the engine/DDL/suite
  level** (18.3, real server) — Railway's specific PG 18 build/image differs
  in packaging, and the platform's private-network behavior (IPv4/IPv6,
  proxying) cannot be reproduced locally.
- No load testing, no multi-worker scaling (Phase 1 scope: one worker).
- Backups are manual; a restore drill is REQUIRED before trusting them.
- Trial credits may expire before a full month of 24/7 runtime (§7 estimates).
