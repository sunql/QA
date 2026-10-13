"""Task 8：跨版本步结果读取（``loadStepResultsByTurn``）+ 报告 ``evidence`` 接线。

真实 PostgreSQL + 真实 ``ResearchSessionService`` / ``_stageExecute``（脚手架复用
``test_step_result_snapshot`` 的确定性替身：SQL runner / chart / LLM 全 fake，零网络）。

**关键承重场景**：``research_run_state`` 会**版本化**（resume ⇒ 新 UUID），步结果挂在
写入当时的版本下（见 ``ResearchStepResult`` 类 docstring / brief D25）⇒ 同一 ``turn_id``
的**两个** run_state 版本各自挂步结果时，按 turn 聚合必须**两者都返回**。只按单个
``run_state_id`` 读会漏掉早前版本 —— 这正是 Task 6 留下的洞。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchRunState
from app.services.report_planner import ReportPlanner
from app.services.research_agent_execution import buildExecutionDeps
from app.services.research_session_service import ResearchSessionService
from app.services.research_step_result import loadStepResultsByTurn
from app.tests.integration.research.test_step_result_snapshot import (
    _FakeChart,
    _fakeResolveClient,
    _FakeUsage,
    _ScriptedRunner,
    _seedDatasource,
    _StageExecuteHarness,
)

pytestmark = pytest.mark.asyncio

QUESTION = "收货量为什么下降"
_SUCCESS_SQL = "SELECT 1 AS amount FROM DUAL"
_EMPTY_SQL = "SELECT 2 AS amount FROM DUAL"
_FAIL_SQL = "SELECT 3 AS amount FROM DUAL"


def _steps() -> list[dict[str, Any]]:
    return [
        {"index": 0, "description": "成功步", "sub_question": "q0", "sql": _SUCCESS_SQL},
        {"index": 1, "description": "空数据步", "sub_question": "q1", "sql": _EMPTY_SQL},
        {"index": 2, "description": "失败步", "sub_question": "q2", "sql": _FAIL_SQL},
    ]


def _outcomes() -> dict[str, Any]:
    return {
        _SUCCESS_SQL: [{"year": 2024, "amount": Decimal("100")}],
        _EMPTY_SQL: [],
        _FAIL_SQL: ValueError("守卫拒绝"),
    }


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


async def _createSessionAndTurn(
    dbSession: AsyncSession,
) -> tuple[ResearchSessionService, uuid.UUID, uuid.UUID]:
    svc = ResearchSessionService()
    dsId = await _seedDatasource(dbSession)
    sessionRow = await svc.createSession(
        dbSession, userId=1, question=QUESTION, datasourceId=dsId
    )
    turn = await svc.appendTurn(
        dbSession, sessionId=sessionRow.id, role="user", content={"question": QUESTION}
    )
    return svc, sessionRow.id, turn.id


async def _addRunStateVersion(
    dbSession: AsyncSession,
    svc: ResearchSessionService,
    *,
    sessionId: uuid.UUID,
    turnId: uuid.UUID,
    parentRunId: uuid.UUID | None,
    createdAt: datetime,
) -> uuid.UUID:
    """落一个 ``research_run_state`` 版本（同 turn_id，显式 created_at 定序）。"""
    run_state = ResearchRunState(
        session_id=sessionId,
        turn_id=turnId,
        mode="research",
        current_question=QUESTION,
        parent_run_id=parentRunId,
        created_at=createdAt,
    )
    await svc.createRunState(dbSession, run_state)
    await dbSession.commit()
    return run_state.id


async def _runStageExecute(
    dbSession: AsyncSession, *, sessionId: uuid.UUID, turnId: uuid.UUID, runStateId: uuid.UUID
) -> None:
    """对指定 run_state 版本跑一次 ``_stageExecute``（3 步 ⇒ 3 行步结果）。"""
    execDeps = buildExecutionDeps(
        runner=_ScriptedRunner(_outcomes()),
        chart=_FakeChart(),
        ontology=None,
        nl2sql=None,
        resolveClient=_fakeResolveClient,
        recordUsage=_FakeUsage().recordUsage,
    )
    harness = _StageExecuteHarness(execDeps, autoConfirm=True)
    state: dict[str, Any] = {
        "question": QUESTION,
        "plan": {"steps": _steps()},
        "stepResults": [],
        "runStateId": runStateId,
    }
    await harness._stageExecute(
        dbSession, sessionId=sessionId, turnId=turnId, emit=None, state=state
    )
    await dbSession.commit()


async def _twoVersionsOneTurn(
    dbSession: AsyncSession,
) -> tuple[ResearchSessionService, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """造「同 turn_id 的两个 run_state 版本」，各挂 3 行步结果。

    返回 ``(svc, sessionId, turnId, v1, v2)``。
    """
    svc, sessionId, turnId = await _createSessionAndTurn(dbSession)
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    v1 = await _addRunStateVersion(
        dbSession, svc, sessionId=sessionId, turnId=turnId, parentRunId=None, createdAt=base
    )
    await _runStageExecute(dbSession, sessionId=sessionId, turnId=turnId, runStateId=v1)
    v2 = await _addRunStateVersion(
        dbSession,
        svc,
        sessionId=sessionId,
        turnId=turnId,
        parentRunId=v1,
        createdAt=base + timedelta(minutes=1),
    )
    await _runStageExecute(dbSession, sessionId=sessionId, turnId=turnId, runStateId=v2)
    return svc, sessionId, turnId, v1, v2


# ---------------------------------------------------------------------------
# 跨版本聚合：两个 run_state 版本都返回（承重）
# ---------------------------------------------------------------------------


async def test_load_step_results_by_turn_covers_all_versions(
    dbSession: AsyncSession,
) -> None:
    _, _, turnId, v1, v2 = await _twoVersionsOneTurn(dbSession)

    rows = await loadStepResultsByTurn(dbSession, turnId)

    assert len(rows) == 6, f"跨版本应聚合 6 行，实际 {len(rows)}"
    # 两个版本的行都在（只按单个 run_state_id 读会漏）
    assert {row.run_state_id for row in rows} == {v1, v2}
    # 排序 (run_state 版本时序, step_index)：v1 在前、版本内按 step_index
    assert [r.run_state_id for r in rows] == [v1, v1, v1, v2, v2, v2]
    assert [r.step_index for r in rows] == [0, 1, 2, 0, 1, 2]


async def test_load_step_results_by_turn_other_turn_is_empty(
    dbSession: AsyncSession,
) -> None:
    await _twoVersionsOneTurn(dbSession)

    assert await loadStepResultsByTurn(dbSession, uuid.uuid4()) == []


# ---------------------------------------------------------------------------
# 报告 payload.evidence：装配时自查步结果并按 turn 跨版本聚合
# ---------------------------------------------------------------------------


async def test_report_payload_evidence_includes_all_versions(
    dbSession: AsyncSession,
) -> None:
    svc, sessionId, turnId, _, _ = await _twoVersionsOneTurn(dbSession)

    planner = ReportPlanner(sessionService=svc)
    payload, renderedMd = await planner.compose(
        dbSession, sessionId=sessionId, turnId=turnId, mode="research"
    )

    evidence = payload["evidence"]["stepResults"]
    assert len(evidence) == 6, f"evidence 应含两个版本共 6 步，实际 {len(evidence)}"
    # 版本时序 + 步序，与读取器一致
    assert [item["index"] for item in evidence] == [0, 1, 2, 0, 1, 2]
    # 每条字段齐全，且不含原始行（体积）
    for item in evidence:
        assert set(item) == {
            "index",
            "type",
            "sql",
            "status",
            "error",
            "summary",
            "schemaVersion",
        }
        assert "rows" not in item and "raw_result_ref" not in item
    # 第一版本第一步：成功步 + 摘要数值落库为字符串
    first = evidence[0]
    assert first["type"] == "成功步"
    assert first["sql"] == _SUCCESS_SQL
    assert first["status"] == "success"
    assert first["summary"]["baseline"] == "100"
    assert first["schemaVersion"] == 1
    assert renderedMd  # 报告照常产出
