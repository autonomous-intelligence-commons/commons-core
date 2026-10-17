"""AES-256-GCM envelope cipher (spec §10.2).

Uses ONLY the maintained ``cryptography`` library — no custom primitives
(spec exclusion: "Custom cryptographic algorithms").

Contract for later agents:

- ``seal(plaintext, aad) -> Envelope`` — encrypt UTF-8 text or bytes with
  AES-256-GCM under the active key; a fresh 96-bit nonce is generated with
  ``os.urandom`` for every call; the AAD binds the envelope to its context.
- ``open(envelope, aad) -> str`` — validates version, algorithm, key id, hex
  encodings and lengths, then verifies the GCM authentication tag before
  returning the UTF-8 plaintext. Raises typed errors:
  :class:`UnknownKeyError`, :class:`TamperDetectedError`,
  :class:`EnvelopeFormatError`.
- Key rotation: sealing always uses the store's active kid; every known kid
  can still open old envelopes. Historical data is NOT re-encrypted during
  rotation (documented limitation, spec §10.3).
- PRIVACY RULE: plaintext, key material and AAD content are never logged.
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from commons_core.security.cryptography.envelope import Envelope
from commons_core.security.cryptography.errors import (
    EnvelopeFormatError,
    TamperDetectedError,
    UnknownKeyError,
)
from commons_core.security.cryptography.key_store import KeyStore

_ENVELOPE_VERSION = 1
_ALGORITHM = "AES-256-GCM"
_NONCE_BYTES = 12
_TAG_BYTES = 16


def _from_hex(value: str, field: str, expected_bytes: int) -> bytes:
    """Decode a hex field with strict length validation."""
    expected_len = expected_bytes * 2
    if len(value) != expected_len:
        raise EnvelopeFormatError(
            f"envelope field {field!r} must be {expected_len} hex characters "
            f"({expected_bytes} bytes); got {len(value)}"
        )
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise EnvelopeFormatError(f"envelope field {field!r} is not valid hex") from exc


class EnvelopeCipher:
    """Seals and opens versioned AES-256-GCM envelopes using a :class:`KeyStore`."""

    def __init__(self, key_store: KeyStore) -> None:
        self._key_store = key_store

    @property
    def active_kid(self) -> str:
        """Key id used for sealing new envelopes."""
        return self._key_store.active_kid

    def list_key_ids(self) -> list[str]:
        """All configured key ids (never the key material)."""
        return self._key_store.list_key_ids()

    def seal(self, plaintext: bytes | str, aad: str) -> Envelope:
        """Encrypt ``plaintext`` under the active key with a fresh 96-bit nonce.

        ``aad`` (associated data) binds the envelope to its context (e.g.
        ``"message:<channel_id>"``) and must be supplied unchanged to
        :meth:`open`.
        """
        if isinstance(plaintext, str):
            plaintext_bytes = plaintext.encode("utf-8")
        else:
            plaintext_bytes = plaintext

        key = self._key_store.get(self._key_store.active_kid)
        if key is None:  # defensive; KeyStore always resolves the active kid
            raise UnknownKeyError(f"active key {self._key_store.active_kid!r} is unknown")

        nonce = os.urandom(_NONCE_BYTES)
        ciphertext_with_tag = AESGCM(key).encrypt(nonce, plaintext_bytes, aad.encode("utf-8"))
        ciphertext, tag = ciphertext_with_tag[:-_TAG_BYTES], ciphertext_with_tag[-_TAG_BYTES:]

        return Envelope(
            v=_ENVELOPE_VERSION,
            kid=self._key_store.active_kid,
            alg=_ALGORITHM,
            nonce=nonce.hex(),
            ciphertext=ciphertext.hex(),
            tag=tag.hex(),
            aad=aad,
        )

    def open(self, envelope: Envelope, aad: str) -> str:
        """Verify and decrypt an envelope, returning the UTF-8 plaintext.

        Validation order: version → algorithm → key id → hex field formats →
        AAD binding → GCM authentication tag. Every failure raises a typed
        subclass of :class:`CryptographyError`
        """
        if envelope.v != _ENVELOPE_VERSION:
            raise EnvelopeFormatError(f"unsupported envelope version {envelope.v!r}")
        if envelope.alg != _ALGORITHM:
            raise EnvelopeFormatError(f"unsupported envelope algorithm {envelope.alg!r}")

        key = self._key_store.get(envelope.kid)
        if key is None:
            raise UnknownKeyError(f"unknown key id {envelope.kid!r}")

        nonce = _from_hex(envelope.nonce, "nonce", _NONCE_BYTES)
        tag = _from_hex(envelope.tag, "tag", _TAG_BYTES)
        # ciphertext length is variable; only hex validity matters here.
        if len(envelope.ciphertext) % 2 != 0 or not _is_hex(envelope.ciphertext):
            raise EnvelopeFormatError("envelope field 'ciphertext' is not valid hex")
        ciphertext = bytes.fromhex(envelope.ciphertext)

        if envelope.aad != aad:
            raise TamperDetectedError(
                "AAD mismatch: envelope is bound to different associated data"
            )

        try:
            plaintext_bytes = AESGCM(key).decrypt(nonce, ciphertext + tag, aad.encode("utf-8"))
        except InvalidTag as exc:
            raise TamperDetectedError("authentication tag verification failed") from exc

        try:
            return plaintext_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvelopeFormatError("decrypted plaintext is not valid UTF-8") from exc


def _is_hex(value: str) -> bool:
    return all(char in "0123456789abcdefABCDEF" for char in value)
