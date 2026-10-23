# Deployment — AIC Phase 1 (Railway preparation & local Docker)

> **No deployment was performed** *(Phase 1 writing-time record, 2026-10-17
> — before the recorded deployment; see the §2 status note)*. No Railway
> resources were created, configured, or modified; no Docker image was
> built or run; no paid service was activated; no real AI model API was
> called. Docker and Railway were **not available in the Phase 1 sandbox**
> — everything in this document was written as *preparation*: files and
> instructions for what was then a future, separately authorized
> deployment. Per the maintainer-provided milestone record, that
> deployment has since occurred (see the §2 status note and
> `docs/RAILWAY.md`); no production-readiness verification is claimed
> here. All deployment actions require explicit user authorization
> after Phase 1.

## 1. Required environment variables

Source of truth: `.env.example` (copy to `.env` locally; values there are
disposable dev values only).

| Variable | Required | Dev default | Production guidance |
|---|---|---|---|
| `AIC_DATABASE_URL` | yes | `sqlite+pysqlite:///./data/commons.db` | `postgresql+psycopg://user:pass@host:5432/commons` — use the Railway PostgreSQL private-network URL. Bare platform schemes (`postgresql://` / `postgres://`, e.g. Railway's `${{Postgres.DATABASE_URL}}` reference) are normalized automatically at Settings load; percent-encoded userinfo is supported (see docs/RAILWAY.md). Install the PostgreSQL driver via the pinned `postgres` dependency extra (`psycopg[binary]==3.2.13` in `pyproject.toml`, matching the production image pin): `pip install -e ".[postgres]"`. |
| `AIC_MASTER_KEYS` | yes (prod) | unset → ephemeral in-memory key (all-zero and other placeholder keys are rejected at startup) | `kid:<hex>` pairs, comma-separated. Generate each key with `python -c "import secrets; print(secrets.token_hex(32))"`. **Never commit.** Empty value → ephemeral in-memory key (dev only; data lost on restart, loud warning). |
| `AIC_ACTIVE_KEY_ID` | yes (prod) | `k1` | The `kid` that seals new envelopes. Must exist in `AIC_MASTER_KEYS` (startup fails otherwise). |
| `AIC_API_HOST` / `AIC_API_PORT` | no | `127.0.0.1` / `8010` | On Railway use `0.0.0.0` and `$PORT` (Railway injects `PORT`; do NOT hardcode). |
| `AIC_ENV` | no | `development` | `production` is **fail-closed at startup**: it requires a PostgreSQL database URL and valid master keys (see §11). Also shown in telemetry (`env` field). |
| `AIC_RATE_LIMIT_PER_MINUTE` | no | 120 | Per-agent request limit. |
| `AIC_MAX_MESSAGES_PER_MINUTE` | no | 60 | Durable per-agent message quota. |
| `AIC_MAX_MESSAGE_BYTES` | no | 65536 | Message size cap (413). |
| `AIC_MAX_MEMORY_ENTRY_BYTES` | no | 65536 | Memory entry size cap (413). |
| `AIC_MAX_PENDING_TASKS` | no | 1000 | Global in-queue task cap (429). |
| `AIC_MAX_TASK_ATTEMPTS` | no | 3 | Retry cap per task. |
| `AIC_TASK_TIMEOUT_SECONDS` | no | 30 | **Enforced** as the worker execution budget: claimed tasks run under `execute_claimed(timeout_seconds)` with a revocable session guard, and the worker refuses to start unless `lease_seconds > task_timeout_seconds` (compose default `--lease-seconds 90`; see §11). Regression-tested in `tests/operational/test_task_timeout.py` (10 tests). *(Historical note: inert at Phase 1 — enforcement was added by the Phase 1 hardening pass.)* |
| `AIC_RETENTION_SECURITY_EVENTS_DAYS` | no | 14 | Retention policy for security events (pruning job deferred — see `docs/SECURITY_CONTROLS.md` SC-26). |
| `AIC_LOG_LEVEL` | no | `INFO` | Keep INFO in prod (log lines are content-free by design). |

**Secret rules:** master keys, database credentials and admin tokens live in
the platform's variable store (Railway Variables) or the local `.env`
(git-ignored) — never in the repository, never in image layers, never in
logs. At the time of writing, `.gitignore` excludes `.env` but **not**
`data/`, `*.key`, or `.venv*` variants; a separate ignore-hygiene patch
(PR-2) has been proposed to add those patterns and is **not yet
implemented**. Until it lands, keep such files out of the repository
manually — ignore rules are a backstop, not a substitute for secret hygiene.

## 2. Railway service layout (planned — historical Phase 1 record)

> **Status note (2026-10-21):** This section was written during Phase 1
> preparation, when the layout was *planned* and nothing had been created
> (no Docker or Railway access existed in the Phase 1 sandbox). Railway
> services have since been created: per the maintainer-provided milestone
> record (`docs/milestones/2026-10-19-phase-1.md` §4; see also
> `docs/RAILWAY.md`, status "DEPLOYED 2026-10-19"), `commons-core`,
> `Postgres`, and `commons-worker` were all reported Online. That
> deployment record does not establish production backup custody,
> off-provider backup retention, full disaster-recovery testing, or
> unattended autonomous Agent operations. The topology and security
> warnings below remain the documented reference.

Project (already existing per spec §1): `autonomous-intelligence-commons`.

| Service | Start command | Notes |
|---|---|---|
| `commons-postgres` | Railway PostgreSQL plugin | PRIVATE network only. Railway exposes `DATABASE_URL` / `DATABASE_PRIVATE_URL` — map the private one into `AIC_DATABASE_URL`. **Do not expose a public port.** |
| `commons-migrate` | `alembic upgrade head` | One-shot job, runs BEFORE api/worker start (see §3 sequencing). Can be run as a Railway "command" service that exits, or manually from a shell with the variables set. |
| `commons-api` | `uvicorn commons_core.api.app:app --host 0.0.0.0 --port $PORT` | The only service needing a public (TLS-terminated) endpoint. |
| `commons-worker` | `python scripts/run_worker.py --loop` | Private; no public endpoint; handles SIGTERM gracefully. UNLIMITED task lifetime by default (explicit `--max-tasks N` bounds a run for tests/batches); idle poll 1.0s. |

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
- Verified locally on SQLite and disposable PostgreSQL (upgrade/downgrade/
  upgrade round-trip, migration `0002` lease fencing included).
- Worker startup after migrations is required (it reads `scheduled_tasks` /
  `system_state`).
- **Lease fencing (migration `0002`, R02-B1)**: the worker verifies at
  startup that `scheduled_tasks.lease_generation` exists and REFUSES to
  start otherwise (fail-closed — a fencing-aware binary must not run
  unfenced against a `0001`-schema database). Deploy order for the `0002`
  rollout is therefore unchanged — `migrate` completes before `worker`
  starts — but a mixed-version window (old worker binary running against
  an upgraded schema, or new binary against an un-upgraded schema) is
  unsupported: stop and drain the old workers, then start the new ones
  (blue-green swap). Tasks left `pending` while workers are stopped are the
  safe state — zero writes, zero data damage; expired leases are recovered
  automatically once a fencing-aware worker starts.

## 4. Logging restrictions

Already enforced in code (see `docs/PRIVACY_AND_SAFETY.md`): logs carry
categories, ids/counts and limits only — no content, no tokens, no keys.
Railway log drains therefore receive content-free lines; still restrict
drain access per platform best practice. `AIC_LOG_LEVEL=INFO` recommended.

## 5. Rollback

1. Application rollback: `git revert <commit>` (or redeploy previous
   revision in Railway) and restart `commons-api` + `commons-worker`.
   **Worker rollback doctrine (R02-B1 lease fencing)**: reverting to an
   UNFENCED legacy worker binary re-opens the original R02-B vulnerability
   (stale workers writing task state unconditionally) and is **NOT a
   safety-preserving rollback**. A safe worker rollback either rolls
   FORWARD to another fencing-aware build, or SUSPENDS worker execution
   entirely (tasks stay `pending` — no data damage) until a reviewed
   recovery procedure exists. Schema downgrade (`0002` → `0001`) by itself
   is not an operational rollback either: the defect lives in code, not in
   the schema.
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
- **Stage 1 backup tooling is implemented** (ADR-0009): a streaming
  `pg_dump → age`-encrypted backup pipeline, verified atomic publication,
  and a fail-closed synthetic restore drill now exist in this repository
  with CI coverage against real PostgreSQL 16 and 18 — see
  `docs/BACKUP.md` for usage, scope, and the Stage 2+ plan. The earlier
  Phase 1 gap "no automated restore test against PostgreSQL" is closed at
  the synthetic level; a real-data drill remains a future, separately
  authorized stage.
- Recommended procedure (when production deployment is authorized):
  the dedicated backup workload of ADR-0009 (Railway Cron, private-network
  `pg_dump`, age public-key encryption, off-provider storage); keys backed
  up independently (offline secure copy of `AIC_MASTER_KEYS`), never in
  the repo.
- Restore: use the restore-drill tooling of `docs/BACKUP.md` against a
  **disposable** target first; pointing production back at the restored
  data is a manual, separately reviewed procedure (there is deliberately
  no automatic production restore command).
- **Stage 2 adds offline recovery-key custody packages** (ADR-0010):
  repository tooling and a runbook for generating, duplicating,
  verifying, and (in a disaster) unlocking offline copies of the age
  identities and `AIC_MASTER_KEYS` — see `docs/RECOVERY_KEYS.md`. The
  synthetic disaster-recovery exercise (total infrastructure loss →
  recovery from two offline package copies) runs in CI; real key
  generation is an owner-gated, offline action. A production restore
  remains manual and owner-authorized — the Stage 1 boundary is
  unchanged.

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

## 9. Local Docker development (verified by the CI docker job)

`Dockerfile` and `compose.yaml` are included in the repository. *(Historical,
Phase 1: they were never built or run in the development sandbox — Docker was
unavailable there.)* Since publication, the `docker` CI job has built the image
and run the compose stack on every push and pull request (non-root user check,
fail-closed startup check, authenticated smoke test, clean teardown) — green
at the 2026-10-20 hygiene-audit baseline. The commands below remain the
manual reference flow for a Docker-capable machine.

Services in `compose.yaml`:

| Service | What it does |
|---|---|
| `postgres` | `postgres:16-alpine` with `pg_isready` healthcheck and a named volume; credentials from `.env` |
| `migrate` | one-shot `alembic upgrade head`; depends on postgres being healthy |
| `api` | uvicorn app on port 8010 (the ONLY host-published port); depends on migrate completing successfully |
| `worker` | `python scripts/run_worker.py --loop` (unlimited task lifetime by default); depends on migrate; no host port |

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

## 11. Production configuration requirements (Phase 1 hardening — fail-closed)

When `AIC_ENV=production` the application REFUSES to start unless ALL of
the following hold (startup exits non-zero before accepting any request —
proven by a live 8-case matrix, see docs/TESTING.md §6):

1. `AIC_DATABASE_URL` targets PostgreSQL (`postgresql+psycopg://...`).
   SQLite is refused for the API, the worker and the CLI.
2. `AIC_MASTER_KEYS` is set: `kid:<64 hex chars>` (32-byte AES-256) per
   key, comma-separated. Generate with:
   `python -c "import secrets; print(secrets.token_hex(32))"`.
3. `AIC_ACTIVE_KEY_ID` names one of the configured kids.
4. No placeholder-pattern keys (all-f / deadbeef-repeat / sequential-hex /
   all-zero — rejected at startup).

The worker and management CLI do NOT require keys (least privilege — they
never decrypt content; the compose worker container deliberately receives
no `AIC_MASTER_KEYS`).

Worker timing: `AIC_TASK_TIMEOUT_SECONDS` (default 30) is enforced. The
worker refuses to start unless `lease_seconds > timeout` — compose runs
`--lease-seconds 90` by default. Tune both together when changing limits.

## 12. Key rotation and recovery (operational procedure)

See docs/CRYPTOGRAPHY.md §6 for the full procedure. Summary: generate a
new key → deploy with BOTH kids (`k1:<old>,k2:<new>`, `AIC_ACTIVE_KEY_ID=k2`)
→ verify historical decryption → retain the old kid as long as recovery
must remain possible (keys are required to decrypt backups; removal of a
kid is an explicit, irreversible decision that makes its ciphertext
undecryptable). Rotation requires a restart; history is not re-encrypted.

## 13. Railway deployment prerequisites (unchanged posture)

Environment variables required: `AIC_DATABASE_URL` (Railway PostgreSQL),
`AIC_MASTER_KEYS`, `AIC_ACTIVE_KEY_ID`, `AIC_ENV=production`. Compatible
with Railway env vars or sealed secrets; no external paid KMS.
*(Historical, Phase 1 writing-time record (2026-10-17): "No Railway
deployment has been performed." was accurate when written; it no longer
describes the present state.)* Per the maintainer-provided milestone
record, a Railway deployment has since occurred (see the §2 status note
and `docs/RAILWAY.md`); that record does not establish production
disaster recovery, independent backup custody, off-provider backups,
or unattended autonomous Agent operation. Private-network reachability
is NOT an authorization boundary (spec §18). Run `alembic upgrade head`
(migrate step) before starting api/worker; see §7 of this document and
compose.yaml for the service topology.

The complete Railway deployment configuration (service layout,
reference-variable syntax, pre-deploy migration strategy, backup/key
custody, cost plan, and step-by-step web-UI instructions) lives in
**docs/RAILWAY.md** — a Phase 1 runbook that, per its own status header
and the maintainer-provided milestone record, was prepared, reviewed,
and then executed (deployed 2026-10-19); it is not a
production-readiness certification.

## 14. External effects subsystem (R02-B2 E1 — FAKE-ONLY, **NOT DEPLOYED**)

This section documents the FUTURE integration plan for the effect-outbox
subsystem delivered by R02-B2 E1. **Nothing in this section is currently
wired into `commons-api` or `commons-worker`:** the dispatcher is a
standalone module (`commons_core.effects.dispatcher.dispatch_tick`) and no
process currently runs it. Real external effects remain **PROHIBITED**
until the separately reviewed E2 stage (process isolation + egress lock).

**State (E1)**: schema (`migration 0003`), gateway, fake-only dispatcher
and fake provider exist and are test-verified in isolation; the capability
allowlist contains ONLY synthetic `fx.*` code constants — no real adapter
exists, no credential exists, and **no environment variable can enable a
real effect** (`AIC_EFFECTS_ENABLED` exposes the synthetic gateway only).

**Fail-closed activation (when a future stage wires the dispatcher):**

1. `alembic upgrade head` must complete first (`0003` — the outbox tables);
2. effects-aware code refuses to dispatch against a `0002`-only schema
   (`verify_effects_schema` — the same fail-closed doctrine as the `0002`
   lease-fencing startup gate; a hard error, never a warning);
3. `AIC_EFFECTS_ENABLED` defaults to **false**: the gateway answers 403
   `EFFECTS_DISABLED` and the dispatcher scans NOTHING (existing rows are
   FROZEN, never cleared) until the owner explicitly enables the SYNTHETIC
   subsystem per environment;
4. mixed-version windows are unsupported (same blue-green discipline as
   §3): stop old workers, migrate, start new ones. A pre-0003 binary
   coexists safely with the 0003 schema (the new tables are inert to it);
   a post-0003 dispatcher binary against a 0002 schema refuses to start.

**Dispatcher disablement / rollback (freeze doctrine — never drop data):**

- Disabling `AIC_EFFECTS_ENABLED` freezes the outbox: no scans, no
  submissions, no cancellations — rows await re-enablement. It is the
  FIRST lever for any incident response;
- `0003 → 0002` schema downgrade **REFUSES to run while effect rows or
  effect events exist** (the migration raises; verified by tests). The
  only safe downgrade precondition is a verified-EMPTY effect system;
  with live rows the procedure is freeze → reconcile/drain (or export) →
  then downgrade;
- rows in `SUBMITTED`/`UNKNOWN` states must never be discarded silently:
  `UNKNOWN` rows are unresolved *external-outcome* records — closing them
  requires provider evidence (reconciliation) or the owner-approved
  manual marking procedure (a real admin interface is deferred to a
  separately reviewed stage; the E1 function is labeled `manual:test-only`).
  Reconciliation evidence is **custody-checked** (S1 remediation): a
  `NEVER_RECEIVED` answer from a fake-provider instance constructed
  *after* the row's submission (a restarted process — its in-memory
  ledger is unrecoverable) is treated as INCONCLUSIVE and the row stays
  `UNKNOWN`; it closes as `FAILED` only when the answering instance held
  receipt custody over the submission. Equally, every asynchronous
  acknowledgment is bound to its **originating claim epoch** (S2
  remediation): a pump-delivered ack carries the submitting attempt's
  epoch/owner, stale epochs are rejected with zero bytes, and an ack
  without provenance is rejected fail-closed (audited) — never silently
  attributed to the current dispatcher attempt. Acknowledgment
  authorization requires the **complete originating attempt identity**
  (S3 remediation): an ack missing either its epoch or its owner is
  rejected fail-closed — the row's current epoch/owner is never
  substituted for missing ack-origin identity (one deduplicated audit
  marker per effect row bounds repeated-invalid-ack noise) — and an ack
  naming a non-matching owner is rejected with zero bytes *before* any
  terminal-state shortcut, so no wrong-owner attempt can obtain an
  idempotent confirmation from an already-`SUCCEEDED` row;
- rolling back the BINARY while leaving the 0003 schema is safe (the old
  binary never imports the effects package — inert tables, zero behavior
  change on the task axis).

**Emergency stop dual axis (design §4.6-5, EFF-11/12):** the existing
emergency stop keeps its task-axis semantics untouched; on the effect
axis, an engaged stop (1) refuses new effect requests (503 `SERVICE_PAUSED`,
symmetric with task creation) and (2) cancels all pre-submission rows
(reason `emergency_stop`) — while rows already `SUBMITTED` are frozen
(no acks applied, no submissions) until the stop is released; they then
complete or reconcile honestly. There is **no path** from `SUBMITTED`
(or any post-submission state) to `CANCELLED` — a submitted effect is
never reported as cancelled (it may already have been applied).

**E2 prerequisites (owner decision D8 — all required before any real
adapter):** dispatcher extracted to a separate process with its own
credentials (the `scripts/run_worker.py` "workers receive NO
AIC_MASTER_KEYS" precedent), an egress allow-list that permits outbound
traffic ONLY from the dispatcher process, the full DG-E gate set, an
operational runbook rehearsal, and explicit owner approval of the first
real capability class. Until then every real-effect path remains
structurally absent, not merely disabled.
