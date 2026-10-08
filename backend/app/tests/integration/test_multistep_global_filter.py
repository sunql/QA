"""多步全局过滤继承集成测试（feat-multistep-global-filter，A+B 落地）。"""
from __future__ import annotations
import json
from decimal import Decimal
from app.domain.enums import DataSourceType
from app.domain.exceptions import NotFoundError
from app.domain.models import DataSource, LlmConfig, OntologyClass
from app.domain.query_plan import QueryPlan
from app.domain.schemas import ChatRequest
from app.services.chat_service import ChatService
from app.services.stream_events import EVENT_DONE, EVENT_ERROR


_MULTI_STEP_QUESTION = (
    "第一步，查询3月份供货量最多的三家供应商（外购+内贸+境内）；"
    "第二步，看一下这三家供应商各自供货量前三的物料分别是什么；"
    "第三步，分析一下这三家供货商最少的物料是什么"
)

_GLOBAL_FILTERS_JSON = json.dumps({
    "global": [
        {"table": "DWD_PURCHASE_ORDER_DTL", "column": "TCLCOD_0",
         "op": "IN", "value": "A02,A03,A04,A05"},
        {"table": "DWD_PURCHASE_ORDER_DTL", "column": "INTER_COM_CODE",
         "op": "=", "value": "1"},
        {"table": "DWD_PURCHASE_ORDER_DTL", "column": "INTER_SITE_CODE",
         "op": "=", "value": "1"},
    ],
    "step_overrides": [],
})


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _GlobalFilterLlm:
    def __init__(self) -> None:
        self.calls: list = []
        self.sqlByStep: list[str] = [
            "SELECT SUPPLIER_CODE, SUM(QTY) FROM ZJTH.DWD_PURCHASE_ORDER_DTL "
            "WHERE TCLCOD_0 IN ('A02','A03','A04','A05') "
            "AND INTER_COM_CODE='1' AND INTER_SITE_CODE='1' "
            "GROUP BY SUPPLIER_CODE",
            "SELECT SUPPLIER_CODE, MATERIAL_CODE, SUM(QTY) "
            "FROM ZJTH.DWD_PURCHASE_ORDER_DTL GROUP BY SUPPLIER_CODE, MATERIAL_CODE",
            "SELECT SUPPLIER_CODE, MATERIAL_CODE, SUM(QTY) "
            "FROM ZJTH.DWD_PURCHASE_ORDER_DTL GROUP BY SUPPLIER_CODE, MATERIAL_CODE",
        ]

    async def complete(self, messages, **kwargs):
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        if "全局过滤提取器" in system:
            return _Resp(_GLOBAL_FILTERS_JSON)
        if "问题改写器" in system:
            # C 层追问改写：把「4月份」合并进上一轮完整两步骤问题（保留「第X步」
            # 标号 → 后续 rule_based_split 零 LLM 命中，与真实链路同构）。
            return _Resp(json.dumps({
                "question": (
                    "第一步，查询4月份供货量最多的三家供应商（外购+内贸+境内）；"
                    "第二步，查询这三家供应商各自供货量前三的物料"
                ),
            }, ensure_ascii=False))
        if "查询拆分器" in system:
            return _Resp(json.dumps({
                "isMultiStep": True,
                "steps": [
                    {"description": "Top 3 供应商", "subQuestion": "查询3月供货量最多的三家供应商"},
                    {"description": "Top 3 物料", "subQuestion": "查询这三家供应商各自供货量前三的物料"},
                    {"description": "最少物料", "subQuestion": "分析这三家供应商最少的物料"},
                ],
                "aggregationHint": "汇总对比",
            }))
        if "解析为查询计划" in system:
            # 计划必须「有可查询引用」才算有效（_isEmptyPlan 方案B）：仅 target 的
            # 空计划会被判 PLAN_EMPTY 并重试到耗尽。这里选中 fake 本体里的
            # DWD_PURCHASE_ORDER_DTL（与 sqlByStep 实际查的表一致）。
            return _Resp('{"target":"采购订单明细",'
                         '"selectedClasses":["DWD_PURCHASE_ORDER_DTL"],'
                         '"selectedProperties":[]}')
        if "生成 SQL 时必须" in system:
            sql_idx = sum(1 for c in self.calls if "生成 SQL 时必须" in c[0][1]) - 1
            sql = self.sqlByStep[min(sql_idx, len(self.sqlByStep) - 1)]
            return _Resp(f"```sql\n{sql}\n```")
        return _Resp("查询完成。")


class _CFallbackLlm(_GlobalFilterLlm):
    """C 兜底链路：首次计划「无法回答」（触发升级追问重试），其余同基类。"""

    def __init__(self) -> None:
        super().__init__()
        self._firstPlanUnanswerable = True

    async def complete(self, messages, **kwargs):
        if "解析为查询计划" in messages[0].content and self._firstPlanUnanswerable:
            self._firstPlanUnanswerable = False
            self.calls.append([(m.role, m.content) for m in messages])
            return _Resp(
                '{"target":"无法回答","selectedClasses":[],"selectedProperties":[]}'
            )
        return await super().complete(messages, **kwargs)


class _FakeDatasourceService:
    def __init__(self, ds: DataSource) -> None:
        self._ds = ds

    async def get(self, session, datasourceId: int) -> DataSource:
        if datasourceId != self._ds.id:
            raise NotFoundError(f"数据源 {datasourceId} 不存在")
        return self._ds


def _purchaseOrderClass() -> OntologyClass:
    """fake 本体类：与 sqlByStep 里实际查的 ZJTH.DWD_PURCHASE_ORDER_DTL 同名。

    瞬态（transient）ORM 实例访问 ``.properties`` 返回空列表、不触发懒加载，
    因此本类可用但无属性 —— 与 fixture 计划 selectedProperties=[] 自洽。
    """
    return OntologyClass(
        id=1, class_name="DWD_PURCHASE_ORDER_DTL", class_alias="采购订单明细",
        source_table="DWD_PURCHASE_ORDER_DTL", version=1,
    )


class _FakeOntologyService:
    def __init__(self, classes=None) -> None:
        self._classes = classes or []

    async def listClasses(self, session):
        return self._classes

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None):
        return []

    async def listMetrics(self, session):
        return []

    async def listJoins(self, session):
        return []


class _FakeAdapter:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[str] = []

    async def execute_read_only(self, sql: str):
        self.executed.append(sql)
        return self._rows


class _FakeRouter:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx):
        return self._config

    def selectFallbackModel(self, configs, excludeId):
        return None


class _FakeEmbeddingService:
    async def storeQueryEmbedding(self, **kwargs):
        return None

    async def searchSimilarQueries(self, question, **kwargs):
        return []


class _FakeTokenUsage:
    def __init__(self) -> None:
        self.records: list[dict] = []

    async def getSessionCost(self, session, sessionId):
        return Decimal("0")

    async def getSessionTurnCount(self, session, sessionId):
        return 0

    async def getLastModelId(self, session, sessionId):
        return None

    async def recordUsage(self, session, **kwargs):
        self.records.append(kwargs)


def _config():
    return LlmConfig(id=1, model_name="test-model", provider="openai",
                     cost_per_1k_input=Decimal("0.001"), cost_per_1k_output=Decimal("0.002"))


def _datasource():
    return DataSource(id=1, name="ZJTH", type=DataSourceType.ORACLE,
                      host="h", port=1521, database_name="svc",
                      username="u", password_encrypted="cipher")


def _dto(question: str):
    return ChatRequest(sessionId="s1", question=question, datasourceId=1)


def _build_service(llm):
    adapter = _FakeAdapter([{"SUPPLIER_CODE": "S0", "QTY": Decimal(100)}])
    tokenUsage = _FakeTokenUsage()
    service = ChatService(
        datasourceService=_FakeDatasourceService(_datasource()),
        ontologyService=_FakeOntologyService(classes=[_purchaseOrderClass()]),
        modelRouterService=_FakeRouter(_config()),
        tokenUsageService=tokenUsage,
        embeddingService=_FakeEmbeddingService(),
        llmFactory=lambda config: llm,
        adapterProvider=lambda datasourceId, ds: adapter,
    )
    return service, llm, tokenUsage, adapter


class TestMultistepGlobalFilterInheritance:
    async def test_all_steps_include_global_filter_in_plan_prompt(self, dbSession):
        llm = _GlobalFilterLlm()
        service, _, _, _ = _build_service(llm)
        await service.processMessage(_dto(_MULTI_STEP_QUESTION), dbSession)
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 3, f"应有 3 次 plan 调用，实际 {len(planCalls)}"
        for idx, planCall in enumerate(planCalls):
            user_prompt = planCall[1][1]
            assert "[global_constraints]" in user_prompt, (
                f"步骤 {idx + 1} 的 plan prompt 缺少 [global_constraints] 块"
            )
            assert "TCLCOD_0" in user_prompt
            assert "INTER_COM_CODE" in user_prompt

    async def test_extract_global_filters_called_once(self, dbSession):
        llm = _GlobalFilterLlm()
        service, _, _, _ = _build_service(llm)
        await service.processMessage(_dto(_MULTI_STEP_QUESTION), dbSession)
        extractCalls = [c for c in llm.calls if "全局过滤提取器" in c[0][1]]
        assert len(extractCalls) == 1

    async def test_audit_records_global_filter_purpose(self, dbSession):
        llm = _GlobalFilterLlm()
        service, _, tokenUsage, _ = _build_service(llm)
        await service.processMessage(_dto(_MULTI_STEP_QUESTION), dbSession)
        purposes = [r.get("purpose") for r in tokenUsage.records]
        assert "multistep_global_filter" in purposes

    async def test_global_filter_audit_row_carries_real_tokens(self, dbSession):
        """H1：抽取调用了 LLM，台账必须记真实 tokens —— 不是 0/0 占位。

        此前 `_resolveGlobalFilters` 写 `_recordUsage(..., 0, 0, ...)` 并注释谎称
        「token 已计到 plan/split 路径」；实际 `extract_global_filters` 把
        `resp.promptTokens/completionTokens` 直接丢弃，这笔消耗在会计上凭空消失。
        """
        llm = _GlobalFilterLlm()
        service, _, tokenUsage, _ = _build_service(llm)
        await service.processMessage(_dto(_MULTI_STEP_QUESTION), dbSession)

        row = next(
            r for r in tokenUsage.records
            if r.get("purpose") == "multistep_global_filter"
        )
        # _GlobalFilterLlm 的 _Resp 固定返回 promptTokens=10 / completionTokens=5
        assert (row["promptTokens"], row["completionTokens"]) == (10, 5)


class TestMultistepGlobalFilterStreaming:
    """流式路径补齐（C1/C2）：流式多步必须与非流式同口径注入全局约束。"""

    async def test_stream_explicit_multi_step_injects_global_constraints(
        self, dbSession,
    ):
        """流式显式多步：每步 plan prompt 必须含 [global_constraints]（C2）。"""
        llm = _GlobalFilterLlm()
        service, _, _, _ = _build_service(llm)

        events = [
            ev async for ev in service.processMessageStream(
                _dto(_MULTI_STEP_QUESTION), dbSession,
            )
        ]

        assert any(ev.event == EVENT_DONE for ev in events)
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 3, f"应有 3 次 plan 调用，实际 {len(planCalls)}"
        for idx, planCall in enumerate(planCalls):
            user_prompt = planCall[1][1]
            assert "[global_constraints]" in user_prompt, (
                f"流式步骤 {idx + 1} 的 plan prompt 缺少 [global_constraints] 块"
            )
            assert "TCLCOD_0" in user_prompt

    async def test_stream_explicit_multi_step_calls_extractor_once(self, dbSession):
        """流式路径必须真正调用全局过滤提取器（只一次）。"""
        llm = _GlobalFilterLlm()
        service, _, tokenUsage, _ = _build_service(llm)

        [ev async for ev in service.processMessageStream(
            _dto(_MULTI_STEP_QUESTION), dbSession,
        )]

        extractCalls = [c for c in llm.calls if "全局过滤提取器" in c[0][1]]
        assert len(extractCalls) == 1
        purposes = [r.get("purpose") for r in tokenUsage.records]
        assert "multistep_global_filter" in purposes

    async def test_stream_follow_up_multistep_no_type_error(self, dbSession):
        """C1 回归：流式「追问上一轮多步」曾因 _streamMultiStep 无
        global_filters 形参而抛 TypeError，被外层吞成通用 internal error。
        """
        llm = _GlobalFilterLlm()
        service, _, _, _ = _build_service(llm)
        await service._saveQueryState(
            dbSession, "s1",
            question=_MULTI_STEP_QUESTION,
            plan=QueryPlan(target="t"),
            sql="SELECT 1 FROM DUAL",
            resultColumns=["C"],
        )

        events = [
            ev async for ev in service.processMessageStream(
                _dto("4月份呢？"), dbSession,
            )
        ]

        assert not [ev for ev in events if ev.event == EVENT_ERROR], (
            "流式追问多步不应产生 error 事件（TypeError 回归）"
        )
        assert any(ev.event == EVENT_DONE for ev in events)


class TestMultistepGlobalFilterCascadeFallback:
    """C 兜底分支（_isFollowUpRetryCandidate 命中 → B 多步重跑）的全局约束对称性。"""

    async def test_c_fallback_multistep_inherits_global_constraints(
        self, dbSession,
    ):
        """非流式 C 兜底：多步重跑的每步 plan prompt 必须含 [global_constraints]。"""
        llm = _CFallbackLlm()
        service, _, tokenUsage, _ = _build_service(llm)
        await service._saveQueryState(
            dbSession, "s1",
            question=_MULTI_STEP_QUESTION,
            plan=QueryPlan(target="t"),
            sql="SELECT 1 FROM DUAL",
            resultColumns=["C"],
        )

        resp = await service.processMessage(_dto("火星人口"), dbSession)

        assert resp.intent == "multi_step"
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        # 1 次 C 首轮（不可回答）+ 2 次改写后多步数据步
        assert len(planCalls) == 3, f"计划调用次数异常：{len(planCalls)}"
        # 负向对照：C 首轮是单步计划，本就不该有全局约束块
        assert "[global_constraints]" not in planCalls[0][1][1]
        for idx, planCall in enumerate(planCalls[1:], start=1):
            assert "[global_constraints]" in planCall[1][1], (
                f"C 兜底重跑的第 {idx} 步 plan prompt 缺少 [global_constraints] 块"
            )
            assert "TCLCOD_0" in planCall[1][1]
        purposes = [r.get("purpose") for r in tokenUsage.records]
        assert "multistep_global_filter" in purposes

    async def test_stream_c_fallback_multistep_inherits_global_constraints(
        self, dbSession,
    ):
        """流式 C 兜底：与非流式同口径注入（对称缺口的两侧各一例）。"""
        llm = _CFallbackLlm()
        service, _, tokenUsage, _ = _build_service(llm)
        await service._saveQueryState(
            dbSession, "s1",
            question=_MULTI_STEP_QUESTION,
            plan=QueryPlan(target="t"),
            sql="SELECT 1 FROM DUAL",
            resultColumns=["C"],
        )

        events = [
            ev async for ev in service.processMessageStream(
                _dto("火星人口"), dbSession,
            )
        ]

        assert not [ev for ev in events if ev.event == EVENT_ERROR], (
            "流式 C 兜底多步不应产生 error 事件"
        )
        assert any(ev.event == EVENT_DONE for ev in events)
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        assert len(planCalls) == 3, f"计划调用次数异常：{len(planCalls)}"
        # 负向对照：C 首轮是单步计划，本就不该有全局约束块
        assert "[global_constraints]" not in planCalls[0][1][1]
        for idx, planCall in enumerate(planCalls[1:], start=1):
            assert "[global_constraints]" in planCall[1][1], (
                f"流式 C 兜底重跑的第 {idx} 步 plan prompt 缺少 [global_constraints] 块"
            )
            assert "TCLCOD_0" in planCall[1][1]
        purposes = [r.get("purpose") for r in tokenUsage.records]
        assert "multistep_global_filter" in purposes


class TestMultistepGlobalFilterFailure:
    async def test_extract_failure_falls_back_to_no_injection(self, dbSession):
        class _BoomLlm(_GlobalFilterLlm):
            async def complete(self, messages, **kwargs):
                if "全局过滤提取器" in messages[0].content:
                    raise RuntimeError("LLM 不可用")
                return await super().complete(messages, **kwargs)

        llm = _BoomLlm()
        service, _, _, _ = _build_service(llm)
        resp = await service.processMessage(_dto(_MULTI_STEP_QUESTION), dbSession)
        assert resp.intent == "multi_step"
        planCalls = [c for c in llm.calls if "解析为查询计划" in c[0][1]]
        for planCall in planCalls:
            assert "[global_constraints]" not in planCall[1][1]
