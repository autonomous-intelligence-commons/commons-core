# Python 3.14.10 Compatibility Verification — Phase 1

**Project:** Autonomous Intelligence Commons (AIC) / `commons-core`
**Record prepared:** 2026-11-03
**Evidence:** Maintainer/operator-provided Windows PowerShell and pytest transcripts from the November 2026 compatibility review
**Assessment:** **Phase 1 compatibility checks passed; production upgrade NOT approved**
**Public disclosure:** Local usernames and absolute workstation paths have been redacted; test counts and technical verification boundaries retained.
**Evidence availability:** This report summarizes results supplied by the operator. Original execution logs, the complete tested worktree snapshot, and the full installed-package inventory are not attached; readers cannot independently verify all reported outcomes from this document alone.
**Scope:** Read-only review and documentation of previously executed validation. No code changes, database operations, Git commits, pushes, or deployments were performed in preparing this record.

## 1. Executive summary

The isolated Python **3.14.10** environment completed **498 passed, 4 skipped, 0 failed** across the selected, separately executed Windows ACL, unit, security, API, Scheduler, and Scheduler Recovery batches. Each reported pytest batch completed with exit code **0**. The warning repeated across batches was a `StarletteDeprecationWarning` concerning `httpx` and `starlette.testclient`; it was **not** a test failure.

This is evidence of compatibility for **the tests actually run**. It does **not** establish complete suite coverage, PostgreSQL 16 behavior under Python 3.14, production deployment readiness, or Windows Backup publication security.

## 2. Source and environment baseline

| Attribute | Recorded value |
|---|---|
| Repository worktree | ``<local-regression-worktree>`` |
| Branch | `fix/aic-windows-regression-20261102` |
| HEAD | `d63e240f56b6a8f657b663d6346c815b2690e29f` |
| Python executable | ``<isolated-python314-venv>/Scripts/python.exe`` |
| Python | `3.14.10` |
| pip | `26.2.2` |
| Primary local data store used in these tests | Per-test temporary SQLite files via project test fixtures (where applicable) |
| Existing Python 3.12 environment | Preserved; not switched or modified during this validation sequence |

**Reproducibility caveat:** The working tree was **not clean**. The current HEAD alone does not identify all code under test. Two tracked checkpoint test files were modified and numerous Windows ACL implementation/tests and documentation files were untracked. The validation evidence is associated with the **HEAD plus this worktree state**. No assertion is made that all changes are captured by Git, or that the worktree was fully hashed or archived.

At the final review, `git status --short -- pyproject.toml src/commons_core/backup/storage.py` produced **no output** (neither tracked file showed a local modification). This does **not** mean the entire worktree was clean.

## 3. Test batches and outcomes

| Batch | Passed | Skipped | Failed | Source / focus |
|---|---:|---:|---:|---|
| Windows ACL combined regression | 112 | 2 | 0 | Windows ACL policy, parsing, native snapshot and security-boundary tests |
| Core round 1 | 59 | 0 | 0 | Authorization, crypto, database URL normalization |
| Core round 2 | 121 | 0 | 0 | Agent services, memory versioning, rate limits, scheduler unit, tokens, communication/memory isolation, governance boundary |
| API and privacy | 45 | 0 | 0 | `test_app_foundation.py`, `test_communication.py`, `test_no_plaintext_at_rest.py` |
| Activities, governance and memory API | 63 | 2 | 0 | `test_activities.py`, `test_governance.py`, `test_memory.py` |
| Scheduler integration | 29 | 0 | 0 | `test_scheduler.py` |
| Emergency Stop, durability, production key management | 60 | 0 | 0 | `test_emergency_stop.py`, `test_emergency_stop_durability.py`, `test_production_key_management.py` |
| Scheduler Recovery | 9 | 0 | 0 | `test_scheduler_recovery.py` |
| **Total (batches reported as distinct)** | **498** | **4** | **0** | **502 test outcomes** |

The ACL combined batch includes tests previously run as smaller sub-batches. Those preliminary runs are **not added again** to the total. Other batch counts are treated as nonoverlapping based on their supplied file selections; a machine-produced unique-node-ID manifest was not captured.

**Selected command outcomes from supplied transcripts:**

- API/privacy: `45 passed, 1 warning in 32.10s`, child and parent exit code `0`.
- Activities/governance/memory: `63 passed, 2 skipped, 1 warning in 48.50s`, child and parent exit code `0`.
- Scheduler integration: `29 passed, 1 warning in 17.71s`, child and parent exit code `0`.
- Security: `60 passed, 1 warning in 26.86s`, child and parent exit code `0`.
- Scheduler recovery: `9 passed, 1 warning in 2.54s`, child and parent exit code `0`.
- Earlier batches: ACL `112 passed, 2 skipped`, core round 1 `59 passed`, core round 2 `121 passed`; all reported successful exit codes.

## 4. Dependencies and package health

The Python 3.14 environment had the following installed package versions as reported by the maintainer from `pip show` output:

| Package | Version |
|---|---|
| alembic | 1.20.0 |
| cryptography | 50.0.2 |
| fastapi | 0.143.3 |
| pydantic | 2.14.1 |
| pydantic-settings | 2.16.0 |
| SQLAlchemy | 2.1.5 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1 |
| pytest | 9.1.1 |
| ruff | 0.17.1 |
| psycopg | 3.2.13 |
| psycopg-binary | 3.2.13 |

`python -m pip check`: **`No broken requirements found.`**

An isolated `pip-audit 2.10.1` check of a snapshot of **37 installed third-party packages** returned **`No known vulnerabilities found`**, exit code `0`. This means *no matching known advisories were reported by that scan at that time*; it is not a guarantee of zero vulnerabilities. Installed transitive dependencies were **not fully locked by hashes**, and the Python 3.14 environment should not be called a reproducible release build.

## 5. Known warnings and caveats

Across the pytest batches the observed warning was:

> `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.`

The warning did not prevent test success. Do not automatically install `httpx2` or change dependency constraints in this phase; evaluate any upstream transition separately.

Some tests were skipped (four total). The transcripts provided do not establish the precise skip reasons for each case; therefore, **skipped** means *not proven to pass*.

## 6. Isolation and safety boundaries

- The Windows PowerShell child-process test runs removed database/PG environment variables **from the child process**, checked that `AIC_TEST_DATABASE_URL` was absent, and used the explicit Python 3.14 venv executable.
- `tests/conftest.py` selects temporary SQLite test databases when `AIC_TEST_DATABASE_URL` is unset. This matters critically: **if a PostgreSQL test URL were supplied, fixtures could reset schema using `Base.metadata.drop_all()` and `create_all()`**. No PostgreSQL integration run was authorized or performed in Phase 1.
- Scheduler integration and Scheduler Recovery tests execute bounded test Workers and temporary SQLite operations. Scheduler Recovery installs SIGTERM/SIGINT handlers in the **pytest child process**, not the parent shell.
- Phase 1 did not intentionally invoke production backup, recovery drill, production database migrations, Docker deployment, or GitHub Actions modification.
- Tests that use real subprocesses, file deletion, restore destinations, or PostgreSQL must **not** be swept into an unrestricted `pytest tests/` run without an individual safety assessment.

## 7. Remaining verification / non-claims

| Area | Status at phase close | Requirement before approval |
|---|---|---|
| PostgreSQL 16 integration/concurrency under Python 3.14 | **Not tested** | Isolated disposable DB and least-privilege role; separate explicit authorization and verified preflight |
| Full operational and backup/restore pipeline | **Not tested as a suite** | Examine subprocess, filesystem, secrets and destination side effects; preserve backups and live systems |
| Worker Lifetime | **Not yet run** | Separate real-subprocess and signal-lifecycle risk evaluation |
| Recovery Keys | **Not yet run** | Assess CLI subprocesses, filesystem deletion and temporary recovery artifacts |
| Docker, CI, deployment | **Not verified under 3.14** | Independent build/runtime and pipeline validation, review Python version matrix |
| Full dependency lock/reproducibility | **Incomplete** | Capture platform-specific lock or hash-pinned transitive dependency set |
| Windows Backup Publication Gate A4-4 | **Not implemented or security-accepted** | Separate pre-creation NTFS ACL enforcement and fail-closed testing; do not infer security from `chmod` on Windows |
| Python 3.14 production migration | **Not approved** | Separate clean worktree, explicit PR review, complete required verification |

**Critical security boundary:** Passing Windows ACL parser/native snapshot tests does **not** establish that `LocalFilesystemStorage.publish()` enforces secure Windows NTFS ACLs *before* artifact/directory creation. The A4-4 publication gate remains outstanding. Do not enable or represent production Windows backup publication as secure based on this record.

## 8. Repository working tree / integration boundaries

At the final check:

- Branch and HEAD matched the values in Section 2.
- Two tracked checkpoint files showed modifications: `tests/dev_checkpoint/ckpt_synth.py` and `tests/dev_checkpoint/test_checkpoint_recovery_e2e.py`.
- Multiple `README-*.txt`, `src/commons_core/backup/windows_*.py`, and `tests/operational/test_windows_*.py` paths were **untracked**.
- `pyproject.toml` and `src/commons_core/backup/storage.py` were **not shown as modified**.

Do not stage, reset, clean, overwrite, or move these files as part of this report. The original F.2 worktree, existing Python 3.12 environment, and PostgreSQL installations remain outside this phase.

**PR policy:** This document is a standalone evidence record, not approval to create or merge a Python 3.14 upgrade PR. When a future PR is authorized, use a separate clean working tree/branch and preserve screenshot-before-submit review. No PR or deployment is claimed here.

## 9. Phase 1 disposition

**PASS — selected Python 3.14 compatibility verification (498 passed / 4 skipped / 0 failed).**

**HOLD — production Python 3.14 upgrade, PostgreSQL 16 acceptance under Python 3.14, and Windows Backup A4-4 publication safety.**

This record is a bounded milestone, not a blanket security certification or production release sign-off.
