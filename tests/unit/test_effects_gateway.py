"""Effect-request authorization gateway tests (R02-B2 E1, EFF-01..03/06/16/17).

Authorization-skeleton acceptance tests executed AFTER implementation (no
RED baseline exists — the baseline has no effect system at all; prompt §8:
a missing module is NOT a RED result). Negative tests prove the fail-closed
gates with EXACT counts (zero rows / zero receipts / exact denial events).

Coverage map (test matrix classification — direct E1):
- EFF-01 (authorization slice): happy-path INSERT semantics.
- EFF-02: unauthorized capability → 422, ZERO rows, task axis unchanged.
- EFF-03 (lease-freshness form): expired lease → 409 LEASE_SUPERSEDED, zero rows.
- EFF-06a: duplicate (task, capability, resource_key) → 409 conflict.
- EFF-16: approval required/expired/digest-mismatch/used, non-consumption on failure.
- EFF-17(d): no caller-asserted identity channel exists — agent id comes from
  the authoritative task row (structural assertion).
- Prompt §6 negative proofs: disabled-by-default, emergency stop, unknown
  REAL capability strings, operation-schema violations, queue bound.
"""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.config.settings import Settings
from commons_core.database import models
from commons_core.database.enums import AgentStatus, EffectStatus, TaskStatus
from commons_core.effects import (
    CAPABILITY_ALLOWLIST,
    DuplicateEffectError,
    EffectAgentNotActiveError,
    EffectApprovalInvalidError,
    EffectApprovalRequiredError,
    EffectNotActiveError,
    EffectQueueFullError,
    EffectsDisabledError,
    EffectServicePausedError,
    InvalidEffectOperationError,
    StaleEffectLeaseError,
    UnknownEffectCapabilityError,
    operation_digest,
    request_effect,
)
from commons_core.infrastructure.system_state import set_emergency_stop

T0 = datetime(2030, 1, 1, tzinfo=UTC)


def effects_settings(**overrides) -> Settings:
    base = {"effects_enabled": True, "env": "test", "master_keys": "", "log_level": "WARNING"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def running_task(session_agent):
    """A task in the authoritative RUNNING state with a live lease (gen 1)."""
    from commons_core.database import models as m

    assert session_agent.status == AgentStatus.ACTIVE
    return m.ScheduledTask(
        kind="noop",
        payload={},
        agent_id=session_agent.id,
        status=TaskStatus.RUNNING,
        run_at=T0,
        lease_expires_at=T0 + timedelta(seconds=30),
        lease_generation=1,
        attempts=1,
    )


def authorize(session, task, capability="fx.echo", operation=None, *, settings=None, **kwargs):
    if operation is None:
        operation = {
            "schema_version": 1,
            "capability": capability,
            "parameters": {"message": "hello"},
        }
    return request_effect(session, task, capability, operation, now=T0, settings=settings, **kwargs)


class TestEnabledGateway:
    def test_authorized_request_creates_outbox_row(self, session, session_agent, running_task):
        session.add(running_task)
        session.flush()
        row = authorize(session, running_task, settings=effects_settings())
        session.commit()

        assert row.status == EffectStatus.AUTHORIZED
        assert row.task_id == running_task.id
        # Agent identity is the AUTHORITATIVE task-row value — there is no
        # caller-asserted identity parameter in the API at all (EFF-17d).
        assert row.agent_id == session_agent.id
        assert row.claim_generation == 1  # snapshot of the live lease generation
        assert row.attempts == 0
        assert row.claim_epoch == 0
        assert row.submitted_at is None
        assert row.request_digest == operation_digest(row.operation)
        spec = CAPABILITY_ALLOWLIST["fx.echo"]
        assert row.risk_level == spec.risk
        assert row.max_attempts == spec.max_attempts

    def test_zero_rows_when_task_not_running(self, session, session_agent, running_task):
        running_task.status = TaskStatus.PENDING
        session.add(running_task)
        session.flush()
        with pytest.raises(EffectNotActiveError):
            authorize(session, running_task, settings=effects_settings())
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_expired_lease_rejected_409_superseded(self, session, session_agent, running_task):
        running_task.lease_expires_at = T0 - timedelta(seconds=1)
        session.add(running_task)
        session.flush()
        with pytest.raises(StaleEffectLeaseError) as excinfo:
            authorize(session, running_task, settings=effects_settings())
        assert excinfo.value.code == "LEASE_SUPERSEDED"
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_suspended_agent_rejected(self, session, session_agent, running_task):
        session_agent.status = AgentStatus.SUSPENDED
        session.add(running_task)
        session.flush()
        with pytest.raises(EffectAgentNotActiveError):
            authorize(session, running_task, settings=effects_settings())
        session.commit()
        assert session.query(models.EffectRequest).count() == 0


class TestFailClosedDefaults:
    def test_disabled_by_default_403_zero_rows(self, session, running_task):
        session.add(running_task)
        session.flush()
        # The PRODUCTION default: Settings() with no effects_enabled override.
        with pytest.raises(EffectsDisabledError):
            authorize(session, running_task, settings=Settings(env="test", master_keys=""))
        session.commit()
        assert session.query(models.EffectRequest).count() == 0
        # Same-transaction denial event recorded (feature-gate denial).
        assert session.query(models.SecurityEvent).count() == 1

    def test_emergency_stop_503_zero_rows(self, session, running_task):
        session.add(running_task)
        set_emergency_stop(session, active=True, principal="admin", now=T0)
        with pytest.raises(EffectServicePausedError):
            authorize(session, running_task, settings=effects_settings())
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_real_capability_strings_rejected_deterministically(self, session, running_task):
        session.add(running_task)
        session.flush()
        # Prompt §7 priority scenario "Unsupported real capability rejection"
        # plus EFF-17: EVERY real-world capability string fails with 422.
        for capability in (
            "http:post",
            "smtp:send",
            "effect:http-post",
            "file:write",
            "payment:charge",
            "agent:invoke",
            "fx.totally_made_up",
            "",
        ):
            with pytest.raises(UnknownEffectCapabilityError) as excinfo:
                authorize(session, running_task, capability=capability, settings=effects_settings())
            assert excinfo.value.status_code == 422
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_allowlist_is_fake_only_code_constants(self):
        # G-E3' suite-level fake-only assertion: every allowlisted capability
        # is an fx.* synthetic; irreversible risk has NO member.
        for name, spec in CAPABILITY_ALLOWLIST.items():
            assert name.startswith("fx."), f"non-synthetic capability {name!r}"
            assert spec.risk in ("low", "medium", "high")
        assert "fx.requires_approval" in CAPABILITY_ALLOWLIST


class TestOperationSchema:
    def test_descriptor_schema_enforced(self, session, running_task):
        session.add(running_task)
        session.flush()
        cfg = effects_settings()
        bad_operations = [
            "not-a-dict",  # not an object
            {"schema_version": 2, "capability": "fx.echo", "parameters": {}},  # version
            {"schema_version": 1, "parameters": {}},  # missing capability
            {"schema_version": 1, "capability": "fx.echo"},  # missing parameters
            {
                "schema_version": 1,
                "capability": "fx.echo",
                "parameters": {"message": 123},
            },  # non-string value
            {
                "schema_version": 1,
                "capability": "fx.echo",
                "parameters": {"message": "x" * 257},
            },  # oversize value
            {
                "schema_version": 1,
                "capability": "fx.echo",
                "parameters": {"message": "ok", "url": "http://evil"},
            },  # off-schema key
            {
                "schema_version": 1,
                "capability": "fx.echo",
                "parameters": {"message": ""},
            },  # empty required
            {
                "schema_version": 1,
                "capability": "fx.echo",
                "parameters": {"secret": "x"},
            },  # forbidden key
        ]
        for operation in bad_operations:
            with pytest.raises(InvalidEffectOperationError):
                authorize(session, running_task, operation=operation, settings=cfg)
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_digest_binds_exact_canonical_form(self):
        digest_a = operation_digest(
            {"schema_version": 1, "capability": "fx.echo", "parameters": {"message": "m"}}
        )
        digest_b = operation_digest(
            {"parameters": {"message": "m"}, "capability": "fx.echo", "schema_version": 1}
        )
        assert digest_a == digest_b  # key order is canonicalized
        digest_c = operation_digest(
            {"schema_version": 1, "capability": "fx.echo", "parameters": {"message": "m2"}}
        )
        assert digest_a != digest_c  # content changes break the digest


class TestDedupAndBounds:
    def test_duplicate_resource_key_conflict(self, session, running_task):
        session.add(running_task)
        session.flush()
        cfg = effects_settings()
        authorize(session, running_task, settings=cfg, resource_key="r1")
        session.commit()
        with pytest.raises(DuplicateEffectError):
            authorize(session, running_task, settings=cfg, resource_key="r1")
        session.commit()
        assert session.query(models.EffectRequest).count() == 1

    def test_null_resource_key_exempt_from_dedup(self, session, running_task):
        session.add(running_task)
        session.flush()
        cfg = effects_settings()
        authorize(session, running_task, settings=cfg)
        authorize(session, running_task, settings=cfg)
        session.commit()
        # Documented NULL semantics (SQLite/PG UNIQUE-with-NULL behavior).
        assert session.query(models.EffectRequest).count() == 2

    def test_queue_full_429(self, session, running_task):
        session.add(running_task)
        session.flush()
        cfg = effects_settings(effects_max_in_flight=1)
        authorize(session, running_task, settings=cfg)
        with pytest.raises(EffectQueueFullError):
            authorize(session, running_task, settings=cfg)
        session.commit()
        assert session.query(models.EffectRequest).count() == 1


class TestApprovalBinding:
    OPERATION = {
        "schema_version": 1,
        "capability": "fx.requires_approval",
        "parameters": {"note": "high-impact"},
    }

    def _approval(self, session, *, capability="fx.requires_approval", digest=None, used=None):
        approval = models.EffectApproval(
            approver="human-op",
            capability=capability,
            bound_request_digest=digest or operation_digest(self.OPERATION),
            expires_at=T0 + timedelta(hours=1),
            used_at=used,
        )
        session.add(approval)
        session.flush()
        return approval

    def test_high_risk_requires_approval(self, session, running_task):
        session.add(running_task)
        session.flush()
        with pytest.raises(EffectApprovalRequiredError):
            authorize(
                session,
                running_task,
                "fx.requires_approval",
                self.OPERATION,
                settings=effects_settings(),
            )
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_high_risk_success_consumes_approval_atomically(self, session, running_task):
        session.add(running_task)
        session.flush()
        approval = self._approval(session)
        row = authorize(
            session,
            running_task,
            "fx.requires_approval",
            self.OPERATION,
            settings=effects_settings(),
            approval_id=approval.id,
        )
        session.commit()
        assert row.approval_id == approval.id
        assert approval.used_at is not None  # consumed IN the authorization transaction

    def test_failed_attempts_never_consume_approval(self, session, running_task):
        session.add(running_task)
        session.flush()
        # Expired approval: request fails, approval untouched (EFF-16c).
        approval = self._approval(session)
        approval.expires_at = T0 - timedelta(seconds=1)
        with pytest.raises(EffectApprovalInvalidError):
            authorize(
                session,
                running_task,
                "fx.requires_approval",
                self.OPERATION,
                settings=effects_settings(),
                approval_id=approval.id,
            )
        session.commit()
        assert approval.used_at is None
        assert session.query(models.EffectRequest).count() == 0

    def test_approval_digest_binding_blocks_replay(self, session, running_task):
        session.add(running_task)
        session.flush()
        # Approval bound to a DIFFERENT operation digest — replay fails (EFF-16b).
        other = {
            "schema_version": 1,
            "capability": "fx.requires_approval",
            "parameters": {"note": "different-content"},
        }
        approval = self._approval(session, digest=operation_digest(other))
        with pytest.raises(EffectApprovalInvalidError):
            authorize(
                session,
                running_task,
                "fx.requires_approval",
                self.OPERATION,
                settings=effects_settings(),
                approval_id=approval.id,
            )
        session.commit()
        assert session.query(models.EffectRequest).count() == 0

    def test_used_approval_rejected(self, session, running_task):
        session.add(running_task)
        session.flush()
        approval = self._approval(session, used=T0 - timedelta(minutes=5))
        with pytest.raises(EffectApprovalInvalidError):
            authorize(
                session,
                running_task,
                "fx.requires_approval",
                self.OPERATION,
                settings=effects_settings(),
                approval_id=approval.id,
            )
        session.commit()
        assert session.query(models.EffectRequest).count() == 0


class TestAuditMinimization:
    def test_denial_events_carry_counts_and_ids_only(self, session, running_task):
        session.add(running_task)
        session.flush()
        with pytest.raises(UnknownEffectCapabilityError):
            authorize(session, running_task, capability="http:post", settings=effects_settings())
        session.commit()
        events = session.query(models.SecurityEvent).all()
        assert len(events) == 1
        details = events[0].details
        assert details["capability"] == "http:post"
        assert details["reason"] == "unknown_capability"
        # No operation parameters ever reach the event details.
        assert "parameters" not in details
        assert "message" not in details

    def test_transition_event_recorded_on_authorization(self, session, running_task):
        session.add(running_task)
        session.flush()
        row = authorize(session, running_task, settings=effects_settings())
        session.commit()
        events = (
            session.query(models.EffectEvent)
            .filter_by(effect_id=row.id)
            .order_by(models.EffectEvent.id)
            .all()
        )
        assert [(e.from_status, e.to_status, e.reason) for e in events] == [
            (None, "authorized", "authorized")
        ]
        # No operation parameters in the audit trail (INV-EF-R8).
        assert all("parameters" not in (e.reason or "") for e in events)
