# Synthetic DR — Threat Model, Public Claims and Security Boundaries

**Review state:** Editorial redaction candidate; **independent publication/security review pending**.

## Threats and intended mitigations

| Risk | Intended control | Present evidence / limitation |
|---|---|---|
| Mis-targeting a database | Offline A, live Online B, future same-path verifier | A/B baseline checked; operation-time enforcement not built |
| Forged run or approval | Protected provenance, independent issuer, single-use approval | Design only; untrusted JSON cannot prove origin |
| Privilege bypass | Protected narrowly scoped executor and separation of duties | Not deployed; elevated administrators can bypass application policy |
| Changed target after check | Fresh target/OID checks and serialization at execution boundary | Race protections and integration not implemented |
| Inventory changes after provisioning | Run-scoped inventory delta policy | Not implemented; baseline B requires original inventory |
| Lost/replaced backup | Verified digest plus usability/restore checks | Not implemented in real exercise; hash alone is not restore proof |
| Untrusted logs | Independently protected evidence store and external checkpoint | Not implemented |
| Host connectivity | WSL/host firewall and forwarding policy review | Local-only listener does not establish full host isolation |

## Publication rules

- Do not publish credentials, tokens, private logs, local host/user identifiers, immutable cluster identifiers, exact operational filesystem locations, presigned URLs or secrets.
- Do not claim that a failed-closed unit test proves absolute security; distinguish design, mock test, live baseline verification and protected deployment.
- Do not publish direct destructive operation commands in public documentation; describe required approval and checks instead.
- Keep detailed operational manifests, protected hashes and logs in independently controlled internal storage.
- Do not claim `production ready`, achieved RPO/RTO, approved Stage 3, or completed Windows backup publication gate.

## Release blockers

1. Independently review the complete intended Git diff and changed-file list for accuracy, redaction, safe paths and unwanted executable changes.
2. Resolve or explicitly document remaining implementation gaps; these do not block publishing **clearly labeled designs**, but **do** block destructive deployment.
3. Confirm documentation links against the actual repository tree, confirm no overwrites, then run repository PR checks.
4. Explicitly approve publication after independent review and PR screenshot inspection.

**Operating state:** design documentation may proceed through PR review; actual destructive DR operations remain **NOT AUTHORIZED**. AIC Stage 3 **NOT STARTED**. A4-4 publication gate **NOT APPROVED**.
