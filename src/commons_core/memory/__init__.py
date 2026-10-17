"""Permission-controlled shared memory domain (Agent B — Phase 1, spec §11/§12).

Implemented on the foundation layer:
- namespaces with owner-derived management (owner-only grants; admins
  deliberately excluded per spec §3.C);
- versioned, immutable-history entries (new row per write, previous active
  row superseded — never updated in place, never deleted);
- read/write permissions per agent; poisoning controls documented in
  ``service`` (untrusted content can never become authority).

Layers: ``schemas`` (pydantic, strict key pattern), ``service`` (domain
logic + defensive permission checks), ``routes`` (authorization via
``authorize()``; keys addressed with the ``{key:path}`` converter).

Privacy invariant: entry payloads are sealed with AAD
``memory:{namespace}:{key}``, decrypted only for authorized readers, never
logged and never echoed in errors.
"""
