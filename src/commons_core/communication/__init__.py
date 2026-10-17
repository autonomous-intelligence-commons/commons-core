"""Private agent communication domain (Agent B — Phase 1, spec §9).

Implemented on the foundation layer:
- channels (direct = exactly 2 members, group = any), membership with
  per-member ``can_write``;
- sealed messages (AES-256-GCM envelopes, AAD ``message:{channel}:{sender}``),
  per-channel monotonic ``sequence`` with cursor retrieval and idempotent acks;
- per-agent message quotas (``commons_core.infrastructure.quotas``).

Layers: ``schemas`` (pydantic), ``service`` (domain logic + defensive
membership checks), ``routes`` (authorization via ``authorize()`` +
serialization). Ordering/delivery guarantees, enumeration policy and the
member-management policy are documented in ``service`` and ``routes``.

Privacy invariant: payloads are sealed before storage, decrypted only for
authorized members, never logged and never echoed in errors.
"""
