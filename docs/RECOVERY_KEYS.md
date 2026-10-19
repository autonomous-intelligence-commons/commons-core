# RECOVERY_KEYS.md — Offline Recovery-Key Custody Runbook (Stage 2)

**Status:** implemented and synthetically verified (ADR-0010, Backup & Recovery
Stage 2). This runbook covers repository-side tooling and owner-side custody
procedures. Everything in the test suite runs on **synthetic** keys; the
"REAL generation" sections below are the only place where production secrets
are ever involved, and they are performed **offline by the project owner** —
never in the AI development environment, never in CI, never on Railway.

**The central question Stage 2 answers:** can AIC recover its encrypted
persisted state after losing access to its original infrastructure, without
exposing private recovery secrets through source code, CI, logs, cloud
services, or Agent activity? The synthetic disaster-recovery exercise
(`tests/operational/test_recovery_dr.py`) proves the mechanical answer end
to end; the custody procedures below turn that mechanics into an operational
capability that survives the loss of a provider, a device, or a single
recovery copy.

---

## 1. The two key domains — never mixed

| | Backup domain | Application domain |
|---|---|---|
| Material | age identities (`AGE-SECRET-KEY-1…`, private) + recipients (`age1…`, public) | `AIC_MASTER_KEYS` key ring (`kid:<64 hex>,…`) + `AIC_ACTIVE_KEY_ID` |
| Protects | Encrypted `pg_dump` archives | Memory chains, messages, activity summaries, governance records (AES-256-GCM envelopes) |
| Who needs it | Restore drills / real restores | The API service at runtime; restore verification |
| Who must NEVER receive the other's material | The backup writer (holds only public recipients) | The backup writer (never receives master keys — true today, preserved by Stage 2) |

Never reuse a backup decryption identity as an application master key or
vice versa. The recovery package keeps the two domains in **separate
sections** precisely so they can be rotated, audited, and if ever necessary
destroyed independently.

## 2. Custody architecture (ADR-0010 in one page)

* **Root of trust: custody identities.** Every recovery package's secret
  section is age-encrypted to one or more dedicated *custody recipients* —
  public keys whose private identities are generated offline and stored
  independently of the package copies. This is deliberately symmetric with
  the backup layer: one well-understood tool (age) protects both.
* **Two byte-identical package copies**, stored in two independent offline
  locations (e.g., two USB sticks in different places, or one USB stick plus
  an offline password-manager export). Losing one copy must not impair
  recovery — `--compare-dir` proves the copies match.
* **Custody identity stored separately** from both package copies. A package
  copy without the custody identity is inert ciphertext; the custody
  identity without a package copy has nothing to decrypt. An attacker needs
  BOTH, which is why they never travel or rest together.
* **Trust anchor: an independently recorded SHA-256** (`custody_manifest_sha256`,
  printed at creation). Record it in at least two places that an attacker who
  steals a copy cannot also rewrite (e.g., a printed inventory card in a
  drawer + a note in your offline password manager). A hash recorded *next
  to* a copy defends nothing.
* **Rejected alternatives** (justified in ADR-0010): passphrase/scrypt mode
  (couples every test and Windows run to terminal emulation), threshold
  secret sharing (unjustified complexity for a single custodian in Phase 1),
  hardware tokens (purchase + plugin supply chain, deferred), GPG signing
  (new dependency, same key-protection problem it claims to solve).

## 3. Package anatomy

A package generation `gen-<timestamp>-<hex8>` is two files:

| File | Contents | Sensitivity |
|---|---|---|
| `<gen>.recovery-metadata.json` | Format version, creation time, generation id, custody + packaged *public* recipients, identity fingerprints, application key **ids** (never material), compatibility requirements (age pin, pg client ≥ 18, Python ≥ 3.12), integrity hashes, trust anchor | **Public** — safe to display, never commit |
| `<gen>.recovery-secrets.age` | JSON payload with the private age identities and the `AIC_MASTER_KEYS` ring, age-encrypted to the custody recipients | **SECRET** — the entire point of the design |

Integrity is layered: age AEAD (tamper-evident decryption), file-level
SHA-256 in the metadata (detects corruption/swap before decryption),
plaintext-level `payload_sha256` (detects a secrets file swapped in from
another package even when the same custody identity can decrypt it), and
the recorded trust anchor (detects wholesale replacement). The verifier
(`scripts/recovery_key_verify.py`) reports each check PASS/FAIL and **never
prints secret material**.

**Authenticity honesty:** none of these hashes alone proves a package is
genuine — an attacker who can replace a copy can replace the hashes stored
with it. Authenticity comes from the *independently recorded*
`custody_manifest_sha256` plus the custody identity being needed for
decryption. The verifier says this loudly whenever `--expected-sha256` is
not supplied.

## 4. Preparing a trusted offline environment

* Use a computer you control, **offline** during generation, with no
  cloud-sync folder anywhere in the output path (the tool refuses known
  sync-folder names — do not defeat that check).
* Windows: a standard (non-administrator) account is fine; Git Bash or
  PowerShell both work. The tool needs Python 3.12+ and the pinned age
  v1.3.2 binaries (same tarball the CI verifies, sha256
  `cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10`).
* Honest limits: a compromised machine defeats any software-only custody.
  If the generation machine is suspected compromised, generate on a
  different, cleaner machine.

## 5. Recognizing public vs private material

| Looks like | Meaning | Rule |
|---|---|---|
| `age1…` (lowercase, ~62 chars) | Public recipient | Safe to store in Railway variables, print, put in manifests |
| `AGE-SECRET-KEY-1…` (uppercase+digits) | Private age identity | Never commit, never paste into chats/CI/AI tools, never leave in a package directory (the verifier fails while it does) |
| `<kid>:<64 hex chars>` | Private application master key material | Same rules as a private identity |
| `<kid>` alone (e.g. `k2`) | Key identifier | Public — appears in envelopes and package metadata |

## 6. REAL generation (owner-authorized, offline only)

> **Boundary:** the AI assistant never performs this section. It is the
> owner's explicit, human-authorized action with real production secrets.

1. With the API service's real `AIC_MASTER_KEYS` and `AIC_ACTIVE_KEY_ID`
   available **in-session only** (PowerShell: `$env:AIC_MASTER_KEYS="…"`
   — avoids permanent shell history; Git Bash: `export` in a session you
   close afterwards; or use a temporary env file you delete immediately):
2. `python scripts/recovery_key_init.py --out-dir E:\\aic-recovery\\primary
   --generate-age-identity --with-application-keys --app-commit <release>`
3. Read the output: it prints the **custody identity file** path, the
   `custody_manifest_sha256`, and the public recipient. **Move the custody
   identity file to its own offline storage NOW** (the verifier refuses the
   package until you do) and record the SHA-256 in two independent places.
4. Configure the backup writer with the printed public recipient
   (`AIC_BACKUP_RECIPIENT` on the backup workload — public, safe).
5. `python scripts/recovery_key_init.py --duplicate-from
   E:\\aic-recovery\\primary --out-dir F:\\aic-recovery\\secondary`
   (different physical medium/location!)
6. `python scripts/recovery_key_verify.py --package-dir <primary>
   --compare-dir <secondary> --identity-file <custody identity>
   --check-decrypt --expected-sha256 <recorded sha256>`
7. Fill the recovery inventory (§7).

Windows permission honesty: `chmod 600` does nothing on NTFS. The tool
prints a warning on Windows. To restrict a directory to your user:
`icacls "E:\\aic-recovery" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"`.

## 7. The recovery inventory

Keep this table on paper (and optionally in your offline password manager —
never in the repository):

| Field | Example |
|---|---|
| Generation | `gen-20261019T103821Z-e6617dc0` |
| Created | 2026-10-19 |
| `custody_manifest_sha256` | `3dde1a5c…` (also on a printed card) |
| Custody identity location | Sealed envelope, home safe |
| Primary copy location | USB stick A, office drawer |
| Secondary copy location | USB stick B, bank deposit box |
| Packaged recipients | `age1…` (public) |
| Application key ids / active | `k1,k2` / `k2` |
| Last verified | 2027-01-02 (both copies PASS) |

## 8. Periodic verification (quarterly, or after any rotation)

Run §6 step 6 plus `--rotation-check <latest backup manifest.json>` — this
proves the package can still decrypt the *newest* backup. Failures mean:

* `secrets-sha256` FAIL → that copy is corrupted; re-duplicate from the
  other copy (§9), then destroy the corrupted one.
* `copy-identical` FAIL → the copies drifted; decide which is authoritative
  (compare against the recorded SHA-256), re-duplicate, destroy the bad one.
* `trust-anchor-sha256` FAIL with `--expected-sha256` → the package is not
  the one you recorded — treat as a security incident (§12).
* `rotation-ready` FAIL → your newest backup is encrypted for a recipient
  this package does not hold; package a new generation immediately (§11).

## 9. Missing or corrupted copies

One copy lost/corrupted ⇒ recover from the survivor: duplicate to a fresh
location, verify, update the inventory, destroy the damaged medium.
**All usable copies lost, or the custody identity lost ⇒ that generation's
secrets are gone** — this fails safely (the verifier says so explicitly)
and is exactly why two copies plus independently stored custody identities
are mandatory. Consequences by material type are in §13.

## 10. Application key rotation (K1 → K2)

1. Generate a new 32-byte key (`kid` e.g. `k2`); append to
   `AIC_MASTER_KEYS`; set `AIC_ACTIVE_KEY_ID=k2`; deploy. New content seals
   under `k2`; **historical ciphertext is NOT re-encrypted** (documented
   limitation — old records keep `kid: k1` forever until a future
   re-encryption stage exists).
2. Create the new package generation containing BOTH keys (§6).
3. Retire `k1` only when (a) no envelope references it — check with a
   content-blind count per key id, e.g.
   `SELECT envelope->>'kid' AS kid, count(*) FROM memory_entries GROUP BY 1;`
   (repeat for messages, activities, governance_records) — and (b) every
   retained backup whose restore you care about has been re-verified with
   the post-retirement key ring.
4. Only then remove `k1` from `AIC_MASTER_KEYS`, create the next package
   generation without it, and destroy the generation that carried it after
   its retention horizon. Rotation is not deletion (§14).

## 11. Backup recipient rotation (and dual-recipient windows)

1. Generate the next operational identity offline and package it (§6).
2. Transition window: run backups with `--recipient <old>` plus the new
   recipient via the pipeline's `extra_recipients` (e.g.
   `run_backup(..., extra_recipients=[<new>])`) so every backup in the
   window decrypts with EITHER identity. Keep the window ≥ your retention
   horizon.
3. Then switch to the new recipient alone. **Rotation never retroactively
   revokes old archives** — they stay decryptable by their own identities
   for as long as retention keeps them. Never destroy an identity while a
   retained backup still depends on it; `--rotation-check` proves coverage.
4. Emergency-custodian case: `--custody-recipient age1…` (public key of a
   successor custodian) packages secrets they can unlock without you —
   use deliberately, record in the inventory.

## 12. Key compromise playbooks

* **Application master key compromised:** rotate (§10). Honest limitation:
  ciphertext already sealed under the compromised key remains readable to
  the attacker until a re-encryption capability exists (future stage);
  containment = rotate + revoke old key from the ring + fresh package
  generation + incident record.
* **Custody identity compromised (or suspected):** generate a NEW custody
  identity, re-create the package (new generation, encrypted to the new
  custody), verify, distribute copies, then securely destroy the OLD
  package copies and the old custody identity file (§14). Old-generation
  ciphertext readable by the thief remains a residual risk — record it.
* **Operational (backup) identity compromised:** rotate recipients (§11);
  re-encrypt retained archives to the new recipient only if the exposure
  justifies the operational cost — decide explicitly, write it down.

## 13. Lost-key consequences (honest table)

| Lost material | Consequence | Prevention |
|---|---|---|
| Custody identity | All package copies of that generation become undecryptable | 2+ independent custody-identity copies (sealed printouts count) |
| Both package copies | Secrets (identities + master keys) unrecoverable | Two copies, independent locations, quarterly verification |
| Retired key `k1` while k1-ciphertext exists | That content is permanently unreadable | Guard-count query (§10.3) before retirement |
| Operational identity | Every retained backup sealed only to it is undecryptable | Dual-recipient windows + `--rotation-check` + retention-aware identity destruction |

## 14. Retired-key handling & future verifiable deletion

Retired ≠ destroyed. Keep retired material in the package generation that
carries it until its retention horizon lapses, then destroy **all** copies
(package copies, custody identity, printed records) and update the
inventory. Stage 2 keeps AIC's append-only storage semantics; a future
verifiable-deletion stage must treat backup archives and custody copies as
explicit deletion targets — this runbook's inventory is the starting point
for that work.

## 15. Restoring outside Railway (the actual disaster runbook)

Prerequisites: one verified package copy + the custody identity + the
recorded `custody_manifest_sha256`; a recovery machine with age v1.3.2,
PostgreSQL client ≥ the source server major (18 for Railway's PG 18.6),
Python 3.12+, and the repo at a compatible release.

1. `python scripts/recovery_key_verify.py --package-dir <copy>
   --identity-file <custody identity> --check-decrypt --expected-sha256 <recorded>`
2. `python scripts/recovery_key_verify.py … --export-identity operational:<file>
   --export-env-file <env file>` → you now hold the age identity file and
   the `AIC_MASTER_KEYS`/`AIC_ACTIVE_KEY_ID` values needed to read restored
   application content.
3. Drill on disposable infrastructure first (Stage 1 drill, content-blind):
   `python scripts/restore_drill.py --server-url <maintenance url>
   --backup-id <id> --identity-file <exported> --storage-dir <vault copy>
   --with-application-keys` (env from step 2).
4. A REAL production restore (new provider, new database) is **manual,
   owner-authorized work** — the repository intentionally provides no
   automated production restore (Stage 1 boundary, unchanged by Stage 2).

## 16. Threat model (Stage 2 spec §4 — seventeen scenarios)

CIAA legend: **C**onfidentiality, **I**ntegrity, **A**vailability,
**Au**thenticity.

| # | Scenario | Primary harm | Prevention | Detection | Recovery | Residual risk |
|---|---|---|---|---|---|---|
| 1 | Total loss of the Railway project | A (everything) | Off-provider package + vault copies | Inventory check | §15 full recovery | RPO gap ≤ backup interval |
| 2 | Loss of the PostgreSQL volume | A (data) | Backups are the only recovery path | Monitoring/backup freshness | Drill-proven restore | As above |
| 3 | Loss of Railway config/env | A (config) | Package carries keys; recipients are re-enterable | §8 verification | Reconfigure + §15 | None material |
| 4 | Compromise of the backup writer | C (future backups) | Writer holds only public recipients; destination guard; least-privilege role | Audit manifests | Rotate recipients (§12) | Old archives stay sealed to old recipients |
| 5 | Compromise of off-provider storage | C+I (copies) | AEAD; verifier refuses tampered copies | sha256 checks | Re-duplicate from other copy | Authenticity needs recorded anchor |
| 6 | Compromise of a contributor account | C (repo only) | No secrets in repo, ever (strict scan) | Hygiene test in CI | Nothing to rotate in-repo | None from custody design |
| 7 | Leakage of a public recipient | None directly (it is public) | — | — | — | Identity must still stay private |
| 8 | Theft of one offline recovery copy | C only if custody identity also stolen | Copy + identity never co-located | §8 | Nothing compromised; optionally rotate | Physical security |
| 9 | Accidental deletion of one copy | A (reduced redundancy) | Two copies | Inventory | Duplicate from survivor | Single-copy window |
| 10 | Corruption of a copy | I | Hash layers; verifier | §8 FAIL | Re-duplicate | — |
| 11 | Loss of the master-key history | C (old content unreadable) | Package carries full ring | Guard-count query | Restore ring from package | Retired keys gone = gone |
| 12 | Unauthorized use of a valid recovery identity | C+Au | Custody identity stored like a house key; recorded anchor distinguishes your package | Verifier anchor check; inventory | Rotate custody generation (§12) | No cryptographic audit trail of misuse |
| 13 | Secrets in CI artifacts | C | Runtime-synthetic keys only; leak-scan tests; artifacts are reports without secrets | CI green | — | Requires discipline in future jobs |
| 14 | Recovery from an outdated snapshot | I (stale data) | Manifest + revision recorded; rotation-check | Drill compares alembic revision | Pick correct manifest | Data between snapshots lost |
| 15 | Restoration of previously deleted Agent data | Privacy | Append-only semantics; retention documented; deletion policy = future stage | — | — | Explicit accepted Phase-1 limitation |
| 16 | Malicious rollback of backup manifests | Au+I | Commit marker binds artifact+manifest; package anchor binds metadata | Marker hash mismatch; anchor mismatch | Re-verify from other copy | Anchor must be independent |
| 17 | Lost/unavailable human custodians | A (no one can act) | Succession §11.4; two-custodian option; printed inventory | Succession drill (manual) | Successor follows §15 | Social, not technical |

## 17. Quick reference

```
# create (offline, real)          python scripts/recovery_key_init.py --out-dir D --generate-age-identity --with-application-keys [--json]
# second copy                     python scripts/recovery_key_init.py --duplicate-from D1 --out-dir D2
# verify everything               python scripts/recovery_key_verify.py --package-dir D --compare-dir D2 --identity-file CUSTODY --check-decrypt --expected-sha256 SHA
# rotation coverage               … --rotation-check backup-manifest.json
# disaster recovery               … --export-identity operational:ID --export-env-file ENV   (then Stage 1 restore_drill.py)
# exit codes                      0 = all requested checks passed; 1 = a check failed; 2 = usage/config error
```

## 18. Demonstrated vs future work

| Demonstrated in Stage 2 (synthetic, CI-proven) | Explicitly future work |
|---|---|
| Package create/verify/unlock/export/duplicate; two-copy redundancy + loss-of-one-copy recovery; trust anchor; wrong-key/corruption/truncation/swap rejection; K1→K2 continuity incl. restoration from a package; recipient rotation + dual-recipient archives; 16-step disaster recovery on PostgreSQL 18.6 with mixed-generation content; secret-leak scans; Windows-honest warnings | Real key generation (owner, offline); off-provider automated backup copy (Stage 3); production restore runbook execution; re-encryption of historical ciphertext; verifiable deletion; hardware-backed custody; threshold/multi-custodian recovery drills |
