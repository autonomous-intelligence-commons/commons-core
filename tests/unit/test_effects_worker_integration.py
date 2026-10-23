"""Handler→gateway integration through the REAL scheduler pipeline (E1).

The gateway joins the handler's ambient transaction (Phase 0 §2.5), so the
authoritative integration is the REAL ``claim_next`` + ``execute_claimed``
flow: a TEST handler (registry.register — the documented test-only extension
point) requests an effect mid-execution and the task axis's outcome decides
the effect row's durability:

- handler authorizes + task SUCCEEDS → the effect row commits atomically
  with the task transaction (EFF-01 end-to-end, generation snapshot from
  the live claim);
- handler authorizes then RAISES → the task failure rollback discards the
  effect row (atomic task-effect — zero orphan authorizations);
- gateway DENIAL inside the handler → the handler fails, the task follows
  the normal retry/backoff policy (EFF-02 task axis, [SOURCE-derivable]),
  zero effect rows survive.

E1 does NOT modify the worker/registry/`run_worker` in any way: the
dispatcher is a standalone module; wiring a dispatcher loop into the
worker process is a deployment-stage decision documented in
DEPLOYMENT.md (E2 authorization required).
"""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.config.settings import Settings
from commons_core.database import models
from commons_core.database.enums import EffectStatus, TaskStatus
from commons_core.effects import FakeEffectProvider, request_effect
from commons_core.effects.dispatcher import dispatch_tick
from commons_core.scheduler import registry, service

T0 = datetime(2030, 1, 1, tzinfo=UTC)

ECHO = {"schema_version": 1, "capability": "fx.echo", "parameters": {"message": "m"}}


def effects_settings(**overrides) -> Settings:
    base = {"effects_enabled": True, "env": "test", "master_keys": "", "log_level": "WARNING"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def fx_agent_task(session, session_agent):
    """A pending task owned by the fixture agent, ready for a real claim."""
    task = models.ScheduledTask(
        kind="fx-test-handler",
        payload={},
        agent_id=session_agent.id,
        status=TaskStatus.PENDING,
        run_at=T0,
    )
    session.add(task)
    session.flush()
    return task


def _run_handler_once(session, task, *, lease_seconds=30):
    """The REAL worker phases: claim (transaction 1) → execute (transaction 2).

    Commits the claim (like the worker loop does) and returns
    ``(executed_task, generation)`` — mirroring ``run_worker``'s phase
    boundaries so the gateway runs inside the true execute_claimed
    transaction.
    """
    claimed = service.claim_next(session, T0, lease_seconds=lease_seconds)
    assert claimed is not None and claimed.id == task.id
    generation = int(claimed.lease_generation)  # captured INSIDE the phase-1 tx
    session.commit()  # the worker commits phase 1 before executing
    executed = service.execute_claimed(
        session, claimed, T0, timeout_seconds=0.0, lease_generation=generation
    )
    session.commit()
    return executed, generation


class TestRealPipelineIntegration:
    def test_handler_effect_commits_atomically_with_task_success(
        self, session, session_agent, fx_agent_task, database
    ):
        seen = {}

        def handler(db_session, task):
            # The REAL handler contract: same session as execute_claimed's
            # open transaction (T2 running marker holds the row lock).
            row = request_effect(
                db_session, task, "fx.echo", ECHO, now=T0, settings=effects_settings()
            )
            seen["effect_id"] = row.id
            seen["generation"] = row.claim_generation

        registry.register("fx-test-handler", handler)
        try:
            executed, generation = _run_handler_once(session, fx_agent_task)
            assert executed.status == TaskStatus.SUCCEEDED
            effect_id = seen["effect_id"]
            # The effect row committed WITH the task transaction and carries
            # the live claim generation as its fencing snapshot.
            with database.session() as s:
                row = s.get(models.EffectRequest, effect_id)
                assert row is not None
                assert row.status == EffectStatus.AUTHORIZED
                assert row.claim_generation == generation
                assert s.get(models.ScheduledTask, executed.id).status == TaskStatus.SUCCEEDED
            # A dispatcher tick then completes the outbox row normally.
            provider = FakeEffectProvider()
            result = dispatch_tick(
                database.session,
                provider,
                now=T0 + timedelta(seconds=1),
                owner="d-1",
                settings=effects_settings(),
            )
            assert result.acked_success == 1
            with database.session() as s:
                assert s.get(models.EffectRequest, effect_id).status == EffectStatus.SUCCEEDED
        finally:
            registry.unregister("fx-test-handler")

    def test_handler_failure_rolls_back_the_effect_row(
        self, session, session_agent, fx_agent_task, database
    ):
        # Atomic task-effect (Phase 0 §2.5): authorize then raise → the
        # task-failure rollback discards the effect INSERT (zero orphans).
        def handler(db_session, task):
            request_effect(db_session, task, "fx.echo", ECHO, now=T0, settings=effects_settings())
            raise RuntimeError("handler failed after authorizing")

        registry.register("fx-test-handler", handler)
        try:
            executed, _generation = _run_handler_once(session, fx_agent_task)
            assert executed.status == TaskStatus.PENDING  # backoff retry
            with database.session() as s:
                assert s.query(models.EffectRequest).count() == 0  # rolled back
                row = s.get(models.ScheduledTask, executed.id)
                assert "RuntimeError" in (row.last_error or "")
        finally:
            registry.unregister("fx-test-handler")

    def test_gateway_denial_fails_the_handler_and_task_axis(
        self, session, session_agent, fx_agent_task, database
    ):
        # EFF-02 task axis: the gateway's deterministic rejection surfaces
        # as a handler failure; the task follows the [SOURCE] retry policy;
        # ZERO effect rows exist (unauthorized content never lands).
        def handler(db_session, task):
            request_effect(db_session, task, "http:post", ECHO, now=T0, settings=effects_settings())

        registry.register("fx-test-handler", handler)
        try:
            executed, _generation = _run_handler_once(session, fx_agent_task)
            assert executed.status == TaskStatus.PENDING
            with database.session() as s:
                assert s.query(models.EffectRequest).count() == 0
                row = s.get(models.ScheduledTask, executed.id)
                assert "UnknownEffectCapabilityError" in (row.last_error or "")
        finally:
            registry.unregister("fx-test-handler")

    def test_disabled_gateway_rejects_inside_handler(
        self, session, session_agent, fx_agent_task, database
    ):
        # The fail-closed default also holds inside the REAL pipeline: with
        # effects disabled the handler's request raises, the task retries.

        def handler(db_session, task):
            request_effect(
                db_session,
                task,
                "fx.echo",
                ECHO,
                now=T0,
                settings=Settings(env="test", master_keys=""),  # disabled default
            )

        registry.register("fx-test-handler", handler)
        try:
            executed, _generation = _run_handler_once(session, fx_agent_task)
            assert executed.status == TaskStatus.PENDING
            with database.session() as s:
                assert s.query(models.EffectRequest).count() == 0
                row = s.get(models.ScheduledTask, executed.id)
                assert "EffectsDisabledError" in (row.last_error or "")
        finally:
            registry.unregister("fx-test-handler")

    def test_task_axis_untouched_by_dispatcher_tick(
        self, session, session_agent, fx_agent_task, database
    ):
        # INV-EF-R12 dual-axis separation: a dispatcher tick neither reads
        # nor writes any task state; running ticks changes nothing on tasks.
        def handler(db_session, task):
            request_effect(db_session, task, "fx.echo", ECHO, now=T0, settings=effects_settings())

        registry.register("fx-test-handler", handler)
        try:
            executed, _generation = _run_handler_once(session, fx_agent_task)
            assert executed.status == TaskStatus.SUCCEEDED
        finally:
            registry.unregister("fx-test-handler")
        provider = FakeEffectProvider()
        dispatch_tick(
            database.session,
            provider,
            now=T0 + timedelta(seconds=1),
            owner="d-1",
            settings=effects_settings(),
        )
        with database.session() as s:
            assert s.get(models.ScheduledTask, executed.id).status == TaskStatus.SUCCEEDED
            # No task row was leased/modified by the tick: attempts unchanged.
            assert s.get(models.ScheduledTask, executed.id).attempts == 1
