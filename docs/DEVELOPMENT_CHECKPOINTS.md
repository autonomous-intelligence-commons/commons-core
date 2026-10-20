# Development Checkpoints & Resume Protocol

**Making AIC development recoverable when an AI coding sandbox disappears.**

This protocol is a repository-side engineering-workflow control. It does NOT
implement production Agent-memory backup or PostgreSQL disaster recovery —
those are covered by ADR-0009 / `docs/BACKUP.md`. It protects *development
state*: commits, worklogs, verification evidence, and the ability to resume.

**Core invariant:** no critical engineering progress may depend exclusively
on the continued existence of one temporary sandbox.

---

## 1. When to apply this protocol

Apply to every substantial AIC task (spec-level assignments, security
remediations, multi-commit features). Do not apply ceremony to trivial
one-line changes; a single well-described commit and PR is enough there.

A task is "substantial" when ANY of:

- it is expected to produce more than one commit;
- it follows an uploaded specification file;
- it involves security-relevant code or threat-model claims;
- it depends on a reconstructed environment (PostgreSQL, age, venv);
- it may outlive one sandbox session.

## 2. Artifacts and roles

| Artifact | Location | Purpose |
| --- | --- | --- |
| Feature branch | in-repo, e.g. `chore/<task>` or `fix/<task>` | carries incremental commits |
| Structured worklog | task worklog file (template: `docs/templates/WORKLOG.template.md`) | single source of truth for state |
| Checkpoint bundle | versioned `.zip` OUTSIDE the repo work tree | portable, verifiable state export |
| External delivery | authorized channel only | durability beyond the sandbox |

Tooling (stdlib-only, no new dependencies):

| Tool | Mode | Purpose |
| --- | --- | --- |
| `scripts/dev_checkpoint.py` | create / `--dry-run` | produce a checkpoint bundle from a clean work tree |
| `scripts/dev_checkpoint_verify.py` | verify / `--check-baseline-in` / `--verify-applied` | integrity + baseline gates |
| `scripts/dev_resume_check.py` | audit | compare surviving sandbox vs. checkpoints; emit next actions |

## 3. Baseline and branching policy

1. Record the exact baseline commit before starting: the latest verified
   `origin/main` (or a documented compatibility base).
2. Create a dedicated feature branch from that baseline
   (`git switch -c <branch> <baseline>`).
3. Commit coherent work incrementally. Preserve completed work BEFORE
   lengthy test runs (a crashed suite must never cost finished edits).
4. Never rebase or rewrite history that any checkpoint bundle already
   exported — patches in the wild must keep applying.
5. Keep unrelated changes on separate branches; never mix tasks.
6. Exclude credentials and generated artifacts (the repo `.gitignore`
   already excludes `.env`, `.venv`, `*.age`, `age-identity*.txt`, …).

Suggested checkpoint commits (a checkpoint may record incomplete work,
but its status must be explicit in the worklog):

1. Repository audit completed.
2. Defect or requirement verified (reproduction evidence).
3. Initial implementation completed.
4. Regression tests added.
5. Integration tests passed.
6. Documentation completed.
7. Final patch bundle verified.

## 4. Status vocabulary — never conflate these states

Development states (per milestone, recorded in the worklog):

```text
designed → implemented → tested-locally → committed-locally → exported
        → retrieved-and-verified-externally → pushed → ci-passed → merged → deployed
```

Delivery states (per checkpoint bundle, recorded in the worklog register):

| Status | Meaning |
| --- | --- |
| `LOCAL_ONLY` | bundle exists only inside the sandbox |
| `EXPORT_PREPARED` | checksums recorded; ready to upload |
| `UPLOAD_ATTEMPTED` | an upload was started (may have failed) |
| `UPLOADED_UNVERIFIED` | upload reported success, not yet re-downloaded |
| `EXTERNALLY_RETRIEVED_VERIFIED` | re-downloaded elsewhere; SHA-256 + structure verified |
| `OWNER_CONFIRMED_PERSISTENT` | ONLY after the owner explicitly confirms durable storage |

A successful upload or a single successful download does NOT establish
permanent retention. Temporary file-hosting services are NOT reliable
long-term storage and must never be described as such. Prefer an
owner-controlled persistent destination. Never create new cloud accounts,
repositories, releases, buckets, or paid services without authorization.

## 5. Structured worklog

Use `docs/templates/WORKLOG.template.md`. Minimum mandatory fields:
project, task identifier, authorized scope, source repository, baseline
commit, current branch, most recent checkpoint commit, completed and
remaining milestones with statuses, test commands AND results, known
failures, unresolved security concerns, environment dependencies, relevant
file locations, checkpoint register (filename + SHA-256 + delivery status),
latest externally verified checkpoint, and the **next exact action required
to resume**.

Update the worklog at meaningful milestones, not only at completion.
A resumed Agent must verify surviving artifacts instead of trusting old
claims (`scripts/dev_resume_check.py`).

## 6. Checkpoint bundle format

Produced by `scripts/dev_checkpoint.py` (stdlib-only; requires only git).
A bundle is a versioned zip — name pattern
`checkpoint-<taskslug>-<UTC timestamp>-<head-short-sha>.zip` — containing:

| File | Content |
| --- | --- |
| `MANIFEST.json` | schema version; repository identity (origin URL, name, branch); FULL baseline and head commit SHAs; head tree SHA; ordered patch list with per-file SHA-256; worklog snapshot name; milestones (completed/pending); recorded test commands; known limitations; environment dependencies; delivery status (`LOCAL_ONLY` at creation); next-action hint |
| `patches/0001-*.patch …` | `git format-patch baseline..head`, strictly ordered |
| `WORKLOG.md` (or recorded name) | snapshot of the task worklog at bundle time |
| `RECOVER.md` | generated, exact step-by-step recovery instructions (clone → verify baseline → branch → `git am` → verify head/tree) incl. Windows Git Bash notes |
| `CHECKSUMS.sha256` | `sha256sum -c`-compatible manifest of every file (except itself) |

Hard rules (enforced by the tools):

- **Clean-tree rule** — the work tree must be clean. Untracked or
  uncommitted changes are NEVER silently bundled. The single explicit
  exception is the worklog file itself: `--allow-dirty-worklog` permits
  dirt only for exactly the `--worklog` path, and the manifest records
  `worklog_dirty: true`. Any other dirt refuses the run. To export
  unfinished code, make an explicit WIP commit — WIP checkpoints are
  allowed as long as their status is explicit.
- **No-overwrite rule** — an existing bundle filename is never overwritten;
  a new checkpoint gets a new versioned filename.
- **Baseline rule** — the baseline commit must exist in the repository and
  be an ancestor of HEAD.
- **Self-verification rule** — after writing, the bundle is re-opened and
  every recorded hash re-verified before the tool reports success.
- **Output-safety rule** — bundles are written outside the repository work
  tree (an inside-repo destination is refused), via a `.part` temp name
  that is atomically renamed; an interrupted run can never leave a
  half-written bundle that looks valid.
- **Secret-hygiene rule** — patch text and the worklog snapshot are
  scanned (see §9); findings refuse the run unless explicitly acknowledged
  with `--acknowledge-scan-findings`, which is recorded in the manifest.

The zip's own SHA-256 is printed at creation and must be recorded in the
worklog register; it cannot be self-contained.

## 7. External delivery workflow

**This is the highest-priority requirement: a checkpoint stored only inside
the sandbox is not durable.**

Export at these milestones:

- after a difficult repository audit;
- after implementing and testing a high-priority security fix;
- before lengthy integration tests;
- before large-scale environment reconstruction;
- after completing a major development milestone;
- before any operation that could destroy the workspace.

For prolonged tasks also enforce a maximum interval without a successful
external checkpoint (recommended: at least once per working session, and
never leave a session's HEAD unexported), without pushing trivial changes.

Required workflow (each step is recorded in the worklog):

1. Generate the checkpoint bundle (`scripts/dev_checkpoint.py`).
2. Record baseline and current commit hashes.
3. Calculate SHA-256 checksums (bundle + manifest).
4. Scan for secrets and unsafe artifacts (tool-enforced + manual review).
5. Deliver through an authorized external mechanism (e.g. the
   owner-approved temporary upload service; never claim it as durable).
6. Confirm the external artifact can be retrieved.
7. Download it again into a separate temporary location when feasible.
8. Verify checksum AND structural integrity — including the "PK" zip magic:
   an HTML error page returned with a 200 status is a REAL failure mode we
   have observed; size + first-bytes check catches it.
9. Record the delivery result and status in the worklog register.
10. Provide the owner the artifact location, SHA-256, and recovery
    instructions (RECOVER.md inside the bundle).

Bounded retry policy (§10) applies to upload/download failures.

## 8. Mandatory end-to-end recovery drill

The protocol is not complete for a given task until one synthetic
external delivery + recovery cycle has been demonstrated:

- **Phase A** — disposable synthetic repo: baseline commit → feature
  branch → harmless change commit → worklog →
  `dev_checkpoint.py` → SHA-256 → secret scan.
- **Phase B** — genuine external delivery: upload via the authorized
  mechanism, independent re-download, SHA-256 comparison, manifest and
  patch-order validation, record the delivery status.
- **Phase C** — simulate total sandbox loss: destroy ONLY the disposable
  working directory; fresh directory; re-clone the synthetic "origin"
  (a bare repo that survives outside the destroyed tree, standing in for
  GitHub); verify the baseline; recreate the branch; `git am` the ordered
  patches; verify head commit AND tree hash against the manifest; recover
  the worklog; resume the remaining synthetic task WITHOUT duplicating
  commits.
- **Phase D** — document: delivery mechanism, retrieval evidence, SHA-256
  comparison, baseline verification, restored tree hash, recovered worklog
  contents, exact outcome, and storage limitations.

Never describe a copy inside the original working directory as an
externally verified transfer. If no external mechanism is available,
complete the local drill, mark the external drill **BLOCKED**, and request
an authorized channel — never mark the criterion passed. Never delete the
real AIC repository during testing.

## 9. Security and secret hygiene

Checkpoint artifacts must NEVER contain: production credentials, API keys,
tokens, age private identities, application master keys, production
database exports, private Agent communication, sensitive environment
variables, unencrypted recovery secrets. Excluded by rule: `.env` files,
virtual environments, PostgreSQL data directories, build caches, test key
material, temporary secret files, unrelated generated artifacts.

`scripts/dev_checkpoint.py` scans patch text and the worklog snapshot for:
`AGE-SECRET-KEY-1` markers, PEM private-key headers, credential-shaped
`key=value` / `key: value` assignments (password/secret/token/api key/
master key/…), and high-entropy base64 literals (with known SHA-1/SHA-256
hex constants whitelisted). Findings refuse the run unless explicitly
acknowledged; the acknowledgement is recorded in the manifest.

Honest limits: an automated scan can NEVER guarantee the absence of all
secrets — review archives before delivery. Checkpoint tooling must not
become a new privileged access mechanism; it reads the repository it is
run against and writes only the requested output directory. It never
targets production: no Railway access, no production databases, no real
encryption keys — only synthetic test material.

## 10. Tool failure and bounded retry policy

Repeated tool or sandbox failures must not trigger unlimited retries.

| Failure class | Retry limit | Backoff | Then |
| --- | --- | --- | --- |
| Shell / tool-bridge transient | 3 | 10s → 30s → 60s | report blocker, stop |
| Git network (fetch/clone) | 3 | 30s → 60s → 120s | report blocker |
| pip / package install | 2 | 60s | report missing capability honestly |
| Artifact upload (HTTP 5xx/timeout) | 3 per channel | 30s → 120s | switch channel; after 2 channels report blocker |
| Interrupted test run | 1 rerun | — | investigate before rerunning more |

When the environment remains unavailable: preserve completed work, export
the most recent safe checkpoint, record the error, report the blocker,
STOP unproductive retries, and provide exact resume instructions. Do not
speculate that undocumented platform quotas are established facts. Do not
spend extensive tool calls without creating verifiable progress.

## 11. Environment reconstruction (after sandbox loss)

Standard recipe (non-destructive by default; disposable test resources;
idempotent where practical; no production credentials):

```bash
# 0. Preconditions (record OS-level deps in the worklog):
#    git >= 2.30, Python 3.12, curl; PostgreSQL 16/18 + age ONLY if the
#    task's tests need them. Nothing here touches production.
git clone https://github.com/autonomous-intelligence-commons/commons-core repo
cd repo
git rev-parse HEAD        # MUST equal the worklog/manifest baseline — verify!
git switch -c <branch> <baseline>

# 1. Reapply checkpoint state (bundle previously verified + extracted):
#    gate: repo HEAD == manifest.baseline  (scripts/dev_checkpoint_verify.py
#          bundle.zip --check-baseline-in .)
git am /path/to/extracted/patches/*.patch        # ordered 0001..NNNN
#    gate: HEAD == manifest.head_commit and HEAD^{tree} == manifest.head_tree
#          (scripts/dev_checkpoint_verify.py bundle.zip --verify-applied .)

# 2. Python environment (pinned):
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,postgres]"      # postgres extra only if PG tests run

# 3. Test databases (ONLY if this task needs them — disposable instances):
#    PostgreSQL 16 @127.0.0.1:5432 and/or pinned 18.6 @127.0.0.1:5433,
#    superuser + throwaway credentials for tests only; age v1.3.2 verified
#    against the CI-pinned sha256. See docs/BACKUP.md and the CI workflow
#    for the exact pins. NEVER point tests at production hosts.

# 4. Migrations + smoke:
alembic upgrade head
python -m pytest -q                    # start with the fast SQLite suite

# 5. Resume:
#    follow the recovered worklog §11 "Next exact action required to resume".
```

The gate in step 1 enforces the baseline rule mechanically; a wrong base
refuses to proceed. Every step reports unavailable capabilities instead of
silently skipping. Do not assume previous binaries, PostgreSQL processes,
or temporary directories still exist.

### Windows Git Bash quick guide (owner)

```bash
# Verify the bundle without any repository tooling:
sha256sum aic-checkpoint-*.zip           # compare against the recorded value
unzip -o aic-checkpoint-*.zip -d ckpt    # or: tar -xf (bsdtar), or PowerShell:
#   Expand-Archive -Path aic-checkpoint-*.zip -DestinationPath ckpt
cd ckpt && sha256sum -c CHECKSUMS.sha256

git clone https://github.com/autonomous-intelligence-commons/commons-core repo
cd repo
git checkout <baseline>                  # git rev-parse HEAD must match
git switch -c <branch>
git am ../ckpt/patches/*.patch           # apply in order (0001, 0002, …)
git rev-parse HEAD HEAD^{tree}           # must match MANIFEST.json
cp ../ckpt/WORKLOG.md <your-worklog-path>
```

If `git am` reports a patch already applied, DO NOT force it — re-run
`scripts/dev_resume_check.py` first (§12): the state may already be
recovered. If the repository tooling scripts are needed, they arrive with
the patches themselves (they live in `scripts/` on the branch).

## 12. Resume procedure (standardized checklist)

`scripts/dev_resume_check.py --repo <path> --worklog <file> --checkpoint <bundle>`
automates this:

1. Inspect the current sandbox (repo present? branch? clean/dirty?).
2. Identify surviving worklogs.
3. Identify surviving commits (branch, unpushed state).
4. Locate previous checkpoint bundles.
5. Verify the latest available checkpoint (hashes + manifest + order).
6. Compare checkpoint state with the surviving workspace:
   - commits in the checkpoint missing from the repo → reapply patches;
   - commits in the repo newer than the checkpoint → local work not yet
     exported;
   - both/neither → report precisely.
7. Reconstruct the minimum required environment (§11).
8. Restore verified committed changes (gated `git am`).
9. Account for uncommitted work explicitly (it should not exist — if it
   does, it was never covered by a checkpoint; decide deliberately).
10. Resume ONLY outstanding tasks, following the worklog's
    "next exact action".

Never redo completed research, never reapply already-applied commits,
never overwrite surviving work. If the environment was fully reset,
report what was actually recovered and what remains unavailable.

## 13. Negative cases — the tools fail safe

The synthetic test matrix (`tests/dev_checkpoint/`) proves explicit
failure (never silent acceptance) for: corrupted archive; truncated
(interrupted-creation) archive; missing patch; modified patch; incorrect
baseline (verify gate + apply gate); invalid manifest (malformed JSON /
missing fields / wrong types); SHA-256 mismatch; missing worklog file;
duplicate patch application; dirty work tree; unexpected untracked files;
unsafe output destination; attempted overwrite; missing git / non-repo;
HTML-instead-of-zip retrieval; checkpoint recorded but file absent
(expired artifact); partial reconstruction (patches not yet applied).

## 14. CI integration

A dedicated `checkpoint-tools` job runs the synthetic matrix on every push/
PR: network-free, service-free, pinned dependencies, preserving ALL
existing jobs and checks. The genuine external-delivery drill (§8) is
deliberately NOT part of CI: it requires external network and file-hosting
services whose availability is not CI-guaranteed; it remains a recorded
manual integration test (see the task delivery report). CI never receives
production secrets and never deploys.

## 15. Known limitations (honest boundaries)

- Zip bundles are opaque to incremental diffing; keep patches as the unit
  of review, the zip is only a transport container.
- The secret scan is regex/entropy-based; it cannot guarantee absence of
  all secrets. Manual review before external delivery remains mandatory.
- Temporary file hosts have short and unspecified retention; delivery
  status `OWNER_CONFIRMED_PERSISTENT` exists precisely because upload
  success ≠ durable storage.
- The resume checker verifies structure and commits, not semantic test
  results; recorded test results must be re-run when the environment is
  rebuilt.
- Checkpoint tooling intentionally has NO network capability of its own:
  delivery is performed by the operator/Agent through authorized channels,
  keeping the tools auditable and incapable of exfiltration.

## 16. Quick reference

```bash
# Create (clean tree, versioned name, refuses overwrite/dirt/secrets):
python scripts/dev_checkpoint.py --output-dir /home/z/checkpoints \
    --worklog /path/to/worklog.md \
    --milestone-done "implementation" --milestone-pending "integration tests" \
    --test "python -m pytest -q" --test-result "488 passed" \
    --next-action "run PG16 suite" --dry-run        # inspect first
python scripts/dev_checkpoint.py …                  # real run → prints SHA-256

# Verify (standalone integrity / baseline gate / applied gate):
python scripts/dev_checkpoint_verify.py bundle.zip
python scripts/dev_checkpoint_verify.py bundle.zip --check-baseline-in repo/
python scripts/dev_checkpoint_verify.py bundle.zip --verify-applied repo/

# Resume audit (compare sandbox vs. checkpoint, emit next actions):
python scripts/dev_resume_check.py --repo repo/ --worklog worklog.md \
    --checkpoint bundle.zip --json
```
