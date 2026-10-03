"""研究状态机：暂停/恢复/动态 checkpoint/终态（真实 PG + fake 依赖）。

Task 5 契约测试。外部依赖全部 fake（ESL / planner / runner / chart / reporter /
usage recorder / LLM），**状态一律落真实 PostgreSQL**（research_session /
research_turn / research_checkpoint / research_finding / research_report）。

fake 与真实 dataclass 字段对齐（Task 5 brief 注：以真实字段为准修 fake，不改产品代码）：
- `MultiStepPlan` / `StepPlan` / `GlobalFilters` 的真实字段见 `app/domain/multi_step_plan.py`
  （StepPlan 无 sql 字段，故执行步用本文件的 `SqlStep` duck-type 表达「计划已带 SQL」）；
- `ESLExtraction` 等见 `app/services/enterprise_semantic_layer.py`；
- `ChartBuild` 由真实 `ChartService._emptyResult` 产出（零 LLM / 零 DB 阈值读取）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.domain.models import LlmConfig
from app.domain.multi_step_plan import MultiStepPlan
from app.services.research_agent_service import (
    LlmUsageRecorder,
    ResearchAgentService,
)
from app.services.research_session_service import ResearchSessionService

QUESTION = "供应商收货量为什么下降"


# ---------------------------------------------------------------------------
# fakes（仅测试资产）
# ---------------------------------------------------------------------------


class FakeEsl:
    """ESL fake：真实 ESLExtraction 形状；conflicts 可注入。"""

    def __init__(self, conflicts=(), empty: bool = False) -> None:
        self._conflicts = conflicts
        self._empty = empty

    async def extract(self, question, *, intent=None):
        from app.services.enterprise_semantic_layer import (
            BusinessObjectRef,
            ESLExtraction,
            MetricRef,
        )
        if self._empty:
            from app.services.enterprise_semantic_layer import EmptyResearchScopeError

            raise EmptyResearchScopeError("三臂检索全空")
        return ESLExtraction(
            businessObjects=[BusinessObjectRef(1, "供应商", "DIM_SUPPLIER", "供应商", 0.9)],
            metrics=[MetricRef(None, "K1", "收货量", None, 0.8)],
            knowledge=[],
            confidenceByArm={"business_object": 0.9, "metric": 0.8, "knowledge": 0.0},
            conflicts=list(self._conflicts),
        )


@dataclass(frozen=True)
class SqlStep:
    """执行步（duck-type）：真实 StepPlan 字段 + 研究链路扩展的 sql。

    真实 `StepPlan` 无 sql 字段——Task 5 的执行面只接受「计划里已带 SQL」的步
    （逐步子问题 → SQL 的生成需要 Nl2SqlService，不在 Task 5 注入面内，见报告）。
    """

    index: int
    description: str
    sub_question: str
    sql: str | None = None
    aggregation_only: bool = False


class FakePlanner:
    """计划 fake：默认真实 `MultiStepPlan`（真实字段名）+ 带 SQL 的步。"""

    def __init__(self, steps=None) -> None:
        self._steps = steps
        self.calls: list[tuple] = []

    async def plan(self, question, classes, client, modelName):
        self.calls.append((question, tuple(classes), client, modelName))
        steps = self._steps
        if steps is None:
            steps = (SqlStep(index=0, description="收货量趋势", sub_question="近12月收货量", sql="SELECT 1"),)
        return MultiStepPlan(steps=tuple(steps), aggregation_hint="", original_question=question)


class FakeRunner:
    """执行 runner fake：与 `ResearchSqlRunner` 契约对齐（execute 抛 / verify 不抛）。"""

    def __init__(self, rows=None, error: str | None = None) -> None:
        self._rows = rows if rows is not None else [{"month": "2026-01", "cnt": 100}]
        self._error = error
        self.verifyCalls: list[str] = []

    async def executeReadonlySql(self, session, sql):
        if self._error is not None:
            raise ValueError(self._error)
        return list(self._rows)

    async def runVerification(self, session, hypothesis):
        self.verifyCalls.append(hypothesis.verificationSql)
        if self._error is not None:
            return {"rows": [], "error": self._error}
        return {"rows": [{"cnt": 1}], "error": None}


class FakeChart:
    """图表 fake：复用真实 `ChartBuild` 形状（`_emptyResult`，零 LLM / 零 DB）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def buildChart(self, **kwargs):
        from app.services.chart_service import ChartService

        self.calls.append(kwargs)
        return ChartService._emptyResult(
            list(kwargs.get("columns") or []), list(kwargs.get("data") or [])
        )


class FakeReporter:
    """ReportPlanner（Task 6）占位 fake：签名与 Task 6 一致。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def compose(self, session, *, sessionId, turnId, mode, llmClient=None):
        self.calls.append({"sessionId": sessionId, "turnId": turnId, "mode": mode})
        return ({"sessionId": str(sessionId), "mode": mode}, f"# 报告 {mode}")


class FakeUsageRecorder:
    """计量 fake：记录每次 LLM 调用（核心约束 #3 的断言点）。"""

    def __init__(self) -> None:
        self.records: list[dict] = []

    async def recordUsage(
        self, session, *, sessionId, purpose, promptTokens, completionTokens, modelName
    ) -> None:
        self.records.append(
            {
                "sessionId": sessionId,
                "purpose": purpose,
                "promptTokens": promptTokens,
                "completionTokens": completionTokens,
                "modelName": modelName,
            }
        )


class FakeLlmResponse:
    def __init__(self, content: str, promptTokens: int, completionTokens: int) -> None:
        self.content = content
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.modelName = "fake-model"


class FakeLlmClient:
    """LLM fake：返回合法假设 JSON（含一条非法 verification_sql 会被适配层滤掉）。"""

    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, messages, **kwargs):
        return FakeLlmResponse(self._content, 120, 40)


HYPOTHESIS_JSON = (
    '[{"statement": "供应商A供货减少", "driver": "收货量",'
    ' "verification_sql": "SELECT 1 AS cnt"},'
    '{"statement": "坏假设", "driver": null, "verification_sql": "DELETE FROM T"}]'
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def makeService(dbSession):
    """服务工厂：默认 fake 依赖 + 真实 ResearchSessionService（真库）。"""

    def _make(**kwargs) -> ResearchAgentService:
        return ResearchAgentService(
            esl=kwargs.get("esl", FakeEsl()),
            sessionService=ResearchSessionService(),
            planner=kwargs.get("planner", FakePlanner()),
            runner=kwargs.get("runner", FakeRunner()),
            chartService=kwargs.get("chartService", FakeChart()),
            llmFactory=kwargs.get("llmFactory"),
            usageRecorder=kwargs.get("usageRecorder", FakeUsageRecorder()),
            reporter=kwargs.get("reporter", FakeReporter()),
            autoConfirm=kwargs.get("autoConfirm", False),
        )

    return _make


async def _newSession(svc, dbSession, question: str = QUESTION):
    return await svc.sessionService.createSession(dbSession, userId=1, question=question)


async def _resolve(svc, dbSession, sessionId, action: str, choice: dict | None = None) -> str:
    """取当前 pending checkpoint 并按 action 续跑（模拟 API 层调用）。"""
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, sessionId)
    assert cp is not None, "期望存在 pending checkpoint"
    return await svc.resumeTurn(
        dbSession, checkpointId=cp.id, action=action, choice=choice or {}
    )


# ---------------------------------------------------------------------------
# 1-3：brief 指定的三条契约测试
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_pauses_at_checkpoint1(dbSession, makeService) -> None:
    svc = makeService()
    s = await _newSession(svc, dbSession)
    status = await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    assert status == "awaiting_user"
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp is not None and cp.phase == "intent"
    assert cp.options["prompt"]  # prompt 无独立列，随 options 落 JSONB（Task 3 契约）
    # 会话状态也落库（不是只在返回值里）
    row = await dbSession.execute(
        text("SELECT status FROM research_session WHERE id = :sid"), {"sid": s.id}
    )
    assert row.scalar_one() == "awaiting_user"


@pytest.mark.asyncio
async def test_esl_conflict_inserts_dynamic_checkpoint_before_plan(dbSession, makeService) -> None:
    from app.services.enterprise_semantic_layer import ESLConflict

    svc = makeService(
        esl=FakeEsl(
            conflicts=[
                ESLConflict(kind="metric_ambiguous", arm="metric", detail="歧义", candidates=[])
            ]
        )
    )
    s = await _newSession(svc, dbSession, question="量")
    status = await svc.startTurn(dbSession, sessionId=s.id, question="量", userId=1)
    assert status == "awaiting_user"
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    # 冲突先于固定 #1（范围确认）：动态点在 _stageEsl 内先于固定 checkpoint 返回
    assert cp is not None and cp.phase == "runtime_dynamic"
    assert cp.options["signal"] == "metric_ambiguous"
    assert cp.options["resumePhase"] == "plan"  # 决策后跳过范围确认直接进计划


@pytest.mark.asyncio
async def test_auto_confirm_runs_to_done_and_publishes(dbSession, makeService) -> None:
    svc = makeService(autoConfirm=True)
    s = await _newSession(svc, dbSession)
    status = await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    assert status == "done"
    reports = await svc.sessionService.listReports(dbSession, s.id)
    assert reports and reports[-1].status == "published"
    assert await svc.sessionService.getPendingCheckpoint(dbSession, s.id) is None


# ---------------------------------------------------------------------------
# 4-7：暂停/恢复、动态点、计量（Task 5 报告要求的证据）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_confirm_walks_three_fixed_checkpoints_to_done(dbSession, makeService) -> None:
    """固定 #1/#2/#3 逐个 confirm：action→status 映射 + 会话终态 + 报告归档。"""
    svc = makeService()
    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "awaiting_user"

    cp1 = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp1.phase == "intent"
    assert await _resolve(svc, dbSession, s.id, "confirm") == "awaiting_user"

    cp2 = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp2.phase == "planning" and cp2.id != cp1.id
    assert await _resolve(svc, dbSession, s.id, "modify", {"plan": "edited"}) == "awaiting_user"

    cp3 = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp3.phase == "hypothesis"
    assert await _resolve(svc, dbSession, s.id, "confirm") == "done"

    # 决策落库：confirm→confirmed / modify→modified（Task 3 review 裁定）
    for cpId, expected in ((cp1.id, "confirmed"), (cp2.id, "modified"), (cp3.id, "confirmed")):
        status = await dbSession.execute(
            text("SELECT status FROM research_checkpoint WHERE id = :cid"), {"cid": cpId}
        )
        assert status.scalar_one() == expected
    assert await svc.sessionService.getPendingCheckpoint(dbSession, s.id) is None
    reports = await svc.sessionService.listReports(dbSession, s.id)
    assert reports[-1].status == "published" and reports[-1].version == 1


@pytest.mark.asyncio
async def test_low_confidence_step_pauses_and_reject_aborts_to_report(
    dbSession, makeService
) -> None:
    """执行步返回空数据 → low_confidence_step 动态点；reject → 终止并归档报告。"""
    svc = makeService(runner=FakeRunner(rows=[]), autoConfirm=False)
    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "awaiting_user"
    assert await _resolve(svc, dbSession, s.id, "confirm") == "awaiting_user"  # 固定 #1 范围确认
    assert await _resolve(svc, dbSession, s.id, "confirm") == "awaiting_user"  # 固定 #2 计划确认

    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp.phase == "low_confidence_step"
    assert cp.options["signal"] == "low_confidence_step"
    assert cp.options["stepIndex"] == 0 and cp.options["resumePhase"] == "execute"
    assert cp.options["abortPhase"] == "report"

    # reject ⇒ 走 abortPhase（终止执行，直接出报告归档）
    assert await _resolve(svc, dbSession, s.id, "reject") == "done"
    reports = await svc.sessionService.listReports(dbSession, s.id)
    assert reports[-1].status == "published"


@pytest.mark.asyncio
async def test_step_failure_from_sql_guard_is_isolated_as_dynamic_signal(
    dbSession, makeService
) -> None:
    """executeReadonlySql 抛 ValueError（SQL Guard 拒绝）→ 步级隔离，不炸整条链路。"""
    svc = makeService(runner=FakeRunner(error="仅允许只读 SELECT"))
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #1
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #2
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp is not None and cp.phase == "low_confidence_step"
    assert cp.options["signal"] == "sql_validation_failed"
    assert "只读" in cp.options["error"]


@pytest.mark.asyncio
async def test_hypothesis_llm_call_is_metered(dbSession, makeService) -> None:
    """假设生成的 LLM 调用必须计量（核心约束 #3）：purpose=hypothesis + token + model。"""
    recorder = FakeUsageRecorder()
    svc = makeService(llmFactory=lambda cfg: FakeLlmClient(HYPOTHESIS_JSON), usageRecorder=recorder)
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #1
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #2 → hypothesis 阶段
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp is not None and cp.phase == "hypothesis"
    assert [c["statement"] for c in cp.options["candidates"]] == ["供应商A供货减少"]  # 非法项被滤掉
    assert recorder.records == [
        {
            "sessionId": str(s.id),
            "purpose": "research_hypothesis",
            "promptTokens": 120,
            "completionTokens": 40,
            "modelName": "fake-model",
        }
    ]

    # confirm 固定 #3 → verify 落 finding（confidence = 候选分 × 0.9）
    assert await _resolve(svc, dbSession, s.id, "confirm") == "done"
    finding = (
        await dbSession.execute(
            text(
                "SELECT claim_text, confidence, supporting_data FROM research_finding"
                " WHERE session_id = :sid"
            ),
            {"sid": s.id},
        )
    ).one()
    assert finding.claim_text == "供应商A供货减少"
    assert finding.confidence == Decimal("0.72")  # 0.8（ESL 指标分）× 0.9
    assert finding.supporting_data["rowCount"] == 1


@pytest.mark.asyncio
async def test_empty_scope_pauses_for_rewrite_even_in_auto_confirm(dbSession, makeService) -> None:
    """三臂全空（EmptyResearchScopeError）→ 固定 #1 强制用户改写（autoConfirm 也不例外）。"""
    svc = makeService(esl=FakeEsl(empty=True), autoConfirm=True)
    s = await _newSession(svc, dbSession)
    status = await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    assert status == "awaiting_user"
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp is not None and cp.phase == "intent" and cp.options["signal"] == "empty_scope"


@pytest.mark.asyncio
async def test_default_usage_recorder_writes_row_and_skips_zero_usage(dbSession) -> None:
    """默认计量实现写 chat 同表台账；零消耗不写空行。

    只走「无 modelName」分支（成本 0）：本测试库 `llm_config` 缺 `disable_thinking`
    列（`alembic_version` 标 0111 但结构滞后于 0109，既有环境漂移，见 Task 5 报告
    open concerns），任何 LlmConfig ORM 读写都会炸；单价公式另由下一条纯断言覆盖。
    """
    recorder = LlmUsageRecorder()
    sessionId = str(uuid.uuid4())
    await recorder.recordUsage(
        dbSession,
        sessionId=sessionId,
        purpose="research_hypothesis",
        promptTokens=1000,
        completionTokens=500,
        modelName=None,
    )
    row = (
        await dbSession.execute(
            text(
                "SELECT model_config_id, prompt_tokens, completion_tokens, total_tokens,"
                " cost, purpose FROM session_token_usage WHERE session_id = :sid"
            ),
            {"sid": sessionId},
        )
    ).one()
    assert row.model_config_id is None
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (1000, 500, 1500)
    assert row.cost == Decimal("0")
    assert row.purpose == "research_hypothesis"

    await recorder.recordUsage(
        dbSession,
        sessionId=sessionId,
        purpose="research_hypothesis",
        promptTokens=0,
        completionTokens=0,
        modelName=None,
    )
    count = (
        await dbSession.execute(
            text("SELECT count(*) FROM session_token_usage WHERE session_id = :sid"),
            {"sid": sessionId},
        )
    ).scalar_one()
    assert count == 1


def test_default_usage_recorder_cost_formula_matches_chat() -> None:
    """成本公式与 chat `_costFor` 同口径：pt/1000*in + ct/1000*out。

    未落库的 ORM 实例即可（不触 DB，绕开上面的 llm_config 漂移）。
    """
    config = LlmConfig(
        model_name="research-fake-model",
        provider="openai",
        cost_per_1k_input=Decimal("1"),
        cost_per_1k_output=Decimal("2"),
    )
    assert LlmUsageRecorder._costFor(config, 1000, 500) == Decimal("2")
    assert LlmUsageRecorder._costFor(None, 1000, 500) == Decimal("0")
