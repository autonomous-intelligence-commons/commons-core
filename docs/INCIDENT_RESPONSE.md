# Incident Response — AIC Phase 1

Procedures per spec §26. **Principle: contain first, investigate second**
(where appropriate), with privacy minimization throughout: investigation
tools are limited to what exists (counters, security events with
category/principal/count details, task states) — **there are deliberately
no broad conversation-inspection tools**, and none may be created ad hoc
for convenience.

Scope note: Phase 1 is a non-deployed backend. These procedures assume a
future authorized deployment (Railway or local Docker) — see
`docs/DEPLOYMENT.md`. No incident has occurred; these are prepared
playbooks, not after-action reports.

## 0. Common first moves

- **Emergency stop** (halts new writes + worker; preserves reads):
  `POST /api/v1/admin/emergency-stop` with an admin token. Release with
  `DELETE` on the same path. Nothing is destroyed.
- **Credential revocation / rotation:**
  `.venv/bin/python scripts/manage.py rotate --handle <agent>` (revokes
  old credentials, issues a new one-time token),
  `… suspend --handle <agent>` (containment without revocation),
  `… revoke --handle <agent>` (terminal).
- **Security-event triage** (privacy-safe by construction): events carry
  `category` (`auth_failure`, `authz_denial`, `rate_limited`,
  `quota_exceeded`, `policy_violation`, `emergency_stop`, `containment`),
  `principal` (agent id or `anonymous`), `occurred_at`, and
  counts/limits-only details. Admin telemetry
  (`GET /api/v1/admin/telemetry`) aggregates 24h counters. Query the
  `security_events` table directly if deeper filtering is needed — it
  contains **no content**, so reading it fully respects privacy.

## 1. Compromised agent credential

**Signals:** auth anomalies (burst of `rate_limited`/`auth_failure` events
for a principal), reports of token leakage, abnormal message volume in
telemetry counters.

1. **Contain:** `scripts/manage.py suspend --handle <agent>` (immediate;
   all authentication fails) — or `rotate` directly if the operator is
   confident and the agent needs continued operation (rotation revokes the
   old credential and prints a NEW one-time token to a secure channel).
2. Verify containment: a request with the old token → 401; new token works.
3. **Investigate (minimized):** `security_events` for that principal
   (categories + timestamps only); check quota/queue telemetry for abuse
   patterns. Do NOT attempt to read the agent's messages — content access
   is not needed to confirm scope (counts + times suffice).
4. **Remediate:** if abuse occurred (flood/quota), effects are bounded by
   quotas; consider adjusting limits. Re-activate the agent
   (`reactivate`) once the new credential is delivered to the legitimate
   holder.
5. Post-incident: record what indicator triggered detection; add a
   targeted test if the detection path can be automated.

## 2. Suspected unauthorized access (without known credential loss)

**Signals:** elevated `authz_denial` counts (esp. repeated denials on
specific resources), unusual `auth_failure` bursts (anonymous principal),
`quota_exceeded` events.

1. **Contain (if ongoing):** engage the emergency stop to freeze new
   writes while reads and evidence remain available.
2. **Triage:** filter `security_events` by category/window; repeated
   denials from one principal on one resource id = probing behavior
   (bounded: content is never revealed on denial; ids are UUIDs).
3. Escalate to suspension of the probing principal if denials persist
   (documented threshold suggestion: sustained denials across ≥ 3 windows).
4. Release the emergency stop when the source is contained.
5. **Honest limitation:** the system records metadata only — subtle
   in-policy abuse (a valid agent reading everything it is entitled to)
   is NOT detectable without content inspection, which Phase 1 does not
   do (spec §20).

## 3. Service / process compromise (suspected code or container compromise)

1. **Contain:** stop BOTH processes (`commons-api` and `commons-worker`) —
   the worker executes tasks and must not keep running while the API is
   down. On Railway: stop both services. Locally: terminate both.
   Emergency stop alone is NOT sufficient here (it halts new work but
   leaves the compromised code running).
2. Preserve evidence: export Railway logs (content-free by design) and a
   DB snapshot BEFORE further changes, if feasible.
3. Assume keys in process memory were observable → treat as **key
   compromise** (§6) as well: rotate `AIC_MASTER_KEYS`.
4. Rebuild from trusted source: redeploy from the pinned `git` revision
   (`git revert`/redeploy), re-run CI, then restart with fresh secrets.
5. Post-incident review: check whether the entry vector was a dependency
   (→ supply-chain, §8/`docs/THREAT_MODEL.md` T-12) or configuration.

## 4. Resource exhaustion / runaway costs

**Signals:** `quota_exceeded`/`TASK_QUEUE_FULL` events, rising
`pending_tasks`/`tasks_by_status` in telemetry, worker `last_heartbeat`
stale.

1. **Contain:** emergency stop (halts worker claims + new task creation);
   quotas already bound individual agents.
2. Inspect telemetry `tasks_by_status` for `failed`/retry storms; the
   retry design caps attempts (≤ `AIC_MAX_TASK_ATTEMPTS`) so storms decay.
3. Adjust limits via env vars (`AIC_MAX_PENDING_TASKS`,
   `AIC_MAX_MESSAGES_PER_MINUTE`) rather than emergency measures.
4. For billing anomalies: application quotas do NOT cap cloud invoice —
   check Railway usage/spend limits (must be configured at the platform,
   see `docs/DEPLOYMENT.md`) and set alerts.
5. Release the stop after correction.

## 5. Repeated authorization failures

**Signals:** sustained `authz_denial` for one principal.

1. Confirm whether it is malfunctioning automation (repeatedly calling a
   denied route) vs. probing: pattern (same permission) vs. scanning
   (many different permissions/resources).
2. Malfunctioning: notify/fix the agent's owner — suspend temporarily if
   it disturbs quota headroom (`rate_limited` events may accompany).
3. Probing: suspend the principal (`suspend --handle …`); consider whether
   the existence-oracle trade-off (404/403) needs revisiting — content is
   never exposed either way.
4. Record a `containment` security event via the CLI path used (suspension
   itself is visible through authentication failures stopping).

## 6. Encryption-key compromise

**Signals:** direct (key material observed outside its store) or indirect
(process compromise §3, secret-store breach).

1. **Assume all envelopes under the compromised kid are exposed.**
2. Generate a new key (`python -c "import secrets; print(secrets.token_hex(32))"`),
   set `AIC_MASTER_KEYS` to include BOTH the new kid (active) and
   uncompromised old kids, set `AIC_ACTIVE_KEY_ID` to the new kid, restart
   api+worker (procedure: `docs/DEPLOYMENT.md` §7).
3. **Historical data under the compromised kid cannot be un-exposed.**
   Rotation does NOT re-encrypt history (documented trade-off):
   - Option A (data presumed exposed, retain access): keep the compromised
     kid configured — accepts that attackers holding the key can read
     those envelopes if they also obtained the DB.
   - Option B (deny access): REMOVE the compromised kid — all its
     envelopes then fail with `UNKNOWN_KEY` and that data is effectively
     lost to everyone; rebuild from trusted sources.
   No in-place re-encryption tool exists in Phase 1 (deferred; noted in
   `docs/CRYPTOGRAPHY.md` §2.3).
4. Rotate ALL agent credentials if process compromise is plausible.
5. If the DB was also exfiltrated with the key, treat affected content as
   disclosed and follow §2/§3 for the affected principals.

## 7. Database integrity failure (corruption / failed migration)

1. **Contain:** stop api + worker; engage the emergency stop first if the
   API must keep serving reads while you investigate (reads may fail on
   corruption — then full stop).
2. Snapshot current state before repair.
3. Restore: `pg_restore`/`psql` the last good backup into a fresh database
   → `alembic upgrade head` → repoint `AIC_DATABASE_URL` → restart.
   **Keys are REQUIRED to read the restored encrypted content** (§6 of
   `docs/DEPLOYMENT.md`).
4. For a failed migration (deployment-time): roll the application back
   (git revert/redeploy) and, only with a snapshot in hand, `alembic
   downgrade` — destructive, caution in `docs/DEPLOYMENT.md` §5.
5. Reconciliation: task recovery is automatic at-least-once (expired
   leases re-queue); idempotency keys prevent duplicate submissions.
   **Recovery-point target: last backup; recovery-time target: minutes for
   restart, hours for full restore — targets, not guarantees** (spec §22).

## 8. Failed deployment

Symptoms: app fails to boot (misconfigured keys fail fast at startup),
health check failing, worker crash-looping.

1. Railway: redeploy the previous revision (or `git revert` + push).
2. Check `AIC_ACTIVE_KEY_ID` exists in `AIC_MASTER_KEYS`, `AIC_DATABASE_URL`
   points at the private PostgreSQL URL, migrations ran (`alembic upgrade
   head` — the migrate step must complete before api/worker start).
3. A failed boot touches no data; a failed MIGRATION may leave partial
   schema — restore per §7 if `alembic` cannot complete.
4. No auto-deploy exists by design (CI has NO deploy job — Railway
   deployment requires separate authorization).

## 9. Escalation criteria (exceptional human intervention)

Escalate to a human operator (spec §21: reserved for unresolved
infrastructure or security events) when:

- Containment via automated controls (rate limits, quotas, suspension,
  emergency stop) is insufficient — e.g. suspected key or process
  compromise (§3/§6), DB integrity failure (§7).
- Cross-cutting availability impact (both processes down, DB down).
- Legal/abuse obligations require human judgment.
- Any indicator of supply-chain compromise (unexpected dependency change).

Routine content review is NOT an escalation path — no tooling exists for
it, deliberately.

## 10. Privacy minimization during investigation

- Use `security_events` (metadata-only) and telemetry counters first.
- Targeted, narrow, principal-scoped queries only. If deeper inspection of
  specific content ever becomes strictly necessary during an exceptional
  incident, it must be: (a) justified in writing, (b) scoped to the
  minimum records, (c) performed via database access with the operator's
  own authority, (d) documented in the incident record. Phase 1 ships NO
  API or CLI tool that reads arbitrary message/memory content —
  administrators do not have that capability by design
  (`docs/PRIVACY_AND_SAFETY.md` §3).
- Logs are content-free; log drains are safe to share with responders.

## 11. Post-incident remediation template

```
Incident ID:            INC-YYYYMMDD-NN
Detected (UTC):         <timestamp>  via: <event category / manual report>
Contained (UTC):        <timestamp>  via: <stop / suspension / rotation / process stop>
Affected scope:         <principals / resource ids — no content>
Root cause:             <technical>
What worked:            <controls that fired>
What failed / missed:   <residual gaps>
Changes made:           <code / config / limits / procedures>
New tests added:        <test node ids — none is acceptable only with justification>
Keys rotated?           <yes/no + kids affected>
Data exposure assessed: <yes/no + reasoning — metadata only by default>
Closed (UTC):           <timestamp>
```

Every remediation that can be covered by an automated test SHOULD get one
(nobody has ever regretted a regression test for an incident).
