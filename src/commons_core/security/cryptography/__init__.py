"""Authenticated-encryption service (spec §10.2).

Contract for later agents: use ``EnvelopeCipher.seal``/``EnvelopeCipher.open``
for ALL stored sensitive content (message payloads, memory entries, activity
summaries, governance records). Never implement custom primitives and never
store plaintext in database columns.
"""

from commons_core.security.cryptography.envelope import Envelope
from commons_core.security.cryptography.errors import (
    CryptographyError,
    EnvelopeFormatError,
    KeyConfigurationError,
    TamperDetectedError,
    UnknownKeyError,
)
from commons_core.security.cryptography.key_store import KeyStore
from commons_core.security.cryptography.service import EnvelopeCipher

__all__ = [
    "CryptographyError",
    "Envelope",
    "EnvelopeCipher",
    "EnvelopeFormatError",
    "KeyConfigurationError",
    "KeyStore",
    "TamperDetectedError",
    "UnknownKeyError",
]
