# ADR-0011 — P0 Backup & Recovery Security Hardening (R10 / R11 / R12)

**Status:** implemented and synthetically verified (2026-10-19 independent
remediation of the P0 findings from the comprehensive risk review).
Supersedes no prior decision; hardens ADR-0009 Stage 1 in three places.

**Context.** The independent risk review (evidence probes T09/T10/T11/T12,
baseline `96c7285`) confirmed three P0 defects in the Stage 1 backup and
recovery machinery:

* **R10 (T12):** the restore destination guard validated the URL *authority*
  while libpq could be redirected through the *query string*
  (`?host=other.example.invalid` became `PGHOST`) or through inherited
  `PG*` environment variables — the "validated" destination and the
  effective destination were different objects.
* **R11 (T09):** the storage idempotent fast path trusted the commit marker
  alone, so deleting a published artifact and re-running the same backup id
  reported `verified-published` success with no recoverable object.
* **R12 (T10/T11):** publication failures left the staged `.age.part`
  ciphertext on disk; `_drop_database` swallowed every exception, so a
  failed cleanup of a disposable drill database was reported as success;
  subprocess waits were unbounded.

## Decisions

### D1 — One canonical connection contract (R10)

`commons_core/backup/connection.py` introduces
`CanonicalConnection` — a single fail-closed parse of a PostgreSQL URL.
The destination guard, `pg_env_from_url`, every SQLAlchemy engine in the
backup/restore paths, and every subprocess environment derive from that
one parse, so validation and execution cannot diverge.

* Query strings are allowlist-only: destination-affecting libpq parameters
  (`host`, `hostaddr`, `port`, `service`, `servicefile`, `user`, `dbname`,
  `options`, `passfile`, …) are rejected with an explicit error; unknown
  parameters are rejected; duplicates are rejected.
* Hostless URLs (Unix-socket fallback) are rejected — the guard cannot
  validate a socket directory.
* `subprocess_env()` rebuilds the child environment with **every inherited
  `PG*` variable stripped** before the canonical values are set, so
  inherited `PGHOST`/`PGSERVICE`/`PGHOSTADDR` can never redirect a
  `pg_dump`/`pg_restore`.
* The drill and the verification battery run under
  `scrubbed_pg_environ()` so lazy SQLAlchemy/psycopg connections are
  equally controlled on the Python side.
* **Honest boundary:** this fixes *internal consistency*, not network
  isolation. A loopback/private-IP check is not a security boundary; real
  isolation still requires network controls and least-privilege DB roles
  (documented in BACKUP.md §6/§7 and the threat model).

### D2 — The committed set, not the marker, defines success (R11)

`LocalFilesystemStorage.publish()` re-verifies the FULL committed object
set on every idempotent replay: the artifact must exist and hash to the
marker's `artifact_sha256`; the manifest must exist and canonically hash
to the marker's `manifest_sha256`; the marker itself must be structurally
sane (right backup id, `verified-published` state, hash fields present).
Any violation raises the new `PublishedSetCorrupted` — an explicit,
actionable failure. Missing objects are never fabricated; a corrupted id
stays immutable (recovery = new backup id or trusted-copy restore).

Publication mechanics were also hardened: unique unpredictable staging
names with `O_EXCL`, fsync of staged contents, exclusive-create promotion
(`os.link` — fails `EEXIST`; race losers hash-compare: equal → idempotent,
different → hard conflict), and directory fsync after promotion
(best-effort; unsupported platforms documented).

`latest.json` is demoted to what it really is — a rebuildable index: a
failed update prints an explicit warning and can never fail a committed,
verified backup. `run_backup` also honors the marker-vs-`latest` split:
a storage failure to update `latest` after the marker exists is a
warning, not a publication failure.

### D3 — Cleanup is explicit, bounded, and verifiable (R12)

* `run_backup` manages the staged artifact with ONE try/except:
  publication failures remove it too (T10 closed), the removal outcome is
  observed, a cleanup failure on the failure path is appended to the
  raised error (`INCOMPLETE CLEANUP …`), and on the success path it is an
  explicit warning. Cleanup never masks the primary failure.
* `_drop_database` never swallows (T11 closed): it raises the new
  `DrillCleanupError`, and it *re-queries* `pg_database` after `DROP` to
  confirm the database is actually gone before claiming success.
* `DrillReport` gains `cleanup_state`/`cleanup_detail` (in JSON and text
  render). A failed cleanup forces `passed=False` — a drill with
  unhandled residue never claims complete success (review scenario D14)
  — and when a primary failure is already propagating, the cleanup
  failure is chained onto it so both stay visible.
* Subprocess waits are bounded by a total deadline
  (`AIC_BACKUP_SUBPROCESS_DEADLINE`, default 900 s); on expiry the whole
  process group is terminated (SIGTERM → 5 s grace → SIGKILL → 10 s reap
  bound) and the failure is explicit (`DeadlineExceeded`). Both pipeline
  and drill stderr capture are size-bounded (newest 256 lines kept).

## Consequences

* Security invariant restored: the connection validated as safe is
  byte-for-byte the connection used by every destructive or
  state-changing recovery operation.
* Truthful publication: "verified-published" now implies a re-verifiable
  object set, and missing/corrupt sets are loud failures.
* Cleanup observability: every failure path reports what happened to the
  staged artifact and to the disposable database.
* Backward compatible: manifest and marker formats are unchanged; the
  fast-path revalidation only *reads* previously published sets. URLs that
  used non-destination query parameters (e.g. `?sslmode=require`) keep
  working; URLs that used destination overrides were never valid and now
  fail loudly.
* **Documented limitations (not silently claimed):**
  * Windows/directory-fsync and hard-link-free filesystems fall back to
    `os.replace` promotion (atomic, but last-writer-wins between
    concurrent same-id publishers there); POSIX keeps exclusive-create.
  * `SIGKILL` of the whole drill process bypasses `finally`; a TTL
    janitor for orphaned `aic_drill_*` databases remains future
    infrastructure work (review recommendation), tracked in BACKUP.md.
  * Process-group kills require POSIX; on Windows only the direct child
    is terminated.
  * A forged-but-consistent storage state (attacker rewrites artifact,
    manifest AND marker hashes coherently) is still only caught by
    possession of the real age identity at drill time — unchanged
    Stage 1 boundary (R13, out of scope here).

## Verification

52 new regression tests
(`tests/operational/test_p0_hardening.py`): 10-class adversarial matrix
for R10 (query overrides, service files, duplicates, Unix sockets,
inherited `PG*`, malformed URLs, guard/env/SQLAlchemy three-way
equality), 16 publication-truthfulness tests for R11 (missing/truncated/
edited objects, unreadable markers, races, retries, latest-failure
semantics), and 14+ cleanup/fault-injection tests for R12 (staged-artifact
observability, drop failure propagation, cleanup verdicts, deadline
termination of SIGTERM-refusing tools, bounded stderr). Full suite green
against real PostgreSQL 16 and 18.6 with pinned age v1.3.2. See
BACKUP.md §13 for the operator-facing summary.
