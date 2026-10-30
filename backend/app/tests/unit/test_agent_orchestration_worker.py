"""Task 8 unit tests: opt-in Worker lifecycle with fake store/runner.

No database, no sleeps: ``runOnce`` is invoked directly, and the ``run`` loop
is driven by fakes that request stop themselves, so every assertion is
deterministic. The disabled feature flag must return before any polling.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any

from app.services.agent_orchestration_types import LeaseLostError
from app.workers.agent_orchestration_worker import AgentOrchestrationWorker


class _SessionContext:
    async def __aenter__(self) -> Any:
        return SimpleNamespace()

    async def __aexit__(self, *excInfo: Any) -> bool:
        return False


class _FakeSessionFactory:
    """Counts how many per-claim sessions the Worker opens."""

    def __init__(self) -> None:
        self.created = 0

    def __call__(self) -> _SessionContext:
        self.created += 1
        return _SessionContext()


class _FakeStore:
    def __init__(self, runs: list[Any] | None = None) -> None:
        self._runs = list(runs or [])
        self.calls: list[dict[str, Any]] = []
        self.onClaim: Any = None

    async def claimNext(
        self,
        session: Any,
        workerId: str,
        leaseSeconds: int,
        now: Any,
    ) -> Any:
        self.calls.append(
            {
                "session": session,
                "workerId": workerId,
                "leaseSeconds": leaseSeconds,
                "now": now,
            }
        )
        if self.onClaim is not None:
            self.onClaim()
        if not self._runs:
            return None
        return self._runs.pop(0)


class _FakeRunner:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self._errors = list(errors or [])

    async def runClaimed(self, runId: int, workerId: str) -> None:
        self.calls.append((runId, workerId))
        if self._errors:
            raise self._errors.pop(0)


class _FakeWarmUp:
    """记录 Worker 启动预热是否被调用（不碰数据库）。"""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, sessionFactory: Any) -> None:
        self.calls.append(sessionFactory)


def _settings(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "enableAgentOrchestration": True,
        "agentOrchestrationPollSeconds": 0.01,
        "agentOrchestrationLeaseSeconds": 60,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _worker(
    *,
    store: Any | None = None,
    runner: Any | None = None,
    factory: Any | None = None,
    settings: Any | None = None,
    warmUp: Any | None = None,
) -> AgentOrchestrationWorker:
    return AgentOrchestrationWorker(
        settings or _settings(),
        factory or _FakeSessionFactory(),
        runner or _FakeRunner(),
        workerId="worker-test",
        store=store or _FakeStore(),
        # 预热替身：单元测试不连数据库（真实预热由集成测试覆盖）。
        warmUp=warmUp if warmUp is not None else _FakeWarmUp(),
    )


async def test_disabled_worker_returns_without_polling(caplog: Any) -> None:
    store = _FakeStore([SimpleNamespace(id=1)])
    runner = _FakeRunner()
    factory = _FakeSessionFactory()
    warmUp = _FakeWarmUp()
    worker = _worker(
        store=store,
        runner=runner,
        factory=factory,
        settings=_settings(enableAgentOrchestration=False),
        warmUp=warmUp,
    )

    with caplog.at_level(logging.INFO):
        await worker.run()

    assert store.calls == []
    assert runner.calls == []
    assert factory.created == 0
    # flag 关闭 ⇒ 一次数据库访问都不发生（预热也不许发生）。
    assert warmUp.calls == []
    assert any("未启用" in record.getMessage() for record in caplog.records)


async def test_run_once_claims_then_awaits_runner() -> None:
    store = _FakeStore([SimpleNamespace(id=42)])
    runner = _FakeRunner()
    worker = _worker(store=store, runner=runner)

    claimed = await worker.runOnce()

    assert claimed is True
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["workerId"] == "worker-test"
    assert call["leaseSeconds"] == 60
    assert call["now"].tzinfo is not None
    assert runner.calls == [(42, "worker-test")]


async def test_default_worker_id_is_process_scoped_when_not_injected() -> None:
    store = _FakeStore([SimpleNamespace(id=3)])
    worker = AgentOrchestrationWorker(
        _settings(), _FakeSessionFactory(), _FakeRunner(), store=store
    )

    await worker.runOnce()

    workerId = store.calls[0]["workerId"]
    assert workerId.endswith(f":{os.getpid()}")
    assert len(workerId) <= 128


async def test_poll_error_is_logged_and_loop_can_continue(caplog: Any) -> None:
    class _ExplodingStore:
        async def claimNext(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("db down")

    worker = _worker(store=_ExplodingStore())

    with caplog.at_level(logging.ERROR):
        await worker._pollOnce()

    assert "db down" in caplog.text
    assert "轮询失败" in caplog.text


async def test_run_once_without_candidate_does_not_call_runner() -> None:
    runner = _FakeRunner()
    factory = _FakeSessionFactory()
    worker = _worker(runner=runner, factory=factory)

    claimed = await worker.runOnce()

    assert claimed is False
    assert runner.calls == []
    assert factory.created == 1


async def test_runner_error_is_logged_with_run_and_worker_then_polling_continues(
    caplog: Any,
) -> None:
    store = _FakeStore([SimpleNamespace(id=7), SimpleNamespace(id=8)])
    runner = _FakeRunner([RuntimeError("boom"), RuntimeError("boom again")])
    worker = _worker(store=store, runner=runner)

    with caplog.at_level(logging.ERROR):
        assert await worker.runOnce() is True
        assert await worker.runOnce() is True

    assert runner.calls == [(7, "worker-test"), (8, "worker-test")]
    logText = caplog.text
    assert "run 7" in logText
    assert "run 8" in logText
    assert "worker-test" in logText


async def test_lost_lease_is_control_flow_not_an_error_log(caplog: Any) -> None:
    store = _FakeStore([SimpleNamespace(id=9)])
    runner = _FakeRunner([LeaseLostError("cancelled")])
    worker = _worker(store=store, runner=runner)

    with caplog.at_level(logging.INFO):
        assert await worker.runOnce() is True

    assert runner.calls == [(9, "worker-test")]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any("租约" in record.getMessage() for record in caplog.records)


async def test_request_stop_before_run_skips_polling() -> None:
    store = _FakeStore([SimpleNamespace(id=1)])
    runner = _FakeRunner()
    factory = _FakeSessionFactory()
    warmUp = _FakeWarmUp()
    worker = _worker(store=store, runner=runner, factory=factory, warmUp=warmUp)

    worker.requestStop()
    await worker.run()

    assert store.calls == []
    assert factory.created == 0
    # 预热拿到的是 Worker 自己的会话工厂（真实预热在集成测试里覆盖）。
    assert warmUp.calls == [factory]


async def test_run_loop_stops_after_store_requests_stop() -> None:
    store = _FakeStore([SimpleNamespace(id=11)])
    runner = _FakeRunner()
    worker = _worker(store=store, runner=runner)
    store.onClaim = worker.requestStop

    await asyncio.wait_for(worker.run(), timeout=2)

    assert len(store.calls) == 1
    assert runner.calls == [(11, "worker-test")]


async def test_run_warms_caches_before_claiming_any_run() -> None:
    """C1：预热必须发生在**任何 run 被 claim 之前**（冷态注册表 ⇒ 工具解析必挂）。"""
    store = _FakeStore([SimpleNamespace(id=12)])
    runner = _FakeRunner()
    warmUp = _FakeWarmUp()
    worker = _worker(store=store, runner=runner, warmUp=warmUp)
    warmedBeforeClaim: list[bool] = []

    def _onClaim() -> None:
        warmedBeforeClaim.append(bool(warmUp.calls))
        worker.requestStop()

    store.onClaim = _onClaim

    await asyncio.wait_for(worker.run(), timeout=2)

    assert len(warmUp.calls) == 1
    assert warmedBeforeClaim == [True]
    assert runner.calls == [(12, "worker-test")]
