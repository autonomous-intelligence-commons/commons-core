"""Privacy tests: no plaintext content at rest, in logs, or in telemetry (spec §11, §20).

Sentinel-plaintext approach: write uniquely identifiable content through the
API, then scan EVERY table column (raw SQL), captured application logs, error
responses and admin telemetry for the sentinel. The envelope cipher stores
hex ciphertext — the sentinel must appear NOWHERE outside authorized API
responses.
"""

import json
import logging
import uuid

import pytest
import sqlalchemy as sa
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.database.base import Base
from commons_core.database.models import MemoryEntry, Message

SENTINEL = f"SENTINEL-{uuid.uuid4()}"


@pytest.fixture()
def actors(client: TestClient) -> dict[str, str]:
    return {
        "alice": provision_and_login(client, "agent-alice"),
        "bob": provision_and_login(client, "agent-bob"),
        "outsider": provision_and_login(client, "agent-outsider"),
        "admin": provision_and_login(client, "ops-admin", is_admin=True),
    }


@pytest.fixture()
def written_sentinel(client: TestClient, actors, caplog) -> dict:
    """Send a sentinel message and write a sentinel memory entry."""
    with caplog.at_level(logging.INFO, logger="commons_core"):
        channel = client.post(
            "/api/v1/channels",
            headers=auth_headers(actors["alice"]),
            json={"handle": "chan-private", "kind": "direct", "initial_members": ["agent-bob"]},
        ).json()
        sent = client.post(
            f"/api/v1/channels/{channel['id']}/messages",
            headers=auth_headers(actors["alice"]),
            json={"payload": f"top secret {SENTINEL}", "correlation_id": "corr-1"},
        )
        assert sent.status_code == 201

        namespace = client.post(
            "/api/v1/memory/namespaces",
            headers=auth_headers(actors["alice"]),
            json={"handle": "ns-private"},
        ).json()
        entry = client.post(
            f"/api/v1/memory/namespaces/{namespace['id']}/entries",
            headers=auth_headers(actors["alice"]),
            json={"key": "private/notes", "payload": f"secret note {SENTINEL}"},
        )
        assert entry.status_code == 201
    return {"channel": channel, "message": sent.json(), "namespace": namespace}


class TestNoPlaintextAtRest:
    def test_sentinel_absent_from_every_database_column(
        self, client: TestClient, database, written_sentinel
    ) -> None:
        # sanity: the rows actually exist (encrypted)
        with database.session() as session:
            assert session.scalar(select(sa.func.count()).select_from(Message)) == 1
            assert session.scalar(select(sa.func.count()).select_from(MemoryEntry)) == 1

        engine = database.engine
        with engine.connect() as connection:
            for table in Base.metadata.sorted_tables:
                rows = connection.execute(sa.select(table)).fetchall()
                for row in rows:
                    for value in row:
                        assert SENTINEL not in str(value), (
                            f"plaintext sentinel leaked into table {table.name!r}"
                        )

    def test_envelope_columns_hold_hex_ciphertext_only(self, database, written_sentinel) -> None:
        with database.session() as session:
            message = session.scalar(select(Message))
            entry = session.scalar(select(MemoryEntry))
        for row, _label in ((message, "message"), (entry, "memory entry")):
            envelope = row.envelope
            assert set(envelope) == {"v", "kid", "alg", "nonce", "ciphertext", "tag", "aad"}
            assert SENTINEL not in envelope["ciphertext"]
            assert SENTINEL not in envelope["aad"]
            # ciphertext and tag are hex; the AAD is the domain binding string
            int(envelope["ciphertext"], 16)
            int(envelope["tag"], 16)

    def test_sentinel_absent_from_logs(self, caplog, written_sentinel) -> None:
        # log records were captured at INFO level during the fixture's sends
        assert SENTINEL not in caplog.text
        assert "top secret" not in caplog.text

    def test_authorized_reader_still_sees_plaintext(
        self, client: TestClient, actors, written_sentinel
    ) -> None:
        """Control: the member CAN decrypt — proving the scan above is meaningful."""
        page = client.get(
            f"/api/v1/channels/{written_sentinel['channel']['id']}/messages",
            headers=auth_headers(actors["bob"]),
        )
        assert page.status_code == 200
        assert SENTINEL in page.json()["messages"][0]["payload"]


class TestNoContentInResponsesOrTelemetry:
    def test_non_member_gets_no_payload(self, client: TestClient, actors, written_sentinel) -> None:
        response = client.get(
            f"/api/v1/channels/{written_sentinel['channel']['id']}/messages",
            headers=auth_headers(actors["outsider"]),
        )
        assert response.status_code == 403
        assert SENTINEL not in response.text
        assert "payload" not in response.text

    def test_telemetry_contains_no_content_or_handles(
        self, client: TestClient, actors, written_sentinel
    ) -> None:
        response = client.get("/api/v1/admin/telemetry", headers=auth_headers(actors["admin"]))
        assert response.status_code == 200
        body = response.json()
        # counters only
        assert body["counts"]["messages"] == 1
        assert body["counts"]["memory_entries"] == 1
        assert body["counts"]["agents_active"] >= 3
        assert "content_policy" in body
        serialized = json.dumps(body)
        assert SENTINEL not in serialized
        # agent handles never appear in telemetry
        for handle in ("agent-alice", "agent-bob", "agent-outsider", "ops-admin"):
            assert handle not in serialized
        # no channel or namespace handles either
        assert "chan-private" not in serialized
        assert "ns-private" not in serialized

    def test_non_admin_cannot_read_telemetry(
        self, client: TestClient, actors, written_sentinel
    ) -> None:
        response = client.get("/api/v1/admin/telemetry", headers=auth_headers(actors["alice"]))
        assert response.status_code == 403

    def test_unauthenticated_telemetry_401(self, client: TestClient, written_sentinel) -> None:
        response = client.get("/api/v1/admin/telemetry")
        assert response.status_code == 401
