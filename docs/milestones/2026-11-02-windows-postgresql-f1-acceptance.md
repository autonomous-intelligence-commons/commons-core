# AIC commons-core — Windows PostgreSQL R06-B F.1 Acceptance Milestone

**Record date:** 2026-11-02
**Publication:** Public-oriented, maintainer-reported evidence summary; local paths, database identifiers, and role names intentionally omitted.
**Status:** **PASS — scoped R06-B F.1 acceptance only**
**Target repository location:** `docs/milestones/2026-11-02-windows-postgresql-f1-acceptance.md`
**Evidence basis:** Maintainer-provided Windows PowerShell, PostgreSQL, pytest, and Git outputs from the 2026-11-01–02 review conversation. The raw logs, source snapshot, and permission outputs are not included with this public document, so a reader cannot independently reproduce or verify each claim from this document alone.
**Scope boundary:** This record does not certify R06-B F.2, production readiness, full secret-scan coverage, or completion of skipped tests.

## 1. Purpose and acceptance scope

Document the maintainer-reported Windows-local PostgreSQL 16 test setup and the R06-B F.1 acceptance results, without changing source code, modifying the separate R06-B F.2 worktree, pushing Git commits, opening or merging pull requests, or deploying anything. The existing R06-B test database (name withheld) was designated off limits.

The F.1 acceptance gate expressly **excludes tests marked `r06b_open`** (`pytest -m "not r06b_open"`), because the B1 shared request-admission behavior remains open pending separately authorized F.2 work. These tests must not be described as passing.

## 2. Reported environment

| Component | Maintainer-reported value or outcome |
| --- | --- |
| Operating system | Windows, PowerShell |
| PostgreSQL | PostgreSQL 16 was reported as installed and running; the `psql` **client** reported **16.15**. Server patch version was not independently confirmed in this record. |
| PostgreSQL service | PostgreSQL Windows service, previously observed running |
| Local endpoint | Localhost PostgreSQL (endpoint withheld) |
| Project directory | AIC Windows compatibility worktree (local path withheld) |
| Python invocation | `.\.venv\Scripts\python.exe` (direct path; no activation required) |
| Python database driver | `psycopg` available in project `.venv`; absent from system-default Python 3.10 |
| SQLAlchemy | Successfully connected to the exact PostgreSQL endpoint and role |
| Dedicated test database | Dedicated local test database (name withheld) |
| Dedicated test role | Dedicated non-superuser test role (name withheld) |
| Existing separate database | Existing R06-B test database (name withheld), owner identity withheld; preservation requirement |
| Git branch | `review/aic-win-compat-20261101` |
| Git remote comparison | `origin/main [ahead 8]` against the locally available remote-tracking ref at inspection time |

The test role was created with `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOREPLICATION`, `NOBYPASSRLS`, and `NOINHERIT`, then assigned a password and enabled for login. The new database is owned by that role. `REVOKE ALL ON DATABASE <dedicated_test_database> FROM PUBLIC` was executed successfully.

**Important isolation limitation:** The role was found to have `CONNECT` (but not `CREATE`) on the existing R06-B test database (name withheld), consistent with PostgreSQL default `PUBLIC` database privileges. No assertion of absolute cross-database connection isolation is made. No modifications to the existing R06-B database were performed by the documented commands. Table-level authorization and network access controls were not comprehensively audited.

## 3. Test implementation and destructive-operation boundary

`tests/conftest.py` selects PostgreSQL using `AIC_TEST_DATABASE_URL` and uses SQLAlchemy ORM `Base.metadata.drop_all()` followed by `Base.metadata.create_all()` for PostgreSQL fixture resets. This is **table reset within a shared test database**, not a newly created per-test PostgreSQL database or independently isolated schema. The `_backend_guard` checks that the backend is PostgreSQL and reachable but does not itself enforce the permitted database name.

For the documented local test runs, a separate one-process preflight built the connection URL with SQLAlchemy's `URL.create`, checked the actual database/role, and checked for unexpected table/schema names before putting `AIC_TEST_DATABASE_URL` in the Python process environment. Passwords were requested interactively with `getpass`; the URL was not written to a tracked configuration file. These safeguards are local invocation practices, **not** permanent protections built into the repository. No guarantee is made about all potential side effects of test code or transient in-process exposure of credentials.

## 4. Maintainer-reported verification results

| Test or check | Maintainer-reported outcome | Interpretation |
| --- | --- | --- |
| Windows SQLite regression (previously reported) | **878 passed** | Earlier baseline; detailed log not reexamined in this milestone |
| Windows ACL regression (previously reported) | **153 passed** | Earlier baseline; detailed log not reexamined here |
| Parallel stability (previously reported) | **10/10 passed** | Earlier baseline; detailed log not reexamined here |
| Ruff (previously reported) | **Passed** | Earlier baseline; detailed log not reexamined here |
| PostgreSQL-only integration | **8 passed, 1 warning**; exit code **0**; **3.30 s** | Successful on local dedicated PostgreSQL test database |
| Unfiltered full PostgreSQL suite | **892 passed, 2 failed, 66 skipped, 1 warning**; exit code **1**; **498.22 s** | **Not** a full-suite pass |
| Official F.1-scoped PostgreSQL suite (`-m "not r06b_open"`) | **892 passed, 66 skipped, 2 deselected, 1 warning**; exit code **0**; **502.60 s** | **PASS for documented F.1 acceptance scope** |
| Final Git working tree check | `git diff --exit-code` and `git diff --cached --exit-code` emitted no differences | Clean tracked working tree at end of checks |
| Final DB environment checks | `AIC_TEST_DATABASE_URL`, `AIC_DATABASE_URL`, `PGPASSWORD` all **NOT SET** | No values present in the inspected PowerShell session |

**Note on counts:** These runs have distinct execution scopes. Do not add their passed totals or interpret the `66 skipped` and `2 deselected` as passing tests. The 892 passing tests are from the scoped PostgreSQL execution, not a claim that every collected test passed.

## 5. Known open B1 / F.2 issues

The two failures in the unfiltered suite were:

1. `TestB1IndependentInstances::test_two_instances_combined_admission_within_limit` — Two independently instantiated in-memory limiters admitted a combined **6** requests (3 east + 3 west) against the specified shared limit of **3**. This is the known B1 shared-admission gap; it is marked `r06b_open` in the test source.
2. `TestB1GenuineMultiprocess::test_two_processes_combined_admission_within_limit` — This test is also marked `r06b_open` and expected to remain failing until F.2. The unfiltered run additionally hit a **Windows `multiprocessing.spawn` harness problem**: a Python script fed on stdin has no re-importable file path (`<stdin>`), causing child processes to fail before the substantive admission assertion. A direct file-based test invocation would be required to distinguish the harness issue from the known functional gap; no such rerun was documented in this milestone.

The test module comments state that B1 is outside the authorized F.1 acceptance scope. The F.1 gate deliberately deselects B1 while retaining a separate, visible unresolved-status record. **Do not remove markers, relax assertions, or claim F.2 is fixed on the basis of F.1 acceptance.**

## 6. Warnings, skipped tests, and residual checks

- **66 skipped:** Their individual reasons have **not** yet been classified. Some may be environment or backend-specific, but no breakdown is established by the supplied outputs. A future review should obtain `pytest -rs` or equivalent skip reporting, without unnecessarily rerunning the full suite merely for counts.
- **1 warning:** `StarletteDeprecationWarning` from `fastapi.testclient` reporting `httpx` deprecation in the Starlette test client. This did not fail the scoped acceptance. Dependency modernization is a separate task; do not make unreviewed dependency changes here.
- **Sensitive-file name screening:** `git ls-files` filtered for selected secret-like names reported only `.env.example`. Its contents and every other tracked file were **not** fully scanned for real credentials. Therefore, no comprehensive secret-free certification is claimed.
- **PostgreSQL permissions:** The default `PUBLIC`-derived `CONNECT` permission on the existing R06-B test database (name withheld) remains observable for the new role. The recorded work intentionally did not alter that existing database. The local target preflight mitigates accidental mis-targeting for the documented tests; it is not a universal access-control barrier.
- **Deployment/production:** No production database, backup/restore drill, deployment, or network-hardening exercise is certified by this milestone.

## 7. Git and integration policy

At final inspection:

```text
review/aic-win-compat-20261101...origin/main [ahead 8]
```

`git diff --exit-code` and `git diff --cached --exit-code` returned without reported differences. This shows the tracked worktree and index were clean at inspection, but does not by itself imply a deep untracked-file or secret-content audit.

**Required boundary:** No push, no PR creation, no merge, no deployment, no modification to the separate R06-B F.2 working tree, and no modification to the existing R06-B test database (name withheld) as part of this milestone. The branch was eight commits ahead of its locally available `origin/main` tracking ref at inspection time; this alone does not establish whether those commits exist on other remote refs. This evidence record documents the earlier F.1 acceptance state. Subsequent documentation-only Git operations are tracked separately and do not retroactively alter the testing evidence.

## 8. Acceptance decision and follow-ups

**Decision: ACCEPT — maintainer-reported Windows PostgreSQL R06-B F.1 scoped acceptance.** The official marked acceptance gate returned exit code 0 with **892 passed, 66 skipped, 2 deselected, 1 warning**. The PostgreSQL-only integration subset also independently returned **8 passed**.

**Not accepted or not yet verified:** B1 shared admission / R06-B F.2; the two deselected tests; the 66 skipped tests; complete secret-content inspection; comprehensive database cross-access audit; and any production readiness assertion.

**Follow-up sequencing:**

1. Preserve this evidence as an archival milestone without modifying the protected worktree.
2. When authorized, classify the 66 skips and review `.env.example` plus broader tracked-content secret risks.
3. Track B1/F.2 independently; run the Windows spawn scenario from an actual file-backed Python entry point when testing it, while retaining the substantive expected-failure status until corrected.
4. Do not widen F.1 acceptance claims to F.2 or deployment.

---

*Prepared from operator-supplied execution logs. This is a maintainer-reported evidence summary, not an independently reproducible test package or a live audit of the operator's machine or repository.*
