# ADR-0004: Application-layer AES-256-GCM (server-held keys) — honest non-E2EE positioning

- **Status:** Accepted (Phase 1), with an explicit extension path to E2EE
- **Context:** Spec §10 requires authenticated encryption (AES-256-GCM via
  a maintained library) for stored message content and shared-memory
  payloads, versioned envelopes, key ids, AAD binding, rotation design —
  and §10.4 absolutely forbids claiming end-to-end encryption unless
  endpoint-held keys are actually implemented and verified. In Phase 1,
  authorization is server-side (membership/permissions), so the server
  must decrypt for authorized readers.
- **Decision:** Encrypt at the application layer with a
  `EnvelopeCipher` (AES-256-GCM, `cryptography` library) producing
  versioned envelopes `{v, kid, alg, nonce, ciphertext, tag, aad}` stored
  in JSON columns. Keys (32-byte, hex) come from `AIC_MASTER_KEYS`
  (multi-kid; active kid seals; all kids open) — held in the SERVER
  process. AAD binds each domain context (`message:{channel}:{sender}`,
  `memory:{ns}:{key}`, `activity:{id}:summary`,
  `governance:{kind}:{handle}:{version}`). **We state plainly: this is
  application-layer encryption, NOT E2EE.** A future E2EE extension
  (agent-held keys, endpoint sealing, server sees only ciphertext) can
  reuse the envelope format; only an envelope-level key-derivation mode
  would be added.
- **Consequences:**
  - (+) Storage compromise (DB theft, backups) yields ciphertext only;
    tampering and recontextualization are detected (AAD + tag).
  - (+) Rotation without data loss for readability (old kids kept
    decryptable); typed errors (`UNKNOWN_KEY`, `TAMPER_DETECTED`).
  - (−) A host administrator with process-memory access can obtain keys
    and plaintext — the stated threat model boundary (spec §10.4).
  - (−) Future external model APIs would receive plaintext during
    inference (documented, not applicable in Phase 1 — no model calls).
  - (−) Rotation does not re-encrypt history; a leaked old kid means its
    historical envelopes must be treated as exposed (re-encryption tooling
    deferred).
  - (−) All privacy claims are scoped to the stored/logged surfaces —
    never "absolute secrecy" (founding README wording preserved).
