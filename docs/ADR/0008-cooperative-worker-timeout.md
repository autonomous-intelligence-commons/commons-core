# ADR-0008: Cooperative worker execution timeout with a revocable session guard

- **Status:** Accepted (Phase 1 Final Security Hardening, Task 4)
- **Context:** `AIC_TASK_TIMEOUT_SECONDS` existed in settings but was
  documented as not enforced: durable leases, retries and graceful
  shutdown bound task lifetime only indirectly, and a long-running handler
  could execute indefinitely within its lease. A mere timer that marks a
  task as timed out while the handler keeps running would be dishonest and
  unsafe (unrestricted side effects after the "failure" was recorded).
  Python cannot forcibly terminate arbitrary running code in-process —
  thread kill / async-exception injection is unreliable and can corrupt
  invariants; signals are main-thread-only and Unix-only.
- **Decision:** Implement the strongest SAFE mechanism practical for this
  architecture, and document its limits explicitly:
  1. When `timeout_seconds > 0`, `execute_claimed` runs the handler on a
     daemon thread and waits at most that wall-clock budget.
  2. On timeout, an `_ExecutionGuard` is REVOKED: the handler had received
     a revocable proxy of the Session whose every method call first
     acquires a shared lock and checks the revoked flag — after
     revocation, ANY call raises `TaskExecutionRevokedError` BEFORE
     touching the database. `revoke()` holds the same lock, so once it
     returns no handler statement is in flight and none can start: an
     abandoned handler can never commit a success or corrupt state after
     the main thread has applied the timeout failure.
  3. A timeout is treated as a normal handler failure: rollback first
     (discard partial writes), then the existing retry/backoff policy
     (attempts >= max_attempts → terminal `failed`). `last_error` records
     the exception class only — never payload content.
  4. Lease/timeout coherence is ENFORCED: `run_worker` refuses to start
     unless `lease_seconds > task_timeout_seconds` (a stuck-but-revoked
     handler must not outlive its lease and risk a second concurrent
     claim). Compose runs the worker with `--lease-seconds 90` against the
     default 30 s timeout.
  5. `timeout_seconds <= 0` keeps the legacy inline execution (no guard
     thread) for compatibility and tests.
- **Consequences:**
  - (+) Database side effects of abandoned handlers are provably bounded
    (test: the zombie thread's post-timeout session call raises
    `TaskExecutionRevokedError`; task state stays consistent).
  - (+) Retry semantics, emergency-stop behavior, graceful shutdown and
    the at-least-once claim guarantees are unchanged; 10 dedicated tests
    cover completes/exceeds/exhausted/zombie/privacy/coherence/E2E/
    concurrency/stop paths.
  - (−) Honest limit: a handler stuck in non-session C calls (sleep,
    network, native extensions) cannot be interrupted in-process — it is
    bounded only by lease expiry plus process restart. The ADR therefore
    records the Phase 2 requirement: future AI execution must run in
    independently cancellable execution environments with bounded external
    API calls.
  - (−) One extra thread per executing task and a lock per session call —
    negligible for the deterministic Phase 1 handler set.
