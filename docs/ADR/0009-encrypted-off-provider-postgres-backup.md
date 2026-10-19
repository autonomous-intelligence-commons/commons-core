# ADR-0009: Daily Encrypted Off-Provider PostgreSQL Backup via a Dedicated Railway Cron Job

**Status:** ACCEPTED (Stage 1 — repository-side tooling implemented and
synthetically verified). Production deployment stages remain PROPOSED and
require separate authorization.
**Date:** 2026-10-19
**Supersedes:** none. **Relates to:** ADR-0002 (SQLite-dev/PostgreSQL-prod),
ADR-0004 (application-layer encryption), ADR-0007 (fail-closed key
management), THREAT_MODEL T-15, SECURITY_CONTROLS SC-31/SC-33.

---

## 1. Decision context

The database is AIC's only durable state. Its four content families
(messages, memory, activity summaries, governance records) are AES-256-GCM
envelopes keyed by `AIC_MASTER_KEYS` (API-service variable only). The
project principle "AIC should not depend permanently on a single
infrastructure provider" requires a backup that (a) exists **outside**
Railway, (b) is strongly encrypted with keys that never need to be on
Railway, (c) can be restored and verified **without** Railway, and (d)
costs near-zero at current scale. Railway's managed volume backups cannot
leave the project (restore confined to the same project + environment) and
are gated behind the Pro plan — defense-in-depth later, never the primary
mechanism.

A backup & recovery research assignment produced a technical report, a
design, a phased plan, and a local synthetic PoC; an independent
architecture review then raised six corrections. This ADR records the
decision and the Stage 1 implementation that encodes those corrections.

## 2. Alternatives considered

| Option | Verdict |
|---|---|
| **A — dedicated Railway backup workload (cron job)** — `pg_dump -Fc` streamed through age public-key encryption → S3-compatible off-provider storage | **ADOPTED** (details §3) |
| B — run backups inside the existing worker/API | **REJECTED** — couples the safety-critical long-running process with outbound network + long-lived storage credentials; a worker crash loop silently kills backups; backups must not depend on worker health |
| C — external runner connects to PG over the public internet | **REJECTED in that form** — exposes PG solely for backups. Hybrid variant (external scheduler triggers a Railway-side one-off via the Railway API) accepted as a future option |
| D — Railway managed volume backups (Pro) | **REJECTED as primary** — same-project-only restore, volume-wipe deletes backups, no key custody role, $20/mo minimum. Reasonable defense-in-depth later |
| E — do nothing / manual dumps | **REJECTED** — violates the continuity principle; human-in-the-loop-only backups decay |

## 3. The architecture (Stage 1 state in **bold**)

```
Railway project (private network)                OFF-RAILWAY (Stage 3+)
┌───────────────────────────────────┐            ┌────────────────────┐
│ Postgres 18.6                     │            │ S3-compatible      │
│  ▲                                │            │ bucket (B2/R2)     │
│  │ private net                     │            │  data/*.age       │
│ ┌┴──────────────────────────────┐ │   TLS,      │  manifest/*.json  │
│ │ commons-backup (Railway Cron)  │◄┼──write-────▶ status/*.json     │
│ │ 1. pg_dump -Fc  ──PIPE──┐      │ │  only key  └──────────────────┘
│ │ 2. age -r PUBLIC ───────┤      │ │            OFFLINE (owner)
│ │ 3. verify (sha256+hdr)  │      │ │            • age identity (2-copy escrow)
│ │ 4. publish: stage→verify→      │ │            • AIC_MASTER_KEYS offline copy
│ │    promote→commit marker       │ │            • restore runbook
│ │ 5. status/latest.json          │ │
│ └────────────────────────────────┘ │            Stage 1 implements the
└───────────────────────────────────┘            SAME protocol over a local
                                                  filesystem adapter + synthetic
The job holds ONLY: DB URL ref, age PUBLIC       tests in CI against real
recipient, storage write key. Never:             PostgreSQL 16 and 18.
AIC_MASTER_KEYS, age identity, read access.
```

**Implemented in Stage 1:** the streaming pipeline (no plaintext dump file
ever), the manifest with documented consistency semantics, verified atomic
publication with a commit marker, the fail-closed restore drill with the
application-level battery, least-privilege backup-role design + tests,
synthetic end-to-end + fault-injection coverage in CI (PG 16 + 18), and
this documentation. **Not implemented:** the Railway service, the S3
adapter, real credentials, retention deletion (Stage 3+; `docs/BACKUP.md`
§12 has the stage map).

## 4. The six independent-review corrections, resolved

1. **No plaintext intermediate dumps** — `pg_dump`'s custom-format output
   is piped directly into `age`; only encrypted bytes are ever staged, and
   any subprocess failure removes the staged artifact and publishes
   nothing. Explicit process supervision (exit statuses, stderr draining,
   SIGPIPE handling); credentials ride in libpq environment variables,
   never argv.
2. **Manifest consistency** — independent row counts are REMOVED from the
   authoritative manifest (they are not snapshot-consistent proof);
   restored data is validated against controlled synthetic expectations
   from the test fixture instead. Every retained field documents its exact
   consistency semantics; pre-dump fields are labeled informational.
3. **Atomic publication & verified completion** — stage under `.part`,
   readback-verify, never overwrite (immutable ids: same bytes idempotent,
   different bytes conflict), POSIX-atomic promote, and a **commit marker
   written only after both objects verify** — the sole success signal. A
   partial publication (no marker) is refused by the restore drill.
   Fault-injection tests prove no marker survives interrupted/failed
   publication.
4. **Storage permissions & deletion protection** — three-role separation
   (writer / verification reader / retention administrator) with an
   explicit threat model for compromised workers and storage credentials
   (`docs/BACKUP.md` §6); the Stage 1 adapter enforces 0600/0700, no
   overwrite, no delete path; verifiable-deletion limits are stated
   honestly (immutable backups ≠ deletion). No real credentials exist in
   Stage 1.
5. **PostgreSQL 18 compatibility** — the entire backup suite runs in CI
   against `postgres:18.6-alpine` with a pinned 18.6 client (and against
   PostgreSQL 16 in the existing job); tool major-version compatibility is
   enforced fail-closed at runtime (client ≥ server); the least-privilege
   backup role is proven sufficient on both majors; no PG 18-specific
   grants beyond the PG 15+ public-schema USAGE requirement.
6. **Synthetic keys & artifact hygiene** — test identities are generated
   at runtime and scoped to pytest temp directories; `.age`, `*.pgdump`,
   `.dump`, `.backup`, identity files are git-ignored; a tracked-files
   scan test rejects committed private key material; logs are scrubbed
   (URLs, 64-hex, explicit secrets) and a test asserts no credentials in
   CLI output; all temporary artifacts are removed on success and
   failure paths.

## 5. Key handling (three separate domains)

| Domain | Material | Lives where | Never lives where |
|---|---|---|---|
| Content keys | `AIC_MASTER_KEYS` (AES-256 kids) | API service + offline copy (`RAILWAY.md` §6) | worker, backup job, repo, logs |
| Backup encryption | age identity (`AGE-SECRET-KEY-1…`) | offline two-copy escrow (Stage 2) | anywhere on Railway, repo, bucket |
| | age recipient (`age1…`, public) | backup service variable; safe to publish | — (public by design) |
| Storage | bucket app key (write-only prefix) | backup service variables | repo, offline docs, API/worker |

The backup pipeline never reads `AIC_MASTER_KEYS` and never accepts an
identity string; the restore drill takes the identity only via an explicit
file argument held by the drill operator. Rotation: dual-recipient
transition window ≥ retention horizon, then drop the old recipient;
multiple key generations are auditable via the manifest's recorded
recipient.

## 6. Threat model summary

| Threat | Outcome under this design | Residual |
|---|---|---|
| Backup worker compromised | Can dump fresh data to its own key (bounded by the SELECT-only DB role), halt/corrupt NEW backups; cannot decrypt existing ones, cannot reach offline keys or API/worker | Fresh-dump exfiltration bounded only by the DB password — mitigated by least-privilege role + (Stage 3) versioning |
| Storage credentials compromised | Ciphertext-only exposure; overwrites detectable via manifest/marker hashes; drill AEAD rejects tampering | Size/timestamp metadata leaks (accepted) |
| age identity compromised (offline) | Historical backups decryptable | Highest-impact custody failure; two-copy escrow + rotation bounds it |
| `AIC_MASTER_KEYS` lost | Dumps restore but content stays encrypted | Pre-existing (T-15); offline-copy procedure is a hard precondition |
| Silent run failure | Every run writes a status; stale-check arrives in Stage 5 | Detection latency until then |

## 7. Consequences

* **Positive:** cross-provider, key-custodian-independent backups with
  CI-proven restore verification on two PostgreSQL majors; zero
  production impact in Stage 1; near-zero marginal cost at current scale
  (storage/cron free tiers; estimates flagged in the research cost
  comparison — re-verify before Stage 3).
* **Negative/limits:** daily dumps cap the RPO target at 24 h; verifiable
  deletion is impossible beyond retention while old encrypted backups
  exist (documented in `BACKUP.md` §8); restore-into-Railway and the
  S3 adapter remain untested until Stages 3–4; the 128-bit age file-key
  design note is accepted (rotate envelope or dual-encrypt if it ever
  matters).
* **Migration path:** weekly full + WAL shipping (pgBackRest/barman)
  when the database approaches ~GB scale; add Railway Pro volume backups
  as in-platform defense-in-depth alongside, never instead of, this
  pipeline.

## 8. Compliance with the project's continuity principle

Backups are infrastructure safeguards for machine-first operation — they
are not an interface for human inspection of Agent content. Every tool
in this ADR emits counts, labels, and PASS/FAIL only; the manifest
carries no Agent data; the drill is content-blind by construction.
