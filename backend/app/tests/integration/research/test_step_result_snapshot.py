"""Task 6 端到端：``research_step_result`` 逐步入库（D23–D27）。

真实 PostgreSQL + 真实 ``ResearchSessionService`` / ``depsForSession``（会话 / 数据源 /
run state 都真落库），只有 **SQL runner / chart / LLM 客户端**是确定性替身（零网络、
零业务库连接）。链路：``_stageExecute`` 循环 —— 每步 ``runStep`` 后把结果写进
``research_step_result`` 并提交。

断言（brief「验收自检」）：
- 跑 N 步 ⇒ 落 N 行，字段齐全；
- ``status`` 三分支（``failed`` / ``empty`` / ``success``）各覆盖一条；
- ``evidence_summary`` 来自 ``result["evidence"]``：数值落库后是**字符串**（``Decimal →
  str``，不是 float）；
- ``raw_result_ref["rows"]`` 超过 ``MAX_FINDING_ROWS`` 时被截断；
- ``loadStepResults`` 按 ``run_state_id`` 取回、按 ``step_index`` 有序。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.domain.research_models import ResearchRunState
from app.infrastructure.security.crypto import encryptApiKey
from app.services.research_agent_execution import buildExecutionDeps
from app.services.research_agent_phases import ResearchAgentPhasesMixin
from app.services.research_agent_ports import (
    MAX_FINDING_ROWS,
    STEP_RESULT_RAW_ROWS_KEY,
)
from app.services.research_session_service import ResearchSessionService
from app.services.research_step_result import buildStepResult, loadStepResults

pytestmark = pytest.mark.asyncio

_SUCCESS_SQL = "SELECT 1 AS amount FROM DUAL"
_EMPTY_SQL = "SELECT 2 AS amount FROM DUAL"
_FAIL_SQL = "SELECT 3 AS amount FROM DUAL"


# ---------------------------------------------------------------------------
# harness（照抄 test_hypothesis_verdict 的最小装配，走 _stageExecute）
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    """按 SQL 文本返回固定结果：列表 ⇒ 行；异常 ⇒ 抛出（模拟 SQL Guard 拒绝 / 驱动错）。"""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self._outcomes = outcomes
        self.executed: list[str] = []

    async def executeReadonlySql(self, session, sql, *, adapter=None):
        self.executed.append(sql)
        outcome = self._outcomes[sql]
        if isinstance(outcome, Exception):
            raise outcome
        return [dict(row) for row in outcome]


class _FakeChart:
    """图表 fake：复用真实 `ChartService._emptyResult`（零 LLM / 零 DB 阈值读取）。"""

    async def buildChart(self, **kwargs):
        from app.services.chart_service import ChartService

        return ChartService._emptyResult(
            list(kwargs.get("columns") or []), list(kwargs.get("data") or [])
        )


class _FakeUsage:
    async def recordUsage(self, *args, **kwargs) -> None:  # pragma: no cover - 本路径不记账
        return None


async def _fakeResolveClient(session, *, state, emit, sessionId):
    return None, None


class _StageExecuteHarness(ResearchAgentPhasesMixin):
    """只装配 ``_stageExecute`` 走得到的依赖。"""

    def __init__(self, execDeps, *, autoConfirm: bool = True) -> None:
        self._exec = execDeps
        self._autoConfirm = autoConfirm


async def _seedDatasource(dbSession: AsyncSession) -> int:
    ds = DataSource(
        name=f"step-result-test-{uuid.uuid4().hex[:8]}",
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=True,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds.id


async def _runStageExecute(
    dbSession: AsyncSession,
    *,
    steps: list[dict[str, Any]],
    outcomes: dict[str, Any],
    autoConfirm: bool = True,
) -> tuple[uuid.UUID, dict[str, Any], Any]:
    """跑一次 ``_stageExecute``，返回 (runStateId, state, pause)。"""
    svc = ResearchSessionService()
    dsId = await _seedDatasource(dbSession)
    sessionRow = await svc.createSession(
        dbSession, userId=1, question="收货量为什么下降", datasourceId=dsId
    )
    turn = await svc.appendTurn(
        dbSession, sessionId=sessionRow.id, role="user", content={"question": "收货量为什么下降"}
    )
    run_state = ResearchRunState(
        session_id=sessionRow.id,
        turn_id=turn.id,
        mode="research",
        current_question="收货量为什么下降",
    )
    await svc.createRunState(dbSession, run_state)
    # commit 落地 run_state 行（default=uuid.uuid4 在 flush 时才填 id），并固定 FK。
    await dbSession.commit()

    execDeps = buildExecutionDeps(
        runner=_ScriptedRunner(outcomes),
        chart=_FakeChart(),
        ontology=None,
        nl2sql=None,
        resolveClient=_fakeResolveClient,
        recordUsage=_FakeUsage().recordUsage,
    )
    harness = _StageExecuteHarness(execDeps, autoConfirm=autoConfirm)
    state: dict[str, Any] = {
        "question": "收货量为什么下降",
        "plan": {"steps": steps},
        "stepResults": [],
        "runStateId": run_state.id,
    }
    pause = await harness._stageExecute(
        dbSession, sessionId=sessionRow.id, turnId=turn.id, emit=None, state=state
    )
    await dbSession.commit()
    return run_state.id, state, pause


def _steps() -> list[dict[str, Any]]:
    return [
        {"index": 0, "description": "成功步", "sub_question": "q0", "sql": _SUCCESS_SQL},
        {"index": 1, "description": "空数据步", "sub_question": "q1", "sql": _EMPTY_SQL},
        {"index": 2, "description": "失败步", "sub_question": "q2", "sql": _FAIL_SQL},
    ]


# ---------------------------------------------------------------------------
# 逐步入库：N 步 ⇒ N 行，status 三分支，字段齐全，有序
# ---------------------------------------------------------------------------


async def test_stage_execute_persists_one_row_per_step(dbSession: AsyncSession) -> None:
    runStateId, state, _ = await _runStageExecute(
        dbSession,
        steps=_steps(),
        outcomes={
            _SUCCESS_SQL: [
                {"year": 2024, "amount": Decimal("100")},
                {"year": 2025, "amount": Decimal("200")},
            ],
            _EMPTY_SQL: [],
            _FAIL_SQL: ValueError("守卫拒绝"),
        },
    )

    rows = await loadStepResults(dbSession, runStateId)

    # N 步 ⇒ N 行
    assert len(rows) == 3, f"预期 3 行，实际 {len(rows)}"

    # status 三分支各一
    assert {row.status for row in rows} == {"success", "empty", "failed"}
    byIndex = {row.step_index: row for row in rows}

    success, empty, failed = byIndex[0], byIndex[1], byIndex[2]
    assert success.status == "success"
    assert empty.status == "empty"
    assert failed.status == "failed"

    # 字段齐全
    assert success.sql_text == _SUCCESS_SQL
    assert success.step_type == "成功步"
    assert success.error is None
    assert failed.sql_text == _FAIL_SQL
    assert failed.error  # 失败步带错误信息

    # 内部载体（驱动原始行）**不进** state["stepResults"]
    assert all(STEP_RESULT_RAW_ROWS_KEY not in result for result in state["stepResults"])


async def test_evidence_summary_numeric_is_string(dbSession: AsyncSession) -> None:
    """D24：``evidence_summary`` 直接取 ``result["evidence"]`` ⇒ ``baseline`` 是字符串。"""
    runStateId, _, _ = await _runStageExecute(
        dbSession,
        steps=[_steps()[0]],
        outcomes={
            _SUCCESS_SQL: [
                {"year": 2024, "amount": Decimal("100")},
                {"year": 2025, "amount": Decimal("200")},
            ],
        },
    )

    rows = await loadStepResults(dbSession, runStateId)
    assert len(rows) == 1

    baseline = rows[0].evidence_summary["baseline"]
    assert baseline == "100"
    assert isinstance(baseline, str) and not isinstance(baseline, float)
    # 原始行同样经 research_json 归一：Decimal → str（不是 float）
    assert rows[0].raw_result_ref["rows"][0]["amount"] == "100"
    assert rows[0].raw_result_ref["schemaVersion"] == 1


async def test_load_step_results_is_scoped_and_ordered(dbSession: AsyncSession) -> None:
    """按 ``run_state_id`` 取回、按 ``step_index`` 升序；另一 run_state 无行。"""
    runStateId, _, _ = await _runStageExecute(
        dbSession,
        steps=_steps(),
        outcomes={_SUCCESS_SQL: [{"amount": 1}], _EMPTY_SQL: [], _FAIL_SQL: ValueError("x")},
    )

    rows = await loadStepResults(dbSession, runStateId)
    assert [row.step_index for row in rows] == [0, 1, 2]

    assert await loadStepResults(dbSession, uuid.uuid4()) == []


# ---------------------------------------------------------------------------
# 纯构造器：原始行按 MAX_FINDING_ROWS 截断
# ---------------------------------------------------------------------------


async def test_build_step_result_truncates_rows_to_limit() -> None:
    rows = [{"i": i} for i in range(MAX_FINDING_ROWS + 25)]

    result = buildStepResult(
        run_state_id=uuid.uuid4(),
        step_index=0,
        step_type="sql",
        sql_text="SELECT 1 FROM DUAL",
        raw_rows=rows,
        evidence_summary=None,
        error=None,
    )

    assert result.status == "success"
    assert len(result.raw_result_ref["rows"]) == MAX_FINDING_ROWS
    assert result.raw_result_ref["rows"][0] == {"i": 0}


async def test_build_step_result_status_derivation() -> None:
    """status 派生：error ⇒ failed；无行无错 ⇒ empty；有行无错 ⇒ success。"""
    failed = buildStepResult(
        run_state_id=uuid.uuid4(),
        step_index=0,
        step_type="sql",
        sql_text=None,
        raw_rows=[{"a": 1}],
        evidence_summary=None,
        error="boom",
    )
    empty = buildStepResult(
        run_state_id=uuid.uuid4(),
        step_index=1,
        step_type="sql",
        sql_text=None,
        raw_rows=[],
        evidence_summary=None,
        error=None,
    )
    assert failed.status == "failed"
    assert empty.status == "empty"


# ---------------------------------------------------------------------------
# FIX ROUND 1（Important）：白名单外驱动标量（bytes）不得杀整轮；逐行保全
# ---------------------------------------------------------------------------


async def test_build_step_result_tolerates_non_json_safe_rows() -> None:
    """纯构造器：含 ``bytes`` 的行只丢那一行，好行保留，构造器不抛。"""
    rows = [{"ok": 1}, {"bad": b"x"}, {"ok2": 2}]

    result = buildStepResult(
        run_state_id=uuid.uuid4(),
        step_index=0,
        step_type="sql",
        sql_text="SELECT 1 FROM DUAL",
        raw_rows=rows,
        evidence_summary=None,
        error=None,
    )

    assert result.status == "success"
    kept = result.raw_result_ref["rows"]
    assert {"ok": 1} in kept
    assert {"ok2": 2} in kept
    assert all("bad" not in row for row in kept), f"坏行未被丢弃: {kept}"
    assert len(kept) == 2


async def test_stage_execute_survives_non_json_safe_row(dbSession: AsyncSession) -> None:
    """Important：含 ``bytes`` 值的行不杀整轮 —— 坏行丢、好行与后续步照常。"""
    runStateId, _, _ = await _runStageExecute(
        dbSession,
        steps=_steps(),
        outcomes={
            _SUCCESS_SQL: [{"ok": 1}, {"bad": b"x"}, {"ok2": 2}],
            _EMPTY_SQL: [],
            _FAIL_SQL: ValueError("守卫拒绝"),
        },
    )

    rows = await loadStepResults(dbSession, runStateId)
    # 后续步照跑：3 步全部落库（不因坏行中断整轮）
    assert len(rows) == 3, f"预期 3 行，实际 {len(rows)}"

    raw = {row.step_index: row for row in rows}[0].raw_result_ref["rows"]
    assert {"ok": 1} in raw
    assert {"ok2": 2} in raw
    assert all("bad" not in row for row in raw), f"坏行未被丢弃: {raw}"


async def test_stage_execute_persists_failed_step_before_checkpoint_pause(
    dbSession: AsyncSession,
) -> None:
    """Minor-1：autoConfirm=False 下失败步触发 checkpoint 早退，但该步行已先落库。"""
    runStateId, _, pause = await _runStageExecute(
        dbSession,
        autoConfirm=False,
        steps=[
            {"index": 0, "description": "成功步", "sub_question": "q0", "sql": _SUCCESS_SQL},
            {"index": 1, "description": "失败步", "sub_question": "q1", "sql": _FAIL_SQL},
            {"index": 2, "description": "不该跑到", "sub_question": "q2", "sql": _EMPTY_SQL},
        ],
        outcomes={
            _SUCCESS_SQL: [{"amount": 1}],
            _EMPTY_SQL: [],
            _FAIL_SQL: ValueError("守卫拒绝"),
        },
    )

    # 早退确实发生（返回了 checkpoint 三元组，而非 None）
    assert pause is not None, "失败步应触发 checkpoint 暂停（提前 return）"

    rows = await loadStepResults(dbSession, runStateId)
    byIndex = {row.step_index: row for row in rows}
    # 失败步的行在早退之前已落库
    assert 1 in byIndex, f"失败步未落库: {sorted(byIndex)}"
    assert byIndex[1].status == "failed"
    # 后续步从未执行
    assert 2 not in byIndex, "暂停后不应继续跑后续步"
