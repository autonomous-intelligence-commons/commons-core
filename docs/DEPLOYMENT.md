# Deployment — AIC Phase 1 (Railway preparation & local Docker)

> **No deployment was performed.** No Railway resources were created,
> configured, or modified; no Docker image was built or run; no paid service
> was activated; no real AI model API was called. Docker and Railway were
> **not available in the Phase 1 sandbox** — everything in this document is
> *preparation*: files and instructions for a future, separately authorized
> deployment. All deployment actions require explicit user authorization
> after Phase 1.

## 1. Required environment variables

Source of truth: `.env.example` (copy to `.env` locally; values there are
disposable dev values only).

| Variable | Required | Dev default | Production guidance |
|---|---|---|---|
| `AIC_DATABASE_URL` | yes | `sqlite+pysqlite:///./data/commons.db` | `postgresql+psycopg://user:pass@host:5432/commons` — use the Railway PostgreSQL private-network URL. Install `psycopg[binary]` alongside the pinned deps when adding the PostgreSQL driver (it is NOT in the Phase 1 pinned set because SQLite was the sandbox DB). |
| `AIC_MASTER_KEYS` | yes (prod) | `k1:<64 hex zeros>` (disposable!) | `kid:<hex>` pairs, comma-separated. Generate each key with `python -c "import secrets; print(secrets.token_hex(32))"`. **Never commit.** Empty value → ephemeral in-memory key (dev only; data lost on restart, loud warning). |
| `AIC_ACTIVE_KEY_ID` | yes (prod) | `k1` | The `kid` that seals new envelopes. Must exist in `AIC_MASTER_KEYS` (startup fails otherwise). |
| `AIC_API_HOST` / `AIC_API_PORT` | no | `127.0.0.1` / `8010` | On Railway use `0.0.0.0` and `$PORT` (Railway injects `PORT`; do NOT hardcode). |
| `AIC_ENV` | no | `development` | Set `production` for clarity in telemetry (`env` field). |
| `AIC_RATE_LIMIT_PER_MINUTE` | no | 120 | Per-agent request limit. |
| `AIC_MAX_MESSAGES_PER_MINUTE` | no | 60 | Durable per-agent message quota. |
| `AIC_MAX_MESSAGE_BYTES` | no | 65536 | Message size cap (413). |
| `AIC_MAX_MEMORY_ENTRY_BYTES` | no | 65536 | Memory entry size cap (413). |
| `AIC_MAX_PENDING_TASKS` | no | 1000 | Global in-queue task cap (429). |
| `AIC_MAX_TASK_ATTEMPTS` | no | 3 | Retry cap per task. |
| `AIC_TASK_TIMEOUT_SECONDS` | no | 30 | **Honest note:** this setting is currently *inert* — it is defined in `config/settings.py` but not enforced anywhere in Phase 1 (the effective bound is the worker lease, `--lease-seconds`, default 30, plus retry caps). Kept as a documented configuration placeholder; enforcement is deferred (recorded in the Phase 1 worklog). |
| `AIC_RETENTION_SECURITY_EVENTS_DAYS` | no | 14 | Retention policy for security events (pruning job deferred — see `docs/SECURITY_CONTROLS.md` SC-26). |
| `AIC_LOG_LEVEL` | no | `INFO` | Keep INFO in prod (log lines are content-free by design). |

**Secret rules:** master keys, database credentials and admin tokens live in
the platform's variable store (Railway Variables) or the local `.env`
(git-ignored) — never in the repository, never in image layers, never in
logs. `.gitignore` already excludes `.env`, `data/`, `*.key`.

## 2. Railway service layout (planned — nothing created)

Project (already existing per spec §1): `autonomous-intelligence-commons`.

| Service | Start command | Notes |
|---|---|---|
| `commons-postgres` | Railway PostgreSQL plugin | PRIVATE network only. Railway exposes `DATABASE_URL` / `DATABASE_PRIVATE_URL` — map the private one into `AIC_DATABASE_URL`. **Do not expose a public port.** |
| `commons-migrate` | `alembic upgrade head` | One-shot job, runs BEFORE api/worker start (see §3 sequencing). Can be run as a Railway "command" service that exits, or manually from a shell with the variables set. |
| `commons-api` | `uvicorn commons_core.api.app:app --host 0.0.0.0 --port $PORT` | The only service needing a public (TLS-terminated) endpoint. |
| `commons-worker` | `python scripts/run_worker.py --loop` | Private; no public endpoint; handles SIGTERM gracefully. |

### Private networking caveat (spec §18 — documented limitation)

Railway private networking is a **reachability** boundary, **not an
authorization** boundary: a private network does not automatically prevent
every service in the project from connecting to PostgreSQL. Application-level
controls remain the actual protection: the worker and API authenticate to the
DB with scoped credentials, and the API authenticates every request
independently (Zero Trust). Keep PostgreSQL unexposed publicly and rely on
the application layers — not the network — for authorization.

### Health check

- `GET /health` (unauthenticated, rate-limit-exempt) returns
  `{"status": "ok"}` — use it as the Railway health check path for
  `commons-api`. (Railway also supports TCP checks; `/health` is the
  application-level signal.)

### Resource sizing

Modest: two small services (API + worker) + PostgreSQL. **No GPU and no
paid model APIs are required or used in Phase 1.** CPU/RAM: smallest
available plans suffice for the deterministic test workload; scale quotas
via env vars rather than instance size. Set provider-level **usage/spend
limits and billing alerts** in the Railway dashboard (application quotas do
not guarantee a fixed cloud invoice — spec §19).

## 3. Startup sequencing

```
postgres (healthy)
    └── alembic upgrade head          (migrate step — must complete OK)
            ├── commons-api start
            └── commons-worker start
```

- Migration runs against `AIC_DATABASE_URL` (env.py resolves it — nothing is
  hardcoded in `alembic.ini`).
- Verified locally on SQLite (upgrade/downgrade/upgrade round-trip); **not
  verified against PostgreSQL in this sandbox** — the first real deployment
  should run `alembic upgrade head` manually and check the result before
  starting the worker.
- Worker startup after migrations is required (it reads `scheduled_tasks` /
  `system_state`).

## 4. Logging restrictions

Already enforced in code (see `docs/PRIVACY_AND_SAFETY.md`): logs carry
categories, ids/counts and limits only — no content, no tokens, no keys.
Railway log drains therefore receive content-free lines; still restrict
drain access per platform best practice. `AIC_LOG_LEVEL=INFO` recommended.

## 5. Rollback

1. Application rollback: `git revert <commit>` (or redeploy previous
   revision in Railway) and restart `commons-api` + `commons-worker`.
2. Schema rollback: **caution** — `alembic downgrade` on a database with
   live data is destructive (e.g. `downgrade base` drops tables). Prefer
   forward-fix migrations; if a downgrade is unavoidable, snapshot the
   database first (see §6) and stop both app processes during the change.
3. Failed deployment (bad env/keys): the app fails fast at startup when
   `AIC_ACTIVE_KEY_ID` is missing from `AIC_MASTER_KEYS` — fix variables and
   redeploy; no data is touched by a failed boot.

## 6. Backup & key recovery

- **Backups contain ciphertext.** Application-layer encryption means a
  `pg_dump`/Railway backup is only usable together with the master keys:
  **losing the keys = permanently losing the encrypted data** (messages,
  memory, activity summaries, governance content).
- Recommended procedure (to be performed when deployment is authorized):
  scheduled `pg_dump` (Railway database backups or a cron job running
  `pg_dump` with private-network access), stored encrypted at rest by the
  provider, access-restricted; keys backed up independently (offline secure
  copy of `AIC_MASTER_KEYS`), never in the repo.
- Restore: `pg_restore`/`psql` into a fresh database → `alembic upgrade
  head` (schema check) → point `AIC_DATABASE_URL` → start api/worker →
  verify with `GET /health` + a sample authorized read. **No automated
  restore test against PostgreSQL exists in Phase 1** (documented gap — see
  `docs/TESTING.md` §untested).

## 7. Key rotation procedure

1. Generate a new key: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. Update `AIC_MASTER_KEYS` to `old-kid:<old hex>,new-kid:<new hex>` (keep
   the old kid!) and `AIC_ACTIVE_KEY_ID=new-kid`.
3. Restart `commons-api` and `commons-worker`.
4. New seals use the new kid; old envelopes remain decryptable via the old
   kid.

**Trade-off (documented):** rotation does NOT re-encrypt historical data.
If an old kid is suspected leaked, treat all envelopes under it as exposed:
remove the kid from `AIC_MASTER_KEYS` (reads of those envelopes then fail
with `UNKNOWN_KEY`) and rebuild from trusted sources. See
`docs/INCIDENT_RESPONSE.md`.

## 8. Emergency shutdown (operational)

```bash
curl -X POST https://<api-host>/api/v1/admin/emergency-stop \
  -H "Authorization: Bearer <ADMIN token>"
```

Effects: new writes → 503 `SERVICE_PAUSED`; reads continue; worker idles.
Release with `DELETE` on the same path. Full procedure:
`docs/INCIDENT_RESPONSE.md`. The admin token comes from
`scripts/manage.py provision --handle ops-admin --admin` run against the
production database (one-time token display).

## 9. Local Docker development (files prepared — NOT executed here)

`Dockerfile` and `compose.yaml` are included in the repository. **They were
never built or run in this sandbox (Docker unavailable)** — treat them as
prepared, unverified artifacts; validate on a Docker-capable machine before
relying on them.

Services in `compose.yaml`:

| Service | What it does |
|---|---|
| `postgres` | `postgres:16-alpine` with `pg_isready` healthcheck and a named volume; credentials from `.env` |
| `migrate` | one-shot `alembic upgrade head`; depends on postgres being healthy |
| `api` | uvicorn app on port 8010 (the ONLY host-published port); depends on migrate completing successfully |
| `worker` | `python scripts/run_worker.py --loop`; depends on migrate; no host port |

Commands (on a Docker machine):

```bash
cp .env.example .env
# REQUIRED: set a real key (the all-zeros example value is rejected at
# startup by design) — generate one:
python3 -c "import secrets; print(secrets.token_hex(32))"   # → AIC_MASTER_KEYS=k1:<hex>
docker compose up --build                 # postgres → migrate → api + worker
docker compose exec api python -c "import commons_core; print('ok')"
docker compose down                       # stop cleanly (worker handles SIGTERM)
```

Compose defaults: `POSTGRES_USER=commons`, `POSTGRES_PASSWORD=commons-dev`,
`POSTGRES_DB=commons`, `AIC_API_PORT=8010` — overridable via `.env`. The
worker container deliberately receives ONLY the database URL (it never
decrypts content — least privilege); the api container additionally gets
`AIC_MASTER_KEYS`/`AIC_ACTIVE_KEY_ID`. `compose up` fails fast with a clear
message when `AIC_MASTER_KEYS` is unset.

Image properties (see `Dockerfile`): `python:3.12-slim` base,
non-root `appuser`, `pip install --no-cache-dir .` (pinned deps from
`pyproject.toml`), `EXPOSE 8010`, `HEALTHCHECK` via python urllib against
`/health` (curl is not present in slim), no secrets baked into layers (all
secrets come from environment at runtime), minimal layer count.

## 10. Recommended GitHub repository settings (require org authorization — NOT changed)

Branch protection / rulesets on `main`; Dependabot security alerts (config
file included: `.github/dependabot.yml`); secret scanning; restricted
deployment credentials; review of security-sensitive changes. Per spec §23,
no GitHub organization settings were modified during Phase 1.
