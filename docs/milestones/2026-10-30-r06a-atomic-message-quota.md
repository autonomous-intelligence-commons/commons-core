# AIC — R06-A Atomic Message Quota Concurrency Remediation

**Milestone date:** October 30, 2026
**Repository:** autonomous-intelligence-commons/commons-core
**Classification:** Security remediation — R06-A (subset of R06, High / P0)
**Integration:** Pull Request #32

## 1. Purpose and Scope

This milestone records repository-side remediation of R06-A: atomic per-agent message quota admission and consistency of the quota transaction with message persistence and sequence-conflict retries.

This is a historical integration record, not production security certification, closure of R06 as a whole, or authorization to advance to Stage 3.

## 2. Original Security Findings

The October 20 security assessment recorded R06 (shared quota and transaction consistency) as High / P0 and unresolved at that historical baseline. The R06-A isolated PostgreSQL 16.2 baseline investigation subsequently reproduced three specific failures:

- **F1 — Last-slot double admission:** two concurrent transactions both reported successful admission to the final available message-quota slot.
- **F2 — First-window creation race:** concurrent creation of a previously absent quota counter could surface an unhandled primary-key `IntegrityError`.
- **F3 — Sequence-retry rollback:** a message sequence conflict caused `send_message()` to roll back the caller's entire transaction; a subsequent successful retry could persist a message without its quota admission.

The prior security review and remediation-status matrix remain historical assessments; they are not retroactively modified by this milestone.

## 3. Implemented Remediation

- `check_message_quota()` uses a single conditional `INSERT ... ON CONFLICT DO UPDATE ... WHERE messages < limit RETURNING messages` operation using the existing `(agent_id, window_start)` primary key, with PostgreSQL and SQLite implementations and explicit handling of unsupported dialects.
- First-window creation and subsequent increments are arbitrated through the same atomic statement, preventing duplicate admission of the final available slot in the tested concurrency conditions.
- `send_message()` scopes sequence-conflict retries to `session.begin_nested()` SAVEPOINTs. A failed message insert rolls back its own attempt without discarding the earlier quota admission in the outer transaction.
- Successful message persistence and required quota consumption continue to share the caller-owned transaction; failed persistence rolls back the quota change.
- Existing 429 `QUOTA_EXCEEDED`, 409 `SEQUENCE_CONFLICT`, authorization, encryption, privacy, and security-event contracts remain unchanged.

No database migration or new dependency was introduced.

## 4. GitHub Integration Evidence

- Remediation PR: https://github.com/autonomous-intelligence-commons/commons-core/pull/32
- Base branch: `main`
- Review branch: `fix/aic-r06a-atomic-message-quota-20261030`
- Maintainer-applied remediation commit: `4d510d1`
- Merge commit: `a86bfecc553257de5d025d61ec92fb8ca831c8b7`
- Integration method: **Create a merge commit**
- PR verification: eight successful required/relevant GitHub checks; CI run #75 passed
- Post-merge verification: `main` CI run #76 passed

The integrated PR changed exactly three files:

1. `src/commons_core/infrastructure/quotas.py`
2. `src/commons_core/communication/service.py`
3. `tests/integration/test_r06_quota_concurrency.py`

## 5. Verification Evidence

The isolated development delivery report recorded the following executed results:

| Verification | Result |
| --- | --- |
| PostgreSQL 16.2 baseline reproduction | 3 failed (F1–F3), 2 passed (protective cases) |
| Remediated R06-A targeted and quota tests | 17 passed |
| Related communication, privacy and security tests | 135 passed |
| Full SQLite suite | 744 passed, 76 skipped, 0 failed |
| Full PostgreSQL 16.2 suite | 757 passed, 63 skipped, 0 failed |
| Ruff lint and formatting | Passed |
| GitHub PR CI #75 | Passed (eight checks) |
| GitHub post-merge `main` CI #76 | Passed |

The PostgreSQL development tests used a disposable PostgreSQL 16.2 instance; GitHub's PostgreSQL 16 CI job also passed. Skipped tests are not reported as passes. These results establish the tested repository behavior, not production environment certification.

## 6. Remaining Limitations

- **R06-B remains open:** `persist_request_count()` uses read-modify-write best-effort telemetry, while the request-rate limiter is process-local and does not enforce one shared cross-worker request ceiling.
- Atomic message quotas operate per agent and fixed minute window. They are not a global monetary spending cap, provider budget reservation ledger, or distributed cross-provider admission system.
- The existing generic `record_event()` session-commit contract remains dependent on caller transaction discipline; it was not redesigned in R06-A.
- The concurrency results apply to the tested database, transaction paths and fault scenarios. Additional production operations and availability characteristics are not certified.
- R06 as a whole must not be marked resolved until its remaining components have separate evidence and approval.

## 7. Safety Boundaries

- E1 remains **FAKE-ONLY**.
- E2 is not implemented or authorized; real external effects remain prohibited.
- No live model providers, production credentials, Railway deployment or production database changes were used or authorized by this remediation.
- Existing R04 Emergency Stop and R02 effect-gateway safeguards are separate controls; this milestone does not expand their scope.
- The original October 20 security review and historical remediation matrix remain unchanged.

## 8. Next Priorities

1. Investigate R06-B cross-worker request-rate admission and durable request-count consistency, beginning with reproducible concurrent PostgreSQL evidence.
2. Define and separately review shared cost-budget reservation semantics before any paid provider integration.
3. Continue Stage 3 prerequisite security work and a separate Go/No-Go decision; do not infer authorization from this milestone.
4. Preserve a separate PR, required CI results, merge evidence and milestone for any future remediation.

## 9. Milestone Conclusion

On October 30, 2026, R06-A atomic message-quota admission and message transaction-retry remediation was merged into `main` through PR #32. GitHub PR CI #75 and post-merge `main` CI #76 succeeded.

Within the documented, tested repository scope, **R06-A is recorded as repository-side remediated**. The broader R06 finding remains open because R06-B and other shared-budget and cross-worker controls are not established by these results. This milestone is not production certification or authorization for E2, real external effects or Stage 3.
