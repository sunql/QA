"""I3-a 单测：采购 Copilot 的纯逻辑与 SSE 转发状态机（无 DB / 无 HTTP）。

真实链路（创建 run、ACL、快照、预算）由集成测试
`app/tests/integration/test_procurement_copilot_api.py` 覆盖；这里只钉住：

1. **域前缀门槛**（Copilot 不是通用 Agent 的旁路）；
2. 请求 → 编排请求的**一对一**映射（不偷偷改字段）；
3. 事件状态机：`run` → `step`* → `done`（终态）/ `error`（连接超时）；
4. 轨迹增量：`afterStepNo` 必须**逐批推进**，否则同一步会被重复推送。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.exceptions import ValidationError
from app.domain.procurement_copilot_schemas import CopilotMessageRequest
from app.services import procurement_copilot as copilot
from app.services.agent_orchestration_types import (
    RunStatus,
    StepKind,
    StepStatus,
)


class _FakeSession:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeFactory:
    """替代 async_sessionmaker：每次轮询开一个「会话」（什么都不做）。"""

    def __init__(self) -> None:
        self.opened = 0

    def __call__(self) -> _FakeSession:
        self.opened += 1
        return _FakeSession()


def _step(no: int, *, kind: StepKind = StepKind.TOOL) -> SimpleNamespace:
    return SimpleNamespace(
        step_no=no,
        iteration_no=1,
        kind=kind,
        status=StepStatus.SUCCEEDED,
        action="CALL_TOOL",
        plan_summary=f"plan-{no}",
        tool_name="material_search",
        answer=None,
        error_code=None,
        error_message=None,
        output_json={"items": []},
        prompt_tokens=1,
        completion_tokens=2,
        cost=0.0,
    )


def _run(status: RunStatus, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "run_id": "run-1",
        "status": status,
        "final_answer": None,
        "pause_code": None,
        "pause_detail": None,
        "error_code": None,
        "error_message": None,
        "total_tokens": 0,
        "cost": 0.0,
        "cost_complete": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeRunService:
    """按轮询次数返回预置的 (run, steps) 快照。"""

    def __init__(self, snapshots: list[tuple[SimpleNamespace, list[Any]]]) -> None:
        self._snapshots = snapshots
        self.calls = 0
        self._current = 0
        self.afterStepNos: list[int] = []

    async def getRun(self, session: Any, runId: str, actor: Any) -> Any:
        # 一轮轮询 = getRun + listSteps 读**同一份**快照
        self._current = min(self.calls, len(self._snapshots) - 1)
        self.calls += 1
        return self._snapshots[self._current][0]

    async def listSteps(
        self,
        session: Any,
        runId: str,
        actor: Any,
        *,
        afterStepNo: int,
        limit: int,
    ) -> list[Any]:
        self.afterStepNos.append(afterStepNo)
        return list(self._snapshots[self._current][1])


@pytest.fixture()
def patchedRuntime(monkeypatch: pytest.MonkeyPatch):
    """把模块内的 run service 换成可编程的假实现。"""

    def _install(snapshots: list[tuple[SimpleNamespace, list[Any]]]) -> _FakeRunService:
        fake = _FakeRunService(snapshots)
        monkeypatch.setattr(
            copilot, "AgentOrchestrationRunService", lambda: fake
        )
        return fake

    return _install


# ---------------------------------------------------------------------------
# 域前缀门槛
# ---------------------------------------------------------------------------


def test_assertProcurementAgent_acceptsPrefix() -> None:
    copilot.assertProcurementAgent("PROCUREMENT_MATERIAL_SEARCH_AGENT")


@pytest.mark.parametrize(
    "code", ["ORCH_RUN_AGENT", "supplier_360_agent", "PROCUREMENTX_AGENT"]
)
def test_assertProcurementAgent_rejectsOthers(code: str) -> None:
    with pytest.raises(ValidationError):
        copilot.assertProcurementAgent(code)


def test_toRunCreate_mapsOneToOne() -> None:
    dto = CopilotMessageRequest(
        message="哪些供应商能供不锈钢板？",
        agent_code="PROCUREMENT_TEST_AGENT",
        model_config_id=7,
        limits={"maxToolCalls": 3},
    )

    create = copilot.toRunCreate(dto)

    assert create.agent_code == "PROCUREMENT_TEST_AGENT"
    assert create.input == "哪些供应商能供不锈钢板？"
    assert create.model_config_id == 7
    assert create.limits is not None and create.limits.max_tool_calls == 3


def test_buildStepsUrl_pointsAtRunSteps() -> None:
    assert copilot.buildStepsUrl("abc") == "/api/v1/agent-runs/abc/steps"


# ---------------------------------------------------------------------------
# SSE 转发状态机
# ---------------------------------------------------------------------------


async def _collect(events) -> list[Any]:
    return [event async for event in events]


@pytest.mark.asyncio
async def test_stream_emitsRunThenStepsThenDone(patchedRuntime: Any) -> None:
    service = patchedRuntime(
        [
            (_run(RunStatus.RUNNING), [_step(1)]),
            (_run(RunStatus.RUNNING), [_step(2)]),
            (_run(RunStatus.SUCCEEDED, final_answer="有 3 家"), []),
        ]
    )

    events = await _collect(
        copilot.streamCopilotRun(
            "run-1",
            SimpleNamespace(userId="u", roles=("admin",)),
            sessionFactory=_FakeFactory(),
            sleep=_noSleep,
        )
    )

    assert [e.event for e in events] == [
        copilot.EVENT_COPILOT_RUN,
        copilot.EVENT_COPILOT_STEP,
        copilot.EVENT_COPILOT_STEP,
        copilot.EVENT_COPILOT_DONE,
    ]
    assert [e.data["stepNo"] for e in events[1:3]] == [1, 2]
    assert events[-1].data["answer"] == "有 3 家"
    assert events[-1].data["status"] == "SUCCEEDED"
    # 轨迹增量：第二次轮询必须从「已见步 + 1」开始（否则重复推送）
    assert service.afterStepNos == [1, 2, 3]


@pytest.mark.asyncio
async def test_stream_timeoutEmitsErrorAndLeavesRunAlone(
    patchedRuntime: Any,
) -> None:
    patchedRuntime([(_run(RunStatus.RUNNING), [])])
    clock = iter([0.0, 0.0, 10.0])

    events = await _collect(
        copilot.streamCopilotRun(
            "run-1",
            SimpleNamespace(userId="u", roles=("admin",)),
            sessionFactory=_FakeFactory(),
            maxWaitSeconds=5.0,
            sleep=_noSleep,
            now=lambda: next(clock, 10.0),
        )
    )

    assert [e.event for e in events] == [
        copilot.EVENT_COPILOT_RUN,
        copilot.EVENT_COPILOT_ERROR,
    ]
    assert events[-1].data["errorType"] == "timeout"
    assert events[-1].data["runId"] == "run-1"


@pytest.mark.asyncio
async def test_stream_pausedIsTerminal(patchedRuntime: Any) -> None:
    patchedRuntime([(_run(RunStatus.PAUSED, pause_code="NEED_INPUT"), [])])

    events = await _collect(
        copilot.streamCopilotRun(
            "run-1",
            SimpleNamespace(userId="u", roles=("admin",)),
            sessionFactory=_FakeFactory(),
            sleep=_noSleep,
        )
    )

    assert events[-1].event == copilot.EVENT_COPILOT_DONE
    assert events[-1].data["pauseCode"] == "NEED_INPUT"


async def _noSleep(_seconds: float) -> None:
    return None
