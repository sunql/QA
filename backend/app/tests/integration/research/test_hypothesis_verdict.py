"""Task 5 端到端：``verdict`` 落库 + 报告侧「已验证」列（D18b/D19/D19b/P2）。

真实 PostgreSQL + 真实 ``ResearchSessionService``（finding 真的写进
``research_finding``），只有 runner / LLM 是确定性替身（零网络）。两条链路：

1. **写入侧**（``_stageVerify``）：verdict 进 ``supporting_data["verdict"]``、证据摘要进
   ``supporting_data["evidence"]``（``Decimal`` 落库后是**字符串**，不是 float）；
   ``state["findings"][i]["verified"]`` ⇔ ``verdict == supported``。
2. **报告侧**（``ReportPlanner.compose``）：``verdict=inconclusive`` 但有行的 finding ⇒
   ``findingsRef[].verified`` 为 ``false``、「已验证」列渲染「否」，且**任何输出字符串里
   都不出现 ``None``**；历史 finding（无 ``verdict`` 键）仍渲染为已验证（回落）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.infrastructure.security.crypto import encryptApiKey
from app.services.report_planner import ReportPlanner
from app.services.research_agent_execution import buildExecutionDeps
from app.services.research_agent_phases import ResearchAgentPhasesMixin
from app.services.research_session_service import ResearchSessionService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 写入侧 harness（镜像 test_research_hypothesis_rewrite_wiring 的最小 harness，
# 但 `_sessions` 用**真身** ResearchSessionService ⇒ finding 真落 PG）
# ---------------------------------------------------------------------------


class _FixedRowsRunner:
    """``runVerification`` 返回固定行 / 固定错误（零网络）。"""

    def __init__(self, rows: list[dict[str, Any]], error: str | None = None) -> None:
        self._rows = list(rows)
        self._error = error
        self.calls = 0

    async def runVerification(self, session, hypothesis, *, adapter=None, dialectArgs=None):
        self.calls += 1
        return {"rows": [dict(r) for r in self._rows], "error": self._error}


class _FakeUsage:
    async def recordUsage(self, *args, **kwargs) -> None:  # pragma: no cover - 本相位不记账
        return None


async def _fakeResolveClient(session, *, state, emit, sessionId):
    return None, None


class _StageVerifyHarness(ResearchAgentPhasesMixin):
    """只装配 ``_stageVerify`` 走得到的依赖（runner / sessions / execDeps）。"""

    def __init__(self, runner, sessionsRepo, execDeps) -> None:
        self._runner = runner
        self._sessions = sessionsRepo
        self._exec = execDeps


async def _seedDatasource(dbSession: AsyncSession) -> int:
    ds = DataSource(
        name=f"verdict-test-{uuid.uuid4().hex[:8]}",
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


async def _runStageVerify(
    dbSession: AsyncSession,
    *,
    statement: str,
    rows: list[dict[str, Any]],
    error: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一次 ``_stageVerify``，返回 (state, finding_row_as_dict)。"""
    svc = ResearchSessionService()
    dsId = await _seedDatasource(dbSession)
    sessionRow = await svc.createSession(
        dbSession, userId=1, question=statement, datasourceId=dsId
    )
    turn = await svc.appendTurn(
        dbSession, sessionId=sessionRow.id, role="user", content={"question": statement}
    )
    await dbSession.commit()

    runner = _FixedRowsRunner(rows, error=error)
    execDeps = buildExecutionDeps(
        runner=None,  # _stageVerify 直接用 self._runner
        chart=None,
        ontology=None,
        nl2sql=None,
        resolveClient=_fakeResolveClient,
        recordUsage=_FakeUsage().recordUsage,
    )
    harness = _StageVerifyHarness(runner, ResearchSessionService(), execDeps)
    state = {
        "hypotheses": [
            {"statement": statement, "driver": "amount", "verificationSql": "SELECT 1 FROM dual"}
        ],
        "stepResults": [],
        "choice": {"selectedIndexes": [0]},
        "esl": None,
        "modelId": None,
    }
    await harness._stageVerify(
        dbSession, sessionId=sessionRow.id, turnId=turn.id, emit=None, state=state
    )
    await dbSession.commit()

    row = (
        await dbSession.execute(
            text(
                "SELECT supporting_data, confidence FROM research_finding"
                " WHERE session_id = :sid"
            ),
            {"sid": sessionRow.id},
        )
    ).one()
    return state, {"supportingData": row.supporting_data, "confidence": row.confidence}


# ---------------------------------------------------------------------------
# 写入侧：verdict / evidence 落库
# ---------------------------------------------------------------------------


async def test_stage_verify_supported_persists_string_evidence(dbSession: AsyncSession) -> None:
    """方向词 + 同向数据 ⇒ verdict=supported；evidence.baseline 落库后是**字符串** ``"100"``。"""
    state, finding = await _runStageVerify(
        dbSession,
        statement="采购金额上升",
        rows=[{"year": 2024, "amount": 100}, {"year": 2025, "amount": 200}],
    )

    data = finding["supportingData"]
    assert data["verdict"] == "supported"
    # D19 反向断言：baseline 是字符串，不是 float（_coerceJsonSafe 未把它变 float）
    baseline = data["evidence"]["baseline"]
    assert baseline == "100"
    assert isinstance(baseline, str) and not isinstance(baseline, float)
    # state["findings"] 的键（D16：不改旧键、只加新键）
    entry = state["findings"][0]
    assert entry["verified"] is True
    assert entry["verdict"] == "supported"
    assert entry["executionSucceeded"] is True
    assert entry["claim"] == "采购金额上升"


async def test_stage_verify_contradicted_with_rows_is_not_verified(
    dbSession: AsyncSession,
) -> None:
    """**有行但数据不支持**（下降）⇒ verdict=contradicted、verified=False。"""
    state, finding = await _runStageVerify(
        dbSession,
        statement="采购金额上升",
        rows=[{"year": 2024, "amount": 200}, {"year": 2025, "amount": 100}],
    )

    assert finding["supportingData"]["verdict"] == "contradicted"
    entry = state["findings"][0]
    assert entry["verified"] is False
    assert entry["verdict"] == "contradicted"


async def test_stage_verify_inconclusive_with_rows_is_not_verified(
    dbSession: AsyncSession,
) -> None:
    """有行但**无基线**（无时间轴）⇒ verdict=inconclusive、verified=False。"""
    state, finding = await _runStageVerify(
        dbSession, statement="采购金额上升", rows=[{"amount": 100}]
    )

    data = finding["supportingData"]
    assert data["verdict"] == "inconclusive"
    assert data["evidence"]["baseline"] is None
    entry = state["findings"][0]
    assert entry["verified"] is False
    assert entry["verdict"] == "inconclusive"


async def test_stage_verify_execution_error_persists_execution_failed(
    dbSession: AsyncSession,
) -> None:
    """I2：验证 SQL **执行失败** ⇒ 写入侧落 ``execution_failed``（此前该路径零 E2E 覆盖）。

    唯一那条 ``execution_failed`` 集成用例是**直接塞进** ``supporting_data`` 的，绕过了
    ``_stageVerify`` 的 writer；本用例让 runner 真返回 ``error``，钉住：
    ``_stageVerify`` 的 ``execution_error`` 接线 + ``verdict`` + ``verified=False`` + ``limitations``。
    """
    state, finding = await _runStageVerify(
        dbSession,
        statement="采购金额上升",
        rows=[],
        error="ORA-00942: table or view does not exist",
    )

    assert finding["supportingData"]["verdict"] == "execution_failed"
    entry = state["findings"][0]
    assert entry["verified"] is False
    assert entry["verdict"] == "execution_failed"
    assert entry["executionSucceeded"] is False
    assert entry["limitations"]
    assert any("执行失败" in item for item in entry["limitations"])


async def test_stage_verify_verdict_value_is_in_domain(dbSession: AsyncSession) -> None:
    """落库 ``verdict`` 的**值域合法性**（不是「verified/verdict 不共存」不变量）。

    ``verified`` 是**派生量**（``verdict == supported``），``supporting_data`` 里没有单独的
    ``verified`` 列 ⇒ 本测试无从断言「verified=True ∧ verdict≠supported 不并存」；它实际只能
    断言落库 ``verdict`` 落在闭集内（以及 supported 用例确实落 supported）。
    """
    _, supported = await _runStageVerify(
        dbSession,
        statement="采购金额上升",
        rows=[{"year": 2024, "amount": 100}, {"year": 2025, "amount": 200}],
    )
    _, inconclusive = await _runStageVerify(
        dbSession, statement="采购金额上升", rows=[{"amount": 100}]
    )

    for finding in (supported, inconclusive):
        verdict = finding["supportingData"]["verdict"]
        assert verdict in {"supported", "contradicted", "inconclusive", "execution_failed"}
    assert supported["supportingData"]["verdict"] == "supported"
    assert inconclusive["supportingData"]["verdict"] != "supported"


# ---------------------------------------------------------------------------
# 报告侧（D18b / P2）
# ---------------------------------------------------------------------------


class _FakeLlm:
    async def complete(self, messages, **kwargs):
        return type("R", (), {"content": "解读：指标下降明显。"})()


async def _seedFinding(
    dbSession: AsyncSession, *, verdict: str | None, rows: list[dict[str, Any]], error: str | None = None
):
    svc = ResearchSessionService()
    sessionRow = await svc.createSession(dbSession, userId=1, question="供应商收货量为什么下降")
    turn = await svc.appendTurn(
        dbSession, sessionId=sessionRow.id, role="user", content={"question": "x"}
    )
    supportingData: dict[str, Any] = {"rowCount": len(rows), "error": error, "rows": rows}
    if verdict is not None:
        supportingData["verdict"] = verdict
    await svc.saveFinding(
        dbSession,
        sessionId=sessionRow.id,
        turnId=turn.id,
        claimText="供应商A供货减少",
        supportingSql="SELECT 1 AS cnt",
        supportingData=supportingData,
        confidence=0.72,
    )
    await dbSession.commit()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: _FakeLlm(),
        sessionFactory=lambda: dbSession,
    )
    return planner, sessionRow.id, turn.id


async def test_report_inconclusive_finding_is_not_verified_without_none_leak(
    dbSession: AsyncSession,
) -> None:
    """verdict=inconclusive 但**有行** ⇒ verified=false、「已验证」列「否」、md 无 ``None``。"""
    planner, sessionId, turnId = await _seedFinding(
        dbSession, verdict="inconclusive", rows=[{"amount": 100}]
    )

    payload, md = await planner.compose(
        None, sessionId=sessionId, turnId=turnId, mode="attribution"
    )

    assert payload["findingsRef"][0]["verified"] is False
    table = next(s for s in payload["sections"] if s["kind"] == "hypothesis_table")
    assert table["blocks"][0]["content"]["rows"][0]["已验证"] == "否"
    # 反向断言（P2）：任何输出字符串里都不得出现字面 "None"
    assert "None" not in md
    assert "证据不足" in md  # 备选假设区块的三分支措辞


async def test_report_historical_finding_without_verdict_still_verified(
    dbSession: AsyncSession,
) -> None:
    """历史 finding（``supporting_data`` 无 ``verdict`` 键）⇒ 回落旧义（error is None）⇒ 已验证。"""
    planner, sessionId, turnId = await _seedFinding(
        dbSession, verdict=None, rows=[{"amount": 100}], error=None
    )

    payload, md = await planner.compose(
        None, sessionId=sessionId, turnId=turnId, mode="attribution"
    )

    assert payload["findingsRef"][0]["verified"] is True
    table = next(s for s in payload["sections"] if s["kind"] == "hypothesis_table")
    assert table["blocks"][0]["content"]["rows"][0]["已验证"] == "是"
    assert "None" not in md


@pytest.mark.parametrize(
    ("verdict", "expected_label"),
    [
        ("execution_failed", "执行失败"),
        ("contradicted", "数据不支持"),
        ("inconclusive", "证据不足"),
    ],
)
async def test_report_unverified_labels_are_chinese_and_none_free(
    dbSession: AsyncSession, verdict: str, expected_label: str
) -> None:
    """三分支措辞中文（P2），且 no ``None`` leak。"""
    planner, sessionId, turnId = await _seedFinding(
        dbSession, verdict=verdict, rows=[{"amount": 100}], error="boom" if verdict == "execution_failed" else None
    )

    _, md = await planner.compose(
        None, sessionId=sessionId, turnId=turnId, mode="attribution"
    )

    assert expected_label in md
    assert "None" not in md
