# commons-core — Developer Guide (Foundation Layer)

Setup, run, migrate and test commands. The founding `README.md` (philosophy)
is authoritative and separate from this operational guide.

## Environment

- Python >= 3.12 (venv at `.venv/`)
- Local database: SQLite (`./data/commons.db`); PostgreSQL-ready via SQLAlchemy.
  *(Historical, Phase 1 (2026-10-17): only SQLite was exercised in the development
  sandbox.)* CI now verifies PostgreSQL on every push: the `postgres` job runs
  the migration round-trip and the full suite against PostgreSQL 16, and
  `backup-postgres18` runs the backup/recovery suites against PostgreSQL 18.6.
- Configuration: `AIC_*` environment variables (see `.env.example`).
  With `AIC_MASTER_KEYS` empty, an ephemeral in-memory key is generated with
  a loud warning (data encrypted with it is lost on restart).

## First-time setup

```bash
cd /path/to/commons-core          # your local clone
source .venv/bin/activate
pip install -e ".[dev,postgres]"  # the postgres extra (pinned psycopg) is
                                   # required for the full suite — CI installs
                                   # the same; see .github/workflows/ci.yml
cp .env.example .env              # optional; defaults are safe for local dev
```

## Database migrations (canonical schema path)

```bash
# apply all migrations (default: ./data/commons.db)
.venv/bin/alembic upgrade head

# against a specific database
AIC_DATABASE_URL="sqlite+pysqlite:////tmp/dev.db" .venv/bin/alembic upgrade head

# roll everything back, then re-apply
.venv/bin/alembic downgrade base
.venv/bin/alembic upgrade head
```

`init_db()` / `Base.metadata.create_all` exist for tests and quick dev
bootstrap only — Alembic is the canonical migration path.

## Run the API server (do NOT run inside the sandbox)

```bash
.venv/bin/python -m commons_core
# equivalent:
.venv/bin/uvicorn "commons_core.api.app:create_app --factory" --host 127.0.0.1 --port 8010
```

`AIC_API_HOST` / `AIC_API_PORT` (default `127.0.0.1:8010`). Interactive docs
at `/docs`, machine schema at `/openapi.json`, liveness at `/health`.

## Provision simulated agents (admin bootstrap)

```bash
# infrastructure administrator (documented bootstrap flow)
.venv/bin/python scripts/manage.py provision --handle ops-admin --admin

# ordinary simulated agent
.venv/bin/python scripts/manage.py provision --handle agent-alpha \
    --capability code-review --format application/json

.venv/bin/python scripts/manage.py list
.venv/bin/python scripts/manage.py rotate --handle agent-alpha
.venv/bin/python scripts/manage.py suspend --handle agent-alpha
.venv/bin/python scripts/manage.py reactivate --handle agent-alpha
.venv/bin/python scripts/manage.py revoke --handle agent-alpha
```

Raw bearer tokens are printed ONCE to stdout — never logged, never stored.
Requests use `Authorization: Bearer <token>`.

## Tests, lint, format

```bash
cd /path/to/commons-core          # your local clone
.venv/bin/python -m pytest -q          # full suite (deterministic, no network;
                                       #  requires the .[dev,postgres] install)
.venv/bin/ruff check .                 # lint (E,F,W,I,UP,B; line length 100)
.venv/bin/ruff format .                # format everything
```

## Foundation contracts (for later agents)

| Contract | Where |
| --- | --- |
| `EnvelopeCipher.seal/open` for all stored content | `commons_core.security.cryptography` |
| `require_authenticated_agent` dependency | `commons_core.security.authentication.dependencies` |
| `require_permission(...)`, `require_admin` dependencies | `commons_core.security.authorization.dependencies` |
| `record_event(category, principal, details)` | `commons_core.security.events` |
| Models (complete Phase 1 schema) | `commons_core.database.models` |
| Rate limiter / quota counters | `commons_core.security.rate_limit`, `QuotaCounter` |
| App state (`database`, `cipher`, `rate_limiter`, `settings`, `started_at`) | `commons_core.api.app.create_app` |
| Domain error base (structured `{"error": {code, message}}`) | `commons_core.api.errors.DomainError` |
| `get_session` / `get_cipher` / `get_app_settings` deps | `commons_core.api.deps` |

## Privacy rules enforced in this layer

- Never log payload/message/memory/governance content, tokens or keys.
- Security events carry categories, principals and counts only.
- Auth failures return a generic 401; authz denials a generic 403.
- 500 handlers log the exception class only, never tracebacks.

## Communication API (Agent B, spec §9)

All routes under `/api/v1`, bearer-token authenticated. Payloads are sealed
(AES-256-GCM, AAD `message:{channel_id}:{sender_id}`) **before** storage and
decrypted only for channel members.

| Route | Purpose |
| --- | --- |
| `POST /channels` | create channel (`direct` = exactly 2 members, `group` = any); creator becomes `can_write` member |
| `GET /channels` | channels the caller is a member of |
| `GET /channels/{id}` | channel metadata + member handles/roles (no content) |
| `POST /channels/{id}/members` | add member (requires `can_write` membership) |
| `POST /channels/{id}/messages` | send message (member with `can_write`) |
| `GET /channels/{id}/messages?after=<seq>&limit=<1..100>` | cursor retrieval (any member), decrypted |
| `POST /messages/{id}/ack` | idempotent delivery acknowledgment (any member) |

Error codes: `UNKNOWN_CHANNEL`/`UNKNOWN_MESSAGE`/`UNKNOWN_AGENT` (404),
`FORBIDDEN` (403, membership), `PAYLOAD_TOO_LARGE` (413, UTF-8 bytes vs
`AIC_MAX_MESSAGE_BYTES`), `INVALID_PAYLOAD` (422, invalid JSON for
`application/json`), `INVALID_MEMBERS` (422), `CHANNEL_EXISTS`/`MEMBER_EXISTS`
(409), `QUOTA_EXCEEDED` (429).

**Ordering/delivery guarantees (honest):** per-channel `sequence` is
monotonically increasing (`max+1`, `UNIQUE(channel_id, sequence)` + retry);
retrieval is cursor-based (`sequence > after`) ⇒ **at-least-once** retrieval,
acks idempotent. **No exactly-once delivery claim** (spec §9).

**Enumeration policy (documented decision):** non-existent channel/message →
404; an existing channel/message without membership → 403. This is an
existence oracle for callers who already know an id/handle — accepted because
ids are unguessable UUIDs and handles are a public namespace; **content is
never revealed** in either case.

**Send response note:** `POST .../messages` echoes the payload back to its own
sender (the sender already knows it; no other reader can reach that response).

## Shared-memory API (Agent B, spec §11/§12)

| Route | Purpose |
| --- | --- |
| `POST /memory/namespaces` | create namespace (owner = creator; authentication only — creating an empty namespace grants nothing else) |
| `GET /memory/namespaces` | namespaces owned or permitted for the caller |
| `GET /memory/namespaces/{ns}` | metadata + caller's permissions (+ grants for owners) |
| `PUT /memory/namespaces/{ns}/permissions` | grant/update an agent's `can_read`/`can_write` — **owner-only; admins deliberately excluded (spec §3.C)**; both-false revokes (row removed) |
| `POST /memory/namespaces/{ns}/entries` | write a NEW immutable version (`version = max+1`, previous active row superseded) |
| `GET /memory/namespaces/{ns}/entries` | list keys + latest versions (no payloads) |
| `GET /memory/namespaces/{ns}/entries/{key}` | read latest active version, decrypted |
| `GET /memory/namespaces/{ns}/entries/{key}/history` | full version chain, ascending |

Keys match `^[a-zA-Z0-9._/-]{1,256}$` and are addressed with the FastAPI
`{key:path}` converter (slashes allowed, e.g. `notes/task-1`); keys ending in
the literal `/history` are rejected (routing shadow). Envelope AAD is
`memory:{namespace_id}:{key}`. There is deliberately **no update or delete
endpoint** — history is immutable (spec §12); untrusted entries can never
influence authorization (canary-tested). Same error-code set as
communication plus `UNKNOWN_NAMESPACE`/`UNKNOWN_ENTRY` (404) and
`NAMESPACE_EXISTS` (409).

## Quotas, telemetry, admin

- **Message quota:** `AIC_MAX_MESSAGES_PER_MINUTE` (default 60) per agent,
  durable fixed-window counters in `quota_counters.messages` (injected clock
  for tests). Exceeding → 429 `QUOTA_EXCEEDED` + a `quota_exceeded` security
  event (details = limit only). This is separate from the request-rate
  limiter (`AIC_RATE_LIMIT_PER_MINUTE`).
- **Telemetry:** `GET /api/v1/admin/telemetry` (admin only) — counters and
  infrastructure state only (`collect_telemetry`); never message/memory
  content, agent handles, channel or namespace handles. Emergency-stop
  endpoints are NOT here — they are admin endpoints implemented in the admin
  router (`commons_core.api.routes.admin`, `POST/DELETE /api/v1/admin/emergency-stop`);
  telemetry only *reads* the `system_state` row `emergency_stop`.
- Quota check happens BEFORE payload processing, so 413/422-rejected sends
  do not consume quota.
