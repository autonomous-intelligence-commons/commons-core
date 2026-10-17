"""Unit tests for the AES-256-GCM envelope cryptography (spec §10.2, §10.3).

Deterministic: fixed test keys (32-byte hex constants — TEST KEY MATERIAL
ONLY, never production keys).
"""

import logging

import pytest

from commons_core.security.cryptography import (
    EnvelopeCipher,
    EnvelopeFormatError,
    KeyConfigurationError,
    KeyStore,
    TamperDetectedError,
    UnknownKeyError,
)
from commons_core.security.cryptography.key_store import EPHEMERAL_KEY_ID

KEY_1 = "11" * 32
KEY_2 = "22" * 32
TWO_KEY_SPEC = f"k1:{KEY_1},k2:{KEY_2}"


@pytest.fixture()
def cipher() -> EnvelopeCipher:
    return EnvelopeCipher(KeyStore(TWO_KEY_SPEC, "k1"))


class TestRoundTrip:
    def test_seal_open_round_trip(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("hello commons", "message:channel-abc")
        assert cipher.open(envelope, "message:channel-abc") == "hello commons"

    def test_envelope_shape(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("payload", "ctx")
        assert envelope.v == 1
        assert envelope.kid == "k1"
        assert envelope.alg == "AES-256-GCM"
        assert len(envelope.nonce) == 24  # 12 bytes hex
        assert len(envelope.tag) == 32  # 16 bytes hex
        assert envelope.aad == "ctx"
        assert "payload" not in envelope.ciphertext  # never plaintext

    def test_bytes_input(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal(b"binary payload", "ctx")
        assert cipher.open(envelope, "ctx") == "binary payload"

    def test_non_utf8_plaintext_rejected(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal(b"\xff\xfe\x00\x01", "ctx")
        with pytest.raises(EnvelopeFormatError):
            cipher.open(envelope, "ctx")


class TestTamperDetection:
    def test_flipped_ciphertext_byte(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        flipped = "1" if envelope.ciphertext[0] != "1" else "0"
        tampered = envelope.model_copy(update={"ciphertext": flipped + envelope.ciphertext[1:]})
        with pytest.raises(TamperDetectedError):
            cipher.open(tampered, "ctx")

    def test_flipped_tag_byte(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        flipped = "1" if envelope.tag[0] != "1" else "0"
        tampered = envelope.model_copy(update={"tag": flipped + envelope.tag[1:]})
        with pytest.raises(TamperDetectedError):
            cipher.open(tampered, "ctx")

    def test_aad_mismatch(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx-a")
        with pytest.raises(TamperDetectedError):
            cipher.open(envelope, "ctx-b")

    def test_truncated_ciphertext_detected(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"ciphertext": envelope.ciphertext[:-4]})
        with pytest.raises(TamperDetectedError):
            cipher.open(tampered, "ctx")


class TestEnvelopeFormat:
    def test_truncated_nonce(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"nonce": "abcd"})
        with pytest.raises(EnvelopeFormatError):
            cipher.open(tampered, "ctx")

    def test_invalid_nonce_hex(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"nonce": "zz" * 12})
        with pytest.raises(EnvelopeFormatError):
            cipher.open(tampered, "ctx")

    def test_invalid_tag_hex(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"tag": "zz" * 16})
        with pytest.raises(EnvelopeFormatError):
            cipher.open(tampered, "ctx")

    def test_invalid_ciphertext_hex(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"ciphertext": "zz" * 10})
        with pytest.raises(EnvelopeFormatError):
            cipher.open(tampered, "ctx")

    def test_unsupported_version(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"v": 2})
        with pytest.raises(EnvelopeFormatError):
            cipher.open(tampered, "ctx")

    def test_unsupported_algorithm(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"alg": "ROT13"})
        with pytest.raises(EnvelopeFormatError):
            cipher.open(tampered, "ctx")


class TestKeys:
    def test_unknown_kid(self, cipher: EnvelopeCipher) -> None:
        envelope = cipher.seal("secret", "ctx")
        tampered = envelope.model_copy(update={"kid": "k9"})
        with pytest.raises(UnknownKeyError):
            cipher.open(tampered, "ctx")

    def test_nonce_uniqueness(self, cipher: EnvelopeCipher) -> None:
        first = cipher.seal("same plaintext", "ctx")
        second = cipher.seal("same plaintext", "ctx")
        assert first.nonce != second.nonce
        assert first.ciphertext != second.ciphertext

    def test_multi_key_rotation_old_envelopes_still_open(self) -> None:
        old_store = KeyStore(TWO_KEY_SPEC, "k1")
        old_cipher = EnvelopeCipher(old_store)
        envelope = old_cipher.seal("archived", "ctx")

        rotated_cipher = EnvelopeCipher(KeyStore(TWO_KEY_SPEC, "k2"))
        # old envelope still opens (rotation-ready: old kids decrypt)
        assert rotated_cipher.open(envelope, "ctx") == "archived"
        # new envelopes use the new active kid
        assert rotated_cipher.seal("fresh", "ctx").kid == "k2"
        assert rotated_cipher.list_key_ids() == ["k1", "k2"]

    def test_ephemeral_dev_key_path(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            store = KeyStore("", "k1")
        assert store.active_kid == EPHEMERAL_KEY_ID
        assert store.list_key_ids() == [EPHEMERAL_KEY_ID]
        assert any("EPHEMERAL" in record.message for record in caplog.records)

    def test_ephemeral_key_works_end_to_end(self) -> None:
        cipher = EnvelopeCipher(KeyStore("", "k1"))
        envelope = cipher.seal("dev secret", "ctx")
        assert cipher.open(envelope, "ctx") == "dev secret"


class TestKeyStoreValidation:
    def test_wrong_length_rejected(self) -> None:
        with pytest.raises(KeyConfigurationError, match="hex characters"):
            KeyStore(f"k1:{'ab' * 31}", "k1")

    def test_invalid_hex_rejected(self) -> None:
        with pytest.raises(KeyConfigurationError, match="hex characters"):
            KeyStore("k1:" + "zz" * 32, "k1")

    def test_duplicate_kid_rejected(self) -> None:
        with pytest.raises(KeyConfigurationError, match="duplicate"):
            KeyStore(f"k1:{KEY_1},k1:{KEY_2}", "k1")

    def test_missing_colon_rejected(self) -> None:
        with pytest.raises(KeyConfigurationError, match="malformed"):
            KeyStore(KEY_1, "k1")

    def test_all_zero_key_rejected(self) -> None:
        with pytest.raises(KeyConfigurationError, match="zeros"):
            KeyStore("k1:" + "00" * 32, "k1")

    def test_active_kid_must_exist(self) -> None:
        with pytest.raises(KeyConfigurationError, match="active key id"):
            KeyStore(TWO_KEY_SPEC, "k3")

    def test_error_messages_never_contain_key_material(self) -> None:
        suspicious = "g0" * 32  # 64 chars of non-hex material
        with pytest.raises(KeyConfigurationError) as excinfo:
            KeyStore(f"k1:{suspicious}", "k1")
        message = str(excinfo.value)
        assert "g0" not in message  # value never echoed
