# ADR-0001: Modular monolith (single FastAPI app + worker process) over microservices

- **Status:** Accepted (Phase 1)
- **Context:** Spec §5 requires a small, understandable modular architecture
  and explicitly warns against unnecessary microservices, excessive
  dependencies, complex orchestration, or distributed infrastructure that
  would be difficult to maintain on a small Railway deployment. Phase 1
  needs: agent identity, authn/authz, private communication, shared
  memory, activities, governance records, durable scheduling, admin plane —
  with strong structural security separation between layers (spec §3).
- **Decision:** Implement ONE FastAPI application process (modular
  packages: `api/`, `communication/`, `memory/`, `activities/`,
  `governance/`, `scheduler/`, `security/`, `infrastructure/`,
  `database/`, `config/`, `agents/`) plus ONE background worker process
  (`scheduler/worker.py` via `scripts/run_worker.py`) sharing the same
  database. No service-to-service HTTP, no message broker, no
  orchestration layer.
- **Consequences:**
  - (+) Deployment is two processes + a database — fits modest hosting;
    operational surface (and the supply-chain / attack surface) stays
    small (7 pinned runtime dependencies).
  - (+) Layer separation (spec §3 A/B/C) is enforced structurally in code
    (authorization engine reads only infra-owned tables) instead of
    relying on network boundaries between microservices.
  - (+) In-process rate limiting and single-writer SQLite-safe patterns
    are adequate for the single-instance Phase 1 topology.
  - (−) The in-memory rate limiter and single-process assumptions break if
    the API is scaled horizontally (documented in
    `security/rate_limit.py`; a shared store would be needed).
  - (−) Process-level isolation between domains is weaker than
    microservice isolation — mitigated by the canary-tested structural
    rules and by the deny-by-default authorization engine.
