# ADR-0005: PostgreSQL-table-based durable scheduling (leases/backoff) over an external broker

- **Status:** Accepted (Phase 1)
- **Context:** Spec §5: "Do not introduce a message broker unless there is
  a clear requirement that PostgreSQL-based durable scheduling cannot
  satisfy." Phase 1 scheduling needs (spec §16): durable task state, retry
  limits, idempotency, concurrency protection, execution leases, timeout
  handling, failure recovery, pause/resume, graceful shutdown, explicit
  inactivity — at modest scale (deterministic handlers only).
- **Decision:** Implement the durable queue as the `scheduled_tasks`
  table: claim via conditional `UPDATE … WHERE status='pending'` +
  rowcount check (SQLite-safe stand-in for `SELECT … FOR UPDATE`), lease
  `lease_expires_at` with expiry recovery that does NOT consume an extra
  attempt (attempts are counted at claim), exponential backoff
  `min(2^attempts, 60s)` with a terminal `failed` state at
  `attempts ≥ max_attempts`, globally unique `idempotency_key`, per-agent
  (100) and global (`AIC_MAX_PENDING_TASKS`) in-queue quotas, and an
  emergency-stop check between iterations. The worker runs in its own
  process (`scripts/run_worker.py`).
- **Consequences:**
  - (+) One fewer service, credential set, and failure domain; fits the
    two-process deployment; operations are plain SQL, inspectable by the
    owner-scoped task API and telemetry counters.
  - (+) Crash recovery is data-driven: an expired lease re-queues the
    task; SIGTERM completes the in-flight task then exits.
  - (−) **At-least-once execution** — a crash after claim re-runs the task
    once the lease expires; no exactly-once claim is made (spec §16/§22
    satisfied honestly: "protection against duplicate processing" is
    best-effort via leases + idempotency keys).
  - (−) Polling (idle sleep 0.05 s default) burns a little CPU vs. a
    push-based broker — acceptable at Phase 1 scale.
  - (−) Claim/quota hot paths rely on short transactions rather than row
    locks; a high-concurrency PostgreSQL deployment should upgrade to
    locking reads (documented in `scheduler/service.py`).
  - (−) Multi-worker scaling is possible (claims are atomic) but
    untested beyond the sequential double-worker recovery test.
