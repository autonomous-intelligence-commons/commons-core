# AIC commons-core — R06-B F.1 Atomic Request Counter Remediation

- **Milestone recorded:** 2026-11-01
- **Scope:** R06-B F.1 only — atomic persistence of authenticated request telemetry
- **Repository:** `autonomous-intelligence-commons/commons-core`
- **Baseline before remediation:** `main` at `c418110` (R06-A milestone, PR #33)
- **Implementation PR:** [#34](https://github.com/autonomous-intelligence-commons/commons-core/pull/34)
- **Windows review commit:** `00852d1`
- **Merge commit:** `d3312bb` (abbreviated; as shown in the provided GitHub Actions screenshot)
- **Post-merge verification:** GitHub Actions CI #82 on `main`, shown successful in the provided screenshot
- **Overall status:** **F.1 complete; R06-B remains OPEN**

## Purpose and confirmed baseline

Independent reproduction on Windows (Python 3.12.10, PostgreSQL 16.15) and in a separate Linux environment (Python 3.12.16, PostgreSQL 16.2) established:

1. **B1 — independent limiter admission:** two isolated `RateLimiter` instances each allowed three requests for the same principal and time window, admitting six in total against a shared intended limit of three. The independent Linux investigation also reproduced the isolation using spawned processes.
2. **B2 — existing-window lost update:** two successful concurrent request-counter increments left `requests=2` rather than the expected `3`; `messages=7` remained unchanged.
3. **B3 — first-window creation race:** concurrent first insertion produced one successful transaction and one `IntegrityError`, leaving `requests=1` rather than `2`.
4. **B4 — interaction with R06-A:** the previous non-atomic request insertion could collide with R06-A's atomic message-quota UPSERT during first-window creation.

These observations were made before the F.1 fix. They are not post-remediation failures.

## Authorized remediation

The F.1 change replaces the ORM read–modify–write / first-row insertion logic in `persist_request_count()` with a database-atomic `INSERT ... ON CONFLICT DO UPDATE` that adds to `quota_counters.requests` at the SQL level. PostgreSQL and SQLite implementations are supported; the existing signature, table layout, and transaction ownership remain unchanged.

The implementation deliberately **does not** change request admission authority: the in-memory limiter remains authoritative in F.1. It also preserves the existing best-effort request telemetry failure behavior and avoids persisting anonymous request identities. There is no production schema migration, new production dependency, Railway deployment, or use of real AI providers.

Four files were changed in PR #34:

- `src/commons_core/security/rate_limit.py` — atomic request-counter persistence
- `tests/integration/test_r06b_request_concurrency.py` — concurrency, interaction, and privacy tests
- `pyproject.toml` — `r06b_open` pytest marker registration
- `.github/workflows/ci.yml` — gating selection and visible, non-blocking B1 reports

## Verification evidence

| Environment / test | Recorded result |
|---|---|
| Linux PostgreSQL 16.2, focused F.1 acceptance | 7 passed, 2 deselected (B1) |
| Linux PostgreSQL 16.2, full gated suite | 764 passed, 63 skipped, 2 deselected |
| Linux SQLite, full gated suite | 748 passed, 79 skipped, 2 deselected |
| Windows PostgreSQL 16.15, focused F.1 acceptance | 7 passed, 2 deselected |
| Windows PostgreSQL 16.15, R06-A regression | 5 passed |
| Windows SQLite, rate-limit unit tests | 8 passed |
| Windows Ruff lint and formatting | Passed |
| Windows SQLite, full gated suite | 741 passed, **7 failed**, 79 skipped, 2 deselected |
| Windows PostgreSQL 16.15, compatibility-selected suite | 755 passed, 65 skipped, **9 deselected** (B1 ×2 plus seven Windows-specific cases) |
| GitHub PR #34 | Eight CI checks passed (screenshot reviewed) |
| GitHub `main` after PR #34 merge | CI #82 passed (screenshot reviewed) |

The seven failures in the unfiltered Windows SQLite run involved checkpoint/Git-tree recovery, POSIX-style filesystem permissions, backup subprocess execution, and worker lifecycle. They were not fixed in F.1 and were excluded **only in the subsequent local Windows PostgreSQL compatibility-selected run**. That selective run must not be described as an unfiltered full-suite pass. Linux CI remained the blocking integration gate.

The `r06b_open` marker excludes two still-failing B1 tests from the blocking SQLite and PostgreSQL jobs, while a separate non-blocking CI step runs the B1 reproductions and exposes their expected failure. Therefore, eight green CI checks **do not** imply B1 was fixed.

## Integration and safety record

PR #34 was reviewed with `base=main`, `compare=review/aic-r06b-f1-windows-20261031`, four changed files, and no GitHub merge conflict. The user chose **Create a merge commit**. The provided GitHub screenshot shows the post-merge workflow on `main` (CI #82) passing. Historical `.patch` files in the Windows repository remained untracked and were not included in the F.1 commit.

This is a **documentation-only milestone**. It must be integrated through a separate Pull Request, without direct pushes to `main`, without production code changes, and without authorizing deployment. **E1 remains FAKE-ONLY; E2 is not authorized.**

## Open issues and next actions

- **R06-B1 remains OPEN.** Multiple worker processes still have independent admission counters. A separate F.2 authorization and security review are needed to choose a shared authority and explicit database-outage behavior; F.1 must not be represented as complete rate-limit enforcement.
- When F.2 is implemented, restore B1 to blocking CI tests and remove the temporary `r06b_open` gating exception only after its regression tests pass.
- Investigate the seven Windows-specific full-suite failures separately rather than weakening F.1 acceptance criteria.
- A separate Dependabot PR #35 upgrading Docker Python 3.12 to 3.15 was closed after an incompatible pinned `psycopg-binary==3.2.13` installation prevented the Docker image build. It did not modify the merged F.1 code.

**Milestone conclusion:** R06-B F.1 atomic request-counter persistence is merged and post-merge CI verified according to the supplied evidence. The broader R06-B workstream remains OPEN pending F.2 shared admission.
