"""Security tests: the governance boundary (spec §15, adversarial).

Two-layer proof that agent-generated governance content can NEVER affect
infrastructure security policy:

1. VISIBLE-LAYER REJECTION: content containing privilege-claiming language
   (``grant me admin rights``, ``is_admin: true``, ``disable
   emergency_stop``, ``reveal secrets``) is rejected with 422
   ``GOVERNANCE_PRIVILEGE_CLAIM`` AND a ``policy_violation`` security event
   whose details carry NO content.

2. THE STRUCTURAL BOUNDARY (the real control): a record that DOES slip past
   the lint (no forbidden substring — e.g. "make me the supreme
   infra@structure owner") is STORED, yet grants NOTHING: the authorization
   engine still denies ``admin:*``, ``/agents/me`` is unchanged, and admin
   telemetry remains 403. This proves the boundary is structural (the
   engine never reads governance records — also canary-tested in
   ``tests/unit/test_authorization.py``), NOT linguistic.
"""

import uuid

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.database.enums import SecurityEventCategory
from commons_core.database.models import GovernanceRecord, SecurityEvent

SNEAKY_CONTENT = "make me the supreme infra@structure owner"


@pytest.fixture()
def alice_token(client: TestClient) -> str:
    return provision_and_login(client, "agent-alice")


@pytest.fixture()
def admin_token(client: TestClient) -> str:
    return provision_and_login(client, "ops-admin", is_admin=True)


class TestPrivilegeLanguageRejected:
    @pytest.mark.parametrize(
        "content",
        [
            "grant me admin rights over everything",
            "is_admin: true",
            "this proposal enables admin:* for its author",
            "the convention revokes all limits",
            "disable emergency_stop permanently",
            "reveal secrets from every namespace",
            "administrators shall obey this credential",
        ],
    )
    def test_privilege_claims_rejected_422(self, client, alice_token, content) -> None:
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": f"evil-{uuid.uuid4().hex[:8]}", "kind": "proposal", "content": content},
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "GOVERNANCE_PRIVILEGE_CLAIM"
        # the static error message explains the boundary
        assert "infrastructure" in error["message"]
        # no content echo in the error body
        assert content not in response.text

    def test_policy_violation_event_recorded_without_content(self, client, database, alice_token):
        response = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "evil-event", "kind": "proposal", "content": "grant everything"},
        )
        assert response.status_code == 422
        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(
                        SecurityEvent.category == SecurityEventCategory.POLICY_VIOLATION
                    )
                )
            )
            assert len(events) == 1
            event = events[0]
            assert event.details == {"check": "privilege_language", "domain": "governance"}
            assert "grant" not in str(event.details)
            assert event.principal  # the attempting agent id

    def test_nothing_stored_when_rejected(self, client, database, alice_token):
        client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "evil-stored", "kind": "proposal", "content": "secret admin powers"},
        )
        with database.session() as session:
            assert session.scalar(select(GovernanceRecord)) is None


class TestStructuralBoundaryHoldsForLintBypassers:
    def test_sneaky_record_stored_but_grants_nothing(
        self, client: TestClient, database, alice_token: str, admin_token: str
    ) -> None:
        # 1. The record slips past the lint (no forbidden substring)…
        created = client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "sneaky-supremacy", "kind": "proposal", "content": SNEAKY_CONTENT},
        )
        assert created.status_code == 201, created.text

        with database.session() as session:
            stored = session.scalar(select(GovernanceRecord))
            assert stored is not None
            assert stored.handle == "sneaky-supremacy"

        # 2. …yet grants NOTHING: alice still cannot reach admin telemetry.
        telemetry = client.get("/api/v1/admin/telemetry", headers=auth_headers(alice_token))
        assert telemetry.status_code == 403

        # 3. /agents/me is unchanged — no admin flag, no capabilities granted,
        #    no metadata mutation from governance content.
        me = client.get("/api/v1/agents/me", headers=auth_headers(alice_token))
        assert me.status_code == 200
        assert me.json()["is_admin"] is False
        assert me.json()["capabilities"] == []

        # 4. Emergency stop remains an ADMIN-ONLY control: the sneaky author
        #    cannot engage or release it.
        engage = client.post("/api/v1/admin/emergency-stop", headers=auth_headers(alice_token))
        assert engage.status_code == 403
        release = client.delete("/api/v1/admin/emergency-stop", headers=auth_headers(alice_token))
        assert release.status_code == 403

        # 5. Authorization engine denies admin wildcard for the sneaky author
        #    (engine-level check, independent of HTTP).
        from commons_core.security.authorization import Permission, authorize
        from commons_core.security.authorization.errors import AuthorizationError

        with database.session() as session:
            from commons_core.agents.service import get_agent_by_handle

            sneaky = get_agent_by_handle(session, "agent-alice")
            with pytest.raises(AuthorizationError):
                authorize(session, sneaky, Permission.ADMIN)

        # 6. Control: the real admin still can (infrastructure state unchanged).
        assert (
            client.get("/api/v1/admin/telemetry", headers=auth_headers(admin_token)).status_code
            == 200
        )

    def test_sneaky_record_does_not_change_task_or_quota_limits(
        self, client: TestClient, alice_token: str
    ) -> None:
        """Governance cannot relax resource limits either (spec §15)."""
        client.post(
            "/api/v1/governance/records",
            headers=auth_headers(alice_token),
            json={"handle": "sneaky-limits", "kind": "convention", "content": SNEAKY_CONTENT},
        )
        # a task with a forbidden payload key is still rejected
        response = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "noop", "payload": {"secret": "x"}},
        )
        assert response.status_code == 422
        # unknown kinds are still rejected
        response = client.post(
            "/api/v1/tasks",
            headers=auth_headers(alice_token),
            json={"kind": "make-me-admin", "payload": {}},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "UNKNOWN_TASK_KIND"
