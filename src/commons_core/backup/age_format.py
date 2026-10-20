"""Structural validation of age encrypted files (no identity required).

Stage 1 review correction 3 requires the backup writer to *verify the
encrypted artifact before declaring local success*. A full cryptographic
verification requires the offline age identity, which the backup writer
must never hold (encrypt-only workload). The pipeline therefore verifies
artifacts in three clearly separated layers, documented in
``docs/BACKUP.md``:

1. **Local structural verification** (this module, writer side, no key):
   parses the plaintext age file header and confirms it is a well-formed
   ``age-encryption.org/v1`` file with at least one X25519 recipient stanza
   and a non-empty encrypted payload.
2. **Remote/storage verification** (publication side): the storage adapter
   re-reads what it wrote and compares SHA-256 (see
   :mod:`commons_core.backup.storage`).
3. **Cryptographic verification** (restore drill, identity side): ``age -d``
   authenticates the full AEAD stream — any bit flip, truncation, or wrong
   identity aborts decryption.

This module intentionally implements layer 1 only. It does NOT attempt to
verify the header MAC (that would require deriving ChaCha20-Poly1305 keys
from stanzas the writer cannot open); claiming it did would overstate the
guarantee. A structurally valid file is *not* yet proven decryptable —
that proof is layer 3, exercised by the synthetic end-to-end suite.

Format reference: age v1 spec — https://age-encryption.org/v1 (the header is
ASCII: ``age-encryption.org/v1``, recipient stanzas (``-> X25519 <ephemeral
share>`` + wrapped-file-key body lines), a ``--- <mac>`` separator line, then
the encrypted 64 KiB-chunked payload).
"""

from __future__ import annotations

MAGIC = b"age-encryption.org/v1"
MAX_HEADER_BYTES = 1 << 20  # 1 MiB is far more than any sane header needs


class AgeFormatError(ValueError):
    """The file is not a structurally valid age v1 encrypted file."""


def _is_base64_line(line: bytes) -> bool:
    if not line:
        return False
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return False
    return all(ch.isalnum() or ch in "+/=-_" for ch in text)


def verify_age_file(path) -> None:
    """Raise :class:`AgeFormatError` unless *path* looks like a valid age file.

    Checks (all structural — no cryptographic claims):

    * file readable, non-empty, and at most one plausible payload chunk set;
    * exact ``age-encryption.org/v1`` magic line;
    * at least one ``-> X25519 <ephemeral share>`` recipient stanza whose
      body lines are base64-ish (the actual wrapped file key);
    * a ``--- <mac>`` header/payload separator line;
    * a non-empty payload after the separator.
    """
    import os

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise AgeFormatError(f"cannot stat encrypted artifact: {exc}") from exc
    if size == 0:
        raise AgeFormatError("encrypted artifact is empty (0 bytes)")
    with open(path, "rb") as fh:
        header_blob = fh.read(MAX_HEADER_BYTES + 1)
    if len(header_blob) > MAX_HEADER_BYTES:
        raise AgeFormatError("no age header separator found within the first MiB")

    lines = header_blob.split(b"\n")
    if not lines or lines[0] != MAGIC:
        first = lines[0][:40] if lines else b"<empty>"
        raise AgeFormatError(f"missing age v1 magic line (got {first!r})")

    x25519_stanzas = 0
    saw_separator = False
    separator_end = None
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.startswith(b"--- "):
            saw_separator = True
            if not _is_base64_line(line[4:]):
                raise AgeFormatError("header MAC line is not base64")
            separator_end = sum(len(part) + 1 for part in lines[: i + 1])
            break
        if not line.startswith(b"-> "):
            raise AgeFormatError(f"unparseable header line {i}: {line[:40]!r}")
        parts = line[3:].split(b" ")
        if len(parts) < 1 or not parts[0]:
            raise AgeFormatError(f"recipient stanza {i} has no type")
        stanza_type = parts[0]
        if stanza_type == b"X25519":
            if len(parts) != 2 or not _is_base64_line(parts[1]):
                raise AgeFormatError("X25519 stanza has a malformed ephemeral share")
            # Multiple X25519 stanzas are legitimate: Stage 2 recipient
            # rotation (spec §8) encrypts one stanza per recipient. The age
            # v1 spec places no single-stanza limit on X25519 recipients.
            x25519_stanzas += 1
        body_offset = i + 1
        while (
            body_offset < len(lines)
            and lines[body_offset]
            and not lines[body_offset].startswith((b"-> ", b"--- "))
        ):
            if not _is_base64_line(lines[body_offset]):
                raise AgeFormatError(f"stanza body line {body_offset} is not base64")
            body_offset += 1
        i = body_offset

    if x25519_stanzas < 1:
        raise AgeFormatError("no X25519 recipient stanza found in the header")
    if not saw_separator or separator_end is None:
        raise AgeFormatError("missing '--- <mac>' header/payload separator")

    payload_size = size - separator_end
    if payload_size <= 0:
        raise AgeFormatError("encrypted payload is missing after the header (truncated file)")
