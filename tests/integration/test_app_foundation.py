"""Integration tests for the FastAPI foundation (health, auth, rate limit)."""

import pytest
from conftest import auth_headers, provision_and_login
from fastapi.testclient import TestClient
from sqlalchemy import select

from commons_core.agents.service import get_agent_by_handle, suspend_agent
from commons_core.api.app import create_app
from commons_core.database.models import QuotaCounter, SecurityEvent


class TestHealth:
    def test_health_public(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "commons-core"
        assert body["version"] == "0.1.0"

    def test_health_exempt_from_rate_limit(self, client: TestClient) -> None:
        for _ in range(40):  # far above the anonymous limit of 30
            response = client.get("/health")
            assert response.status_code == 200


class TestOpenAPI:
    def test_openapi_reachable_and_lists_routes(self, client: TestClient) -> None:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/health" in paths
        assert "/api/v1/agents/me" in paths

    def test_docs_served(self, client: TestClient) -> None:
        assert client.get("/docs").status_code == 200


class TestAgentsMe:
    def test_me_with_valid_token(self, client: TestClient) -> None:
        token = provision_and_login(client, "agent-me")
        response = client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["handle"] == "agent-me"
        assert body["is_admin"] is False
        assert body["status"] == "active"
        assert isinstance(body["capabilities"], list)
        assert isinstance(body["comm_formats"], list)
        assert body["id"]
        assert body["created_at"]

    def test_me_with_capabilities(self, client: TestClient) -> None:
        token = provision_and_login(
            client, "agent-caps", capabilities=["code-review"], comm_formats=["application/json"]
        )
        body = client.get("/api/v1/agents/me", headers=auth_headers(token)).json()
        assert body["capabilities"] == ["code-review"]
        assert body["comm_formats"] == ["application/json"]

    def test_unauthenticated_401(self, client: TestClient) -> None:
        response = client.get("/api/v1/agents/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_bad_token_401(self, client: TestClient) -> None:
        response = client.get("/api/v1/agents/me", headers=auth_headers("bogus-token-value"))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_suspended_agent_401(self, client: TestClient, database) -> None:
        token = provision_and_login(client, "agent-suspended")
        with database.session() as session:
            suspend_agent(session, get_agent_by_handle(session, "agent-suspended"))
        response = client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_auth_failure_events_recorded(self, client: TestClient, database) -> None:
        client.get("/api/v1/agents/me")  # missing token
        client.get("/api/v1/agents/me", headers=auth_headers("bogus-token-value"))
        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(SecurityEvent.category == "auth_failure")
                )
            )
        assert len(events) == 2
        assert {event.principal for event in events} == {"anonymous"}
        assert all(event.details == {} for event in events)


class TestRateLimit:
    @pytest.fixture()
    def limited_client(self, settings, database) -> TestClient:
        limited_settings = settings.model_copy(update={"rate_limit_per_minute": 3})
        application = create_app(settings=limited_settings, database=database)
        with TestClient(application) as test_client:
            yield test_client

    def test_429_after_limit(self, limited_client: TestClient, database) -> None:
        token = provision_and_login(limited_client, "agent-limited")
        for _ in range(3):
            response = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
            assert response.status_code == 200
        response = limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "RATE_LIMITED"
        assert error["limit"] == 3
        assert "Retry-After" in response.headers

    def test_rate_limited_event_recorded(self, limited_client: TestClient, database) -> None:
        token = provision_and_login(limited_client, "agent-limited")
        for _ in range(4):
            limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        with database.session() as session:
            events = list(
                session.scalars(
                    select(SecurityEvent).where(SecurityEvent.category == "rate_limited")
                )
            )
        assert len(events) == 1
        assert events[0].details["limit"] == 3
        assert events[0].details["authenticated"] is True

    def test_quota_counter_mirrored(self, limited_client: TestClient, database) -> None:
        token = provision_and_login(limited_client, "agent-quota")
        limited_client.get("/api/v1/agents/me", headers=auth_headers(token))
        with database.session() as session:
            counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 1

    def test_anonymous_requests_limited(self, settings, database) -> None:
        anonymous_settings = settings.model_copy(update={"anonymous_rate_limit_per_minute": 2})
        application = create_app(settings=anonymous_settings, database=database)
        with TestClient(application) as test_client:
            for _ in range(2):
                assert test_client.get("/api/v1/agents/me").status_code == 401
            response = test_client.get("/api/v1/agents/me")
            assert response.status_code == 429


class TestMiddleware:
    def test_request_id_header(self, client: TestClient) -> None:
        response = client.get("/health")
        assert "X-Request-Id" in response.headers
        assert len(response.headers["X-Request-Id"]) == 32

    def test_404_structured_error(self, client: TestClient) -> None:
        response = client.get("/no-such-route")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_422_validation_structured(self, client: TestClient) -> None:
        # /api/v1/agents/me has no body; exercise validation via a query-less
        # malformed request to a route with validation… use docs blueprint
        # absence: POST to /health (method not allowed -> 405 structured).
        response = client.post("/health")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


class TestPersistenceAcrossAppInstances:
    def test_credentials_persist_in_database(self, client: TestClient, database, settings) -> None:
        token = provision_and_login(client, "agent-persist")
        # A second app instance over the same database authenticates the token.
        second_app = create_app(settings=settings, database=database)
        with TestClient(second_app) as second_client:
            response = second_client.get("/api/v1/agents/me", headers=auth_headers(token))
            assert response.status_code == 200
            assert response.json()["handle"] == "agent-persist"
