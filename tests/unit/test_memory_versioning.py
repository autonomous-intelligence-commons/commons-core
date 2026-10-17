"""Unit tests: memory versioning mechanics at the service level (spec §11, §12).

Covers: version increments, the supersede chain (immutable history), the
UNIQUE(namespace, key, version) constraint, and AAD binding of envelopes.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from commons_core.agents.service import provision_agent
from commons_core.database.models import MemoryEntry, new_uuid
from commons_core.memory import service
from commons_core.memory.service import entry_aad
from commons_core.security.cryptography import (
    Envelope,
    EnvelopeCipher,
    KeyStore,
    TamperDetectedError,
)


@pytest.fixture()
def cipher() -> EnvelopeCipher:
    return EnvelopeCipher(KeyStore("", "dev"))


@pytest.fixture()
def author(session):
    return provision_agent(session, "agent-author")


@pytest.fixture()
def namespace(session, author):
    return service.create_namespace(session, author, handle="ns-unit")


class TestVersionChain:
    def test_versions_increment(self, session, author, namespace, cipher) -> None:
        first = service.write_entry(
            session,
            author,
            namespace,
            key="k",
            payload="v1",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        second = service.write_entry(
            session,
            author,
            namespace,
            key="k",
            payload="v2",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        third = service.write_entry(
            session,
            author,
            namespace,
            key="k",
            payload="v3",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        assert (first.version, second.version, third.version) == (1, 2, 3)

    def test_supersede_chain_links_forward(self, session, author, namespace, cipher) -> None:
        entries = [
            service.write_entry(
                session,
                author,
                namespace,
                key="k",
                payload=f"v{i}",
                content_type="text/plain",
                cipher=cipher,
                max_bytes=1024,
            )
            for i in range(1, 4)
        ]
        v1, v2, v3 = entries
        assert v1.superseded_by == v2.id
        assert v2.superseded_by == v3.id
        assert v3.superseded_by is None

        history = service.read_history(session, author, namespace, "k")
        assert [entry.version for entry in history] == [1, 2, 3]
        assert [entry.superseded_by is not None for entry in history] == [True, True, False]

        latest = service.read_entry(session, author, namespace, "k")
        assert latest.id == v3.id
        assert service.decrypt_entry(latest, cipher) == "v3"

    def test_old_versions_are_immutable_data(self, session, author, namespace, cipher) -> None:
        service.write_entry(
            session,
            author,
            namespace,
            key="k",
            payload="original",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        service.write_entry(
            session,
            author,
            namespace,
            key="k",
            payload="rewritten",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        history = service.read_history(session, author, namespace, "k")
        assert service.decrypt_entry(history[0], cipher) == "original"
        assert service.decrypt_entry(history[1], cipher) == "rewritten"

    def test_unique_constraint_rejects_duplicate_version(
        self, session, author, namespace, cipher
    ) -> None:
        original = service.write_entry(
            session,
            author,
            namespace,
            key="k",
            payload="v1",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        duplicate = MemoryEntry(
            id=new_uuid(),
            namespace_id=namespace.id,
            key="k",
            envelope=original.envelope,
            author_id=author.id,
            version=1,  # collides with the existing row
            size_bytes=original.size_bytes,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_aad_binding_is_namespace_and_key_specific(
        self, session, author, namespace, cipher
    ) -> None:
        entry = service.write_entry(
            session,
            author,
            namespace,
            key="path/key",
            payload="bound",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        envelope = Envelope(**entry.envelope)
        assert envelope.aad == entry_aad(namespace.id, "path/key")
        assert envelope.aad == f"memory:{namespace.id}:path/key"
        # decrypting with the CORRECT aad works; a different aad fails closed
        assert cipher.open(envelope, entry_aad(namespace.id, "path/key")) == "bound"
        with pytest.raises(TamperDetectedError):
            cipher.open(envelope, entry_aad(namespace.id, "other/key"))

    def test_other_namespace_same_key_is_isolated(self, session, author, namespace, cipher) -> None:
        other_author = provision_agent(session, "agent-author-two")
        other_ns = service.create_namespace(session, other_author, handle="ns-unit-two")
        service.write_entry(
            session,
            author,
            namespace,
            key="shared",
            payload="mine",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        entry = service.write_entry(
            session,
            other_author,
            other_ns,
            key="shared",
            payload="theirs",
            content_type="text/plain",
            cipher=cipher,
            max_bytes=1024,
        )
        # both are version 1 — the version constraint is per namespace
        assert entry.version == 1
        rows = list(
            session.scalars(
                select(MemoryEntry)
                .where(MemoryEntry.key == "shared")
                .order_by(MemoryEntry.namespace_id)
            )
        )
        assert {service.decrypt_entry(row, cipher) for row in rows} == {"mine", "theirs"}
