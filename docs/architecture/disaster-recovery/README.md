# Synthetic DR Safety Foundation — Documentation Index

**Status:** Public design record; non-production. **Date:** 2026-11-05.

This set records the scoped safety work completed for a **dedicated, isolated-purpose, synthetic-data PostgreSQL disaster-recovery test environment**. It is **not** a production DR runbook, executable authorization, deployable security policy or proof of complete host isolation.

## Documents

| Document | Purpose |
|---|---|
| [Offline preflight](offline-preflight-design-v1.2.md) | Verify an intended, stopped test cluster before deliberate startup |
| [Online preflight](online-preflight-design-v1.0.md) | Verify the actual running server using a dedicated local connection |
| [Destructive safety gate](destructive-safety-gate-design-v1.0.md) | Dry-run proposal validation; deliberately incapable of destructive execution |
| [Execution-point safety](execution-point-safety-design-v0.1.md) | Mock-only enforcement-flow model and unimplemented protections |
| [Synthetic run lifecycle](synthetic-dr-run-lifecycle-v1.0.md) | Six-stage run design, evidence and state transitions |
| [Trusted evidence and authorization](trusted-evidence-authorization-v1.0.md) | Proposed provenance, independently issued approval, and least-privilege boundaries |
| [Security and publication boundaries](../../security/disaster-recovery/publication-security-boundaries.md) | Threats, blockers, non-claims and publication rules |
| [Milestone](../../milestones/2026-11-05-synthetic-dr-safety-foundation.md) | Scoped acceptance evidence and pending work |

## Evidence interpretation

- Offline preflight **A v1.2**: user-observed real WSL baseline success (38 checks), plus three error-condition CLI checks and 16 isolated parser tests.
- Online preflight **B v1.0**: user-observed real running-cluster baseline success (30 checks), 20 isolated tests, then controlled shutdown.
- Dry-run safety gate **v1.0**: **23 isolated tests**, plan-validation only.
- Execution-point simulation **v0.1**: **15 isolated tests**; mock operation adapter only.
- Synthetic run lifecycle **v1.0**: **21 isolated tests**; no database operations.

Test counts are reported from user-provided console output and prior draft artifacts; the public repository CI has **not** independently rerun these tests as part of this documentation package. All claims are version- and scope-limited. Design or simulation success is not authorization for an actual destructive DR operation.

## Fixed boundaries

Existing Windows-host PostgreSQL services, production databases, production backups and production credentials are **out of scope** and were not modified by this documentation work. AIC Stage 3 remains **NOT STARTED**; the separate Windows backup publication gate remains **NOT APPROVED**; real destructive DR exercise **NOT AUTHORIZED**. Recovery objectives remain **unmeasured**.
