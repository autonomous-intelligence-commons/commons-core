# Destructive Safety Gate v1.0 — Dry-Run Plan Validation

**Status:** **DRY RUN ONLY**; no real operation authority.

## Purpose

Validate the shape of a proposed synthetic DR plan against a restricted test scope. The prototype rejects known forbidden maintenance databases, unexpected targets, additional operations, and caller claims that approval/evidence already exists.

## Scoped evidence

User-observed **23/23** isolated tests passed with exit 0, including wrong cluster/port/identity, malformed operations, disallowed targets, duplicate JSON fields, and unsupported claims of provenance or approval.

## Non-claims

- **No SQL, database connection, start/stop or backup/restore capability** is provided.
- Any caller-provided provenance fields are **assertions**, not independent evidence of creation.
- Passing plan validation cannot stop a separately privileged administrative tool from bypassing the gate.
- An editable plan or checksum is not an independently issued approval.

The true execution path must eventually own both revalidation and operation dispatch under independently reviewed privilege boundaries. The dry-run prototype is **not** that path.
