"""研究状态机：暂停/恢复/动态 checkpoint/终态（真实 PG + fake 依赖）。

Task 5 契约测试（Task 6 起：报告阶段走**真实** `ReportPlanner`，仅 LLM / ESL / planner /
runner / chart 是 fake）。外部依赖全部 fake（ESL / planner / runner / chart /
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
from app.services.nl2sql_service import SqlResult
from app.services.report_planner import ReportPlanner
from app.services.research_agent_ports import STEP_MISSING_SQL, LlmUsageRecorder
from app.services.research_agent_service import ResearchAgentService
from app.services.research_session_service import ResearchSessionService
from app.services.token_usage_service import TokenUsageService

QUESTION = "供应商收货量为什么下降"


# ---------------------------------------------------------------------------
# fakes（仅测试资产）
# ---------------------------------------------------------------------------


class FakeEsl:
    """ESL fake：真实 ESLExtraction 形状；conflicts / 空 scope 可注入，记录调用问题。"""

    def __init__(self, conflicts=(), empty: bool = False, emptyOnce: bool = False) -> None:
        self._conflicts = conflicts
        self._empty = empty
        self._emptyOnce = emptyOnce
        self.calls: list[str] = []

    async def extract(self, question, *, intent=None):
        from app.services.enterprise_semantic_layer import (
            BusinessObjectRef,
            ESLExtraction,
            MetricRef,
        )
        self.calls.append(question)
        if self._empty or (self._emptyOnce and len(self.calls) == 1):
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
    """计划 fake：默认真实 `MultiStepPlan`（真实字段名）+ 带 SQL 的步。

    `replanSteps`：第二次及以后调用的产物（Task 6.5-4 modify 重跑计划用）。
    """

    def __init__(self, steps=None, replanSteps=None) -> None:
        self._steps = steps
        self._replanSteps = replanSteps
        self.calls: list[tuple] = []

    async def plan(self, question, classes, client, modelName):
        self.calls.append((question, tuple(classes), client, modelName))
        steps = self._steps if len(self.calls) == 1 else (self._replanSteps or self._steps)
        if steps is None:
            steps = (SqlStep(index=0, description="收货量趋势", sub_question="近12月收货量", sql="SELECT 1"),)
        return MultiStepPlan(steps=tuple(steps), aggregation_hint="", original_question=question)


class FakeRunner:
    """执行 runner fake：与 `ResearchSqlRunner` 契约对齐（execute 抛 / verify 不抛）。

    `error` 模拟 SQL Guard 拒绝（ValueError）；`raiseError` 模拟任意异常（含编程错误），
    用于验证 `_runStep` 的异常收窄。`executed` 记录真正执行过的 SQL（Task 6.5-1 断言点）。
    """

    def __init__(self, rows=None, error: str | None = None, raiseError=None) -> None:
        self._rows = rows if rows is not None else [{"month": "2026-01", "cnt": 100}]
        self._error = error
        self._raiseError = raiseError
        self.verifyCalls: list[str] = []
        self.executed: list[str] = []

    async def executeReadonlySql(self, session, sql):
        self.executed.append(sql)
        if self._raiseError is not None:
            raise self._raiseError
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


class RaisingReporter:
    """报告装配抛错：验证报告阶段异常**上抛 + 会话 failed**（不静默 done）。"""

    async def compose(self, session, *, sessionId, turnId, mode, llmClient=None):
        raise RuntimeError("reporter boom")


class FakeUsageRecorder:
    """计量 fake：记录每次 LLM 调用（核心约束 #3 的断言点）。"""

    def __init__(self) -> None:
        self.records: list[dict] = []

    async def recordUsage(
        self, session, *, sessionId, purpose, promptTokens, completionTokens, modelName,
        cachedTokens=None,
    ) -> None:
        self.records.append(
            {
                "sessionId": sessionId,
                "purpose": purpose,
                "promptTokens": promptTokens,
                "completionTokens": completionTokens,
                "modelName": modelName,
                "cachedTokens": cachedTokens,
            }
        )


# ---------------------------------------------------------------------------
# Task 6.5 fakes：模型路由 / 本体 / NL2SQL（Step 0 真实签名对齐）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeModelConfig:
    """模型配置 fake（duck-type LlmConfig 的路由面）。"""

    id: int = 7
    model_name: str = "fake-routed-model"
    provider: str = "openai"
    is_active: bool = True


class FakeModelConfigs:
    """模型配置 provider fake：duck-type `ModelConfigService.list(session, activeOnly=)`。"""

    def __init__(self, configs=None) -> None:
        self._configs = list(configs) if configs is not None else [FakeModelConfig()]
        self.listCalls = 0

    async def list(self, session, *, activeOnly: bool = False):
        self.listCalls += 1
        return list(self._configs)


class FakeModelRouter:
    """路由 fake：取首个候选（真实实现 `ModelRouterService.selectModel`，测试求确定性）。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def selectModel(self, configs, prompt, ctx):
        self.calls.append((tuple(configs), prompt, ctx))
        return configs[0]


@dataclass(frozen=True)
class FakeClass:
    """本体类 fake（只需 `source_table` / `class_name` 两个筛选字段）。"""

    source_table: str = "DIM_SUPPLIER"
    class_name: str = "供应商"


class FakeOntology:
    """本体 fake：duck-type `OntologyService.listClasses(session)`。"""

    def __init__(self, classes=None) -> None:
        self._classes = list(classes) if classes is not None else [FakeClass()]

    async def listClasses(self, session, *, includeExpired: bool = False):
        return list(self._classes)


class FakeNl2Sql:
    """NL2SQL fake：duck-type `Nl2SqlService.generateSql`（Step 0 真实签名前 4 位参）。

    真实签名：`generateSql(question, classes, llmClient, modelConfig, *, ...,
    session=None)` —— 前四位位置参数固定，其余全 keyword。`error` 注入生成失败。
    """

    def __init__(self, sql: str = "SELECT 42 AS cnt", error: Exception | None = None) -> None:
        self._sql = sql
        self._error = error
        self.calls: list[dict] = []

    async def generateSql(self, question, classes, llmClient, modelConfig, **kwargs):
        self.calls.append(
            {
                "question": question,
                "classes": tuple(classes),
                "llmClient": llmClient,
                "modelConfig": modelConfig,
                **kwargs,
            }
        )
        if self._error is not None:
            raise self._error
        return SqlResult(sql=self._sql, promptTokens=0, completionTokens=0)


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


class ScriptedLlmClient:
    """按调用序切换内容：第 1 次喂假设 JSON，之后喂报告文本（同一客户端服务两个阶段）。"""

    def __init__(self, hypothesisJson: str, reportText: str) -> None:
        self._hypothesisJson = hypothesisJson
        self._reportText = reportText
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        content = self._hypothesisJson if self.calls == 1 else self._reportText
        return FakeLlmResponse(content, 120, 40)


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
    """服务工厂：fake 外部依赖 + **真实** ResearchSessionService / ReportPlanner（真库）。

    报告阶段默认走真实 `ReportPlanner`（Task 6 接线后即产品默认路径）；`reporter` 可覆写
    以测异常路径（`RaisingReporter`）。
    """

    def _make(**kwargs) -> ResearchAgentService:
        sessions = ResearchSessionService()
        return ResearchAgentService(
            esl=kwargs.get("esl", FakeEsl()),
            sessionService=sessions,
            planner=kwargs.get("planner", FakePlanner()),
            runner=kwargs.get("runner", FakeRunner()),
            chartService=kwargs.get("chartService", FakeChart()),
            llmFactory=kwargs.get("llmFactory"),
            usageRecorder=kwargs.get("usageRecorder", FakeUsageRecorder()),
            reporter=kwargs.get("reporter", ReportPlanner(sessionService=sessions)),
            autoConfirm=kwargs.get("autoConfirm", False),
            # Task 6.5：模型路由 / 本体 / NL2SQL 三端口默认注入确定性 fake。
            # 默认 fake 配置存在 ⇒ 注入的 llmFactory 会收到**非 None** 的配置；
            # llmFactory=None 时无可用配置 ⇒ 走 research.error 降级（既有语义）。
            modelConfigs=kwargs.get("modelConfigs", FakeModelConfigs()),
            modelRouter=kwargs.get("modelRouter", FakeModelRouter()),
            ontology=kwargs.get("ontology", FakeOntology()),
            nl2sql=kwargs.get("nl2sql"),
        )

    return _make


async def _newSession(svc, dbSession, question: str = QUESTION):
    return await svc.sessionService.createSession(dbSession, userId=1, question=question)


async def _resolve(
    svc, dbSession, sessionId, action: str, choice: dict | None = None, emit=None
) -> str:
    """取当前 pending checkpoint 并按 action 续跑（模拟 API 层调用）。"""
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, sessionId)
    assert cp is not None, "期望存在 pending checkpoint"
    return await svc.resumeTurn(
        dbSession, checkpointId=cp.id, action=action, choice=choice or {}, emit=emit
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
    """真实 ReportPlanner 装配路径：三 mode 模板 + 归档 + MD 渲染。"""
    scripted = ScriptedLlmClient(HYPOTHESIS_JSON, "解读：收货量下降明显。")
    svc = makeService(autoConfirm=True, llmFactory=lambda cfg: scripted)
    s = await _newSession(svc, dbSession)
    status = await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    assert status == "done"
    reports = await svc.sessionService.listReports(dbSession, s.id)
    assert reports and reports[-1].status == "published" and reports[-1].version == 1
    assert await svc.sessionService.getPendingCheckpoint(dbSession, s.id) is None

    report = reports[-1]
    assert [sec["kind"] for sec in report.payload["sections"]] == [
        "executive_summary",
        "data",
        "knowledge",
        "methodology",
    ]
    # chart 块的行数据逐字来自 finding.supporting_data["rows"]（Task 6 数据来源契约）
    charts = [
        block
        for sec in report.payload["sections"]
        for block in sec["blocks"]
        if block["type"] == "chart"
    ]
    assert charts and charts[0]["content"]["rows"] == [{"cnt": 1}]
    assert charts[0]["sourceRefs"][0]["kind"] == "finding"
    assert report.rendered_md.startswith("# ")
    assert "| cnt |" in report.rendered_md and "| 1 |" in report.rendered_md  # 逐字
    assert "解读：收货量下降明显。" in report.rendered_md


@pytest.mark.asyncio
async def test_report_llm_calls_are_metered_and_numbers_verbatim(dbSession, makeService) -> None:
    """报告文本块 LLM 调用计量（累加）+ 数字仍逐字来自 finding 数据（不被 LLM 改写）。"""
    recorder = FakeUsageRecorder()
    # 单实例共享：llmFactory 每个阶段各调一次，若每阶段新建则 calls 永远停在 1，
    # 报告阶段会拿到假设 JSON 而非报告文本。
    scripted = ScriptedLlmClient(HYPOTHESIS_JSON, "解读：收货量下降明显。")
    svc = makeService(
        llmFactory=lambda cfg: scripted,
        usageRecorder=recorder,
        autoConfirm=True,
    )
    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "done"
    report = (await svc.sessionService.listReports(dbSession, s.id))[-1]

    # LLM 文本进了执行摘要；chart 块数字仍是 finding 数据（LLM 只拿到 claim 摘要）
    assert report.payload["sections"][0]["blocks"][0]["content"] == "解读：收货量下降明显。"
    charts = [
        block
        for sec in report.payload["sections"]
        for block in sec["blocks"]
        if block["type"] == "chart"
    ]
    assert charts[0]["content"]["rows"] == [{"cnt": 1}]
    # 报告阶段 2 次调用（执行摘要 + 1 条结论解读）累加计量：240 / 80
    assert [r for r in recorder.records if r["purpose"] == "research_report"] == [
        {
            "sessionId": str(s.id),
            "purpose": "research_report",
            "promptTokens": 240,
            "completionTokens": 80,
            "modelName": "fake-model",
            "cachedTokens": None,
        }
    ]


@pytest.mark.asyncio
async def test_reporter_failure_marks_session_failed(dbSession, makeService) -> None:
    """报告阶段异常必须上抛 + 会话 failed（不静默 done、不留半份报告）。"""
    svc = makeService(autoConfirm=True, reporter=RaisingReporter())
    s = await _newSession(svc, dbSession)
    with pytest.raises(RuntimeError):
        await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    status = (
        await dbSession.execute(
            text("SELECT status FROM research_session WHERE id = :sid"), {"sid": s.id}
        )
    ).scalar_one()
    assert status == "failed"
    assert await svc.sessionService.listReports(dbSession, s.id) == []


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
            "cachedTokens": None,
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


# ---------------------------------------------------------------------------
# fix round 1：空臂恢复通道 / 异常收窄 / 降级可见性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_scope_resume_with_rewritten_question_reruns_esl(dbSession, makeService) -> None:
    """HIGH-1：空 scope 恢复必须带改写问题**重跑 ESL**（不是拿空臂直进 plan）。

    两条修复一起验证：① options["resumePhase"] 显式压过固定表（intent→esl 而非 plan）；
    ② resumeTurn 的 choice["question"] 通道换问题并清派生态。
    """
    esl = FakeEsl(emptyOnce=True)
    svc = makeService(esl=esl)
    s = await _newSession(svc, dbSession, question="量")
    assert await svc.startTurn(dbSession, sessionId=s.id, question="量", userId=1) == "awaiting_user"
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp.phase == "intent" and cp.options["signal"] == "empty_scope"
    assert cp.options["resumePhase"] == "esl"  # 显式恢复点（固定表里 intent→plan）

    assert (
        await svc.resumeTurn(
            dbSession,
            checkpointId=cp.id,
            action="confirm",
            choice={"question": QUESTION},
        )
        == "awaiting_user"
    )
    # ESL 真的用新问题重跑了（不是跳过）
    assert esl.calls == ["量", QUESTION]
    cp2 = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp2.id != cp.id
    assert cp2.options["signal"] == "fixed_scope"  # 走到固定 #1，而非直接 plan
    assert cp2.options["arms"]["businessObjects"]  # 非空三臂进了新 checkpoint
    # 改写后的问题落 turn 内容（可追溯）
    content = (
        await dbSession.execute(
            text(
                "SELECT content FROM research_turn WHERE session_id = :sid AND role = 'user'"
                " ORDER BY turn_index DESC LIMIT 1"
            ),
            {"sid": s.id},
        )
    ).scalar_one()
    assert content["question"] == QUESTION


@pytest.mark.asyncio
async def test_intent_checkpoint_confirm_still_resumes_at_plan(dbSession, makeService) -> None:
    """HIGH-1 回归：普通范围确认（无改写问题）仍从 plan 续跑，ESL 不重跑。"""
    esl = FakeEsl()
    svc = makeService(esl=esl)
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    assert await _resolve(svc, dbSession, s.id, "confirm") == "awaiting_user"
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp.phase == "planning"  # 固定表路径没被显式优先规则翻转
    assert esl.calls == [QUESTION]  # ESL 只跑了一次


@pytest.mark.asyncio
async def test_runner_programming_error_is_not_masked_as_low_confidence(
    dbSession, makeService
) -> None:
    """MEDIUM-5：runner 抛非 DB 异常（编程错误）必须上抛并把会话标 failed。

    不能被伪装成「该步无数据」→ low_confidence_step →（autoConfirm 下）静默 done。
    """
    svc = makeService(runner=FakeRunner(raiseError=RuntimeError("boom")), autoConfirm=True)
    s = await _newSession(svc, dbSession)
    with pytest.raises(RuntimeError):
        await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    status = (
        await dbSession.execute(
            text("SELECT status FROM research_session WHERE id = :sid"), {"sid": s.id}
        )
    ).scalar_one()
    assert status == "failed"
    # 编程错误不产生 finding / 报告
    assert await svc.sessionService.getPendingCheckpoint(dbSession, s.id) is None


@pytest.mark.asyncio
async def test_step_failure_emits_research_error_event(dbSession, makeService) -> None:
    """MEDIUM-7：步失败除动态点外还必须发 `research.error {code, message}`。"""
    events: list[tuple[str, dict]] = []

    async def collect(event: str, payload: dict) -> None:
        events.append((event, payload))

    svc = makeService(runner=FakeRunner(error="仅允许只读 SELECT"))
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #1
    await _resolve(svc, dbSession, s.id, "confirm", emit=collect)  # 固定 #2 → execute

    errors = [p for (e, p) in events if e == "research.error"]
    assert [p["code"] for p in errors] == ["sql_validation_failed"]
    assert "只读" in errors[0]["message"] and errors[0]["stepIndex"] == 0


@pytest.mark.asyncio
async def test_missing_llm_client_is_user_visible_not_silent(dbSession, makeService) -> None:
    """MEDIUM-4：llmFactory 取不到客户端必须显式留痕（`research.error` + 报告降级标记）。"""
    events: list[tuple[str, dict]] = []

    async def collect(event: str, payload: dict) -> None:
        events.append((event, payload))

    svc = makeService(autoConfirm=True)  # llmFactory 默认 None
    s = await _newSession(svc, dbSession)
    status = await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1, emit=collect)
    assert status == "done"
    codes = [p["code"] for (e, p) in events if e == "research.error"]
    assert "llm_unavailable" in codes  # 不静默
    reportEvent = [p for (e, p) in events if e == "research.report"][-1]
    assert reportEvent["degraded"] is True
    doneEvent = [p for (e, p) in events if e == "research.done"][-1]
    assert doneEvent["degraded"] is True


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


# ---------------------------------------------------------------------------
# Task 6.5：步 SQL 生成 / 模型路由 / 计量口径 / modify 反馈
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_without_sql_generates_via_nl2sql(dbSession, makeService) -> None:
    """Task 6.5-1：无 sql 的计划步走真实 NL2SQL 生成，生成的 SQL 交给 runner 执行。"""
    planner = FakePlanner(
        steps=(SqlStep(index=0, description="收货量趋势", sub_question="近12月收货量"),)
    )
    nl2sql = FakeNl2Sql(sql="SELECT 42 AS cnt")
    runner = FakeRunner(rows=[{"cnt": 42}])
    svc = makeService(
        planner=planner,
        nl2sql=nl2sql,
        runner=runner,
        llmFactory=lambda cfg: FakeLlmClient(HYPOTHESIS_JSON),
        autoConfirm=True,
    )
    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "done"

    assert [c["question"] for c in nl2sql.calls] == ["近12月收货量"]
    assert runner.executed == ["SELECT 42 AS cnt"]  # 生成的 SQL 真的进了执行面
    # 生成用的模型配置是路由选中的那个（非 None），且本体类按 ESL 物理表筛过
    assert nl2sql.calls[0]["modelConfig"] is not None
    assert [c.class_name for c in nl2sql.calls[0]["classes"]] == ["供应商"]
    # 步结果里记的是**生成后**的 SQL（报告/动态点据此可追溯）
    finding = (
        await dbSession.execute(
            text("SELECT supporting_data FROM research_finding WHERE session_id = :sid"),
            {"sid": s.id},
        )
    ).one()
    assert finding.supporting_data["rowCount"] == 1


@pytest.mark.asyncio
async def test_step_sql_generation_failure_hits_missing_sql_branch(dbSession, makeService) -> None:
    """Task 6.5-2：生成器抛错 → 步走 `STEP_MISSING_SQL` 分支（不炸链路，转动态点）。"""
    planner = FakePlanner(steps=(SqlStep(index=0, description="x", sub_question="y"),))
    nl2sql = FakeNl2Sql(error=RuntimeError("生成炸了"))
    runner = FakeRunner()
    svc = makeService(
        planner=planner, nl2sql=nl2sql, runner=runner,
        llmFactory=lambda cfg: FakeLlmClient(HYPOTHESIS_JSON),
    )
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #1 → plan → 固定 #2
    assert await _resolve(svc, dbSession, s.id, "confirm") == "awaiting_user"  # → execute

    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp is not None and cp.phase == "low_confidence_step"
    assert cp.options["error"] == STEP_MISSING_SQL  # 该分支真的被走到（可测）
    assert cp.options["signal"] == "low_confidence_step"
    assert runner.executed == []  # 无 SQL 不执行任何查询
    assert nl2sql.calls  # 生成器确实被调用了（失败发生在生成里，不是没接线）


@pytest.mark.asyncio
async def test_model_routing_uses_configured_model(dbSession, makeService) -> None:
    """Task 6.5-2：llmFactory 收到**非 None** 的已选模型配置；无 key 时显式降级。"""
    seen: list = []
    scripted = ScriptedLlmClient(HYPOTHESIS_JSON, "解读：收货量下降明显。")

    def factory(config):
        seen.append(config)
        return scripted

    svc = makeService(autoConfirm=True, llmFactory=factory)
    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "done"
    assert seen and all(cfg is not None for cfg in seen)  # 不再 factory(None)
    assert {cfg.model_name for cfg in seen} == {"fake-routed-model"}

    # key 缺失（factory 恒返 None）⇒ 无可构造客户端的配置 ⇒ research.error 降级
    events: list[tuple[str, dict]] = []

    async def collect(event: str, payload: dict) -> None:
        events.append((event, payload))

    svc2 = makeService(autoConfirm=True, llmFactory=lambda cfg: None)
    s2 = await _newSession(svc2, dbSession)
    status = await svc2.startTurn(
        dbSession, sessionId=s2.id, question=QUESTION, userId=1, emit=collect
    )
    assert status == "done"
    assert "llm_unavailable" in [p["code"] for (e, p) in events if e == "research.error"]


@pytest.mark.asyncio
async def test_metered_client_captures_cached_tokens(dbSession, makeService) -> None:
    """Task 6.5-3：响应带 cached 字段 ⇒ recordUsage 收到 cachedTokens（计量盲区封堵）。"""

    class CachedLlmClient:
        async def complete(self, messages, **kwargs):
            resp = FakeLlmResponse(HYPOTHESIS_JSON, 120, 40)
            resp.cachedTokens = 800
            return resp

    recorder = FakeUsageRecorder()
    svc = makeService(
        llmFactory=lambda cfg: CachedLlmClient(), usageRecorder=recorder, autoConfirm=True
    )
    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "done"
    cached = [r["cachedTokens"] for r in recorder.records if r["purpose"] == "research_hypothesis"]
    assert cached == [800]


@pytest.mark.asyncio
async def test_planning_modify_reruns_planner_with_choice(dbSession, makeService) -> None:
    """Task 6.5-4：计划 checkpoint `modify` 把 choice 回灌 planner 重跑（不恢复旧计划）。"""
    planner = FakePlanner(
        steps=(SqlStep(index=0, description="原计划", sub_question="原", sql="SELECT 1"),),
        replanSteps=(SqlStep(index=0, description="新计划", sub_question="新", sql="SELECT 2"),),
    )
    runner = FakeRunner()
    svc = makeService(planner=planner, runner=runner)
    s = await _newSession(svc, dbSession)
    await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1)
    await _resolve(svc, dbSession, s.id, "confirm")  # 固定 #1 → plan → 固定 #2
    cp = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp is not None and cp.phase == "planning"
    assert len(planner.calls) == 1  # 首轮计划只跑一次

    status = await _resolve(svc, dbSession, s.id, "modify", {"instruction": "只看华东"})
    assert status == "awaiting_user"
    assert len(planner.calls) == 2  # 重跑计划
    assert "只看华东" in planner.calls[1][0]  # choice 回灌进 planner 输入
    assert runner.executed == ["SELECT 2"]  # 新计划进 state 并被执行（旧计划未执行）
    # 新计划没有再次暂停在计划点：直接走到假设点
    cp2 = await svc.sessionService.getPendingCheckpoint(dbSession, s.id)
    assert cp2 is not None and cp2.phase == "hypothesis"


@pytest.mark.asyncio
async def test_compose_requires_or_meters_client(dbSession, makeService) -> None:
    """Task 6.5-3（F3）：报告装配**不自建未计量客户端**；降级时零 LLM 调用。"""
    recorder = FakeUsageRecorder()
    svc = makeService(autoConfirm=True, llmFactory=lambda cfg: None, usageRecorder=recorder)
    assert svc._reporter._llmClientFactory is None  # 自建路径已删除（选法 = 删除）

    s = await _newSession(svc, dbSession)
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "done"
    assert [r for r in recorder.records if r["purpose"] == "research_report"] == []


@pytest.mark.asyncio
async def test_routing_context_carries_session_cost_and_turns(dbSession, makeService) -> None:
    """M1（fix round 1）：路由上下文与 chat 同口径 —— 已有真实用量行 ⇒ 三项非默认。

    只传 `sessionId` 时 `sessionCost=0` / `sessionTurnCount=0`，路由器的**预算超限降级**
    （`model_router_service.py:70`）与**会话亲和**（`:77`）两条规则对研究链路恒不触发。
    这里先落一条真实 `session_token_usage` 行，再断言 router 实际收到的 ctx。
    """
    router = FakeModelRouter()
    svc = makeService(
        modelRouter=router, autoConfirm=True,
        llmFactory=lambda cfg: FakeLlmClient(HYPOTHESIS_JSON),
    )
    s = await _newSession(svc, dbSession)
    await TokenUsageService().recordUsage(
        dbSession, sessionId=str(s.id), modelConfigId=None, modelName="prior-model",
        promptTokens=100, completionTokens=50, cost=Decimal("3.5"), purpose="seed",
    )
    assert await svc.startTurn(dbSession, sessionId=s.id, question=QUESTION, userId=1) == "done"

    assert router.calls, "路由必须被调用（有可用配置）"
    ctx = router.calls[0][2]
    assert ctx.sessionId == str(s.id)
    assert ctx.sessionCost > 0  # 非零 ⇒ 预算规则可达（此前恒 0）
    assert ctx.sessionTurnCount >= 1  # 非零 ⇒ 亲和规则可达（此前恒 0）
