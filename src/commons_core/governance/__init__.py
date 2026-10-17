"""Internal governance data models module (Agent C — Phase 1, spec §15).

Reserved for versioned governance records and procedure proposals.

HARD BOUNDARY (spec §15): governance records may affect collaboration-level
behavior only. They must NEVER change infrastructure security controls — the
authorization engine never reads governance tables. Models: GovernanceRecord.
"""
