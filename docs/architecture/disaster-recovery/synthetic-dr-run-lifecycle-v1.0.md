# Synthetic DR Run Lifecycle v1.0

**Status:** Adopted design candidate and simulated state machine; destructive execution **NOT AUTHORIZED**.

## Six-stage lifecycle

1. **Identify:** trusted controller generates a unique run identity; Offline A must have passed before controlled startup and baseline Online B must confirm the live cluster.
2. **Create synthetic resources:** future constrained controller creates a fresh, absent, run-owned database and records independently generated provenance and synthetic-data checksums.
3. **Backup and verify:** bind a backup artifact digest, generation metadata and restore-usability evidence to the originating run and target.
4. **Approve and revalidate:** independent approver issues exact-operation, short-lived, single-use approval; protected executor rechecks live server, target, inventory and backup at operation time.
5. **Controlled loss and recovery:** only after **separate explicit authorization**, perform synthetic-only deletion and independent restore authorization, then compare restored schema, records and content digests.
6. **Preserve evidence and stop:** reconcile operation outcomes, archive a protected audit trail, safely stop the named test cluster and confirm its stopped state.

## State and failure semantics

Illustrative states: `IDENTIFIED` → `BASELINE_VERIFIED` → `SYNTHETIC_CREATED` → `BACKUP_VERIFIED` → `APPROVED` → `DELETED` → `RESTORED` → `VERIFIED` → `CLEANED` → `CLOSED`. Any invalid transition enters `BLOCKED`; no automatic retry, fallback destructive cleanup or reuse of consumed approval. The actual safe-stop path must be independent of an operation approval.

A restore must **not** overwrite an existing database. After creating the synthetic database, a separately designed *phase-aware* server verifier must authorize only independently attested inventory changes; the strict initial Online B baseline is not suitable.

Restore safety must validate the archive's trusted origin and exact bytes, its database-selection semantics, and the effective destination. Archive-supplied names, SQL, ownership or privileges must not widen the approved scope. Restoration may execute archive-supplied code; a matching digest alone does not establish safety. Verification must use separately authorized disposable targets, reject partial or ambiguous outcomes, and must not trigger automatic destructive cleanup. These controls are not implemented or validated by this simulation.

## Observed status

**21/21** isolated lifecycle tests passed in the user's WSL, with exit 0. This demonstrates simulated transition checks, not real trusted evidence issuance, permission isolation, database deletion/restoration or measured recovery performance.

## Recovery measurement

The proposed RTO window is from declared synthetic-loss time to verified restored-data usability; RPO must be based on recoverable committed synthetic data rather than a backup file's timestamp alone. **RTO target: 1 hour; RPO target: 24 hours. Neither has been measured or demonstrated.**
