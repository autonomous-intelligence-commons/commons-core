"""Integration tests: shared memory happy paths (spec §11, §12)."""

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient


@pytest.fixture()
def owner_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-owner")


@pytest.fixture()
def namespace(client: TestClient, owner_token: str) -> dict:
    response = client.post(
        "/api/v1/memory/namespaces",
        headers=auth_headers(owner_token),
        json={"handle": "ns-alpha", "description": "test namespace"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _write(client: TestClient, token: str, ns: str, key: str, payload: str, **extra) -> dict:
    response = client.post(
        f"/api/v1/memory/namespaces/{ns}/entries",
        headers=auth_headers(token),
        json={"key": key, "payload": payload, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _read(client: TestClient, token: str, ns: str, key: str) -> dict:
    response = client.get(
        f"/api/v1/memory/namespaces/{ns}/entries/{key}", headers=auth_headers(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestNamespaceLifecycle:
    def test_create_namespace_owner_permissions(self, namespace: dict, owner_token: str) -> None:
        body = namespace
        assert body["handle"] == "ns-alpha"
        assert body["created_by_handle"] == "agent-owner"
        assert body["my_permissions"] == {"is_owner": True, "can_read": True, "can_write": True}
        assert body["grants"] == []

    def test_duplicate_handle_conflict(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        response = client.post(
            "/api/v1/memory/namespaces",
            headers=auth_headers(owner_token),
            json={"handle": "ns-alpha"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NAMESPACE_EXISTS"

    def test_invalid_handle_rejected(self, client: TestClient, owner_token: str) -> None:
        response = client.post(
            "/api/v1/memory/namespaces",
            headers=auth_headers(owner_token),
            json={"handle": "NOT VALID"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_NAMESPACE_HANDLE"


class TestEntryVersioning:
    def test_write_read_roundtrip(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        written = _write(client, owner_token, "ns-alpha", "greeting", "hello world")
        assert written["version"] == 1
        assert written["payload"] == "hello world"
        assert written["author"] == "agent-owner"
        assert written["superseded"] is False
        assert written["size_bytes"] == len(b"hello world")

        read = _read(client, owner_token, "ns-alpha", "greeting")
        assert read["payload"] == "hello world"
        assert read["version"] == 1

    def test_rewrite_creates_version_two_and_supersedes(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        _write(client, owner_token, "ns-alpha", "counter", "v1-content")
        second = _write(client, owner_token, "ns-alpha", "counter", "v2-content")
        assert second["version"] == 2
        assert second["superseded"] is False

        latest = _read(client, owner_token, "ns-alpha", "counter")
        assert latest["version"] == 2
        assert latest["payload"] == "v2-content"

        history = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries/counter/history",
            headers=auth_headers(owner_token),
        )
        assert history.status_code == 200
        versions = history.json()["versions"]
        assert [entry["version"] for entry in versions] == [1, 2]
        assert versions[0]["superseded"] is True
        assert versions[0]["payload"] == "v1-content"  # history is immutable, readable
        assert versions[1]["superseded"] is False

    def test_third_write_continues_chain(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        _write(client, owner_token, "ns-alpha", "k", "a")
        _write(client, owner_token, "ns-alpha", "k", "b")
        third = _write(client, owner_token, "ns-alpha", "k", "c")
        assert third["version"] == 3
        history = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries/k/history",
            headers=auth_headers(owner_token),
        ).json()["versions"]
        assert [entry["version"] for entry in history] == [1, 2, 3]
        assert [entry["superseded"] for entry in history] == [True, True, False]

    def test_keys_are_independent(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        _write(client, owner_token, "ns-alpha", "key-one", "one")
        _write(client, owner_token, "ns-alpha", "key-two", "two")
        assert _read(client, owner_token, "ns-alpha", "key-one")["version"] == 1
        assert _read(client, owner_token, "ns-alpha", "key-two")["version"] == 1

    def test_slashed_key_addressed_via_path_converter(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        _write(client, owner_token, "ns-alpha", "notes/task-1", "deep value")
        read = _read(client, owner_token, "ns-alpha", "notes/task-1")
        assert read["payload"] == "deep value"
        _write(client, owner_token, "ns-alpha", "notes/task-1", "updated")
        history = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries/notes/task-1/history",
            headers=auth_headers(owner_token),
        )
        assert history.status_code == 200
        assert len(history.json()["versions"]) == 2

    def test_unknown_entry_404(self, client: TestClient, namespace: dict, owner_token: str) -> None:
        response = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries/missing", headers=auth_headers(owner_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_ENTRY"

        history = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries/missing/history",
            headers=auth_headers(owner_token),
        )
        assert history.status_code == 404

    def test_valid_json_entry_accepted(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        written = _write(
            client, owner_token, "ns-alpha", "json-key", '{"a": 1}', content_type="application/json"
        )
        assert written["payload"] == '{"a": 1}'

    def test_invalid_json_entry_rejected(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        response = client.post(
            "/api/v1/memory/namespaces/ns-alpha/entries",
            headers=auth_headers(owner_token),
            json={"key": "json-bad", "content_type": "application/json", "payload": "{oops"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_PAYLOAD"

    def test_list_keys_reports_latest_versions_only(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        _write(client, owner_token, "ns-alpha", "zeta", "one")
        _write(client, owner_token, "ns-alpha", "zeta", "two")
        _write(client, owner_token, "ns-alpha", "alpha", "solo")
        response = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries", headers=auth_headers(owner_token)
        )
        assert response.status_code == 200
        keys = {entry["key"]: entry["latest_version"] for entry in response.json()["keys"]}
        assert keys == {"alpha": 1, "zeta": 2}
        # no payloads leak through the key listing
        assert "payload" not in response.text
        assert "solo" not in response.text


class TestPermissionSharing:
    def test_owner_grants_read_write_then_read_only(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        colleague_token = provision_and_login(client, "agent-colleague")

        # no access before the grant
        denied = client.get(
            "/api/v1/memory/namespaces/ns-alpha/entries/anything",
            headers=auth_headers(colleague_token),
        )
        assert denied.status_code == 403

        # grant read+write
        grant = client.put(
            "/api/v1/memory/namespaces/ns-alpha/permissions",
            headers=auth_headers(owner_token),
            json={"agent_handle": "agent-colleague", "can_read": True, "can_write": True},
        )
        assert grant.status_code == 200, grant.text
        assert grant.json() == {
            "agent_handle": "agent-colleague",
            "can_read": True,
            "can_write": True,
        }

        _write(client, owner_token, "ns-alpha", "shared", "owner writes")
        colleague_write = _write(client, colleague_token, "ns-alpha", "shared", "colleague writes")
        assert colleague_write["version"] == 2
        assert colleague_write["author"] == "agent-colleague"
        assert _read(client, colleague_token, "ns-alpha", "shared")["payload"] == "colleague writes"

        # the namespace now appears in the colleague's list
        listed = client.get(
            "/api/v1/memory/namespaces", headers=auth_headers(colleague_token)
        ).json()["namespaces"]
        assert [ns["handle"] for ns in listed] == ["ns-alpha"]
        assert listed[0]["my_permissions"] == {
            "is_owner": False,
            "can_read": True,
            "can_write": True,
        }

        # re-grant read-only (upsert): write now denied, read still allowed
        downgrade = client.put(
            "/api/v1/memory/namespaces/ns-alpha/permissions",
            headers=auth_headers(owner_token),
            json={"agent_handle": "agent-colleague", "can_read": True, "can_write": False},
        )
        assert downgrade.status_code == 200
        write_denied = client.post(
            "/api/v1/memory/namespaces/ns-alpha/entries",
            headers=auth_headers(colleague_token),
            json={"key": "shared", "payload": "should fail"},
        )
        assert write_denied.status_code == 403
        assert _read(client, colleague_token, "ns-alpha", "shared")["version"] == 2

    def test_revoke_by_false_flags_removes_access(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        colleague_token = provision_and_login(client, "agent-colleague")
        client.put(
            "/api/v1/memory/namespaces/ns-alpha/permissions",
            headers=auth_headers(owner_token),
            json={"agent_handle": "agent-colleague", "can_read": True, "can_write": False},
        )
        # sanity: with a read grant, a missing key is 404 (readable, just absent)
        assert (
            client.get(
                "/api/v1/memory/namespaces/ns-alpha/entries/missing",
                headers=auth_headers(colleague_token),
            ).status_code
            == 404
        )
        revoked = client.put(
            "/api/v1/memory/namespaces/ns-alpha/permissions",
            headers=auth_headers(owner_token),
            json={"agent_handle": "agent-colleague", "can_read": False, "can_write": False},
        )
        assert revoked.status_code == 200
        assert revoked.json() == {
            "agent_handle": "agent-colleague",
            "can_read": False,
            "can_write": False,
        }
        # revoked: reads are denied and the namespace disappears from the list
        assert (
            client.get(
                "/api/v1/memory/namespaces/ns-alpha/entries/missing",
                headers=auth_headers(colleague_token),
            ).status_code
            == 403
        )
        assert (
            client.get("/api/v1/memory/namespaces", headers=auth_headers(colleague_token)).json()[
                "namespaces"
            ]
            == []
        )

    def test_owner_view_lists_grants(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        provision_and_login(client, "agent-colleague")
        client.put(
            "/api/v1/memory/namespaces/ns-alpha/permissions",
            headers=auth_headers(owner_token),
            json={"agent_handle": "agent-colleague", "can_read": True, "can_write": False},
        )
        detail = client.get(
            "/api/v1/memory/namespaces/ns-alpha", headers=auth_headers(owner_token)
        ).json()
        assert detail["grants"] == [
            {"agent_handle": "agent-colleague", "can_read": True, "can_write": False}
        ]

    def test_grant_unknown_agent_404(
        self, client: TestClient, namespace: dict, owner_token: str
    ) -> None:
        response = client.put(
            "/api/v1/memory/namespaces/ns-alpha/permissions",
            headers=auth_headers(owner_token),
            json={"agent_handle": "agent-ghost", "can_read": True},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_AGENT"

    def test_unknown_namespace_404(self, client: TestClient, owner_token: str) -> None:
        response = client.get(
            "/api/v1/memory/namespaces/ns-ghost/entries/x", headers=auth_headers(owner_token)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "UNKNOWN_NAMESPACE"
