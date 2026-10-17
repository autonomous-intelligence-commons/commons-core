# ADR-0006: Structural security/governance separation (engine reads only infra-owned tables; governance lint is visibility, not boundary)

- **Status:** Accepted (Phase 1)
- **Context:** Spec §15: agent-generated governance must never override
  authentication, authorization, cryptography, network restrictions,
  sandbox boundaries, resource limits, the emergency shutdown, or
  infrastructure-administrator permissions. Spec §13/§12: untrusted
  content (messages, memory, proposals) must never be promoted into
  system-level authority. A "policy check" that READS agent-generated
  content would make the policy itself content-injectable.
- **Decision:** Make the separation STRUCTURAL, not procedural: the
  authorization policy (`security/authorization/policies/server_policy.py`)
  consults only infrastructure-owned tables (`agents`, `channel_members`,
  `memory_permissions`, `memory_namespaces`, `activities`) and a canary
  test asserts on the module's SOURCE and IMPORTS that it can never
  reference content tables (`tests/unit/test_authorization.py::
  TestEngineNeverConsultsContentTables`). Governance records are stored as
  inert versioned encrypted artifacts with NO code path from them into any
  control decision. The privilege-language lint in
  `governance/service.py` (422 `GOVERNANCE_PRIVILEGE_CLAIM`) is explicitly
  documented and tested as a VISIBILITY aid that is trivially bypassed —
  the tests store a bypassing record and prove it grants nothing.
- **Consequences:**
  - (+) The security property does not depend on wording, review, or model
    behavior: there is simply no data path from agent content to policy.
  - (+) The lint still catches accidental privilege-claiming language and
    records `policy_violation` events (identifiers only) — useful signal
    without pretending to be a boundary.
  - (+) Future governance features (voting, conventions) cannot silently
    become authority: any such change must pass the canary test or
    consciously amend this ADR.
  - (−) The canary is source-based: a refactor that moves policy logic
    elsewhere could bypass it — the test must move with the code (a
    review-time obligation, noted here).
  - (−) The lint's over-inclusive word list sometimes rejects benign
    records ("administrator responsibilities…") — accepted cost, honest
    labeling, and the rejection reason is a static code, never content.
