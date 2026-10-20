# ADR-0011 — P0 Backup & Recovery Security Hardening (R10 / R11 / R12)

**Status:** implemented and synthetically verified (2026-10-19 independent
remediation of the P0 findings from the comprehensive risk review);
**amended 2026-10-20** by the Stage 2 + P0 integration residual security
review (§8 process-environment mutation, §9 atomic publication fallback).
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
* In-process connections (SQLAlchemy/psycopg) use **explicit connection
  parameters plus a fail-closed inherited-environment audit** (residual
  review §8 amendment; replaces the former process-global
  `scrubbed_pg_environ()` mutation of `os.environ`):
  * the canonical URL pins `host`/`port`/`database` (mandatory) and
    `user`/`password`/allowlisted query parameters when present — conninfo
    always beats the environment for slots it sets, so inherited
    `PGHOST`/`PGPORT`/`PGDATABASE` (and `PGUSER`/`PGPASSWORD` when the URL
    carries them) are provably inert;
  * `audit_in_process_environment()` refuses, before any connection, the
    variables the URL can never override — `PGHOSTADDR`, `PGSERVICE`,
    `PGSERVICEFILE`, `PGTARGETSESSIONATTRS`, `PGREQUIRESSL`,
    `PGSSLMINPROTOCOLVERSION`, `PGSSLMAXPROTOCOLVERSION`,
    `PGCHANNELBINDING` unconditionally (a service file or `PGHOSTADDR`
    redirects TCP even when the URL names a host); `PGPASSWORD`/
    `PGPASSFILE` only when the URL carries no password; `PGUSER` only
    when the URL carries no user; TLS trust material
    (`PGSSLCERT`/`PGSSLKEY`/`PGSSLPASSWORD`/`PGSSLROOTCERT`/`PGSSLCRL`/
    `PGSSLCRLDIR`) only when the URL does not pin the matching parameter;
  * `psycopg_connect_kwargs()` pins `sslmode` to libpq's documented
    default (`prefer`) when the URL does not set it, so an inherited
    `PGSSLMODE` can never downgrade the transport;
  * no process-global state is ever mutated — concurrent in-process
    connections from multiple threads are safe by construction (the
    former scrub was not thread-safe). Behavioural, non-security
    parameters (`PGAPPNAME`, `PGCONNECT_TIMEOUT`, `PGOPTIONS`,
    `PGCLIENTENCODING`, `PGDATESTYLE`, `PGTZ`, keepalives) may still be
    inherited; they cannot change the destination, credentials, identity
    or transport security mode.
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

**Residual review §9 amendment — no destructive fallback:** where hard
links are unavailable (`EPERM`/`EXDEV`/`ENOSYS`; exFAT/FAT32, some network
mounts) publication now **fails safely** with `PublicationUnsupportedError`
instead of falling back to `os.replace` (atomic but last-writer-wins — it
could silently overwrite an already-committed backup). A per-directory
capability probe (`_verify_publication_support`) runs after the read-only
idempotent fast path and before anything is staged, so an unsupported
filesystem aborts publication up front with operator-facing guidance;
the error is terminal (never retried). Supported storage filesystems are
those with hard-link support — ext4/xfs/btrfs/zfs/APFS/NTFS. `latest.json`
remains the one deliberate `os.replace` user: it is a rebuildable index,
not a committed object.

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
  * Storage filesystems without hard-link support (exFAT/FAT32, some
    network mounts) cannot publish NEW backups at all — they fail safely
    with `PublicationUnsupportedError` before anything is staged (§9
    amendment). Read-only idempotent replays of already-committed sets
    still work anywhere.
  * `SIGKILL` of the whole drill process bypasses `finally`; a TTL
    janitor for orphaned `aic_drill_*` databases remains future
    infrastructure work (review recommendation), tracked in BACKUP.md.
  * Process-group kills require POSIX; on Windows only the direct child
    is terminated.
  * In-process connections may still inherit purely behavioural `PG*`
    variables (application name, connect timeout, options, encodings,
    date style, timezone, keepalives) — documented, not destination-,
    credential- or transport-relevant (§8 amendment).
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

The 2026-10-20 residual review added: the §8 in-process environment
matrix (refusal rules, kwargs pinning, no-mutation proof at engine
construction, three live-PostgreSQL tests including two-thread
concurrent connections under hostile pinnable environment) and the §9
fail-safe publication matrix (probe refusal leaves nothing staged,
committed sets byte-identical under link-less republication, read-only
replay without links, mid-publish promotion failure cleans staging,
non-retried capability error, end-to-end `run_backup` surfacing).
