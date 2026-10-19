# Backup & Recovery — Stage 1 (ADR-0009)

**Status:** IMPLEMENTED (repository-side tooling + synthetic verification only).
**Production deployment, cloud storage, credentials, and scheduling are NOT
part of Stage 1** — see §12 for the exact boundary and the Stage 2+ plan.

Stage 1 delivers the reviewed, tested foundation required by
[ADR-0009](ADR/0009-encrypted-off-provider-postgres-backup.md): a streaming
encrypted-backup pipeline, a fail-closed restore drill, verified atomic
publication, and a deterministic synthetic test suite that runs in CI
against real PostgreSQL 16 and 18.

Everything below runs on **synthetic data only**. No command in this
document touches the Railway production database.

---

## 1. Scope summary

| Implemented in Stage 1 | NOT implemented (future stages) |
|---|---|
| `scripts/backup_db.py` — streaming `pg_dump → age` CLI | Railway Cron service / scheduling |
| `scripts/restore_drill.py` — content-blind synthetic restore drill | S3-compatible remote storage adapter |
| `commons_core/backup/*` — pipeline, manifest, storage, guards | Real cloud credentials of any kind |
| Local filesystem storage adapter (atomic, verified) | Backup lifecycle / retention deletion |
| Synthetic end-to-end test suite (unit + integration + fault injection) | Production restore command (deliberately nonexistent) |
| CI: backup tests run against PostgreSQL 16 **and** 18 | Deletion/retention reconciliation |
| Manifest with documented consistency semantics | Restore drills against real production data |

## 2. The backup pipeline (corrections 1 & 3)

### 2.1 Streaming — no plaintext dump file ever exists

`pg_dump --format=custom` writes its custom-format output to a **pipe** that
feeds `age -r <RECIPIENT>` directly; only the *encrypted* artifact is
staged on disk (`.age.part`), and only after both subprocesses exit `0` is
it verified and published. The original research PoC wrote a plaintext dump
to disk first — the independent review rejected that, and this is the fix
(also visible in code: `commons_core/backup/pipeline.py::_stream_dump_to_age`).

Process supervision is explicit — never shell-pipeline behavior:

* Credentials reach `pg_dump` via **libpq environment variables**
  (`PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD`) parsed from the URL — no
  secrets ever appear in argv, process listings, or tracebacks.
* Both subprocesses' exit statuses are inspected; **either** failing kills
  the whole pipeline and removes the staged artifact.
* If `age` dies early (bad recipient, disk full), `pg_dump` receives
  SIGPIPE and dies loudly — reported as *downstream exited prematurely*,
  never mistaken for success.
* `pg_dump` producing "some output" is never treated as success: the only
  success indicator is the storage adapter's commit marker (§2.3).
* All captured stderr is scrubbed of URL credentials and 64-hex secrets
  before it can reach any log (`destination.scrub_secrets`).

### 2.2 Local verification — three honest layers

| Layer | Where | What it proves | What it does NOT prove |
|---|---|---|---|
| 1. Structural (`age_format.verify_age_file`) | Backup writer (no key) | The artifact is a well-formed `age-encryption.org/v1` file with an X25519 stanza and non-empty payload | That it is decryptable — the writer holds no identity, by design |
| 2. Storage readback (`storage.LocalFilesystemStorage`) | Publication (each object) | The bytes written to storage match the SHA-256 the pipeline measured | Nothing about ciphertext semantics |
| 3. Cryptographic (`age -d` in the drill) | Restore drill (identity side) | Full AEAD authentication — any bit flip, truncation, wrong identity aborts | Nothing about storage durability |

### 2.3 Verified atomic publication — the commit marker is the only success signal

The storage protocol (`commons_core/backup/storage.py`; any future S3
adapter must implement the same `StorageAdapter` contract):

1. stage under a temporary `*.part` name with mode `0600`;
2. read back and verify SHA-256 (miniature remote-upload verification);
3. **never overwrite**: an existing final object with equal bytes makes
   the publish idempotent (retry-safe); different bytes are a hard
   `BackupConflictError` — backup ids are immutable;
4. promote with `os.replace` — atomic on one POSIX filesystem. **A future
   object-storage adapter must NOT assume rename atomicity**: it must
   copy-then-verify-then-delete a staging key instead;
5. only after BOTH objects are verified, write the commit marker
   `status/<backup_id>.json`. **A backup without a marker is not a
   backup** — the restore drill refuses to touch it.

Transient storage failures are retried (3 attempts, linear backoff). A
crash between steps leaves a recoverable *partial* publication: re-running
with the same id and identical content completes it (idempotent), while a
fresh full-pipeline rerun with the same id is refused (new dump bytes
differ — immutable-id conflict, the safe direction).

## 3. The manifest (correction 2)

`commons_core/backup/manifest.py` — authoritative, non-sensitive. Field
semantics (full docstring in code):

* `backup_id`, timestamps — unique/immutable; wall-clock context, not
  snapshot markers.
* `source.server_version`, `source.database_name`, `alembic_revision` —
  read **before** pg_dump started, from a separate session. They are
  *informational context*, explicitly NOT snapshot-consistent proof. The
  restore drill compares the restored revision against the manifest as a
  **sanity signal only**.
* `artifact.{sha256,size_bytes}` — the authoritative block, computed after
  encryption; fully deterministic for the published bytes.
* `artifact.recipient` — the age **public** recipient (public by design;
  enables key-rotation audits: which key generation can decrypt which
  backup).
* `tools` — exact `--version` output of the binaries actually run.

**Row counts are deliberately absent.** The research PoC stored
independent count queries in the manifest; the review established that
counts taken before/after `pg_dump` read different MVCC snapshots and
prove nothing about the dump. Stage 1 validates restored data against
**controlled synthetic expectations** produced by the source fixture
(`tests/operational/backup_synth.py`) — a separate concern from backup
metadata, by design. If snapshot-consistent counts are ever genuinely
needed, the sanctioned path is a PostgreSQL snapshot-sharing design — to
be reviewed separately.

Never in any manifest: Agent memory, message content, URLs, passwords,
key material (beyond the public recipient).

## 4. The restore drill (restore safety)

`scripts/restore_drill.py` / `commons_core/backup/restore.py` — the ONLY
supported restore path in Stage 1. **There is no production restore
command, by decision.**

Fail-closed gates, in order:

1. **Destination guard** (`destination.validate_restore_destination`):
   * hosts matching `railway|rlwy` are refused outright (no override);
   * hosts must be loopback/RFC1918 private unless
     `--allow-nonlocal-destination` is explicitly passed (recorded in the
     report; review it manually);
   * the database name must match the disposable-database pattern
     (`aic_test_*`, `aic_drill_*`, `aic_backup_*`, …) or be explicitly
     allowlisted with `--allow-db NAME` — the "explicit test-environment
     allowlisting" the review required, layered on top of the host checks;
   * URLs are never echoed — errors use `describe_url`, which drops the
     username, password, and query string entirely (scheme/host/port/
     database only, under both `postgresql://` and `postgresql+psycopg://`
     spellings); tool stderr is scrubbed of the run's libpq username and
     password before it can appear in any failure detail.
   * **Heuristic limitations (documented honestly):** a private-IP
     production database would pass the host check — the disposable-NAME
     allowlist is the real gate; host heuristics are defense-in-depth,
     not proof. Python's `ipaddress` treats some reserved ranges
     (e.g. `203.0.113.0/24`) as private — documented behavior, verified
     in tests.
2. The drill creates a **uniquely named** disposable database
   (`aic_drill_<hex>`), refusing to reuse an existing one, and drops it on
   **every** path (success, failure, crash) unless `--keep-database`
   (recorded in the report as an override needing manual cleanup).
3. A missing commit marker, unreadable manifest, artifact/manifest hash
   mismatch, manifest/marker canonical-hash mismatch, or incompatible
   client/server versions each abort before anything is restored.
4. Decryption streams `age -d` stdout straight into `pg_restore` stdin —
   **the plaintext dump never lands on disk on the restore side either**.
   Connection parameters reach `pg_restore` via libpq env vars; argv
   carries only the public database name.
5. A non-zero `pg_restore` exit is a failure even if objects were
   created — they are destroyed with the dropped database.
6. The application battery (below) must pass for the drill to report PASS.

### 4.1 Application-level battery (the four required demonstrations)

Run with `--with-application-keys` (keys come from the environment —
`AIC_MASTER_KEYS`/`AIC_ACTIVE_KEY_ID`, the same fail-closed variables the
API uses; never from argv) and `--expected-facts` (the synthetic
expectation JSON):

1. **Restoring the database alone does not recover encrypted content** —
   restored rows come back as AES-256-GCM envelopes (ciphertext present,
   plaintext absent); without master keys the drill FAILS loudly
   ("content recovery is unproven") rather than silently passing.
2. **The correct application master key decrypts memory chains,
   messages, activity summaries, and governance records byte-for-byte.**
3. **A wrong application master key fails** (the battery itself probes a
   random wrong key against a sample envelope and requires rejection).
4. **A tampered envelope (one flipped ciphertext character) is rejected
   by the AEAD tag.**

The report is machine-readable JSON (`--report-json`) and prints only
labels, counts, and PASS/FAIL — never content, never credentials.

## 5. Key handling (three separate domains — never mixed)

| Domain | Material | Backup writer gets | Restore drill gets |
|---|---|---|---|
| **Backup encryption** | age X25519 identity (`AGE-SECRET-KEY-1…`) | ❌ never | ✅ via explicit `--identity-file` (offline custody in production; synthetic runtime identity in Stage 1 tests) |
| | age recipient (`age1…`, public) | ✅ (that is all it needs) | — |
| **Application content** | `AIC_MASTER_KEYS` (AES-256 kids) | ❌ never — the dump already contains application ciphertext | ✅ optionally, from the environment, for battery checks |
| **Database** | DB URL/password | ✅ (via env) | ✅ (via env) |

* The backup pipeline **refuses** `AGE-SECRET-KEY-*` strings as recipients
  (preflight) and never reads `AIC_MASTER_KEYS`.
* Backup encryption deliberately does **not** reuse `AIC_MASTER_KEYS`
  (separate compromise/rotation lifecycles, per the review).
* Key rotation (documented procedure, to be executed on demand):
  generate a new identity offline → encrypt to **both** recipients for a
  transition window ≥ the retention horizon → drop the old recipient →
  re-encrypt archived backups only if compromise is suspected. Multiple
  retained key generations are supported by the manifest's recorded
  recipient.
* **What loss means, stated honestly:** losing the age identity makes
  every existing backup permanently undecryptable (mitigated by
  two-copy offline escrow — Stage 2 custody work, not done here); losing
  `AIC_MASTER_KEYS` leaves dumps restorable but content unreadable (the
  existing `RAILWAY.md` §6 offline-key procedure remains a hard
  precondition of the whole design).

**Stage 2 (ADR-0010) — custody is now specified and tool-supported.**
The "two-copy offline escrow" mitigation above is no longer future
work: age identities and the `AIC_MASTER_KEYS` ring are packaged into
versioned offline **recovery packages** (public metadata JSON + an
age-encrypted secrets section), duplicated to two byte-identical
offline copies, and verified by content-blind tooling — see
[ADR-0010](ADR/0010-offline-recovery-key-custody.md) and the
[RECOVERY_KEYS.md](RECOVERY_KEYS.md) runbook for the custody
architecture, the owner-side offline procedures, and the honest
consequences table (both copies lost ⇒ that generation's secrets are
gone).

**Dual-recipient backups are implemented (Stage 2).**
`run_backup(..., extra_recipients=[...])` encrypts each archive to the
primary recipient *and* the extra public recipients, recording an
additive `artifact["recipients"]` list in the manifest
(`schema_version` stays 1; single-recipient manifests keep their exact
Stage 1 meaning; `recipient` remains the primary). The rotation
procedure above is therefore executable as a dual-recipient
**transition window**: run the window with the old recipient as
primary and the new one as an extra for ≥ the retention horizon, then
switch — full procedure (including successor custodians) in
[RECOVERY_KEYS.md](RECOVERY_KEYS.md) §11.

## 6. Storage roles & threat model (correction 4)

### 6.1 Role separation (design; Stage 3 enforces it in cloud IAM)

| Role | May do | May never do |
|---|---|---|
| **Backup writer** (the future cron job) | write+read its own staging keys, write final keys, write commit markers | read other paths, delete/overwrite existing final objects (conflicts are hard errors in the adapter), read application secrets |
| **Verification reader** (drill runner / watchdog) | read final objects + markers | write/delete anything |
| **Retention administrator** (owner-gated, future stage) | delete expired objects per reviewed policy | read plaintext it cannot decrypt anyway; bypass review |

* The Stage 1 filesystem adapter models this: `0700` directories, `0600`
  files, no delete path for final objects, conflicts refused.
* **A future S3 adapter must use a bucket policy scoped to one prefix,
  object versioning ON where supported, and a separate retention-lock
  review.** Object versioning defeats malicious overwrite; retention
  locks (provider-dependent) defeat deletion. Stage 3 makes those
  choices; Stage 1 only documents them.
* The production API and worker never receive storage credentials
  (ADR-0009 least-privilege invariant).

### 6.2 Threat model — compromised backup worker / storage credentials

| Attacker holds | Can do | Cannot do |
|---|---|---|
| Backup job (RCE) | re-dump the database to its own age key (exfiltration of fresh dumps is bounded by the DB role), corrupt new backups, halt backups | decrypt any existing backup (no identity), touch API/worker (separate services), reach offline keys |
| Storage write key | overwrite/corrupt backup objects | decrypt them; forge a passing drill (AEAD + manifest/marker hashes); write past the versioning planned for Stage 3 |
| Storage account (full) | read ciphertext + sizes/timestamps | decrypt; alter content undetectably (manifest sha chain lives in commit markers; drill verifies both) |
| age identity (offline, worst case) | decrypt all backups of that key generation | be present anywhere in the deployment (rotation bounds forward exposure) |

**Honest limits:** a compromised backup worker can exfiltrate a fresh
plaintext dump of whatever its DB role can SELECT — the database password
is the real boundary, which is why the backup role is SELECT-only (§7)
and why Stage 3 pins storage-versioning. Making backups immutable does
NOT solve data deletion — see §8.

## 7. The PostgreSQL backup role (least privilege)

Designed and **tested** privilege set (see
`TestBackupRolePrivileges` in `tests/operational/test_backup_pipeline.py`):

```sql
CREATE ROLE aic_backup_reader LOGIN;            -- no superuser, ever
GRANT CONNECT ON DATABASE <db> TO aic_backup_reader;
GRANT USAGE  ON SCHEMA public TO aic_backup_reader;
GRANT SELECT ON ALL TABLES    IN SCHEMA public TO aic_backup_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO aic_backup_reader;  -- pg_dump reads sequence state
ALTER DEFAULT PRIVILEGES FOR ROLE <app_owner> IN SCHEMA public
    GRANT SELECT ON TABLES TO aic_backup_reader;                        -- future migrations stay readable
```

* Verified sufficient for a full `-Fc` dump + restore drill against
  PostgreSQL 16.2 and 18.6 (the same tests prove the *negative*: a role
  missing table SELECT grants fails the pipeline and publishes nothing).
* **PostgreSQL 18 notes:** no 18-specific grants were needed beyond the
  15+ `public` schema USAGE requirement (already required since PG 15).
  pg_dump needs no table-level privilege beyond SELECT for AIC's schema:
  no extensions, no large objects, no row-level security policies, no
  event triggers exist in the database (audited). If any of those
  appear, this section must be re-reviewed (e.g. RLS policies can make
  rows invisible to a dump — logical backups then silently omit them).
* **Documented omission risk:** a role without access to some table
  cannot dump it; the pipeline fails loudly rather than producing a
  partial backup that omits data silently (tested).
* This role must be created in production only in Stage 3, after review.

## 8. Deletion & retention compatibility (designed, NOT implemented)

AIC is currently append-only (no tombstones, no erasure protocol — PR #8's
research digest demands future deletion-then-restore drills). Stage 1
implements **no deletion rules** (per the review) but the boundary is
designed:

* **What is implemented now:** backups expire only by future
  retention-window cleanup; nothing in Stage 1 deletes anything, ever.
* **What is only designed:** a `deletion_markers` ledger recorded when
  deletion features arrive; a restore-reconciliation step replaying that
  ledger over a restored database before the app boots; retention
  windows as the honest bound on "resurrection" of old rows.
* **What cannot yet be guaranteed:** ciphertext inside archived AEAD
  backups cannot be selectively rewritten without breaking the tag —
  **verifiable deletion beyond the retention horizon is impossible while
  any encrypted backup of that era exists.** At best, "cryptographic
  erasure" means destroying the keys that would decrypt those artifacts.
  Any stronger claim is dishonest; the restore drill battery surfaces
  this honestly (restored old backups re-expose old state, including
  previously superseded rows).

## 9. Test coverage & CI

`tests/operational/test_backup_pipeline.py` — 54 tests:

* **Unit (no tools needed):** manifest semantics (no row counts!),
  destination guard, storage protocol (marker-last, conflict, idempotent
  completion, upload-failure injection, restrictive permissions, no
  `.part` leftovers), age structural validation (hand-crafted valid +
  rejected forms), secret scrubbing, Git hygiene (no tracked private
  keys; artifacts git-ignored).
* **Integration (real age + real PostgreSQL — mocking is explicitly
  insufficient here):** the full backup → publish → drill roundtrip on
  **synthetic data through the application's real service functions**
  (real AES-256-GCM envelopes); the four application-encryption
  demonstrations; pg_dump connection failure; partial output then
  nonzero exit; age failure (invalid recipient); age premature exit →
  SIGPIPE; truncated artifact; corrupted ciphertext **even after
  forging manifest AND marker hashes** (AEAD is the last gate); wrong
  identity; missing/corrupted manifest; manifest↔marker↔artifact
  mismatches; partial publication (no marker); conflicting backup id;
  idempotent retry semantics; insufficient backup-role privileges
  (fail-closed); least-privilege role success; Railway/public-host/
  production-name destination refusals; explicit allowlist override;
  no plaintext files anywhere; no credentials in CLI output.

`tests/operational/test_recovery_keys.py` — 38 tests (ADR-0010,
Stage 2): the recovery-key **continuity scenarios 1–11** (single-copy
loss, both-copy loss, custody-identity loss, K1→K2 continuity,
recipient rotation, …), package mechanics (create/verify/unlock/
export/duplicate roundtrip, hygiene, trust anchor, tamper/truncate/
swap rejection, copy drift, overwrite & unsafe-directory refusal,
read-only storage, interrupted creation, stateless verify, wrong
identities, rotation readiness), CLI exit codes, and secret-leak
scans (no key material in argv, output, or logs).

`tests/operational/test_recovery_dr.py` — the synthetic
**disaster-recovery exercise** (ADR-0010, Stage 2): 16 steps on real
PostgreSQL 18.6 + age — total loss of the source database, then
recovery from two offline package copies + the moved-out custody
identity + the vault, proving BOTH key generations' memory chains
decrypt byte-for-byte with the recovered ring — plus the
**rotation-archives suite**: dual-recipient transition-window backups
decrypt with either identity, the retired recipient disappears from
future backups only, and the retained old-recipient archive still
drills green.

CI (`.github/workflows/ci.yml`):

* The six existing required checks are untouched.
* The `postgres` (16) job additionally downloads pinned age v1.3.2 and
  sets the backup test environment, running the whole suite against a
  real PostgreSQL 16 service container.
* A new `backup-postgres18` job runs the backup suite against a
  `postgres:18.6-alpine` service container with a checksum-pinned
  PGDG 18.6 client — the review's PostgreSQL 18 requirement. The job
  fails loudly if any mandatory backup verification fails; there is no
  silent skip and no SQLite fallback.
* Stage 2 (ADR-0010): the recovery suites also run in CI —
  `test_recovery_keys.py` and `test_recovery_dr.py` both run in the
  `backup-postgres18` job (same pinned age + PG 18.6 environment), and
  the `test_recovery_keys.py` unit suite also runs in the SQLite
  `test` job with the pinned age binaries.
* Tool pinning: age v1.3.2 from the official release tarball with its
  SHA-256 pinned (verified byte-identical to the official asset);
  `age --version` is asserted in the job; client tools are version-
  pinned packages; server images are exact tags.
* No secrets, no production access, disposable instances only, per the
  CI security posture documented at the top of the workflow file.

## 10. CLI usage (synthetic environments only in Stage 1)

```bash
# One backup (environment fallbacks: AIC_BACKUP_DATABASE_URL,
# AIC_BACKUP_RECIPIENT, AIC_BACKUP_STORAGE_DIR):
python scripts/backup_db.py \
  --database-url postgresql+psycopg://commons:pw@127.0.0.1:5432/aic_test_x \
  --recipient age1... \
  --storage-dir ./backup-root [--json]

# One restore drill (content-blind, disposable target, auto-cleanup):
python scripts/restore_drill.py \
  --server-url postgresql+psycopg://commons:pw@127.0.0.1:5432/postgres \
  --backup-id aic-20261019T061234Z-ab12cd34 \
  --identity-file /run/generated/synthetic-identity.txt \
  --storage-dir ./backup-root \
  [--expected-facts expected.json] [--with-application-keys] \
  [--report-json report.json]
```

Exit codes (both CLIs): `0` success/PASS, `1` pipeline/drill failure
(nothing published / target still cleaned up), `2` configuration error.

## 11. RPO/RTO honesty

Daily RPO 24 h and RTO ≤ 1 h remain **targets, not measurements**. The
mechanical restore path is proven on synthetic data end-to-end in CI
(~24 s for the whole 54-test suite locally, single drill ≪ 1 min);
Stage 4's real-data drill against production-identical infrastructure
is where the RTO claim would first become a measurement.

## 12. Stage 2+ dependencies (future, separately authorized)

1. **Stage 2 — custody: DONE (ADR-0010).** Offline identity generation,
   two-copy offline escrow (byte-identical, `--compare-dir`-verified),
   custody-identity distribution/storage rules, and `AIC_MASTER_KEYS`
   offline-copy packaging + verification are implemented,
   tool-supported, and synthetically CI-proven — see
   [ADR-0010](ADR/0010-offline-recovery-key-custody.md) and
   [RECOVERY_KEYS.md](RECOVERY_KEYS.md). What remains is the
   off-provider **automated copy** of backup archives (Stage 3 storage
   work, item 2 below).
2. **Stage 3 — storage + scheduling:** choose B2/R2, dedicated bucket,
   write-scoped app key, object versioning; S3 `StorageAdapter`
   implementing §2.3's protocol (copy-verify-delete staging, since
   object-storage rename is NOT atomic); Railway Cron service
   `commons-backup` (`0 2 * * *` UTC, no domain, non-root, pinned image).
3. **Stage 4 — real-data drill** on owner hardware (throwaway PG ≥ 18).
4. **Stage 5 — reliability:** bucket lifecycle rules, external stale
   check on `status/latest.json`, automated monthly drill cadence.
5. **Stage 6 — deletion integration** (depends on future deletion
   features; see §8).

Nothing in Stage 1 provisions any of these resources, holds any real
credential, or alters Railway in any way.
