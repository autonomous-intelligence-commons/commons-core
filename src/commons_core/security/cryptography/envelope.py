"""Versioned encrypted envelope model (spec §10.2).

An :class:`Envelope` is the canonical serialization of encrypted content in
the database: message payloads, memory entries, activity summaries, and
governance records store ``envelope.model_dump()`` in a JSON column.

This model deliberately performs only *structural* typing; strict validation
(hex encoding, nonce/tag lengths, algorithm, version) happens in
:class:`commons_core.security.cryptography.service.EnvelopeCipher.open` at
decryption time so malformed envelopes surface as typed errors.
"""

from pydantic import BaseModel


class Envelope(BaseModel):
    """AES-256-GCM encrypted payload with key identifier and binding AAD."""

    v: int = 1
    kid: str
    alg: str = "AES-256-GCM"
    nonce: str
    ciphertext: str
    tag: str
    aad: str
