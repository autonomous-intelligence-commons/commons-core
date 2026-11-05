# Online Preflight B v1.0 — Live Identity Verification

**Status:** Functional baseline acceptance recorded; not a production security gate.

## Goal

After the offline gate has passed and the dedicated test cluster is deliberately started, inspect the **actual** PostgreSQL instance over an explicitly selected local Unix socket and maintenance connection, using read-only queries and system checks. Reject identity or configuration mismatch.

## Baseline checks

- Server version, live system identity, data/config/authentication paths, listener configuration and local socket connection.
- Normal (non-recovery) server state, expected baseline inventory of databases/roles/clients, no unexpected settings errors or restart-pending values, and the intended local listener.
- Fail-closed mismatch handling for wrong version, identity, user, port, inventory, recovery mode, unexpected clients or configuration errors.

## Scoped results

The user observed **20/20** isolated tests passing, then **30/30** live checks passing with exit 0 on the dedicated WSL test cluster, followed by controlled stop and a confirmed `down` state. This supports only the checked baseline and test session.

## Critical limitation

Baseline B intentionally expects the **unprovisioned** cluster inventory. Once a run-owned synthetic database is created, B must **not** be reused unchanged: a separately reviewed, phase-aware live verifier must allow only attested, run-specific inventory changes, while preserving every server-identity check. Neither localhost binding nor the Unix socket proves full separation from the Windows host.

**No destructive actions authorized.**
