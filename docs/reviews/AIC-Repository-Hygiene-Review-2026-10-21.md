# AIC — Repository Hygiene & Technical Debt Review

**Review date: 2026-10-21.**

This is the public, disclosure-reviewed version of the completed **AIC Repository Hygiene & Technical Debt Audit**. It summarizes the audit's evidence-supported findings for external maintainers and independent reviewers. It is not the full internal audit report; the complete report, findings matrix, and evidence bundle are retained as owner-restricted records. No exploitation procedures, credentials, or internal operational details are included.

**Repository:** [autonomous-intelligence-commons/commons-core](https://github.com/autonomous-intelligence-commons/commons-core)

**Audit baseline commit:** `491ac5c780bef4a756bf9969ddd3737e18358daa` (`main`, merge of PR #17)
**Audit baseline tree hash:** `145b257148b011ed69d1a2d7bc8ec929f3624d78`

The audit was executed on 2026-10-20 (UTC) in an isolated fresh clone of the repository at the baseline commit; this public document was prepared on 2026-10-21 (Australia/Brisbane, UTC+10). The audit baseline differs from the baseline pinned by the existing public security review (`fdcb7f80a6ac88586993e9df2a04fe2d3945bed4`) by two documentation-only commits (the PR #16 and PR #17 merges); no security-relevant code changed between them, so the R01–R24 statuses in the existing remediation-status matrix carry over without re-assessment.

Related public reviews: [AIC Public Security Review (2026-10-20)](AIC-Public-Security-Review-2026-10-20.md) · [AIC Remediation Status Matrix (2026-10-20)](AIC-Remediation-Status-Matrix-2026-10-20.md)

---

## 1. Executive Summary

The Stage 2 engineering wave — backup and recovery hardening (PR #12, PR #15), the development checkpoint and resume protocol (PR #14), and the P0 security hardening integration — has been merged. Before proceeding to **Stage 2.1 — Reliability, Stress & Failure-Injection Validation**, a read-only hygiene audit was performed to document the repository's maintainability status at the merged result.

The audit's headline conclusions:

- **Code hygiene is strong.** Lint and formatting checks pass under the repository's configured rule set; there are no dead modules, no unused imports under the enabled rules, no TODO/FIXME markers, and no development artifacts among the tracked files. All runnable tests pass (617 passed / 42 skipped / 0 failed on the SQLite backend; see Section 4).
- **The principal technical debt is documentation truthfulness, not code.** A claim-by-claim assessment of the repository's developer-facing instruction documents found 16 stale claims and 3 contradictory claims. Left uncorrected, these can cause automated agents or new contributors to follow instructions that the implementation no longer honors (for example, an all-zero development master key that the key store unconditionally rejects).
- **Two Git ignore gaps create a prospective hygiene risk.** The default development database directory (`data/`), `*.key` files, and virtual-environment variants (`.venv*`) are not covered by the ignore rules, despite a deployment document claiming otherwise. No sensitive artifacts were actually tracked at the baseline — the risk is prospective, not a present exposure.
- **A small set of dead-code candidates and six large modules exist**, but none constitutes a functional blocker. All code-level cleanup was deliberately deferred to protect a frozen validation baseline.
- **Large-scale refactoring was intentionally not recommended before Stage 2.1.** Running reliability, stress, and failure-injection validation against a stable, reproducible baseline is more valuable than interleaving refactoring with validation. The two recommended patches (documentation truth alignment and Git ignore hygiene) are limited to documentation and development configuration, with no intended changes to application runtime behavior.
- **Maintainability findings are separate from security findings.** This review is not a security audit. The earlier public Astra Max security review (R01–R24) remains applicable, and unresolved findings there are tracked separately. Cleaning up code or documentation does not automatically close any security finding.

**Stage 2.1 readiness conclusion: AIC can proceed to isolated Stage 2.1 validation without completing repository cleanup first, subject to the documented limitations and isolation requirements** (Section 8).

## 2. Scope and Methodology

The audit was strictly read-only:

- No application code, tests, configuration, or documentation was modified.
- No formatting was applied, no files or data were deleted, no branches were switched, and no Git history was touched.
- No pushes, pull requests, or publications were performed; no production environment was accessed; no live credentials were used; no Stage 2.1 stress testing was begun.
- The audit workspace was an isolated fresh clone of the repository at the baseline commit; local tooling environments were kept untracked and outside the delivered artifacts.

The audit combined the following methods:

1. **Repository inventory** — tracked-file census with per-file line counts, module mapping across domains, and an import-graph analysis.
2. **Static code inspection** — symbol-reference analysis for dead-code candidacy, duplication and divergence analysis, ignore-rule verification (`git check-ignore`), and complexity observations under advisory (non-enabled) rule sets.
3. **Test, fixture, and CI review** — assessment of the test suite structure, backend selection and self-skipping behavior, fixture safety classification, and the CI workflow's eight jobs.
4. **Non-destructive validation execution** — lint, formatting check, and the full test suite on the SQLite backend (Section 4).
5. **Instruction-document assessment** — claim-by-claim verification of the repository's de facto developer/AI instruction files against the implementation, tests, and CI.
6. **Independent re-verification** — the highest-impact findings from the parallel analysis tracks were re-checked one by one against source evidence by a separate review pass before being accepted.

Anti-false-positive discipline for dead-code candidacy: every zero-reference candidate was re-verified by exact-name search across source, tests, scripts, CI, documentation, and configuration; framework-invoked entry points (exception handlers, field validators, event listeners) were identified and excluded from candidacy; and the absence of direct references alone was **not** treated as proof that a function is safe to delete.

Method limitations, stated plainly:

- Static reference analysis cannot fully exclude dynamic access patterns. The audit verified that the codebase contains no dynamic module-import machinery, which reduces but does not eliminate this limitation.
- Tests were executed on the SQLite backend only; PostgreSQL-gated suites self-skipped and were not executed during the audit (Section 4).
- No Docker smoke, dependency scanning, load, stress, or failure-injection execution was performed during the audit.
- The audit does not claim exhaustive verification of every claim in every document; it assessed the substantive operational and setup claims in the files listed in Section 5.5.

## 3. Repository Inventory

Verified inventory at the baseline commit:

| Area | Files | Lines | Notes |
| --- | ---: | ---: | --- |
| Application package (`src/commons_core`) | 79 `.py` | 12,154 | API, agents, scheduler, security, backup, governance, infrastructure |
| Tests (`tests/`) | 38 `.py` | 12,568 | 35 test modules across 7 subdirectories, plus the root conftest and 2 fixture/synthesis helpers |
| CLI scripts (`scripts/`) | 10 `.py` | 2,706 | Management, worker, backup/restore, and development-checkpoint tools |
| Database migrations | 2 `.py` | 522 | Single Alembic revision with a complete downgrade |
| **All Python** | **129 `.py`** | **27,950** | |
| **All tracked files** | **174** | **35,568** | Includes 34 Markdown documents (33 under `docs/`, plus the root `README.md`), the CI workflow, and configuration files |

Six Python files are at or above approximately 800 lines:

| File | Lines | Kind |
| --- | ---: | --- |
| `tests/operational/test_p0_hardening.py` | 1,540 | test module |
| `tests/operational/test_backup_pipeline.py` | 1,509 | test module |
| `src/commons_core/backup/recovery_keys.py` | 1,079 | source (largest source file) |
| `src/commons_core/backup/restore.py` | 913 | source |
| `tests/operational/test_recovery_keys.py` | 878 | test module |
| `scripts/dev_checkpoint.py` | 824 | CLI script |

The CI configuration is a single workflow with eight jobs (lint, test, checkpoint-tools, postgres, backup-postgres18, docker, migrate-check, dependency-audit) plus Dependabot configuration. The dependency-audit job is non-blocking by design.

**File size and line counts are inventory facts. They do not, by themselves, establish code quality** — the large modules above are assessed individually in Section 5.4, and the audit found no quality defect attributable to size alone.

## 4. Validation Results

Checks executed during the audit, at the baseline commit, in an isolated environment using the repository's pinned dependencies (including the pinned `age` v1.3.2 encryption tool binaries, matching the CI pin):

| Check | Result |
| --- | --- |
| Ruff lint (repository-configured rule set, `--no-cache`) | **All checks passed** |
| Ruff formatting check | **Passed — no reformatting required** (163 files already formatted in the audit environment; all 129 tracked Python files conform. The additional scanned files reside in the isolated audit tool environment, which the current ignore rules do not exclude — see Section 5.2.) |
| Full test suite, SQLite backend | **617 passed / 42 skipped / 0 failed** (1 known deprecation warning from the HTTP test client; ≈67 seconds) |

Notes on the test execution:

- The 42 skipped tests are PostgreSQL-gated suites self-skipping by design when PostgreSQL connection settings are absent. The self-skipping behavior itself was verified during the audit; the suites were not silently ignored, and a fail-loud backend guard prevents accidental silent fallback.
- **PostgreSQL integration tests were not executed during this audit.** They provision and drop databases, which conflicts with the audit's read-only posture toward the local evaluation environment. Their self-skipping was verified instead.
- **Prior GitHub CI outcomes are separate evidence and must not be conflated with the audit's own executions.** At the same baseline commit, GitHub reported all eight workflow jobs green — including the PostgreSQL 16 job (migration round-trip, schema verification, full suite) and the PostgreSQL 18.6 backup/recovery job. These are GitHub-reported results; they were not re-executed locally during the audit. Docker smoke testing and dependency scanning are likewise covered by CI at the baseline (the dependency-audit job is non-blocking by design).
- Passing the existing test suite is evidence of the audit-time behavior of the tested code paths. It is **not** formal proof of security or reliability.

## 5. Technical Debt Findings

Findings below are separated into **confirmed** (re-verified against source evidence), **probable** (strong evidence, one residual uncertainty), and **requires investigation** (deferred judgment). All items are observations and proposals from a read-only audit; nothing has been implemented.

### 5.1 Stale and contradictory development documentation — CONFIRMED

The repository's de facto instruction documents (Section 5.5) were assessed claim by claim against the implementation at the baseline. Of 34 substantive claims assessed: **15 verified current, 16 stale, 3 contradictory.**

The systematic pattern is not wrong new information but **undated historical statements**: several developer-facing documents were written during the Phase-1 development window (frozen 2026-10-17) and were not updated across the later Stage 2 hardening waves. Readers and automated agents cannot distinguish current rules from time-of-writing records.

The highest-impact stale or contradictory statements — each capable of causing an automated agent or new contributor to fail or misbehave if followed verbatim:

| Document | Statement (abridged) | Verified reality at the baseline |
| --- | --- | --- |
| `docs/DEPLOYMENT.md` | Presents an all-zero development master key as the development default | The key store unconditionally rejects all-zero keys (fail-closed); leaving the setting unset produces an ephemeral in-memory key |
| `docs/DEPLOYMENT.md`, `docs/TESTING.md` | Describes `AIC_TASK_TIMEOUT_SECONDS` as inert / not enforced | The timeout is enforced: worker startup rejects inconsistent lease/timeout settings, an execution guard bounds claimed tasks, and regression tests cover the behavior |
| `docs/DEPLOYMENT.md` | Claims `.gitignore` already excludes `.env`, `data/`, and `*.key` | Verified with `git check-ignore`: only `.env` is ignored; `data/` and `*.key` are not (see Section 5.2) |
| `docs/DEPLOYMENT.md` | States that `psycopg` is not in the pinned dependency set | The `postgres` extra pins `psycopg[binary]==3.2.13`; the production image pins the same version |
| `docs/DEVELOPMENT.md`, `docs/TESTING.md` | Hardcodes a machine-specific absolute path; states the `dev` extra alone suffices for the full suite | A generic repository path should be used; some tests require the `postgres` extra (CI installs `.[dev,postgres]`, with the rationale documented in the workflow itself) |
| `docs/DEVELOPMENT.md` | Attributes the emergency-stop endpoints to the scheduler domain | The endpoints are implemented in the admin router |
| `docs/TESTING.md`, `docs/SECURITY_CONTROLS.md` | Carries outdated suite counts ("342 passed") | 617 passed / 42 skipped measured at the baseline |
| `docs/TESTING.md`, `docs/SECURITY_CONTROLS.md`, `docs/DEPLOYMENT.md` | Phase-1 statements that Docker/CI had never executed remain unmarked as history | The CI workflow documents successful runs; some statements contradict later sections of the same documents |

Minor count drift and cross-reference issues were also confirmed (test-count references in `docs/BACKUP.md`, a worklog-template section reference, the README quickstart, and two documented environment variables missing from `.env.example`). These are additive fixes.

Counterpoint, stated for fairness: **the operator-critical documents were verified accurate claim by claim.** `docs/BACKUP.md` and `docs/DEVELOPMENT_CHECKPOINTS.md` matched the implementation, tests, and CI on every assessed claim, including all fail-closed gates. The drift is concentrated in developer-facing documents.

### 5.2 Git ignore and artifact protection gaps — CONFIRMED

- `data/` (which contains the default development database file), `*.key`, and `.venv*` (virtual-environment name variants) are **not** covered by the repository's ignore rules. Verified with `git check-ignore` at the baseline.
- A deployment document asserts these are already excluded (Section 5.1, third row). The assertion is false for two of the three named patterns.
- **No sensitive artifacts were actually tracked at the baseline.** The tracked file set was verified clean: no caches, logs, editor files, `.env` files, or large binaries; the largest tracked file is a test source file of roughly 70 KB.
- The risk is therefore **prospective**: a future `git add -A` in a development environment could stage the local development database or key material that the ignore rules fail to cover.

The proposed correction is PR-2 (Section 6). To be explicit: ignore rules are a hygiene backstop and **are not a substitute for secret scanning or proper credential handling**; the audit's documentation findings about ignore coverage do not change how credentials must be managed.

### 5.3 Dead-code candidates — CONFIRMED (six), plus suspected items

**Confirmed candidates** (each verified to have zero non-definition references across source, tests, scripts, CI, docs, and configuration, with dynamic-access patterns excluded):

1. `backup/restore.py` — `_libpq_env`: a deprecated shim whose docstring says it is "kept for callers/tests" that no longer exist.
2. `backup/tools.py` — `age_major_version`: zero references, and misnamed (it returns the full version string).
3. `agents/service.py` — `get_agent`: zero references (routes use other accessors).
4. `backup/destination.py` and `backup/connection.py` — two currently unused `describe()` diagnostic helpers with inconsistent output-redaction behavior. Their removal or consolidation should be evaluated alongside the existing security remediation work. This is a cleanup and misuse-prevention consideration, not a confirmed vulnerability.
5. `backup/recovery_keys.py` — one unused unpacked local variable (invisible to the configured lint rules).

**Suspected candidates — these require investigation before any removal decision:**

- `activities/service.py` — `activities_channel`: an unreferenced metadata helper.
- `infrastructure/quotas.py` — two unreferenced quota helpers (designed for a Retry-After hint that was never wired up; quota enforcement itself is unaffected).
- `database/db.py` — `init_db`: unreferenced in production code paths, but documented as a development-convenience API.

The audit deliberately did **not** label any unused-looking function as safe to delete merely because direct references were not found. Two related facts support this discipline:

- Twelve methods with zero name references were verified to be **framework entry points** (exception handlers, field validators, event listeners) and are not dead code. These are recorded to prevent accidental deletion.
- `backup/pipeline.py`'s `pg_env_from_url` has zero production callers but is **not** dead code: it is the parser-parity test surface for the resolved connection-contract finding (R10) and is named in [ADR-0011](../ADR/0011-p0-backup-recovery-hardening.md). It must be retained; see the guardrails in Section 6.

### 5.4 Large modules — CONFIRMED as inventory; splits deferred

The six files from Section 3 were assessed individually:

| File | Assessment | Proposal |
| --- | --- | --- |
| `tests/operational/test_p0_hardening.py` (1,540) | Intentional **independent second implementation** of the P0 regression scenarios — deliberate CI-matrix redundancy, not accidental duplication | Optional class-based split, deferred until after Stage 2.1 |
| `tests/operational/test_backup_pipeline.py` (1,509) | 20 unit tests plus five integration classes | Same treatment |
| `backup/recovery_keys.py` (1,079) | Three clear responsibility clusters (errors/data types; `age` identity generation and tool discovery; package lifecycle). The largest source file. Lies on the ADR-0010 emergency-decryption path, with 38 tests pinning its semantics | Split possible while preserving the package's public API; recommended only with demonstrated maintenance pain, Stage 2.2+ |
| `backup/restore.py` (913) | Four clear clusters (scratch-DB lifecycle; streaming decrypt-restore; verification battery; drill orchestration) | Split possible; deferred (overlaps resolved/partially-addressed security areas) |
| `tests/operational/test_recovery_keys.py` (878) | 38 tests | Optional split, low priority |
| `scripts/dev_checkpoint.py` (824) | Self-contained CLI with a clear four-part structure | **Keep as is**; size judged reasonable for a standalone CLI |

Two notes keep this section honest. First, advisory (non-enabled) complexity analysis found 22 hotspots, concentrated in fail-closed validation paths (recovery package creation and verification, restore drills); branch density is inherent to fail-closed verification, and the audit does not recommend refactoring for it. Second, **file size alone is not a defect** — every split proposal above is tied to a demonstrated responsibility boundary and carries a stated regression risk, and all are deferred.

### 5.5 AI development instruction file observations

The repository contains no `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md`. Its de facto instruction surfaces are `README.md`, `docs/DEVELOPMENT.md`, `docs/DEVELOPMENT_CHECKPOINTS.md`, `docs/TESTING.md`, `docs/DEPLOYMENT.md`, `docs/SECURITY_CONTROLS.md`, `docs/BACKUP.md`, and `.env.example`.

The stale and contradictory claims in Section 5.1 are the primary agent-execution risk: an automated agent following the current text verbatim could install an insufficient dependency set, attempt to start with a rejected key default, misattribute endpoints, or believe the ignore rules cover files they do not. The correction of these statements is PR-1.

Equally important is what the assessment confirmed must be **preserved verbatim** in any documentation cleanup: the fail-closed production startup rules, all fail-closed backup/restore gates, secret-hygiene conventions, checkpoint-protocol invariants, supply-chain pinning, and the fail-loud test backend guard. These were verified current and correct.

### 5.6 Areas observed healthy

For balance, the audit explicitly records what it did **not** find:

- Lint and formatting fully green under the repository's configured rules; zero unused imports, variables, or redefinitions under the enabled rules.
- Zero `TODO`/`FIXME`/`XXX`/`HACK` markers across source, scripts, tests, and migrations.
- No dead modules — every source module has inbound imports.
- No development residue among tracked files.
- Test fixtures fail loud by design: no silent SQLite fallback; a backend guard aborts on misconfigured database settings.
- The backup package core (`connection`, `destination`, `manifest`, `pipeline`, `storage`, `tools`, `age_format`) is standard-library-only with zero internal imports, matching the intended isolation of the security-critical backup path.
- The import graph is acyclic at module level.
- The operator documentation (`BACKUP.md`, `DEVELOPMENT_CHECKPOINTS.md`) is accurate claim by claim.

### 5.7 Other deferred observations — probable / requires investigation

Recorded for completeness; all deferred per Section 6:

- **Duplicated binary-tool resolver logic** in two backup modules (same environment-variable → path → executable-check shape, divergent error types, partly intentional). Convergence is possible with a parameterized helper and must preserve custody fail-closed error semantics.
- **Two-process orchestration implemented twice** (piped subprocess pair with bounded stderr capture and deadlines) with partially intentional error-shape divergence. Any shared runner must preserve exact supervision semantics and be gated by the fault-injection suites.
- **Mechanical helper duplication across operational test suites** (environment plumbing copied verbatim, a publish helper near-identical, an identical fake-tool writer, a duplicated database fixture, and several synthesis idioms). Scenario-level overlap between the hardening suites was judged **intentional CI-matrix redundancy** worth keeping; only the mechanical copies are consolidation candidates.
- **A test-fixture URL parsing quirk:** some operational fixtures derive database names from connection URLs in a way that mis-handles URLs carrying query strings; the CI configuration does not use such URLs. Deferred test-infrastructure hardening, to be revisited with the consolidation above.
- **Timing-sensitive tests** (sub-second to multi-second waits, plus a drain deadline) are flake-prone on heavily loaded runners; manage via execution discipline during Stage 2.1 rather than pre-emptive changes.
- **Minor naming and structure notes:** two same-named UTC-now helpers with different return types; repeated `sys.path` bootstrap preambles across CLI scripts (a documented trade-off, kept as is).

## 6. Recommended Cleanup Sequence

**Everything in this section is a proposal. Nothing has been implemented, merged, or pushed.** The owner will review and authorize each change separately.

### PR-1 — Documentation Truth Alignment

A single documentation-only pull request: correct the stale and contradictory statements in Section 5.1; date-stamp time-of-writing statements as historical records rather than deleting them; refresh the recorded test counts; add the two documented environment variables to `.env.example`; fix cross-references.

- **Impact:** No intended application runtime behavior changes; documentation and example configuration only.
- **Verification:** documentation diff review (no CI job covers docs directly); the corrected claims should be re-checked against the implementation.
- **Rollback:** single-commit revert.

### PR-2 — Git Ignore Hygiene

A single `.gitignore`-only pull request: add `data/`, `*.key`, and `.venv*` patterns.

- **Verification:** each new pattern must match via `git check-ignore`; `git ls-files -i --exclude-standard` must return empty (no existing tracked file may become newly ignored); the change must not be batched with any code change.
- **Boundaries:** `.gitignore` rules are a hygiene backstop and are **not a substitute for secret scanning or proper credential handling**. This PR addresses prospective staging risk only; it does not change how secrets are managed.

### Deferred Changes

All code-level cleanup is deferred until after Stage 2.1 (Stage 2.2 window), to keep the validation baseline frozen:

- Dead-code removal batch (Section 5.3 confirmed items only, after investigation of suspected items), with the package's public API untouched.
- Test-harness mechanical consolidation, gated by **identical collected test IDs before and after** and an unchanged suite baseline.
- Module splits (Section 5.4), preserving the package public API and updating the ADRs that describe the affected modules.
- Subprocess and resolver convergence, gated by the fault-injection and custody suites, with ADRs updated before implementation.

### Engineering guardrails (binding for any later cleanup)

- **Publication policies must not be merged.** The atomic-write staging idioms in backup storage and recovery-key custody look similar, but their promote policies are deliberately divergent: no-overwrite publication (immutability invariant) versus custody convenience for a pre-verified fresh directory. A naive deduplication would break the no-overwrite invariant (R11). Any future unification requires an ADR update and the full no-overwrite / race / corruption regression matrix.
- **Keep `pg_env_from_url`.** Despite zero production callers, it is the R10 parser-parity test surface named in ADR-0011 and must not be removed as dead code.
- **Preserve fail-closed semantics** in any change to custody or recovery paths (emergency-decryption path; see [ADR-0010](../ADR/0010-offline-recovery-key-custody.md)).

## 7. Relationship to Security Findings

- **This hygiene review is not a security audit** and reaches no security conclusions of its own. Passing lint and tests is not proof of security.
- **The earlier public Astra Max security review remains fully applicable.** Its findings R01–R24 and their current statuses are maintained in the [AIC Remediation Status Matrix (2026-10-20)](AIC-Remediation-Status-Matrix-2026-10-20.md); the review itself is at [AIC Public Security Review (2026-10-20)](AIC-Public-Security-Review-2026-10-20.md). As of that matrix: 2 findings verified resolved (R10, R11), 4 partially addressed (R09, R12, R14, R15), 16 still open, 1 not independently verified (R19), and 1 historical or operational limitation (R03).
- The audit baseline differs from the security review's pinned baseline only by the two documentation-only commits noted in the header, so those statuses carry over without re-assessment. This hygiene review does not establish that any Astra Max finding has been resolved, and no risk status has been changed here.
- **Cleaning up code or documentation does not automatically close any security finding.** For example, removing the unused `describe()` helper (Section 5.3) is adjacent to the secrets-in-operational-output finding (R15) and eliminates a latent misuse hazard, but it closes nothing in R15.
- Conversely, the open security findings are largely capability or production-evidence gaps (runtime authorization, task ownership, atomic quota admission, execution isolation, cost boundaries, operational recovery) — not repository-hygiene defects. Cleanup can neither fix them nor substitute for the security remediation work.
- No exploitation instructions or restricted evidence from the original security review are reproduced in this document.

## 8. Stage 2.1 Readiness Assessment

**Question: Can AIC proceed to isolated Stage 2.1 reliability, stress, and failure-injection validation without completing repository cleanup first?**

**Answer: Yes — subject to the documented limitations and isolation requirements.**

The reasoning:

1. **No functional blockers were found.** No dead modules, no unused imports under the enabled rules, no tracked development artifacts; lint, formatting, and all runnable tests pass at the baseline (617 passed / 0 failed). The only prohibited actions in this review are the guardrails in Section 6 — engineering rules to respect during later cleanup, not defects to fix first.
2. **The validation baseline is stable and reproducible.** The 617/42 test result and the green lint/format checks are pinned to a specific commit and were reproduced independently of CI; the CI matrix was green at the same commit as reported by GitHub. Freezing this baseline gives Stage 2.1 results a clean reference point.
3. **Recommended measures are separated from functional prerequisites.** PR-1 and PR-2 are recommended risk-reduction measures that remove documented agent-misexecution vectors; they are not prerequisites identified by the hygiene audit. If the owner chooses to begin Stage 2.1 without them, the compensating measure is to brief executing agents and contributors to disregard the stale and contradictory instructions identified in Section 5.1.
4. **The real risks for Stage 2.1 are the open security items, and they are the purpose of the validation, not preconditions of it.** Lease-ownership races, stop-signal commit semantics, atomic quota admission, and timing determinism are exactly the behaviors Stage 2.1 is designed to exercise under stress and failure injection.

**Isolation requirements:** Stage 2.1 must run in a disposable, isolated, synthetic environment with synthetic data and synthetic or pinned tooling. It must not perform destructive testing against Railway production, must not use live credentials, and should not interleave code cleanup with stress testing — keep the baseline frozen for the duration of validation.

This readiness assessment applies only to isolated validation activities. It does not establish readiness for production deployment or unattended autonomous AI Agent operation.

## 9. Limitations and Next Steps

**Limitations of this review:**

- Tests were executed on the SQLite backend only; PostgreSQL-gated suites self-skipped. The PostgreSQL-relevant evidence in this document is GitHub-reported CI at the same commit, not local audit execution.
- No coverage-percentage measurement was taken; the audit makes no claim about coverage numbers.
- No Docker, dependency-scan, load, stress, or failure-injection execution was performed during the audit; the dependency-audit CI job is non-blocking by design, and supply-chain assurance remains tracked separately.
- Static reference analysis cannot fully exclude dynamic access patterns (mitigated by verifying the absence of dynamic-import machinery, but not eliminated).
- Documentation findings reflect the baseline commit; documentation drifts again with every change, and corrected claims should be re-verified at the time of any future audit.

**Recommended sequence:**

1. **PR-1 — Documentation truth alignment** (owner review and merge).
2. **PR-2 — Git ignore hygiene** (owner review and merge).
3. **Stage 2.1 — isolated reliability, stress, and failure-injection validation** on the frozen baseline.
4. **Stage 2.2+ — deferred cleanup** under the guardrails in Section 6 (dead-code batch, test-harness consolidation, module splits, convergence work).

**What would require fresh validation after code changes:**

- Any code change invalidates the frozen baseline: re-run the full SQLite suite and the relevant CI jobs (including the PostgreSQL 18.6 backup job for backup-area changes) before relying on numbers.
- Documentation-only PRs still require diff review of every corrected claim against the implementation.
- The `.gitignore` PR requires the check-ignore verifications in Section 6.
- Test consolidation requires identical collected test IDs before and after, with the suite baseline unchanged.
- Module splits require the package public API and ADR statements to remain in sync, plus the relevant domain suites green.

---

*Provenance and integrity: this public document was derived from the completed internal audit report, findings matrix, and evidence bundle (baseline `491ac5c780bef4a756bf9969ddd3737e18358daa`) through a disclosure review that classified all audit content for public suitability. Restricted internal materials — including local sandbox paths, internal work records, and historical security-evidence details — are excluded. The owner will review this document and authorize GitHub publication separately.*
