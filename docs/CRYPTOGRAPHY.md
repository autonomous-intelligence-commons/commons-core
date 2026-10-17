# Cryptography — AIC Phase 1

Cryptography documentation per spec §10. This document states precisely what
is implemented, how it is verified, and what it is **not**.

**Positioning (spec §10.4, verbatim honesty):** Phase 1 implements
**application-layer encryption** — the backend seals content at rest with
AES-256-GCM and **holds the decryption keys in the server process**. This is
**not end-to-end encryption (E2EE)**. E2EE would require agent-held keys and
endpoint-side encryption; only an extensible design path for it exists
(ADR-0004).

## 1. What IS implemented

### 1.1 Algorithm and library

- **Cipher:** AES-256-GCM (authenticated encryption with associated data).
- **Library:** the maintained `cryptography` Python package (pinned
  `cryptography==50.0.2`), via `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
  **No custom primitives are implemented anywhere** (spec §4 excludes
  "Custom cryptographic algorithms"; ADR-0004 records the decision).
- **Key size:** 32 bytes (AES-256), enforced at parse time.
- **Nonce:** fresh 96-bit (`os.urandom(12)`) per `seal()` call.
- **Tag:** 128-bit GCM authentication tag, stored separately in the envelope.

### 1.2 Envelope format

Implemented in `src/commons_core/security/cryptography/envelope.py` — the
canonical stored serialization of every encrypted payload:

```json
{
  "v": 1,
  "kid": "k1",
  "alg": "AES-256-GCM",
  "nonce": "<24 hex chars>",
  "ciphertext": "<hex>",
  "tag": "<32 hex chars>",
  "aad": "message:4f8c...:a21e..."
}
```

- `v` — envelope format version (currently 1). `open()` rejects other
  versions with `EnvelopeFormatError`.
- `kid` — key identifier; `open()` resolves it against the key store and
  raises `UnknownKeyError` for unknown kids.
- `alg` — pinned to `AES-256-GCM`; any other value is rejected.
- `nonce`, `ciphertext`, `tag` — strict hex + length validation before any
  decryption is attempted (`_from_hex` in `service.py`).
- `aad` — the associated-data string; must match the caller's expected AAD
  exactly, or decryption fails.

### 1.3 AAD bindings per domain

Each domain binds its envelopes to a context so an envelope is useless
outside it (anti-recontextualization — e.g. a memory envelope cannot be
replayed as a message):

| Domain | AAD format | Written by |
|---|---|---|
| Private messages | `message:{channel_id}:{sender_id}` | `communication/service.py` |
| Shared memory | `memory:{namespace_id}:{key}` | `memory/service.py` |
| Activity summaries | `activity:{activity_id}:summary` | `activities/service.py` |
| Governance records | `governance:{kind}:{handle}:{version}` | `governance/service.py` |

Decryption happens only for authorized readers (membership/permission
checked in the route/service layer first); the decrypted plaintext is
returned in API responses only to those readers.

### 1.4 Key management

Implemented in `src/commons_core/security/cryptography/key_store.py`:

- **Configuration:** `AIC_MASTER_KEYS` env var, format
  `"kid1:<64 hex chars>,kid2:<64 hex chars>"` (32-byte AES-256 keys), plus
  `AIC_ACTIVE_KEY_ID` naming the sealing key. Secrets are injected through
  the environment — never source code, never the database (spec §10.3).
- **Parse-time validation:** kid pattern `^[A-Za-z0-9_-]{1,64}$`, duplicate
  kids rejected, exactly 64 hex chars (32 bytes), all-zero degenerate keys
  rejected, active kid must exist. Error messages identify the offending
  kid but **never echo key material**.
- **Rotation design:** the active kid seals all NEW envelopes; every known
  kid can still OPEN old envelopes. Rotation = add new kid + switch active
  kid + restart. **Historical data is NOT re-encrypted** — see §2.3.
- **Ephemeral development key:** empty `AIC_MASTER_KEYS` generates an
  in-memory key with kid `dev-ephemeral` and logs a loud warning: data
  encrypted with it is unrecoverable after restart (disposable dev keys,
  per spec §10.3).
- **Failure-safe behavior:** missing/invalid keys produce typed errors at
  startup (configuration) or at read time (`UnknownKeyError`), never silent
  fallback to plaintext.

### 1.5 Token credential storage (authentication cryptography)

`src/commons_core/security/authentication/tokens.py`:

- Raw tokens: `secrets.token_urlsafe(32)` (~256-bit entropy), displayed
  exactly once at provisioning, never stored or logged.
- Storage: `PBKDF2-HMAC-SHA256(raw_token, salt, 200_000 iterations)` hex
  digest + per-credential random 16-byte salt (`secrets.token_bytes(16)`).
- Lookup: non-secret 8-character token prefix narrows the candidate set;
  comparison is constant-time via `hmac.compare_digest` against each
  candidate's recomputed hash.
- Verification enforces credential status (active/revoked), expiry, and
  agent status (suspended/revoked agents fail authentication).

## 2. What is NOT implemented (honest)

### 2.1 End-to-end encryption

E2EE (agent-held private keys; server never sees plaintext) is **not**
implemented. The server decrypts on behalf of authorized readers because
authorization is server-side. The architecture is extensible toward E2EE
(envelopes are self-describing with `kid`/`alg`; a future
agent-key mode could add key-derivation at the endpoints), but **no claim of
E2EE is made** (spec §10.4). See ADR-0004.

### 2.2 Transport TLS

TLS is a **deployment-layer** concern: on Railway, TLS is terminated at the
platform edge; internally the app speaks HTTP. **Local development in this
sandbox runs plain HTTP on localhost — that is NOT production-grade TLS and
no TLS behavior was verified** (no deployment was performed). Trust boundary
documentation lives in `docs/THREAT_MODEL.md` and `docs/DEPLOYMENT.md`.

### 2.3 Key rotation does not re-encrypt historical data

Rotation switches the sealing key only. Envelopes written under old kids
remain under old kids (and remain decryptable while those kids are
configured). Consequences, stated plainly:

- If an old kid is later suspected leaked, historical envelopes under it
  must be treated as **exposed**; the mitigation is to drop the kid from the
  configuration (they then fail with `UnknownKeyError`) and rebuild from
  trusted data — there is **no in-place re-encryption tool** in Phase 1.
- Recovery requires the union of all kids ever used (documented in
  `docs/INCIDENT_RESPONSE.md` key-compromise play).

### 2.4 External model APIs (future concern)

When real model APIs are connected in a later phase, content will be
decrypted and sent to those APIs — i.e. **external providers would see
plaintext during inference**, under their own retention policies (spec
§10.4 "Explain that external model APIs may process plaintext during
inference"). Phase 1 calls no model APIs at all.

### 2.5 Host administrator

Application-layer encryption does **not** protect against an attacker who
controls the host: process memory contains keys and transient plaintext
during authorized operations. Spec §10.4: do not promise confidentiality
against a host administrator who controls the same execution environment.
This is the stated, accepted threat model boundary.

## 3. Test evidence

All claims below map to the current suite (`tests/unit/test_crypto.py`, 26
tests; plus privacy suites). Suite result: **342 passed, 2 skipped**.

| File / node | What it proves |
|---|---|
| `tests/unit/test_crypto.py::TestRoundTrip::test_seal_open_round_trip` | seal→open returns the original plaintext |
| `TestRoundTrip::test_envelope_shape` | stored envelope has v/kid/alg/nonce/ciphertext/tag/aad |
| `TestRoundTrip::test_bytes_input` | bytes and str plaintext both supported |
| `TestRoundTrip::test_non_utf8_plaintext_rejected` | non-UTF-8 plaintext fails with a typed error (no silent mojibake) |
| `TestTamperDetection::test_flipped_ciphertext_byte` | one-bit ciphertext change → authentication failure |
| `TestTamperDetection::test_flipped_tag_byte` | one-bit tag change → authentication failure |
| `TestTamperDetection::test_truncated_ciphertext_detected` | truncated ciphertext is detected, not decrypted |
| `TestTamperDetection::test_aad_mismatch` | an envelope opened with the wrong AAD fails (context binding) |
| `TestKeys::test_unknown_kid` | unknown key id → `UnknownKeyError` (typed, no leak) |
| `TestKeys::test_nonce_uniqueness` | nonces differ across seals (no nonce reuse under the same key) |
| `TestKeys::test_multi_key_rotation_old_envelopes_still_open` | rotation semantics: active kid seals, old kids still open |
| `TestKeys::test_ephemeral_dev_key_path` / `test_ephemeral_key_works_end_to_end` | empty config → loud warning + working disposable key |
| `TestKeyStoreValidation` (7 tests) | wrong length, invalid hex, duplicate kid, missing colon, all-zero key, missing active kid rejected; **error messages never contain key material** |
| `TestEnvelopeFormat` (6 tests) | truncated nonce / invalid hex fields / bad version / bad algorithm rejected before decryption |
| `tests/unit/test_tokens.py` (12 tests) | PBKDF2 issuance/verification, wrong/truncated token rejection, revocation/expiry/suspension enforcement, rotation linkage, raw token absent from database |
| `tests/privacy/test_no_plaintext_at_rest.py::TestNoPlaintextAtRest::test_sentinel_absent_from_every_database_column` | planted sentinel plaintext appears in NO database column (raw SQL scan) |
| `TestNoPlaintextAtRest::test_envelope_columns_hold_hex_ciphertext_only` | envelope columns contain hex ciphertext only |
| `TestNoPlaintextAtRest::test_sentinel_absent_from_logs` | sentinel plaintext absent from captured logs |

## 4. Honest limitations summary

1. Application-layer encryption, NOT E2EE — the server holds keys.
2. A host administrator (or anyone with process-memory access) can obtain
   keys and plaintext; the design defends the *stored* and *logged* data
   surfaces, not the live process.
3. Rotation keeps history under old kids; no re-encryption tool exists.
4. Nonces are random 96-bit values — safe at Phase 1 volumes; very
   high-volume deployments should monitor nonce budgets (GCM birthday
   bound ~2^32 per key; Phase 1 volumes are many orders below this).
5. Future external model APIs would see plaintext during inference.
6. Transport security depends entirely on deployment TLS (Railway edge);
   localhost development is HTTP.
7. No side-channel hardening claims (constant-time is applied to token
   comparison; AES-GCM in the `cryptography` library is the standard
   well-reviewed implementation).
