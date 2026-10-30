"""Task 8 integration tests: atomic claim, lease recovery and guard (real PG).

Every test drives the real ``AgentOrchestrationStore`` against PostgreSQL so
that ``FOR UPDATE SKIP LOCKED`` actually participates: two independent
sessions race for QUEUED rows, live leases stay untouched, expired leases are
recovered into a fresh window, and the execution guard reads the row under
lock (not a stale identity-map copy).

Runs are seeded directly through the ORM: Task 8 has no HTTP surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.agent_orchestration_models import AgentOrchestrationRun
from app.services.agent_orchestration_store import AgentOrchestrationStore
from app.services.agent_orchestration_types import (
    LeaseLostError,
    OrchestrationPauseError,
    PauseCode,
)
from app.tests import _pg_support

LEASE_SECONDS = 60


@pytest.fixture()
async def sessionFactory(
    pg_engine: Any,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh real-PG session factory, with the whole schema truncated."""

    await _pg_support._truncateAll(pg_engine)
    factory = async_sessionmaker(
        pg_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    yield factory
    await _pg_support._truncateAll(pg_engine)


def _runRow(**overrides: Any) -> AgentOrchestrationRun:
    values: dict[str, Any] = {
        "run_id": str(uuid4()),
        "agent_code": "claim-agent",
        "profile_revision": 1,
        "instructions_snapshot": "instructions",
        "tool_bindings_snapshot": [],
        "max_observation_chars": 1000,
        "question": "how many suppliers?",
        "created_by": "tester",
        "model_config_id": 1,
        "status": "QUEUED",
        "max_iterations": 5,
        "max_tool_calls": 5,
        "max_cost_usd": Decimal("1"),
        "timeout_seconds": 60,
    }
    values.update(overrides)
    return AgentOrchestrationRun(**values)


async def _seedRun(
    factory: async_sessionmaker[AsyncSession], **overrides: Any
) -> AgentOrchestrationRun:
    async with factory() as session:
        run = _runRow(**overrides)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def _loadRun(
    factory: async_sessionmaker[AsyncSession], runId: int
) -> AgentOrchestrationRun:
    async with factory() as session:
        run = await session.get(AgentOrchestrationRun, runId)
        assert run is not None
        return run


async def _claimOnce(
    factory: async_sessionmaker[AsyncSession],
    store: AgentOrchestrationStore,
    workerId: str,
    now: datetime,
) -> tuple[int, str] | None:
    async with factory() as session:
        run = await store.claimNext(session, workerId, LEASE_SECONDS, now)
        if run is None:
            return None
        return (run.id, run.lease_owner)  # type: ignore[return-value]


async def test_concurrent_claims_never_double_claim(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions race 100 rounds; every QUEUED run gets exactly one owner."""

    store = AgentOrchestrationStore()
    seen: dict[int, str] = {}
    rounds = 100
    for _ in range(rounds):
        first = await _seedRun(sessionFactory)
        second = await _seedRun(sessionFactory)
        now = datetime.now(UTC)

        claims = await asyncio.gather(
            _claimOnce(sessionFactory, store, "worker-a", now),
            _claimOnce(sessionFactory, store, "worker-b", now),
        )

        roundIds: list[int] = []
        for claim in claims:
            assert claim is not None
            runId, owner = claim
            assert owner in {"worker-a", "worker-b"}
            assert runId not in seen, f"run {runId} was claimed twice"
            seen[runId] = owner
            roundIds.append(runId)
        assert sorted(roundIds) == sorted([first.id, second.id])

    assert len(seen) == rounds * 2
    for runId, owner in seen.items():
        persisted = await _loadRun(sessionFactory, runId)
        assert persisted.status == "RUNNING"
        assert persisted.lease_owner == owner


async def test_claim_sets_owner_lease_started_at_and_revision(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _seedRun(sessionFactory, revision=1, started_at=None)
    now = datetime.now(UTC)

    claimed = await _claimOnce(sessionFactory, AgentOrchestrationStore(), "worker-a", now)

    assert claimed is not None
    assert claimed[0] == run.id
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "RUNNING"
    assert persisted.lease_owner == "worker-a"
    assert persisted.lease_until == now + timedelta(seconds=LEASE_SECONDS)
    assert persisted.started_at is not None
    assert persisted.started_at == now
    assert persisted.revision == 2


async def test_claim_returns_none_when_queue_is_empty(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    claimed = await _claimOnce(
        sessionFactory, AgentOrchestrationStore(), "worker-a", datetime.now(UTC)
    )

    assert claimed is None


async def test_live_running_lease_is_not_reclaimable(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=30),
        started_at=now,
    )

    claimed = await _claimOnce(sessionFactory, AgentOrchestrationStore(), "worker-b", now)

    assert claimed is None
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "RUNNING"
    assert persisted.lease_owner == "worker-a"
    assert persisted.lease_until == now + timedelta(seconds=30)


async def test_expired_lease_is_recovered_into_a_fresh_window(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="dead-worker",
        lease_until=now - timedelta(seconds=5),
        started_at=now - timedelta(seconds=300),
        revision=1,
    )

    claimed = await _claimOnce(sessionFactory, AgentOrchestrationStore(), "worker-b", now)

    assert claimed is not None
    assert claimed[0] == run.id
    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.status == "RUNNING"
    assert persisted.lease_owner == "worker-b"
    assert persisted.lease_until == now + timedelta(seconds=LEASE_SECONDS)
    # 每次 QUEUED -> RUNNING 都重开计时窗口：旧 owner 跑掉的 300 秒不计新 owner。
    assert persisted.started_at == now
    # revision：seed=1，恢复 +1，claim +1。
    assert persisted.revision == 3


async def test_stale_owner_cannot_renew_lease(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=30),
        started_at=now,
    )
    store = AgentOrchestrationStore()

    async with sessionFactory() as session:
        assert await store.renewLease(session, run.id, "worker-b", LEASE_SECONDS, now) is False
    async with sessionFactory() as session:
        assert await store.renewLease(session, run.id, "worker-a", LEASE_SECONDS, now) is True

    persisted = await _loadRun(sessionFactory, run.id)
    assert persisted.lease_owner == "worker-a"
    assert persisted.lease_until == now + timedelta(seconds=LEASE_SECONDS)
    assert persisted.revision == run.revision  # 续租不算状态迁移，不改 revision


async def test_expired_lease_cannot_be_renewed_even_by_its_owner(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="worker-a",
        lease_until=now - timedelta(seconds=1),
        started_at=now - timedelta(seconds=120),
    )

    async with sessionFactory() as session:
        renewed = await AgentOrchestrationStore().renewLease(
            session, run.id, "worker-a", LEASE_SECONDS, now
        )

    assert renewed is False


async def test_guard_rejects_stale_owner_and_accepts_current_owner(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=30),
        started_at=now,
    )
    loaded = await _loadRun(sessionFactory, run.id)
    store = AgentOrchestrationStore()

    async with sessionFactory() as session:
        with pytest.raises(LeaseLostError) as excInfo:
            await store.assertExecutionAllowed(session, loaded, "worker-b", now)
    assert excInfo.value.reason == "lease_owner_mismatch"

    async with sessionFactory() as session:
        await store.assertExecutionAllowed(session, loaded, "worker-a", now)


async def test_guard_reloads_under_lock_instead_of_trusting_stale_object(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    """A stale in-memory run must not hide a cancel committed by another session."""

    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=30),
        started_at=now,
    )
    stale = await _loadRun(sessionFactory, run.id)

    async with sessionFactory() as session:
        target = await session.get(AgentOrchestrationRun, run.id)
        assert target is not None
        target.cancel_requested = True
        await session.commit()

    async with sessionFactory() as session:
        with pytest.raises(LeaseLostError) as excInfo:
            await AgentOrchestrationStore().assertExecutionAllowed(
                session, stale, "worker-a", now
            )

    assert excInfo.value.reason == "cancelled"


async def test_guard_check_order_cancel_then_owner_then_lease_then_status(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    store = AgentOrchestrationStore()

    async def _guardError(run: AgentOrchestrationRun, owner: str) -> LeaseLostError:
        loaded = await _loadRun(sessionFactory, run.id)
        async with sessionFactory() as session:
            with pytest.raises(LeaseLostError) as excInfo:
                await store.assertExecutionAllowed(session, loaded, owner, now)
        return excInfo.value

    cancelled = await _seedRun(
        sessionFactory,
        status="PAUSED",
        cancel_requested=True,
        lease_owner="worker-a",
        lease_until=now - timedelta(seconds=5),
    )
    assert (await _guardError(cancelled, "worker-a")).reason == "cancelled"

    ownerMismatch = await _seedRun(
        sessionFactory,
        status="PAUSED",
        lease_owner="worker-a",
        lease_until=now - timedelta(seconds=5),
    )
    assert (await _guardError(ownerMismatch, "worker-b")).reason == "lease_owner_mismatch"

    expired = await _seedRun(
        sessionFactory,
        status="PAUSED",
        lease_owner="worker-a",
        lease_until=now - timedelta(seconds=5),
    )
    assert (await _guardError(expired, "worker-a")).reason == "lease_expired"

    notRunning = await _seedRun(
        sessionFactory,
        status="PAUSED",
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=30),
    )
    assert (await _guardError(notRunning, "worker-a")).reason == "not_running"


async def test_guard_rejects_a_run_row_that_no_longer_exists(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    ghost = _runRow()
    ghost.id = 987654321

    async with sessionFactory() as session:
        with pytest.raises(LeaseLostError) as excInfo:
            await AgentOrchestrationStore().assertExecutionAllowed(
                session, ghost, "worker-a", datetime.now(UTC)
            )

    assert excInfo.value.reason == "run_missing"


async def test_guard_pauses_with_canonical_budget_detail(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    store = AgentOrchestrationStore()
    cases: list[tuple[str, dict[str, Any]]] = [
        ("iteration", {"iteration_count": 5}),
        ("tool", {"tool_call_count": 5}),
        ("cost", {"cost": Decimal("1")}),
        ("time", {"started_at": now - timedelta(seconds=61)}),
    ]

    for budgetKey, overrides in cases:
        values: dict[str, Any] = {
            "status": "RUNNING",
            "lease_owner": "worker-a",
            "lease_until": now + timedelta(seconds=30),
            "started_at": now,
        }
        values.update(overrides)
        run = await _seedRun(sessionFactory, **values)
        loaded = await _loadRun(sessionFactory, run.id)
        async with sessionFactory() as session:
            with pytest.raises(OrchestrationPauseError) as excInfo:
                await store.assertExecutionAllowed(session, loaded, "worker-a", now)
        error = excInfo.value
        assert error.code == PauseCode.BUDGET_EXHAUSTED.value
        assert error.detail == {"budget": budgetKey}


async def test_guard_allows_run_one_step_below_every_budget(
    sessionFactory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    run = await _seedRun(
        sessionFactory,
        status="RUNNING",
        lease_owner="worker-a",
        lease_until=now + timedelta(seconds=30),
        started_at=now - timedelta(seconds=59),
        iteration_count=4,
        tool_call_count=4,
        cost=Decimal("0.99999999"),
    )
    loaded = await _loadRun(sessionFactory, run.id)

    async with sessionFactory() as session:
        await AgentOrchestrationStore().assertExecutionAllowed(
            session, loaded, "worker-a", now
        )
