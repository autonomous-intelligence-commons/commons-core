"""Unit tests for the fixed-window rate limiter (frozen clock, no I/O)."""

from datetime import UTC, datetime, timedelta

import pytest

from commons_core.security.rate_limit import RateLimiter, persist_request_count

_BASE = datetime(2025, 1, 1, 12, 0, 30, tzinfo=UTC)


class FakeClock:
    """Deterministic clock advancing in fixed steps."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def tick(self, **delta) -> None:
        self.now = self.now + timedelta(**delta)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(_BASE)


class TestPerAgentLimit:
    def test_allows_up_to_limit_then_denies(self, clock) -> None:
        limiter = RateLimiter(per_agent_limit=3, anonymous_limit=30, clock=clock)
        decisions = [limiter.check("agent-1") for _ in range(4)]
        assert [d.allowed for d in decisions] == [True, True, True, False]
        assert decisions[0].remaining == 2
        assert decisions[-1].retry_after_seconds >= 1

    def test_new_window_resets_counter(self, clock) -> None:
        limiter = RateLimiter(per_agent_limit=2, anonymous_limit=30, clock=clock)
        assert limiter.check("agent-1").allowed
        assert limiter.check("agent-1").allowed
        assert not limiter.check("agent-1").allowed
        clock.tick(seconds=31)  # crosses the minute boundary
        assert limiter.check("agent-1").allowed

    def test_principals_are_isolated(self, clock) -> None:
        limiter = RateLimiter(per_agent_limit=1, anonymous_limit=30, clock=clock)
        assert limiter.check("agent-1").allowed
        assert not limiter.check("agent-1").allowed
        assert limiter.check("agent-2").allowed

    def test_invalid_limits_rejected(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(per_agent_limit=0)


class TestAnonymousLimit:
    def test_anonymous_bucket_separate_limit(self, clock) -> None:
        limiter = RateLimiter(per_agent_limit=120, anonymous_limit=2, clock=clock)
        assert limiter.check("anonymous:1.2.3.4").allowed
        assert limiter.check("anonymous:1.2.3.4").allowed
        decision = limiter.check("anonymous:1.2.3.4")
        assert not decision.allowed
        assert decision.limit == 2

    def test_anonymous_hosts_isolated(self, clock) -> None:
        limiter = RateLimiter(per_agent_limit=120, anonymous_limit=1, clock=clock)
        assert limiter.check("anonymous:1.1.1.1").allowed
        assert limiter.check("anonymous:2.2.2.2").allowed

    def test_pruning_keeps_memory_bounded(self, clock) -> None:
        limiter = RateLimiter(per_agent_limit=120, anonymous_limit=30, clock=clock)
        limiter._check_calls = 1023  # force prune on next check
        limiter.check("agent-1")
        assert len(limiter._counts) == 1


class TestQuotaPersistence:
    def test_persist_request_count(self, session, session_agent) -> None:
        from sqlalchemy import select

        from commons_core.database.models import QuotaCounter

        window = _BASE.replace(second=0, microsecond=0)
        persist_request_count(session, session_agent.id, window)
        persist_request_count(session, session_agent.id, window, amount=2)
        counters = list(session.scalars(select(QuotaCounter)))
        assert len(counters) == 1
        assert counters[0].requests == 3
        assert counters[0].agent_id == session_agent.id
