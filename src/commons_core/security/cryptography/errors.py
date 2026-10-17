"""Typed cryptography errors.

All cryptography failures raise subclasses of :class:`CryptographyError` so
the API layer can map them to structured 4xx responses without leaking
internal details (spec §10.2 "safe handling of invalid ciphertext").
"""


class CryptographyError(Exception):
    """Base class for all cryptography-layer failures."""


class UnknownKeyError(CryptographyError):
    """The envelope references a key id (``kid``) that is not configured."""


class TamperDetectedError(CryptographyError):
    """AES-GCM authentication failed: ciphertext, tag, or AAD does not match."""


class EnvelopeFormatError(CryptographyError):
    """The envelope is structurally invalid (version, algorithm, hex fields)."""


class KeyConfigurationError(CryptographyError):
    """``AIC_MASTER_KEYS`` is misconfigured (bad hex, wrong length, duplicates)."""
