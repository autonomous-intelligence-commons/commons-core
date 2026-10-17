# ADR-0003: Hashed bearer tokens (PBKDF2) over mTLS/JWT for agent authentication

- **Status:** Accepted (Phase 1)
- **Context:** Spec §7 asks for provider-independent agent identity with
  cryptographically secure credentials, and explicitly suggests "prefer
  hashed API-token verification for the initial local architecture"
  (spec §7) over heavier mechanisms. Agents are machine clients; there is
  no user browser, no SSO, and Phase 1 excludes federation. mTLS would
  require client-certificate infrastructure (issuance, rotation,
  revocation lists) and TLS termination passthrough on Railway; JWT would
  introduce signing keys, expiry semantics, and revocation complexity
  (JWTs are hard to revoke).
- **Decision:** Opaque bearer tokens (`secrets.token_urlsafe(32)`,
  ~256-bit) issued by the operator CLI, displayed ONCE, stored ONLY as
  `PBKDF2-HMAC-SHA256(token, per-credential salt, 200,000 iterations)`
  hashes; verification uses a non-secret 8-char prefix to narrow
  candidates, then constant-time comparison (`hmac.compare_digest`), then
  status checks (credential active/expired; agent active/suspended/revoked).
  Rotation links credentials (`rotated_from`); revocation/suspension take
  effect on the next request.
- **Consequences:**
  - (+) Stateless verification needs only the DB; revocation and
    suspension are immediate and durable — better containment than JWT.
  - (+) No signing keys to protect; a DB leak reveals only salted hashes.
  - (+) Simple, operator-friendly provisioning via `scripts/manage.py`.
  - (−) Per-request PBKDF2 cost (~tens of ms): mitigated by the
    middleware's pre-authentication (exactly ONE verification per request,
    shared with the auth dependency).
  - (−) No delegation/scoped capabilities per credential — one token = the
    agent's full capability set. Per-credential scoping is a future
    extension point.
  - (−) Token transport security depends entirely on deployment TLS
    (Railway edge); localhost development is plain HTTP (documented).
