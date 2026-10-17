"""Integration tests: internal governance records (spec §15).

Covers creation, versioning via ``supersedes``, decryption for authorized
(active) readers, kind filtering, history listing, and encryption-at-rest of
record content (raw SQL sentinel scan). The adversarial privilege-claim
boundary is tested separately in
``tests/security/test_governance_boundary.py``.
"""

import uuid

import pytest
import sqlalchemy as sa
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.database.base import Base
from commons_core.database.models import GovernanceRecord

SENTINEL = f"GOVERNANCE-SENTINEL-{uuid.uuid4()}"


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def bob_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-bob")


@pytest.fixture()
def proposal(client: TestClient, alice_token: str) -> dict:
    response = client.post(
        "/api/v1/governance/records",
        headers=auth_headers(alice_token),
        json={"handle": "conv-alpha", "kind": "proposal", "content": f"be excellent {SENTINEL}"},
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestGovernanceCreation:
    def test_create_proposal_version_one(
        self, client: TestClient, proposal: dict, alice_token: str
    ) -> None:
        body = proposal
        assert body["handle"] == "conv-alpha"
        assert body["kind"] == "proposal"
        assert body["version"] == 1
        assert body["status"] == "draft"
        assert body["author"] == "agent-alice"
        assert "content" not in body  # metadata only, no echo

    def test_content_decrypts_for_authorized_reader(
        self, client: TestClient, proposal: dict, bob_token: str
    ) -> None:
        # governance:read is granted to every ACTIVE agent — records are
        # internal-shared convention artifacts, not private messages.
        detail = client.get(
            "/api/v1/governance/records/conv-alpha", headers=auth_headers(bob_token)
        )
        assert detail.status_code == 200
        assert detail.json()["content"] == f"be excellent {SENTINEL}"
        assert detail.json()["version"] == 1

    def test_amendment_with_supersedes_creates_version_two(
        self, client: TestClient, proposal: dict, alice_token: str
    ) -> None:
        amendment = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={
                "handle": "conv-alpha",
                "kind": "amendment",
                "content": "be excellent, but tersely",
                "supersedes": "conv-alpha",
            },
        )
        assert amendment.status_code == 201, amendment.text
        assert amendment.json()["version"] == 2
        assert amendment.json()["kind"] == "amendment"

        detail = client.get(
            "/api/v1/governance/records/conv-alpha", headers=auth_headers(alice_token)
        )
        assert detail.json()["version"] == 2
        assert detail.json()["content"] == "be excellent, but tersely"

    def test_previous_version_marked_superseded(
        self, client: TestClient, proposal: dict, alice_token: str
    ) -> None:
        client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={
                "handle": "conv-alpha",
                "kind": "convention",
                "content": "v2 text",
                "supersedes": "conv-alpha",
            },
        )
        history = client.get(
            "/api/v1/governance/records",
            params={"history": "conv-alpha"},
            headers=auth_headers(alice_token),
        ).json()["records"]
        assert [(rec["version"], rec["status"]) for rec in history] == [
            (1, "superseded"),
            (2, "draft"),
        ]

    def test_history_lists_both_versions_with_content(
        self, client: TestClient, proposal: dict, alice_token: str
    ) -> None:
        client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={
                "handle": "conv-alpha",
                "kind": "amendment",
                "content": "amended text",
                "supersedes": "conv-alpha",
            },
        )
        history = client.get(
            "/api/v1/governance/records",
            params={"history": "conv-alpha"},
            headers=auth_headers(alice_token),
        )
        assert history.status_code == 200
        records = history.json()["records"]
        assert [rec["version"] for rec in records] == [1, 2]
        assert records[0]["content"] == f"be excellent {SENTINEL}"
        assert records[1]["content"] == "amended text"

    def test_supersedes_must_reference_own_handle(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={
                "handle": "conv-beta",
                "kind": "amendment",
                "content": "text",
                "supersedes": "conv-alpha",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PAYLOAD"

    def test_supersedes_unknown_handle_404(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={
                "handle": "conv-ghost",
                "kind": "amendment",
                "content": "text",
                "supersedes": "conv-ghost",
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_GOVERNANCE_RECORD"

    def test_duplicate_handle_without_supersedes_409(self, client, proposal, alice_token) -> None:
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "conv-alpha", "kind": "proposal", "content": "again"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "GOVERNANCE_EXISTS"

    def test_invalid_handle_rejected(self, client, alice_token) -> None:
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "BAD HANDLE", "kind": "proposal", "content": "x"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_GOVERNANCE_HANDLE"

    def test_oversized_content_413(self, client, alice_token) -> None:
        max_bytes = client.app.state.settings.max_memory_entry_bytes
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "conv-big", "kind": "proposal", "content": "x" * (max_bytes + 1)},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    def test_unknown_record_404(self, client, alice_token) -> None:
        response = client.get(
            f"/api/v1/governance/records/{uuid.uuid4()}", headers=auth_headers(alice_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_GOVERNANCE_RECORD"

    def test_unauthenticated_rejected(self, client) -> None:
        assert (
            client.post(
                "/api/v1/governance/records",
                json={"handle": "conv-anon", "kind": "proposal", "content": "x"},
            ).status_code
            == 401
        )
        assert client.get("/api/v1/governance/records").status_code == 401


class TestGovernanceListing:
    def test_list_and_kind_filter(self, client: TestClient, alice_token: str) -> None:
        for handle, kind in (("conv-one", "proposal"), ("conv-two", "convention")):
            created = client.post(
                "/api/v1/governance/records",
                headers=auth_headers(alice_token),
                json={"handle": handle, "kind": kind, "content": f"{handle} text"},
            )
            assert created.status_code == 201
        listing = client.get(
            "/api/v1/governance/records", headers=auth_headers(alice_token)
        ).json()["records"]
        assert {rec["handle"] for rec in listing} == {"conv-one", "conv-two"}
        filtered = client.get(
            "/api/v1/governance/records",
            params={"kind": "convention"},
            headers=auth_headers(alice_token),
        ).json()["records"]
        assert {rec["handle"] for rec in filtered} == {"conv-two"}


class TestGovernanceEncryptedAtRest:
    def test_sentinel_absent_from_every_database_column(
        self, client: TestClient, database, proposal: dict
    ) -> None:
        with database.session() as session:
            assert session.scalar(select(sa.func.count()).select_from(GovernanceRecord)) == 1
            row = session.scalar(select(GovernanceRecord))
            envelope = row.envelope
            assert set(envelope) == {"v", "kid", "alg", "nonce", "ciphertext", "tag", "aad"}
            assert envelope["aad"] == f"governance:proposal:{row.handle}:1"
            int(envelope["ciphertext"], 16)  # hex ciphertext only

        engine = database.engine
        with engine.connect() as connection:
            for table in Base.metadata.sorted_tables:
                for db_row in connection.execute(sa.select(table)).fetchall():
                    for value in db_row:
                        assert SENTINEL not in str(value), (
                            f"plaintext sentinel leaked into table {table.name!r}"
                        )
