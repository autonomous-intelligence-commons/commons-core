# AIC / commons-core — Python 3.14.10 + PostgreSQL 16.15 Phase 2.5 Regression Acceptance Record

**Record date:** 2026-11-03
**Status:** Scoped regression evidence recorded; **not a production release approval**
**Repository:** `commons-core-windows-regression-20261102` (local working copy)
**Branch:** `fix/aic-windows-regression-20261102`
**Observed HEAD:** `d63e240f56b6a8f657b663d6346c815b2690e29f`
**HEAD subject:** `merge: include F.1 acceptance milestone in R06-B F.2 integration`

## 1. Scope and outcome

This record summarizes the selected Phase 2.5 PostgreSQL regression tests executed locally under Python 3.14.10 and PostgreSQL 16.15. Based on the PowerShell/pytest outputs supplied during this review, all **80 recorded PostgreSQL test executions passed**, distributed as follows:

| Selected test area | Recorded passes | Scope note |
|---|---:|---|
| PostgreSQL operational quota | 12 | Selected quota tests |
| Integration — App Foundation | 18 | All 18 tests from the reviewed integration file |
| Integration — Memory | 17 | All 17 tests from the reviewed integration file |
| Integration — Communication | 19 | All 19 tests from the reviewed integration file, across five batches |
| Integration — Governance | 14 | All 14 tests from the reviewed integration file, across four batches |
| **Total** | **80** | Sum of passing test-run records, not a fresh unified-suite run |

Prior stages are separately reported as **498** Phase 1 passing test records and **61** Phase 2.1–2.4 passing records. The reported **639** combined passing records (498 + 61 + 80) have **not** been deduplicated by pytest node ID and must not be described as 639 distinct tests.

## 2. Selected integration coverage

### Communication — 19/19

Recorded green results cover direct-channel creation and membership limits; invalid or duplicate handles; membership-restricted channel listing; absence of message content from channel metadata; message send/retrieve, acknowledgement idempotency, ordering, cursor pagination and limits; JSON validation; payload size and empty-payload rejection; absent resources; and group-member add/participation/authorization behavior.

### Governance — 14/14

Recorded green results cover creation metadata and version one; retrieval by an authorized active agent; amendment and `supersedes` versioning; previous-version superseded status; historical content retrieval; same-handle and unknown-record validation; duplicate/invalid handles; oversized input; unauthenticated denial; list/kind filtering; and the encrypted-at-rest sentinel test.

The sentinel test checks envelope shape and that one synthetic plaintext sentinel string does not appear in the values scanned across SQLAlchemy-managed tables. **It is not a comprehensive database encryption, backup, logging, key-management or penetration-test audit.** The adversarial governance privilege-claim boundary has a separate security test file and is not established by the 14 governance integration tests alone.

## 3. Test environment and safety boundaries

- Python test interpreter: isolated **Python 3.14.10** virtual environment, separate from the original Python 3.12 environment.
- PostgreSQL: **16.15**, local server at `127.0.0.1:5432`.
- Authorized test database: `<dedicated_test_database>`.
- Authorized test role: `<dedicated_test_role>`, a dedicated low-privilege role.
- Repeated preflight: asserted target database name, role, PostgreSQL 16 version, read-only preflight transaction, the exact expected inventory of 19 `public` tables, and no unexpected non-system schemas.
- The pytest child environment removed inherited `AIC_*`, `PG*`, and `DATABASE_URL` variables before setting `AIC_TEST_DATABASE_URL` to the dedicated test database.
- Selected pytest node IDs were checked using exact `--collect-only` comparison before each execution. Execution used `-p no:cacheprovider`, bounded subprocess timeouts, and checked child/parent return codes.
- **Important:** pytest itself was not read-only; shared database fixtures drop and recreate test tables **within the dedicated test database**. Run no concurrent jobs against that database.
- No instruction was given to modify the original Python 3.12 environment, alter other PostgreSQL databases, or release/deploy production artifacts.

These safeguards and outcomes are based on supplied terminal records; they do not substitute for an independent forensic audit of the host or database access controls.

## 4. Latest read-only repository verification

A subsequent PowerShell read-only check reported:

```text
CURRENT BRANCH: fix/aic-windows-regression-20261102
CURRENT COMMIT: d63e240f56b6a8f657b663d6346c815b2690e29f
CURRENT SUBJECT: merge: include F.1 acceptance milestone in R06-B F.2 integration
```

**Existing modified tracked files (preserve):**

```text
 M tests/dev_checkpoint/ckpt_synth.py
 M tests/dev_checkpoint/test_checkpoint_recovery_e2e.py
```

**Existing untracked artifacts (preserve; not automatically part of the Git commit):**

```text
?? README-A4-2-Bounds.txt
?? README-A4-2-Native-Snapshot.txt
?? README-A4-3-Descriptor-Snapshot.txt
?? README-Phase-A2.txt
?? README-Phase-A3-1.txt
?? README-Phase-A3-2-Bridge.txt
?? README-Phase-A4-1.txt
?? README.txt
?? src/commons_core/backup/windows_acl.py
?? src/commons_core/backup/windows_acl_bounds.py
?? src/commons_core/backup/windows_acl_bridge.py
?? src/commons_core/backup/windows_acl_descriptor_snapshot.py
?? src/commons_core/backup/windows_acl_native_snapshot.py
?? src/commons_core/backup/windows_acl_parser.py
?? src/commons_core/backup/windows_acl_pipeline.py
?? src/commons_core/backup/windows_acl_policy.py
?? src/commons_core/backup/windows_owner_sid_bounds.py
?? src/commons_core/backup/windows_token_identity.py
?? tests/operational/test_windows_acl.py
?? tests/operational/test_windows_acl_bounds.py
?? tests/operational/test_windows_acl_bridge.py
?? tests/operational/test_windows_acl_descriptor_integration.py
?? tests/operational/test_windows_acl_descriptor_policy_positive.py
?? tests/operational/test_windows_acl_descriptor_snapshot.py
?? tests/operational/test_windows_acl_native_api_failures.py
?? tests/operational/test_windows_acl_native_dacl_failures.py
?? tests/operational/test_windows_acl_native_integration.py
?? tests/operational/test_windows_acl_native_snapshot.py
?? tests/operational/test_windows_acl_parser.py
?? tests/operational/test_windows_acl_pipeline.py
?? tests/operational/test_windows_acl_policy.py
?? tests/operational/test_windows_backup_publication_gate.py
?? tests/operational/test_windows_owner_sid_bounds.py
?? tests/operational/test_windows_token_identity.py
?? tests/operational/test_windows_token_identity_cleanup.py
?? tests/operational/test_windows_token_identity_failures.py
?? tests/operational/test_windows_token_identity_remaining_failures.py
```

The scoped `git status --short` check showed `pyproject.toml` and `src/commons_core/backup/storage.py` as clean. The working tree **is not clean**. The passing tests are attributed to the observed working environment and observed HEAD, **not necessarily to a clean checkout of the HEAD commit**. No claim is made that the untracked Windows ACL implementations or tests are included in, or validated by, the 80-test PostgreSQL result.

## 5. Warnings and limitations

- Repeated non-fatal warning: `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` No dependency changes were made as part of this record.
- There has been **no fresh single-command execution of the entire repository test suite** as part of this record, and no separate test-node deduplication.
- Security tests beyond the selected integration scope, recovery-key tests requiring unavailable `age`/`age-keygen`, and all Windows Backup Publication Gate requirements remain separate work items.
- A passing sentinel scan is evidence for the tested value in the tested tables, not blanket assurance that all plaintext cannot leak into logs, backups, memory dumps, storage snapshots or other channels.
- The record is a documentation artifact, **not** an instruction to merge, publish, reset, stash or stage existing worktree content.

## 6. Publication and integration decision

**Decision: ACCEPT scoped Phase 2.5 PostgreSQL regression evidence for documentation only.**

**A4-4 Windows Backup Publication Gate: NOT APPROVED.** This acceptance record must not be interpreted as authorization for the Windows backup publication path, production deployment, or an unrestricted release.

For GitHub integration, add **only this reviewed milestone file** through the established feature-branch Pull Request process, without staging or altering unrelated modified/untracked Windows ACL and Development Checkpoint work. Review the resulting PR diff and CI checks before any merge; do not assume this Markdown file has already been added to the user's repository.

## 7. Evidence provenance
**Evidence availability:** This document is based on execution records and source excerpts supplied by the operator. The original raw logs, full tested-worktree snapshot, and complete pytest node-ID inventory are not attached to this document. Readers cannot independently verify all reported test outcomes, environment versions, or database access permissions from this document alone.


- Supplied chronological PowerShell outputs for the selected Phase 2.5 pytest runs, including exact node collection and result counts.
- Supplied read-only source of `tests/integration/test_governance.py` used to establish the governance test inventory and sentinel assertion semantics.
- Supplied final read-only `git branch`, `git log -1`, and `git status --short` output, reporting the commit and outstanding worktree files.

No raw passwords, connection strings containing credentials, or private key material are included in this document.
