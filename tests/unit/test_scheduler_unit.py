"""Unit tests: scheduler mechanics (backoff math, payload lint, claim order,
transition guards) and the activity status transition matrix (spec §16, §14).
"""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.activities.service import transition_status
from commons_core.agents.service import provision_agent
from commons_core.api.errors import InvalidStateError
from commons_core.database.enums import ActivityStatus
from commons_core.database.models import Activity
from commons_core.scheduler import registry, service
from commons_core.security.authorization.errors import AuthorizationError

T0 = datetime(2030, 1, 1, tzinfo=UTC)


class TestBackoffMath:
    @pytest.mark.parametrize(
        ("attempts", "expected"),
        [
            (1, 2),
            (2, 4),
            (3, 8),
            (4, 16),
            (5, 32),
            (6, 60),  # 2^6 = 64 capped at 60
            (7, 60),
            (12, 60),
        ],
    )
    def test_exponential_backoff_with_cap(self, attempts, expected) -> None:
        assert service.backoff_delay(attempts) == expected

    def test_negative_attempts_rejected(self) -> None:
        with pytest.raises(ValueError):
            service.backoff_delay(-1)


class TestPayloadLint:
    def test_forbidden_keys_found_at_any_depth(self) -> None:
        payload = {"a": 1, "nested": {"list": [{"token": "x"}]}}
        assert service.find_forbidden_payload_keys(payload) == ["token"]

    def test_case_insensitive_match(self) -> None:
        assert service.find_forbidden_payload_keys({"SECRET": 1}) == ["SECRET"]
        assert service.find_forbidden_payload_keys({"Credentials": 1}) == ["Credentials"]

    def test_allowed_keys_pass(self) -> None:
        assert service.find_forbidden_payload_keys({"note": "x", "activity_id": "y", "n": 5}) == []

    def test_non_dict_payloads_scanned_safely(self) -> None:
        assert service.find_forbidden_payload_keys([{"secret": 1}, 3, "text"]) == ["secret"]

    def test_deep_nesting_rejected(self) -> None:
        payload: dict = {}
        node = payload
        for _ in range(40):
            node["child"] = {}
            node = node["child"]
        with pytest.raises(service.PayloadNestingTooDeepError):
            service.find_forbidden_payload_keys(payload)

    def test_payload_size_is_canonical_json_bytes(self) -> None:
        assert service._payload_size_bytes({"a": 1, "b": 2}) == len(b'{"a":1,"b":2}')


class TestClaimMechanics:
    def test_claim_orders_oldest_run_at_first(self, session, session_agent) -> None:
        ids = []
        for seconds in (30, 10, 20):
            task, _ = service.create_task(
                session,
                session_agent,
                kind="noop",
                payload={},
                run_at=T0 + timedelta(seconds=seconds),
                max_pending_tasks=1000,
                max_task_attempts=3,
            )
            ids.append(task.id)
        claimed = service.claim_next(session, T0 + timedelta(seconds=60), 30)
        assert claimed.id == ids[1]  # run_at = T0+10 is oldest
        assert claimed.status == service.TaskStatus.LEASED
        assert claimed.attempts == 1
        assert claimed.lease_expires_at is not None

    def test_claim_skips_none_when_not_yet_due(self, session, session_agent) -> None:
        service.create_task(
            session,
            session_agent,
            kind="noop",
            payload={},
            run_at=T0 + timedelta(hours=1),
            max_pending_tasks=1000,
            max_task_attempts=3,
        )
        assert service.claim_next(session, T0, 30) is None

    def test_claim_requires_positive_lease(self, session) -> None:
        with pytest.raises(ValueError):
            service.claim_next(session, T0, 0)

    def test_execute_requires_leased_state(self, session, session_agent) -> None:
        task, _ = service.create_task(
            session,
            session_agent,
            kind="noop",
            payload={},
            max_pending_tasks=1000,
            max_task_attempts=3,
        )
        with pytest.raises(InvalidStateError):
            # lease_generation=0: the task was never claimed (INV-9 — no
            # authorized execution path at generation 0); the fail-fast
            # status pre-check fires before any database access.
            service.execute_claimed(session, task, T0, lease_generation=0)

    def test_cancel_only_pending(self, session, session_agent) -> None:
        task, _ = service.create_task(
            session,
            session_agent,
            kind="noop",
            payload={},
            max_pending_tasks=1000,
            max_task_attempts=3,
        )
        service.cancel_task(session, session_agent, task)
        assert task.status == service.TaskStatus.CANCELLED
        with pytest.raises(InvalidStateError):
            service.cancel_task(session, session_agent, task)  # terminal now

    def test_unknown_kind_rejected_at_create(self, session, session_agent) -> None:
        with pytest.raises(service.UnknownTaskKindError):
            service.create_task(
                session,
                session_agent,
                kind="does-not-exist",
                payload={},
                max_pending_tasks=1000,
                max_task_attempts=3,
            )

    def test_registry_roundtrip(self) -> None:
        registry.register("test-temp", lambda session, task: None)
        try:
            assert registry.get_handler("test-temp") is not None
            assert "test-temp" in registry.known_kinds()
        finally:
            registry.unregister("test-temp")
        assert registry.get_handler("test-temp") is None
        registry.unregister("test-temp")  # no error when absent


class TestWorkerArgumentGuards:
    def test_max_tasks_must_be_positive(self) -> None:
        from commons_core.scheduler.worker import run_worker

        with pytest.raises(ValueError):
            run_worker(lambda: None, once=True, max_tasks=0)


class TestActivityTransitionMatrix:
    @pytest.fixture()
    def activity_row(self, session, session_agent) -> Activity:
        activity = Activity(handle="act-matrix", created_by=session_agent.id)
        session.add(activity)
        session.flush()
        return activity

    @pytest.mark.parametrize(
        ("start", "target", "allowed"),
        [
            ("proposed", "active", True),
            ("active", "completed", True),
            ("active", "abandoned", True),
            ("proposed", "completed", False),
            ("proposed", "abandoned", False),
            ("proposed", "proposed", False),
            ("active", "active", False),
            ("active", "proposed", False),
            ("completed", "active", False),
            ("completed", "abandoned", False),
            ("abandoned", "active", False),
            ("abandoned", "completed", False),
        ],
    )
    def test_matrix(self, session, session_agent, activity_row, start, target, allowed) -> None:
        from commons_core.database.enums import ActivityStatus as Status

        activity_row.status = Status(start)
        session.flush()
        if allowed:
            transition_status(session, session_agent, activity_row, Status(target))
            assert activity_row.status == Status(target)
        else:
            with pytest.raises(InvalidStateError):
                transition_status(session, session_agent, activity_row, Status(target))

    def test_non_creator_cannot_transition(self, session, session_agent, activity_row) -> None:
        other = provision_agent(session, "agent-matrix-other")
        with pytest.raises(AuthorizationError):
            transition_status(session, other, activity_row, ActivityStatus.ACTIVE)
