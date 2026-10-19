# ADR-0010: Offline recovery-key custody packages

**Status:** ACCEPTED for Backup & Recovery Stage 2 (2026-10-19) —
repository-side tooling implemented and synthetically verified. Real key
generation remains an owner-gated, offline action (never CI, never Railway).
Extends ADR-0009.
**Date:** 2026-10-19
**Supersedes:** none. **Relates to:** ADR-0009 (encrypted off-provider
backup), ADR-0004 (application-layer encryption), ADR-0007 (fail-closed key
management), THREAT_MODEL T-15, `docs/BACKUP.md` §5/§9/§12,
`docs/RECOVERY_KEYS.md` (operational runbook).

---

## 1. Decision context

ADR-0009 (Stage 1) proved the backup/restore mechanics — streaming
`pg_dump → age`, verified atomic publication, and a fail-closed
content-blind restore drill, CI-proven on real PostgreSQL 16 and 18 — but
it left the recovery keys themselves nowhere structured: the age identity
existed only as a synthetic runtime artifact, and offline escrow of it and
of `AIC_MASTER_KEYS` was a documented intention ("Stage 2 custody work,
not done here" — `docs/BACKUP.md` §5).

Two distinct key domains must both survive total infrastructure loss:

* the **backup domain** — age X25519 identities (`AGE-SECRET-KEY-1…`,
  private) and recipients (`age1…`, public) that encrypt/decrypt the
  `pg_dump` archives;
* the **application domain** — the `AIC_MASTER_KEYS` AES-256-GCM key ring
  (`kid:<64 hex>,…`) plus `AIC_ACTIVE_KEY_ID`, which seals every message,
  memory entry, activity summary, and governance record.

Constraints the design had to satisfy:

1. Recoverable **after losing the original infrastructure entirely**
   (provider, devices, Railway configuration) — nothing essential may
   exist only on Railway or only in one place.
2. Private recovery secrets must never leak into the repository, CI,
   logs, cloud services, or Agent activity — everything CI touches is
   **synthetic**; the only real-secret operations are performed offline
   by the human owner.
3. **Single custodian** in Phase 1 (manual succession supported;
   threshold/multi-party not yet justified).
4. The owner runs **Windows** day-to-day — no tool behavior may depend
   on POSIX-only assumptions (permissions, terminal emulation).
5. Custody must be **mechanically verifiable** — provable in CI on
   synthetic keys, not aspirational.

## 2. Alternatives considered

| Option | Verdict |
|---|---|
| **A — versioned recovery package: public metadata JSON + age-encrypted secrets section; custody rooted in dedicated age identities; two byte-identical offline copies; independently recorded SHA-256 trust anchor** | **ADOPTED** (details §3) |
| B — passphrase/scrypt root (`age -p`) instead of custody identities | **REJECTED** — couples every test and Windows run to terminal/pty emulation and weakens the uniform no-terminal story public-key custody gives |
| C — threshold secret sharing (Shamir/SSSS) across custodians | **REJECTED** — complexity unjustified for a single custodian in Phase 1; `--custody-recipient` already provides a manual succession path |
| D — hardware tokens (YubiKey / PGP smartcard) as the custody root | **REJECTED (deferred)** — procurement plus plugin supply-chain trust, and the same physical-custody problem in new packaging |
| E — GPG/SSH signatures as the trust anchor | **REJECTED** — a new dependency that protects the package with yet another key needing exactly the same custody; no net gain over the recorded anchor |
| F — storing the custody identity inside the package | **REJECTED** — circular: the package would be its own key and a single stolen copy would suffice |
| G — SHA-256 alone as authenticity | **REJECTED** — insufficient against wholesale replacement; a hash stored next to a copy defends nothing (spec §9); authenticity requires an independently recorded anchor |

## 3. The decision (ten points)

1. **Versioned recovery package.** A generation `gen-<timestamp>-<hex8>`
   is two files: `<gen>.recovery-metadata.json` (public: format version,
   creation time, generation id, custody + packaged *public* recipients,
   identity fingerprints, application key **ids** — never material —
   compatibility requirements, integrity hashes, trust anchor) and
   `<gen>.recovery-secrets.age` (SECRET: a JSON payload with the private
   age identities and the `AIC_MASTER_KEYS` ring, age-encrypted to the
   custody recipients; payload version 1).
2. **Custody root: dedicated age custody identities, NOT passphrases.**
   Every secrets section is encrypted to one or more custody recipients
   (X25519 AEAD) whose private identities are generated offline and
   stored independently of all package copies. Rationale: identical
   behavior in tests, CI, PowerShell, and Git Bash with no terminal
   coupling; one well-understood tool (age) protects both the backup and
   custody layers; multi-custodian ready via `--custody-recipient`
   without protocol changes.
3. **Two byte-identical package copies in independent offline locations;
   custody identities stored separately from both.** A copy without the
   custody identity is inert ciphertext; the identity without a copy has
   nothing to decrypt — an attacker needs BOTH. The verifier's hygiene
   check **refuses** the package while the custody identity still sits
   in the package directory (move-out is enforced, not advised);
   `--duplicate-from` + `--compare-dir` prove the copies match.
4. **Trust anchor: an independently recorded `custody_manifest_sha256`**
   (SHA-256 over the canonical metadata minus the trust-anchor field),
   printed at creation and verified via `--expected-sha256`. Honest
   limitation, stated loudly by the tool: a hash recorded *next to* a
   copy defends nothing — the anchor must live where a copy-thief cannot
   rewrite it (e.g., a printed card plus an offline password-manager
   note).
5. **Integrity layering.** age AEAD (tamper-evident decryption) +
   file-level SHA-256 in the metadata (corruption/swap detected before
   decryption) + plaintext `payload_sha256` (detects a secrets file
   swapped in from another package even under the same custody identity)
   + generation/claims consistency checks (metadata claims must match
   the decrypted payload).
6. **Key domains stay separated.** Separate package sections, independent
   rotation, independent destruction; no universal key; a backup
   decryption identity is never reused as an application master key or
   vice versa (extends ADR-0009 §5).
7. **Backup layer: the smallest backward-compatible multi-recipient
   extension.** `run_backup(..., extra_recipients=…)` adds an additive
   `artifact["recipients"]` list to the manifest; `schema_version` stays
   1; single-recipient manifests are unchanged in meaning; `recipient`
   remains the primary. This makes ADR-0009's rotation transition window
   executable (dual-recipient archives decryptable with EITHER identity)
   and supports successor custodians without a format break.
8. **Tooling.** `commons_core/backup/recovery_keys.py` plus the
   `recovery_key_init.py` / `recovery_key_verify.py` CLIs; refuses git
   repositories, known cloud-sync folders, and overwrites of existing
   packages; atomic writes (staging + `os.replace`), metadata written
   last; POSIX 0600 with honest Windows caveats (chmod is a no-op on
   NTFS — the tool warns and prints an `icacls` line); secrets never in
   argv, output, or logs — enforced and leak-scanned in tests;
   content-blind PASS/FAIL output only.
9. **Correction — `age_format` layer-1 check:** now allows one stanza per
   recipient (multi-recipient age files are legitimate per the age v1
   format). The previous single-stanza assumption made every
   dual-recipient backup fail its own layer-1 structural verification
   (found by the Stage 2 rotation-exercise test, not by Stage 1 tests).
10. **Correction — Stage 1 hygiene-scan alphabet:** fixed to the Bech32
    data alphabet — real age identities contain `0`/`8`/`9`, which the
    old Base32 `[A-Z2-7]` pattern misclassified.

## 4. Consequences

* **Positive:** provider loss is survivable (offline package + vault
  restore path); machine loss is survivable (custody identities stored
  independently of copies); single-copy loss is survivable
  (byte-identical duplicate + `--compare-dir`); all of it synthetically
  CI-proven — 94 backup/recovery tests, including a 16-step
  disaster-recovery exercise on real PostgreSQL 18.6 that recovers both
  key generations from two offline copies; content-blind outputs
  preserve the machine-first privacy posture (no Agent content in any
  tool output).
* **Negative/accepted:** the owner must maintain physical custody
  discipline (two copies, identity stored separately, anchor recorded
  independently) — tooling verifies and refuses bad states but cannot
  enforce it; if **both copies or the custody identity are lost**, that
  generation's secrets are gone (fails safe; mitigated by the
  two-copies + independent-identity-storage redundancy); custody
  identities have **no cryptographic misuse-audit trail** — detection is
  procedural (inventory, anchor verification, periodic drills);
  rotation never revokes old archives — retained backups stay
  decryptable by their own identities, so destruction must be
  retention-aware.
* **Migration path:** Stage 3 hooks below; no existing manifest or
  archive changes meaning (the recipients list is additive only).

## 5. Stage 3 hooks

* **Off-provider automated copy** of backup archives (S3 adapter) — the
  remaining custody gap after Stage 2 (`docs/BACKUP.md` §12).
* **Re-encryption** of historical ciphertext after a master-key
  compromise (today: rotate-and-contain only).
* **Verifiable deletion** alignment: backup archives and custody copies
  must become explicit deletion targets; the `RECOVERY_KEYS.md`
  inventory is the starting point for that work.
* Optional **second custodian** (dual custody recipients + succession
  drill) — the package protocol already supports it; the procedure
  stays manual in Phase 1.
