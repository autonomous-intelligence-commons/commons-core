# AIC Development Worklog — <TASK TITLE>

> Copy this template to a task-specific worklog (for example
> `docs/worklogs/2026-XX-XX-<task-slug>.md` or the sandbox-local
> `worklog.md`) at the START of every substantial task, and update it at
> every meaningful milestone — not only at completion.
>
> A newly resumed Agent must be able to reconstruct the current state from
> this file alone, WITHOUT repeating previous research. Never claim a
> milestone is complete merely because it appears in an old worklog:
> verify surviving artifacts and recorded commits first
> (`scripts/dev_resume_check.py` automates this).

---

## 1. Identification

| Field | Value |
| --- | --- |
| **Project name** | Autonomous Intelligence Commons (AIC) — `commons-core` |
| **Task identifier** | `<e.g. AIC Phase 1 — Backup & Recovery Stage 3>` |
| **Task slug** | `<short-kebab-case-name used in branch / bundle names>` |
| **Authorized scope** | `<one-paragraph summary of what this task may and may not touch; reference the governing spec file>` |
| **Source repository** | `https://github.com/autonomous-intelligence-commons/commons-core` |
| **Governing spec** | `<path of the uploaded specification file>` |

## 2. Repository state (update at every commit)

| Field | Value |
| --- | --- |
| **Baseline commit** | `<full 40-char SHA of the verified main commit this task started from>` |
| **Current branch** | `<feature branch name>` |
| **Most recent checkpoint commit** | `<full SHA>` |
| **Commits on branch (count)** | `<N>` |
| **Working tree** | `<clean / dirty — if dirty, list files and why>` |
| **Unpushed?** | `<yes/no — pushed state must be honest; pushing requires owner authorization>` |

## 3. Milestones

Use the status vocabulary from `docs/DEVELOPMENT_CHECKPOINTS.md` §4.
A checkpoint MAY record incomplete work, but its status MUST be explicit.

### Completed

| # | Milestone | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Repository audit completed | `<designed / implemented / tested-locally / committed-locally / …>` | `<command output, file, commit>` |

### Remaining

| # | Milestone | Status | Notes |
| --- | --- | --- | --- |
| 1 | `<…>` | pending | `<what is needed>` |

## 4. Test commands and results

Record the exact commands and the observed results (never a paraphrase
that hides a failure).

```text
$ <command>
<summary of result — e.g. "521 passed / 2 skipped", "FAILED: …">
```

## 5. Known failures

| Failure | Impact | Status |
| --- | --- | --- |
| `<symptom>` | `<what it blocks>` | `<open / worked-around / fixed in <commit>>` |

## 6. Unresolved security concerns

| Concern | Context | Disposition |
| --- | --- | --- |
| `<…>` | `<…>` | `<accepted-with-rationale / escalated-to-owner / open>` |

## 7. Environment dependencies

Record everything a fresh sandbox must reconstruct (see
`docs/DEVELOPMENT_CHECKPOINTS.md` §11 for the standard recipe).

| Dependency | Version / pin | How to verify |
| --- | --- | --- |
| Python | 3.12.x | `python --version` |
| PostgreSQL 16 (test) | `<e.g. 16.15 @127.0.0.1:5432>` | `pg_isready` |
| PostgreSQL 18 (test) | `<e.g. 18.6 @127.0.0.1:5433>` | `pg_isready` |
| age | `<e.g. v1.3.2, sha256 cbe24006…>` | `age --version` |
| Python venv | `<install command — e.g. pip install -e ".[dev,postgres]">` | `python -m pytest --version` |

## 8. Relevant file locations

| Artifact | Location |
| --- | --- |
| Task spec | `<path to the uploaded task-spec file>` |
| Repository | `<local repository clone path>` |
| Checkpoint bundles | `<directory holding generated checkpoint zips>` |
| Delivery bundles | `<directory holding final delivery zips>` |
| Evidence logs | `<…>` |

## 9. Checkpoint register

Append one row per generated checkpoint bundle. NEVER reuse a filename.
Delivery statuses: `LOCAL_ONLY` → `EXPORT_PREPARED` → `UPLOAD_ATTEMPTED` →
`UPLOADED_UNVERIFIED` → `EXTERNALLY_RETRIEVED_VERIFIED` →
`OWNER_CONFIRMED_PERSISTENT` (see `docs/DEVELOPMENT_CHECKPOINTS.md` §8;
only the owner's explicit confirmation justifies the last state).

| Bundle filename | Bundle SHA-256 | Baseline → head | Delivery status | Retrieval evidence |
| --- | --- | --- | --- | --- |
| `<name.zip>` | `<sha256>` | `<short-sha>…<short-sha>` | `<status>` | `<URL + re-download match / —>` |

## 10. Latest externally verified checkpoint

| Field | Value |
| --- | --- |
| **Bundle** | `<filename + sha256>` |
| **Verified at (UTC)** | `<timestamp>` |
| **Verification method** | `<re-downloaded to separate location; sha256 match; manifest + patch order validated>` |
| **Storage limitation** | `<e.g. "temporary host, short retention — owner must move to durable storage">` |

## 11. Next exact action required to resume

> Write this so precisely that a resumed Agent can execute it as its first
> command without reading anything else.

```text
<e.g. "1. Verify checkpoint bundle <sha256> (scripts/dev_checkpoint_verify.py).
       2. Fresh clone origin at <baseline>; git switch -c <branch>.
       3. git am patches from bundle (ordered).
       4. Run <exact next test command>.
       5. Continue remaining milestone #<N>: <description>.">
```

## 12. Revision history

| Timestamp (UTC) | Entry |
| --- | --- |
| `<ts>` | `<what changed in this worklog and why>` |
