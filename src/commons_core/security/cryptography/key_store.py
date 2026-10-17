"""Master key store (spec §10.3).

Parses ``AIC_MASTER_KEYS`` (format ``"kid1:<64 hex chars>,kid2:<64 hex chars>"``)
into a ``kid -> 32-byte AES-256 key`` mapping. Every key must be exactly 32
bytes. Duplicate kids, malformed hex, wrong lengths and degenerate all-zero
keys are rejected with clear errors that never echo key material.

When the configuration is empty an *ephemeral* in-memory key (kid
``dev-ephemeral``) is generated and a loud warning is logged: this is a
disposable development key and data encrypted with it is lost on restart
(mirrors spec §10.3 "local testing may use automatically generated disposable
development keys").

PRIVACY RULE: key material is never logged, never persisted, and never
included in exception messages.
"""

import logging
import os
import re

from commons_core.security.cryptography.errors import KeyConfigurationError

_LOGGER = logging.getLogger(__name__)

#: Key id used for the ephemeral development key.
EPHEMERAL_KEY_ID = "dev-ephemeral"

_KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]*$")
_KEY_BYTES = 32  # AES-256
_HEX_LEN = _KEY_BYTES * 2


def parse_master_keys(spec: str) -> dict[str, bytes]:
    """Parse a ``"kid:hex,kid:hex"`` master-key specification.

    Raises :class:`KeyConfigurationError` on any malformed entry. Error
    messages identify the offending kid but never the key value.
    """
    entries = [entry.strip() for entry in spec.split(",") if entry.strip()]
    if not entries:
        raise KeyConfigurationError("master key specification is empty")

    keys: dict[str, bytes] = {}
    for entry in entries:
        kid, separator, hex_value = entry.partition(":")
        if not separator:
            raise KeyConfigurationError(
                "master key entry is malformed (expected 'kid:<hex>'); "
                f"unparseable entry starts with {entry[:12]!r}"
            )
        kid = kid.strip()
        hex_value = hex_value.strip()
        if not _KEY_ID_RE.fullmatch(kid):
            raise KeyConfigurationError(f"master key id {kid!r} is not a valid key identifier")
        if kid in keys:
            raise KeyConfigurationError(f"duplicate master key id {kid!r}")
        if len(hex_value) != _HEX_LEN or not _HEX_RE.fullmatch(hex_value):
            raise KeyConfigurationError(
                f"master key {kid!r} must be {_HEX_LEN} hex characters "
                f"({_KEY_BYTES} bytes); got {len(hex_value)} characters"
            )
        key = bytes.fromhex(hex_value)
        if len(key) != _KEY_BYTES:
            raise KeyConfigurationError(f"master key {kid!r} must decode to {_KEY_BYTES} bytes")
        if key == b"\x00" * _KEY_BYTES:
            raise KeyConfigurationError(
                f"master key {kid!r} is all zeros — refusing a degenerate placeholder key"
            )
        keys[kid] = key
    return keys


class KeyStore:
    """Holds the configured master keys with an active (sealing) key id.

    Multiple keys are supported so old envelopes remain decryptable after
    rotation: the active kid seals new envelopes, all known kids can open.
    """

    def __init__(self, master_keys: str, active_key_id: str = "k1") -> None:
        """Build the store from a master-key spec.

        An empty spec triggers the ephemeral development-key path (with a loud
        warning). A non-empty spec with an ``active_key_id`` not present in the
        map raises :class:`KeyConfigurationError`.
        """
        if not master_keys.strip():
            self._keys: dict[str, bytes] = {EPHEMERAL_KEY_ID: os.urandom(_KEY_BYTES)}
            self._active_kid = EPHEMERAL_KEY_ID
            _LOGGER.warning(
                "AIC_MASTER_KEYS is empty — generated an EPHEMERAL in-memory encryption "
                "key (kid=%s). This is a disposable development key: data encrypted with "
                "it is UNRECOVERABLE after restart. Configure AIC_MASTER_KEYS "
                "(kid:<64 hex chars for 32 bytes>) for durable key material.",
                EPHEMERAL_KEY_ID,
            )
            return

        self._keys = parse_master_keys(master_keys)
        active = active_key_id.strip()
        if active not in self._keys:
            raise KeyConfigurationError(
                f"active key id {active!r} is not present in AIC_MASTER_KEYS "
                f"(known ids: {', '.join(sorted(self._keys))})"
            )
        self._active_kid = active

    @property
    def active_kid(self) -> str:
        """Key id used to seal new envelopes."""
        return self._active_kid

    def get(self, kid: str) -> bytes | None:
        """Return the raw key for ``kid`` or ``None`` if unknown."""
        return self._keys.get(kid)

    def list_key_ids(self) -> list[str]:
        """Return all configured key ids (sorted, never the keys themselves)."""
        return sorted(self._keys)
