# Security Controls Register — AIC Phase 1

Controls register per the Phase 1 specification (§25), cross-linked to the
threat model (`docs/THREAT_MODEL.md`, T-01…T-15).

**Status vocabulary (honest):**

- **Implemented and tested** — code exists; automated tests in this
  repository exercise it (suite result at the 2026-10-20 audit baseline:
  617 passed, 42 skipped on SQLite; Phase 1 record when this register was
  written: 342 passed, 2 skipped).
- **Implemented, partially verified** — code exists; some verification done,
  but a stated gap remains (e.g. environment not exercised).
- **Designed, not implemented** — design exists in code/docs only.
- **Deferred** — future phase.

**No control is marked complete based on documentation alone.** Framework
alignments at the end are reference mappings, not certifications.

## Controls

| ID | Control | Security objective | Threats | Implementation location | Enforcement mechanism | Verification (real test nodes) | Status | Residual limitations |
|---|---|---|---|---|---|---|---|---|
| SC-01 | Bearer-token authentication with PBKDF2-HMAC-SHA256 hashing (200,000 iterations, per-credential 16-byte salt) | Authenticity | T-01, T-11 | `src/commons_core/security/authentication/tokens.py` | Raw token never stored; lookup via non-secret 8-char prefix; hash compare only | `tests/unit/test_tokens.py::TestIssueAndVerify::test_issue_and_verify`, `test_hash_format`, `TestRawTokenNeverStored::test_raw_token_absent_from_database` | Implemented and tested | A stolen valid token works until revoked; client-side storage outside scope |
| SC-02 | Constant-time credential comparison | Prevent timing oracles | T-01 | `tokens.py` (`hmac.compare_digest`) | `hmac.compare_digest` over candidate hashes | `test_wrong_token_rejected`, `test_truncated_token_rejected` | Implemented and tested | Prefix lookup itself is not constant-time (prefix is non-secret by design) |
| SC-03 | One-time token display at provisioning | Credential secrecy | T-11 | `scripts/manage.py::_print_token_once`; `tokens.py::IssuedToken` | Token printed to stdout once; `repr` is privacy-safe | `test_raw_token_absent_from_database`; CLI exercised in Task 4/6 smoke runs | Implemented and tested | Operator must capture stdout securely (procedure, not code) |
| SC-04 | Credential rotation, revocation, suspension, expiry | Containment / least privilege | T-01, T-11 | `tokens.py::revoke_credential`, `agents/service.py` lifecycle; `scripts/manage.py rotate/suspend/reactivate/revoke` | Status checks inside `verify_token` (credential ACTIVE, expiry, agent status) | `test_revoked_credential_fails`, `test_expired_credential_fails`, `test_suspended_agent_fails`, `test_revoked_agent_fails`, `test_rotation_links_credentials` | Implemented and tested | Detection of compromise is indicator-based |
| SC-05 | Deny-by-default authorization engine | Authorization on every operation | T-02, T-03 | `security/authorization/engine.py`, `engine.Permission` | `authorize()` evaluates server policy; unknown permission/missing state → deny + `authz_denial` event; no fallback allow | `tests/unit/test_authorization.py::TestDenyByDefault` (5 tests), `test_denial_records_security_event` | Implemented and tested | — |
| SC-06 | Server-side policy from infrastructure-owned state only (structural) | Content can never become authority | T-03, T-09, T-10 | `security/authorization/policies/server_policy.py` (never imports/queries content tables; `PERMITTED_MODEL_REFERENCES`) | Module-level structural constraint + canary test on source/imports | `tests/unit/test_authorization.py::TestEngineNeverConsultsContentTables::test_engine_source_references_only_permitted_models`, `test_engine_imports_only_permitted_models`, `test_content_rows_cannot_grant_permissions` | Implemented and tested | Relies on canary test + review to survive future edits |
| SC-07 | Agent/admin identity separation; `admin:*` wildcard only via `is_admin` | Privilege separation | T-03 | `agents/service.py::provision_agent`, `server_policy.py` | `is_admin` set only through provisioning; admin wildcard grants ONLY `admin:*` family, not channel/memory access | `TestAdminWildcard::test_admin_gets_admin_wildcard`, `test_non_admin_denied_admin`, `test_admin_is_not_granted_private_channel_access` | Implemented and tested | Admin token compromise still enables availability abuse (emergency stop) |
| SC-08 | IDOR/404-oracle policy (404 unknown vs 403 existing-but-denied; activities 404 for non-members) | Prevent enumeration-driven disclosure | T-02 | `communication/service.py`, `memory/service.py`, `activities/service.py` route resolution order | Resolve-or-404 (`UNKNOWN_*`) then authorize (403 + event) | `tests/security/test_communication_isolation.py::TestEnumerationAndIdor::test_unknown_channel_404`, `test_guessed_message_id_404`; activity tests in `tests/integration/test_activities.py` | Implemented and tested | Existence oracle for known ids/handles — documented, accepted trade-off |
| SC-09 | Cross-agent / cross-channel / cross-workspace isolation | Confidentiality | T-02, T-04 | `server_policy.py` membership/permission checks; domain services' defensive re-checks | Membership (`channel_members`), `memory_permissions`, activity membership gates | `tests/security/test_communication_isolation.py` (10 tests), `tests/security/test_memory_isolation.py` (15 tests) | Implemented and tested | — |
| SC-10 | Memory grants owner-only; admins excluded (privacy boundary) | Confidentiality vs operators | T-02, T-04 | `memory/service.py`, `server_policy.py` | Only namespace owner can PUT permissions; admin is_admin grants no namespace powers | `test_grant_by_non_owner_403`, `test_grant_by_admin_403_privacy_boundary`, `test_admin_cannot_read_entries` | Implemented and tested | Deliberate: no admin override path exists (even for incidents) |
| SC-11 | AES-256-GCM application-layer envelope encryption | Confidentiality at rest | T-04, T-05 | `security/cryptography/service.py` (`EnvelopeCipher`), `envelope.py` | seal before storage; open validates version/alg/kid/hex/tag/AAD | `tests/unit/test_crypto.py` (26 tests: round-trip, tamper, wrong-key, formats) | Implemented and tested | Application-layer only, NOT E2EE; host admin with memory access defeats it (documented) |
| SC-12 | Unique 96-bit nonces per seal operation | GCM correctness | T-05 | `service.py::seal` (`os.urandom(12)`) | Fresh nonce per call | `TestKeys::test_nonce_uniqueness` | Implemented and tested | Random nonces: birthday-bound safe at Phase 1 volumes; enormous-volume deployments should track nonce budgets |
| SC-13 | AAD context binding per domain | Integrity / anti-recontextualization | T-05 | AADs: `message:{channel}:{sender}`, `memory:{ns}:{key}`, `activity:{id}:summary`, `governance:{kind}:{handle}:{version}` (in the four domain services) | open() requires identical AAD | `TestTamperDetection::test_aad_mismatch` | Implemented and tested | — |
| SC-14 | Versioned envelopes with key ids (multi-kid key store; active kid seals, all kids open) | Key rotation without data loss | T-05, T-15 | `security/cryptography/key_store.py`, envelope `v/kid` fields | Parse-time validation (lengths, hex, duplicates, all-zero rejection); active-kid must exist | `TestKeys::test_multi_key_rotation_old_envelopes_still_open`, `test_unknown_kid`, `TestKeyStoreValidation` (7 tests) | Implemented and tested | Rotation does NOT re-encrypt history (documented trade-off) |
| SC-15 | No key material or tokens in logs/DB | Secret hygiene | T-04, T-11 | `key_store.py` privacy rule; `infrastructure/logging.py` policy; `api/app.py::_unhandled` | Error messages identify kids, never keys; 500s log exception class only | `TestKeyStoreValidation::test_error_messages_never_contain_key_material`, `tests/privacy/test_no_plaintext_at_rest.py::test_sentinel_absent_from_logs` | Implemented and tested | Process memory still holds keys (inherent) |
| SC-16 | Ephemeral development key path with loud warning | Safe dev defaults | T-15 | `key_store.py` (empty `AIC_MASTER_KEYS` → `dev-ephemeral` + warning) | Warning log on construction | `test_ephemeral_dev_key_path`, `test_ephemeral_key_works_end_to_end` | Implemented and tested | Dev data lost on restart (by design, loudly stated) |
| SC-17 | Request rate limiting (per-agent 120/min default; anonymous 30/min; exempt health/docs) | Abuse prevention | T-07, T-08 | `security/rate_limit.py`, `api/middleware.py::RateLimitMiddleware` | In-memory fixed window; 429 + `Retry-After` + `rate_limited` event (never an IP in events) | `tests/unit/test_rate_limit.py` (8 tests) | Implemented and tested | Per-process counters (single-process assumption); no network-layer limits |
| SC-18 | Durable per-agent message quota (60/min default) | Abuse prevention | T-07 | `infrastructure/quotas.py::check_message_quota` | DB fixed-window counter (`quota_counters.messages`), injectable clock; 429 `QUOTA_EXCEEDED` + event (limit only) | `tests/operational/test_quotas.py` (6 tests) | Implemented and tested | Read-then-increment without row lock — concurrent overshoot possible (documented; Postgres should add FOR UPDATE) |
| SC-19 | Payload size limits (message/memory 64 KiB default; task payload 4 KiB JSON; nesting ≤ 32) | DoS / abuse prevention | T-08, T-14 | `communication/service.py`, `memory/service.py`, `scheduler/service.py` | Byte-size checks against settings → 413 `PAYLOAD_TOO_LARGE`; nesting/forbidden-key checks → 422 | `test_oversized_summary_rejected_413`, `test_entry_size_limit_413`, scheduler unit tests | Implemented and tested | — |
| SC-20 | Task queue quotas (per-agent 100 in-queue; global `AIC_MAX_PENDING_TASKS` 1000) | Availability | T-08, T-14 | `scheduler/service.py::_check_queue_quota` | Non-terminal-status counts; 429 `TASK_QUEUE_FULL` + `quota_exceeded` event | `tests/operational/test_task_quotas.py` (6 tests) | Implemented and tested | Global count includes all agents — a single agent may occupy up to its per-agent cap |
| SC-21 | Retry caps + exponential backoff (attempts ≤ `AIC_MAX_TASK_ATTEMPTS` default 3; backoff `min(2^attempts, 60s)`; attempts counted at claim) | Availability / cost | T-14 | `scheduler/service.py::backoff_delay`, `execute_claimed` | Terminal `failed` state; no infinite retry | `tests/unit/test_scheduler_unit.py`, `test_lease_expiry_after_final_attempt_fails_terminally` | Implemented and tested | — |
| SC-22 | Execution leases + expiry recovery | Duplicate-processing protection | T-06, T-14 | `scheduler/service.py::claim_next/expire_leases` | Conditional `UPDATE ... WHERE status='pending'` + rowcount; lease expiry re-queues WITHOUT new attempt | `tests/recovery/test_scheduler_recovery.py::TestLeaseExpiry` (3 tests) | Implemented and tested | At-least-once semantics: a crash mid-execution may re-run the task (documented; no exactly-once claim) |
| SC-23 | Task idempotency keys (globally unique) | Duplicate protection | T-06 | `scheduler/service.py::create_task` | Replay returns existing task (same owner) or 409 `IDEMPOTENCY_CONFLICT` | `test_replay_of_idempotency_key_after_restart` | Implemented and tested | Opt-in: tasks without a key can be duplicated by client retries |
| SC-24 | Emergency stop (pause semantics) | Containment | T-07, T-08, T-14 | `infrastructure/system_state.py::set_emergency_stop`, `api/routes/admin.py`, `api/deps.py::require_not_paused`, `scheduler/worker.py` | Admin-only POST/DELETE; writes → 503 `SERVICE_PAUSED`; reads served; worker idles; `emergency_stop` event (action only) | `tests/security/test_emergency_stop.py` (16 tests incl. `test_worker_does_not_claim_while_paused`, `test_message_send_503`, `test_message_retrieval_200`) | Implemented and tested | In-flight task completes (between-iteration check — documented); governance creation stays open (inert artifacts) |
| SC-25 | Security-event recording with privacy-safe details | Detection | T-01–T-14 | `security/events.py::record_event`; categories in `database/enums.py` | Categories `auth_failure`, `authz_denial`, `rate_limited`, `quota_exceeded`, `policy_violation`, `emergency_stop`, `containment`; details = counts/limits only; recording never breaks request path | `test_denial_records_security_event`, `test_stop_recorded_as_security_event`, `test_policy_violation_event_recorded_without_content` | Implemented and tested | Metadata only — not every threat is detectable without content inspection (accepted, spec §20) |
| SC-26 | Privacy-preserving telemetry (admin-only, counters-only) | Observability without surveillance | T-04 | `infrastructure/telemetry.py::collect_telemetry` (+ `CONTENT_POLICY`), `api/routes/admin.py` | No handles/content; `content_policy` line in every payload; admin capability required | `test_telemetry_contains_no_content_or_handles`, `test_non_admin_cannot_read_telemetry`, `test_unauthenticated_telemetry_401` | Implemented and tested | Retention of security events (14-day setting) is a pruning policy — actual pruning job NOT implemented (deferred) |
| SC-27 | Logging policy (categories + principals, never content) | Privacy | T-04 | `infrastructure/logging.py` + code-wide convention; `api/app.py` error handlers | Format carries level/logger/message; 500 handler logs class + path + request id; validation errors stripped of input echo | `test_sentinel_absent_from_logs`, `TestNoPlaintextAtRest::test_authorized_reader_still_sees_plaintext` | Implemented and tested | Enforcement is by convention + tests, not a log filter — a future careless log line needs review/tests to catch |
| SC-28 | Governance structural boundary + privilege-language lint (visibility, NOT boundary) | Autonomy/security separation | T-03, T-09 | `governance/service.py` (lint, `PRIVILEGE_TERMS`, versioning), `server_policy.py` (boundary) | Lint → 422 `GOVERNANCE_PRIVILEGE_CLAIM` + `policy_violation` event; engine never reads governance records | `tests/security/test_governance_boundary.py` (11 tests incl. lint-bypass structural proof) | Implemented and tested (lint explicitly NOT a boundary) | Lint trivially bypassed (documented); boundary is the canary-tested engine isolation |
| SC-29 | Deterministic simulated task handlers only (no code execution, no model APIs, no network tools) | Execution isolation | T-09, T-13 | `scheduler/registry.py` (whitelist: `noop`, `heartbeat`, `activity_reminder`), task payload policy | Unknown kind → 422; forbidden payload keys; no shell/eval/fetch primitives anywhere | `tests/unit/test_scheduler_unit.py` (36 tests); OpenAPI surface assertions | Implemented and tested | Future real-model execution requires a separate sandbox + threat review (spec §17) |
| SC-30 | Supply-chain hygiene (pinned deps; CI/audit prepared) | Integrity of the build | T-12 | `pyproject.toml` (exact pins, 7 runtime deps); `.github/workflows/ci.yml` (prepared), `.github/dependabot.yml` (prepared), `Dockerfile`/`compose.yaml` (prepared) | Pins in metadata; CI lint/test/migrate-check + non-blocking pip-audit job (least-privilege `permissions: contents: read`) | *(Historical, Phase 1: not executable in that sandbox; CI had not yet run.)* CI has run successfully on GitHub since publication — all eight jobs (including Docker build and both PostgreSQL suites) were green at the 2026-10-20 audit baseline | Implemented (pinning); CI and Docker executed on GitHub Actions (green at the 2026-10-20 audit baseline); Dependabot prepared (activation is GitHub-side, not tracked in this register) | No lock file; no image scanning; no action-SHA guarantee beyond documented intent |
| SC-31 | Recovery: persistence across restart, lease expiry, graceful shutdown | Resilience | T-15 (partial), T-06 | `scheduler/worker.py` (SIGTERM handling, stop event), tests | New engine on same file processes surviving tasks; in-flight task completes before exit | `tests/recovery/test_scheduler_recovery.py` (9 tests: `test_tasks_survive_engine_teardown_and_process_afterwards`, `test_sequential_workers_execute_each_task_exactly_once`, `TestGracefulShutdown`) | Implemented and tested | Full DB backup/restore (pg_dump) NOT tested — documented procedure only |
| SC-32 | Encryption-key rotation procedure (documented) | Key compromise response | T-15 | `docs/DEPLOYMENT.md` + `KeyStore` multi-kid design | Add new kid → set active kid → restart; old envelopes still open | `test_multi_key_rotation_old_envelopes_still_open` | Implemented (design) / partially verified (single-process test only) | No re-encryption tool; no automated rotation; losing a kid = losing its historical data |

## Framework reference mapping (NOT a certification)

These mappings are *reference alignments* to public frameworks. They do not
assert compliance, certification, audit, or attestation of any kind.

| Control IDs | NIST CSF 2.0 function(s) | NIST SP 800-207 tenet(s) | OWASP API Top 10 (2023) | OWASP LLM/agentic guidance |
|---|---|---|---|---|
| SC-01…SC-04 | PR.AA (authn/authz), DE.CM | "All subjects authenticated continuously"; least privilege | API1 (BOLA), API2 (Broken Authentication) | Credential hygiene for agent identity |
| SC-05…SC-10 | PR.AA, PR.IR | Per-session/least-privilege authorization decisions | API1 (BOLA), API5 (Broken Function-Level AuthZ) | Least-privilege agent capabilities |
| SC-11…SC-16, SC-32 | PR.DS (data security) | Cryptography in transit/at rest | API3 (Broken Object Property Level AuthZ - property exposure) | Data protection for LLM context |
| SC-17…SC-21 | PR.PS, DE.CM | Deny by default + rate limiting ("all resource requests authorized") | API4 (Unrestricted Resource Consumption) | Abuse limits for autonomous actors |
| SC-22, SC-23, SC-31 | RC.RP (recovery) | — | API8 (Security Misconfiguration, availability) | Reliable agent tasking |
| SC-24, SC-25 | DE.CM, RS.MI (containment) | — | — | Kill-switch / emergency controls for agentic systems |
| SC-26, SC-27 | PR.DS (confidentiality), DE.AE | — | API8 — logging hygiene | Privacy of agent communications |
| SC-28 | PR.IR (platform security) | "Security policy is dynamic"; policy independent of workload | — | Prompt-injection & untrusted content isolation |
| SC-29 | PR.IR, PR.PS | Zero-trust on tools (no implicit trust in generated content) | API6 (SSRF-class: unrestricted outbound) | Tool use with server-side authorization |
| SC-30 | GV.SC (supply chain), ID.RA | — | — | Supply-chain guidance for LLM applications |

## Controls not claimed

For honesty, the following are explicitly NOT provided in Phase 1: end-to-end
encryption (agent-held keys); transport TLS inside the app (deployment
concern); mTLS; WAF/DDoS protection; SBOM or dependency signing; container
image scanning; automated backups with restore tests; LLM-based (or any
content-based) threat detection; compliance certification of any kind.

## Phase 1 hardening additions (Task IDs 10/11)

Status vocabulary used in this register: **Implemented and tested** (code +
automated tests executed) / **Partially verified** (implemented; some
verification pending an environment that does not exist here) /
**Designed, not implemented** / **Deferred**.

| ID | Control | Protects | Threat | Implementation | Verification | Status | Residual risk |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SC-33 | Production fail-closed key management (no ephemeral fallback; active-kid + placeholder validation; SQLite refused) | Confidentiality of stored envelopes; recoverability after restart | T-15 (key loss/compromise), misconfiguration | `config/validation.py`, `key_store.py` (env-aware), `app.py` startup validation; worker/CLI least-privilege (no keys needed) | `tests/security/test_production_key_management.py` (24 tests) + live 8-case subprocess matrix (TESTING.md §6) | **Implemented and tested** | Keys required to decrypt backups; host/ Railway admins still exceed app-layer guarantees (not E2EE) |
| SC-34 | Worker execution-timeout enforcement + abandoned-handler session guard | Availability; task-state integrity | T-06-style resource exhaustion via long-running handlers | `scheduler/service.py` (`execute_claimed(timeout_seconds)`, `_ExecutionGuard` revocable session proxy), `worker.py` (lease > timeout coherence) | `tests/operational/test_task_timeout.py` (10 tests incl. zombie-guard and concurrency) | **Implemented and tested** | No hard kill of non-session C calls — bounded by lease expiry + restart (documented) |
| SC-35 | PostgreSQL-backed integration verification (migrations + full suite + scheduler concurrency with FOR UPDATE SKIP LOCKED) | Integrity of the production datastore path | SQLite-vs-PostgreSQL behavioral divergence | `tests/conftest.py` fail-loud backend selection; PG dialect locking in `claim_next`; backend-aware recovery tests | 382 passed / 2 skipped on PostgreSQL 16.2; alembic round-trip + schema inspection; live production E2E | **Implemented and tested** | CI replication of the PG job executes on GitHub Actions (same commands) |
| SC-36 | Lease/timeout coherence enforcement | Task-state consistency under concurrency | Double-claim while a stuck handler lingers | `run_worker` refuses `lease_seconds <= task_timeout_seconds`; compose worker `--lease-seconds 90` vs default timeout 30 | `test_worker_refuses_lease_not_exceeding_timeout` | **Implemented and tested** | None known within stated semantics |
| SC-37 | Docker image & compose verification in CI | Build integrity; runtime posture (non-root, secret injection, startup order) | T-12 supply chain / build drift | `.github/workflows/ci.yml` `docker` job (build, uid=1001 check, fail-closed startup check, compose stack + authenticated smoke + worker task + teardown) | *(Historical: not executable in the Phase-1 development sandbox — no Docker runtime.)* Executed on GitHub Actions on every push/PR; green at the 2026-10-20 audit baseline | **Implemented and tested** (executed in CI) | Runs on GitHub-hosted runners; production-host-specific behavior remains untested |

Register updates to existing controls:

- **SC-30 (supply chain):** CI now includes a PostgreSQL job (service
  container) and a Docker build+compose job (runner tooling only, no
  third-party Actions in the docker job); pip-audit remains non-blocking
  but findings are VISIBLE (job log + JSON artifact + warning annotation)
  and must be triaged per this document. Status of CI execution: the
  original four jobs have run on GitHub since publication, and the jobs
  added later have executed on subsequent pushes as well — all eight jobs
  (including `docker` and both PostgreSQL suites) were green at the
  2026-10-20 hygiene-audit baseline (`491ac5c`).
- **SC-32 (key rotation):** upgraded from "Implemented (design) /
  partially verified" — rotation behavior is now covered by dedicated
  automated tests (retained-kid decryptability, removal → typed
  `UnknownKeyError`, active-kid sealing, production multi-kid startup) and
  a documented operator procedure (CRYPTOGRAPHY.md §6, DEPLOYMENT.md) →
  **Implemented and tested** (single-process scope; no re-encryption tool
  remains a documented limitation).
