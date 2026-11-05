# Trusted DR Evidence & Authorization v1.0 — Design Candidate

**Status:** Design / first-pass self-review only. **No deployed authority or privilege change.**

## Three independent controls

**Evidence trust:** A future controlled issuer must bind the run identity, synthetic database creation, server identity, backup digest, ordered event sequence and verification outcomes. Hash chaining without protected storage or an independently held checkpoint is not sufficient protection against rewriting the whole history.

**Independent approval:** The approver must be separate from the requester and executor. Approval must be specific to one run, server, target, backup and operation, time-limited, revocable, and single-use with durable consumption state. A proposal value such as `approved=true` is not approval. A crash following token consumption requires outcome reconciliation, not an automatic retry.

**Privilege enforcement:** A regular operator must not own a target database or retain a privilege path that can bypass the only approved executor. The protected executor would need narrowly limited access to run-owned resources only; administrators with root/superuser access remain able to bypass application-level policy, so absolute resistance to administrators is not claimed.

Required privilege review includes effective sudo permissions, executable/configuration writability, database ownership, role memberships and SET ROLE paths. NOCREATEDB or non-superuser status alone does not establish inability to drop a database. The future design must prevent ordinary operators from assuming the executor's OS/DB identity or modifying its executable, inputs or evidence. These controls remain unimplemented and require separate review before any permission change or destructive exercise.

## Known blockers

- Broad administrative privileges in the current test operator environment have **not** been constrained.
- Protected event issuance, storage, independent anchor, one-time approval and replay prevention are **not implemented**.
- No protected executor, phase-aware verifier or demonstrated race-safe operation boundary exists.
- Host-level localhost forwarding/isolation needs separate evaluation.
- No actual backup-restoration exercise or measured recovery objectives exist.

## Safeguards and review order

Read-only privilege inventory → separately reviewed minimal-permission and evidence-store design → disposable mock integration → independent security assessment → explicit authorization for any permission changes → separately authorized synthetic destructive exercise. This documentation **does not** approve deployment, modification of OS/DB roles or real database operations.
