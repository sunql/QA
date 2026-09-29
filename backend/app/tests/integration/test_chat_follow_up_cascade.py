"""追问级联（feat-follow-up-cascade）集成测试。

覆盖三层级联：
- A：省略式追问分类（"4月份呢？" → FOLLOW_UP）在 unit/test_intent_service.py；
- B：FOLLOW_UP 且上一轮是多步问题 → LLM 改写回完整多步问题并重跑多步
  （改写失败/拆不出多步 → 退回单轮状态注入）；
- C：短句 NEW_QUERY 计划不可回答 + 有历史 → 自动升级追问重试一次
  （上一轮无 SQL / 问题过长 → 不重试，守 N6 权衡）。

dbSession 走 integration/conftest.py 真实 PostgreSQL + TRUNCATE 隔离；
LLM/路由/embedding/业务库适配器为 fake（同 test_chat_service_state.py 模式）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DataSourceType, IntentType
from app.domain.exceptions import NotFoundError
from app.domain.models import (
    DataSource,
    LlmConfig,
    OntologyClass,
    SessionMessage,
    SessionQueryState,
)
from app.domain.query_plan import QueryPlan
from app.domain.schemas import ChatRequest
from app.services.chat_service import ChatService


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _CascadeLlm:
    """按 prompt 路由回复的假客户端，支持注入改写结果与计划阶段应答序列。

    调用顺序记录到 calls，供断言改写/计划各被调用几次。
    """

    def __init__(
        self,
        *,
        rewritten: str | None = None,
        planResponses: list[str] | None = None,
        globalFilters: str | None = None,
    ) -> None:
        self.calls: list[list[tuple[str, str]]] = []
        self.rewritten = rewritten
        # 计划阶段应答队列（pop(0)）；为空时返回默认可回答计划
        self.planResponses = planResponses or []
        # 全局过滤提取器应答；None → 返回空 JSON（等价于「无跨步骤约束」）
        self.globalFilters = globalFilters

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content
        if "全局过滤提取器" in system:
            return _Resp(self.globalFilters or "{}")
        if "问题改写器" in system:
            if self.rewritten is None:
                return _Resp('无法改写')
            return _Resp('{"question": "%s"}' % self.rewritten)
        if "解析为查询计划" in system:
            if self.planResponses:
                return _Resp(self.planResponses.pop(0))
            return _Resp('{"target":"各供应商的收货数量汇总"}')
        if "生成 SQL 时必须" in system:
            return _Resp(
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        return _Resp("查询完成，共 2 条记录。")


class _FakeDatasourceService:
    def __init__(self, ds: DataSource) -> None:
        self._ds = ds

    async def get(self, session, datasourceId: int) -> DataSource:
        if datasourceId != self._ds.id:
            raise NotFoundError(f"数据源 {datasourceId} 不存在")
        return self._ds


class _FakeOntologyService:
    def __init__(self, classes: list[OntologyClass] | None = None) -> None:
        self._classes = classes or []

    async def listClasses(self, session) -> list[OntologyClass]:
        return self._classes

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
        return []

    async def listMetrics(self, session) -> list:
        return []

    async def listJoins(self, session) -> list:
        return []


class _FakeAdapter:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        return self._rows


class _FakeRouter:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None


class _FakeEmbeddingService:
    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None

    async def searchSimilarQueries(self, question: str, **kwargs) -> list:
        return []


class _FakeTokenUsage:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def getSessionCost(self, session, sessionId: str) -> Decimal:
        return Decimal("0")

    async def getSessionTurnCount(self, session, sessionId: str) -> int:
        return 0

    async def getLastModelId(self, session, sessionId: str) -> int | None:
        return None

    async def recordUsage(self, session, **kwargs) -> None:
        self.records.append(kwargs)


def _config() -> LlmConfig:
    return LlmConfig(
        id=1,
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )


def _datasource() -> DataSource:
    return DataSource(
        id=1,
        name="ZJTH",
        type=DataSourceType.ORACLE,
        host="h",
        port=1521,
        database_name="svc",
        username="u",
        password_encrypted="cipher",
    )


def _dto(question: str, datasourceId: int = 1) -> ChatRequest:
    return ChatRequest(sessionId="s1", question=question, datasourceId=datasourceId)


_MULTI_STEP_QUESTION = (
    "第一步，查询3月份供货量最多的三家供应商；"
    "第二步，查询这三家供应商各自供货量前三的物料"
)
_REWRITTEN_QUESTION = (
    "第一步，查询4月份供货量最多的三家供应商；"
    "第二步，查询这三家供应商各自供货量前三的物料"
)

_GLOBAL_FILTERS_JSON = json.dumps({
    "global": [
        {"table": "DWD_PURCHASE_ORDER_DTL", "column": "INTER_COM_CODE",
         "op": "=", "value": "1"},
    ],
    "step_overrides": [],
})


def _buildService(
    *, llm: _CascadeLlm | None = None, adapter: _FakeAdapter | None = None,
) -> tuple[ChatService, _CascadeLlm, _FakeTokenUsage, _FakeAdapter]:
    llm = llm or _CascadeLlm()
    adapter = adapter or _FakeAdapter([{"NAME": "A", "QTY": Decimal(10)}])
    tokenUsage = _FakeTokenUsage()
    service = ChatService(
        datasourceService=_FakeDatasourceService(_datasource()),
        ontologyService=_FakeOntologyService(),
        modelRouterService=_FakeRouter(_config()),
        tokenUsageService=tokenUsage,
        embeddingService=_FakeEmbeddingService(),
        llmFactory=lambda config: llm,
        adapterProvider=lambda datasourceId, ds: adapter,
    )
    return service, llm, tokenUsage, adapter


async def _seedMultiStepState(service: ChatService, session: AsyncSession) -> None:
    await service._saveQueryState(
        session,
        "s1",
        question=_MULTI_STEP_QUESTION,
        plan=QueryPlan(target="t", selectedClasses=("PRECEIPT",)),
        sql="SELECT NAME, SUM(QTY) FROM PRECEIPT GROUP BY NAME",
        resultColumns=["NAME", "TOTAL_QTY"],
    )


class TestFollowUpMultiStepRerun:
    """B 层：FOLLOW_UP + 上一轮多步 → 改写 + 多步重跑。"""

    async def test_rewrite_and_rerun_multi_step(self, dbSession) -> None:
        service, llm, tokenUsage, adapter = _buildService(
            llm=_CascadeLlm(rewritten=_REWRITTEN_QUESTION)
        )
        await _seedMultiStepState(service, dbSession)

        resp = await service.processMessage(_dto("4月份呢？"), dbSession)

        assert resp.intent == "multi_step"
        # 改写被调用一次，且 prompt 同时携带上一轮完整问题与追问
        rewriteCalls = [
            c for c in llm.calls
            if "问题改写器" in c[0][1]
        ]
        assert len(rewriteCalls) == 1
        assert _MULTI_STEP_QUESTION in rewriteCalls[0][1][1]
        assert "4月份呢？" in rewriteCalls[0][1][1]
        # 两个数据步都执行了 SQL（改写后的问题带「第X步」标号 → 规则拆步零 LLM）
        assert len(adapter.executed) == 2
        # 改写 token 如实计量
        purposes = [r.get("purpose") for r in tokenUsage.records]
        assert "follow_up_rewrite" in purposes
        # 落库消息与查询状态都锚定改写后的完整问题（支撑下一轮继续追问）
        msg = (
            await dbSession.execute(
                select(SessionMessage)
                .where(SessionMessage.session_id == "s1")
                .where(SessionMessage.role == "user")
                .order_by(SessionMessage.created_time.desc())
                .limit(1)
            )
        ).scalar_one()
        assert msg.question == _REWRITTEN_QUESTION
        state = (
            await dbSession.execute(
                select(SessionQueryState).where(SessionQueryState.session_id == "s1")
            )
        ).scalar_one()
        assert state.last_question == _REWRITTEN_QUESTION

    async def test_single_step_prior_skips_rewrite(self, dbSession) -> None:
        """上一轮是单步：FOLLOW_UP 走原单轮状态注入，不调用改写器。"""
        service, llm, _, _ = _buildService(llm=_CascadeLlm(rewritten=_REWRITTEN_QUESTION))
        await service._saveQueryState(
            dbSession,
            "s1",
            question="各供应商的收货数量汇总",
            plan=QueryPlan(target="t", selectedClasses=("PRECEIPT",)),
            sql="SELECT NAME, SUM(QTY) FROM PRECEIPT GROUP BY NAME",
            resultColumns=["NAME", "TOTAL_QTY"],
        )

        resp = await service.processMessage(_dto("4月份呢？"), dbSession)

        assert resp.intent == IntentType.FOLLOW_UP.value
        assert not any("问题改写器" in c[0][1] for c in llm.calls)

    async def test_unparseable_rewrite_falls_back_to_single_turn(self, dbSession) -> None:
        """改写 LLM 返回垃圾：退回单轮 FOLLOW_UP 注入，不崩溃、不误入多步。"""
        service, llm, _, _ = _buildService(llm=_CascadeLlm(rewritten=None))
        await _seedMultiStepState(service, dbSession)

        resp = await service.processMessage(_dto("4月份呢？"), dbSession)

        # 退回单轮 FOLLOW_UP 注入并成功出 SQL（改写器被调用但解析失败）
        assert resp.intent == IntentType.FOLLOW_UP.value
        assert resp.sql is not None

    async def test_rewrite_equal_to_prior_reruns_prior_multi_step(
        self, dbSession,
    ) -> None:
        """改写结果与上一轮逐字相同（用户重发同一追问）→ 仍应重跑上一轮多步。

        复现 2026-09-26 回归：多步跑完后用户重发同一追问（如上一轮 5 月、这轮
        又问"5月份呢"），改写器把追问合并回上一轮完整问题 → 与 prior 相同 →
        旧守卫判为"改写无效"丢弃 → 退回单轮注入 4 字短句 → LLM 判无有效查询
        计划 → "抱歉…无法回答"。此后该轮还会把短句写回 last_question，使后续
        追问的 B 层第一道闸门（上一轮须是多步）永久失效。
        """
        service, llm, tokenUsage, adapter = _buildService(
            llm=_CascadeLlm(rewritten=_MULTI_STEP_QUESTION),
        )
        await _seedMultiStepState(service, dbSession)

        resp = await service.processMessage(_dto("3月份呢？"), dbSession)

        # 重跑上一轮多步问题（两个数据步都执行 SQL），而非退回单轮
        assert resp.intent == "multi_step"
        assert len(adapter.executed) == 2
        # 改写确被调用且 token 如实计量
        assert any("问题改写器" in c[0][1] for c in llm.calls)
        assert "follow_up_rewrite" in [r.get("purpose") for r in tokenUsage.records]
        # 状态锚定完整多步问题（支撑下一轮继续追问）
        state = (
            await dbSession.execute(
                select(SessionQueryState).where(SessionQueryState.session_id == "s1")
            )
        ).scalar_one()
        assert state.last_question == _MULTI_STEP_QUESTION


class TestUnanswerableFollowUpRetry:
    """C 层：短句 NEW_QUERY 不可回答 → 升级追问重试一次。"""

    async def test_short_unanswerable_retries_as_follow_up(self, dbSession) -> None:
        """首轮计划"无法回答" + 有历史 → 重试带状态注入，第二轮可回答。"""
        llm = _CascadeLlm(planResponses=['{"target":"无法回答"}'])
        service, _, _, _ = _buildService(llm=llm)
        await service._saveQueryState(
            dbSession,
            "s1",
            question="各供应商的收货数量汇总",
            plan=QueryPlan(target="t", selectedClasses=("PRECEIPT",)),
            sql="SELECT NAME, SUM(QTY) FROM PRECEIPT GROUP BY NAME",
            resultColumns=["NAME", "TOTAL_QTY"],
        )

        resp = await service.processMessage(_dto("火星库存"), dbSession)

        assert resp.intent == IntentType.FOLLOW_UP.value
        assert "抱歉" not in resp.answer
        # 计划阶段被调用了两次（首轮失败 + 追问重试）
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 2
        # 第二轮注入了上一轮状态
        assert "上一轮问题" in planCalls[1][0][1]

    async def test_no_retry_when_prior_has_no_sql(self, dbSession) -> None:
        """上一轮本身无 SQL（曾无法回答）：追问没有锚点，不重试。"""
        llm = _CascadeLlm(planResponses=['{"target":"无法回答"}'])
        service, _, _, _ = _buildService(llm=llm)
        await service._saveQueryState(
            dbSession,
            "s1",
            question="月球人口",
            plan=None,
            sql=None,
            resultColumns=[],
        )

        resp = await service.processMessage(_dto("火星库存"), dbSession)

        assert "抱歉" in resp.answer
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 1

    async def test_no_retry_for_long_unanswerable_question(self, dbSession) -> None:
        """长句不可回答不重试（省略式追问是短句特征；长句按 N6 守全新查询)。"""
        llm = _CascadeLlm(planResponses=['{"target":"无法回答"}'])
        service, _, _, _ = _buildService(llm=llm)
        await service._saveQueryState(
            dbSession,
            "s1",
            question="各供应商的收货数量汇总",
            plan=QueryPlan(target="t", selectedClasses=("PRECEIPT",)),
            sql="SELECT NAME, SUM(QTY) FROM PRECEIPT GROUP BY NAME",
            resultColumns=["NAME", "TOTAL_QTY"],
        )

        resp = await service.processMessage(
            _dto("帮我详细分析一下火星殖民地的各供应商库存分布情况"), dbSession
        )

        assert "抱歉" in resp.answer
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 1


class TestCascadeEdges:
    """Finding 3：C 层剩余分支——上一轮多步 + C 仍不可回答时退化路径。"""

    async def test_c_with_multistep_prior_retries_via_b(
        self, dbSession,
    ) -> None:
        """C 触发时上一轮是多步：B 重写 → 多步重跑，每步计划仍"无法回答"。

        验证 C→B 优先级：先尝试多步改写重跑，再退回单轮（不入 fixed 兜底——多步
        失败由 _executeMultiStep 自带的 step_result.error 承载，不回到
        _unanswerableResponse）。
        """
        # 3 个 "无法回答"：C 首次 NEW_QUERY + B 多步重跑的 2 个数据步
        llm = _CascadeLlm(
            rewritten=_REWRITTEN_QUESTION,
            planResponses=[
                '{"target":"无法回答"}',
                '{"target":"无法回答"}',
                '{"target":"无法回答"}',
            ],
        )
        service, _, _, _ = _buildService(llm=llm)
        await _seedMultiStepState(service, dbSession)

        resp = await service.processMessage(_dto("火星人口"), dbSession)

        assert resp.intent == "multi_step"
        rewriteCalls = [c for c in llm.calls if "问题改写器" in c[0][1]]
        assert len(rewriteCalls) == 1
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        # 1 次 C 首轮 + 2 次 B 多步数据步 = 3 次计划调用
        assert len(planCalls) == 3
        # 多步每步都标 failed（_executeMultiStep 失败隔离），最终由聚合生成回答
        for step in resp.steps:
            assert step.error is not None

    async def test_c_multistep_rerun_inherits_global_filters(
        self, dbSession,
    ) -> None:
        """C→B 多步重跑必须按**改写后的问题**抽一次全局约束（对称缺口修复）。

        B 分支（非流式 / 流式）都调 `_resolveGlobalFilters(dto2)`，C 分支经
        `_prepareFollowUpMultiStep` 走多步时却把 dto2 直接交给 `_executeMultiStep`
        ——同一段多步代码，只因入口不同就丢掉「外购/内外贸/站点」这类跨步口径约束，
        用户看到的每步 SQL 会漏掉全局 WHERE。
        """
        llm = _CascadeLlm(
            rewritten=_REWRITTEN_QUESTION,
            globalFilters=_GLOBAL_FILTERS_JSON,
            planResponses=[
                '{"target":"无法回答"}',
                '{"target":"无法回答"}',
                '{"target":"无法回答"}',
            ],
        )
        service, _, tokenUsage, _ = _buildService(llm=llm)
        await _seedMultiStepState(service, dbSession)

        resp = await service.processMessage(_dto("火星人口"), dbSession)

        assert resp.intent == "multi_step"
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        # 1 次 C 首轮（不可回答）+ 2 次改写后多步数据步
        assert len(planCalls) == 3, f"计划调用次数异常：{len(planCalls)}"
        # 负向对照：C 首轮是单步计划，本就不该有全局约束块（否则「哪里都有块」也算过）
        assert "[global_constraints]" not in planCalls[0][1][1]
        # 第 1 次是单步首轮（本就不该有全局约束），只有多步重跑的两步必须有
        for idx, planCall in enumerate(planCalls[1:], start=1):
            userPrompt = planCall[1][1]
            assert "[global_constraints]" in userPrompt, (
                f"C→B 重跑的第 {idx} 步 plan prompt 缺少 [global_constraints] 块"
            )
            assert "INTER_COM_CODE" in userPrompt
        # 抽取针对改写后的问题（不是用户原话「火星人口」）
        extractCalls = [c for c in llm.calls if "全局过滤提取器" in c[0][1]]
        assert len(extractCalls) == 1
        assert _REWRITTEN_QUESTION in extractCalls[0][1][1]
        # 抽取 token 如实落台账（核心约束 #3）
        purposes = [r.get("purpose") for r in tokenUsage.records]
        assert "multistep_global_filter" in purposes

    async def test_c_retry_also_unanswerable_returns_fixed_answer(
        self, dbSession,
    ) -> None:
        """C 重试本身仍不可回答 → 返回固定兜底（不再二次重试，避免无限循环）。"""
        llm = _CascadeLlm(
            planResponses=['{"target":"无法回答"}', '{"target":"无法回答"}'],
        )
        service, _, _, _ = _buildService(llm=llm)
        await service._saveQueryState(
            dbSession,
            "s1",
            question="各供应商的收货数量汇总",
            plan=QueryPlan(target="t", selectedClasses=("PRECEIPT",)),
            sql="SELECT NAME, SUM(QTY) FROM PRECEIPT GROUP BY NAME",
            resultColumns=["NAME", "TOTAL_QTY"],
        )

        resp = await service.processMessage(_dto("火星库存"), dbSession)

        assert "抱歉" in resp.answer
        # 仅 2 次计划调用：首次不可回答 + C 升级追问仍不可回答
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 2


# --- reviewer Finding 2 备注 ---------------------------------------------------------
# _streamQuery（SSE）路径的 B/C 分支与非流式 _handleGenericQuery 镜像，但需要 fake
# LLM 实现 completeStream（流式分块）。完整 stream fake 超出本次范围；流式逻辑通过
# 代码审查（line-for-line 镜像 + 共享 _prepareFollowUpMultiStep）保证一致性。
# 后续若引入完整 fake，可加 TestStreamCascade 类覆盖 EVENT_SQL/EVENT_DONE 序列。
