"""ChatService 编排单元测试（纯逻辑，无 IO）。

mock 全部依赖，验证：
- 闲聊快捷路径（不调 LLM、不记录 Token）
- 完整流水线（NL2SQL → 执行 → 图表 → 回答 → 记录 3 次 usage）
- 数据源不存在抛 NotFoundError
- NL2SQL 重试耗尽抛 Nl2SqlError
触 DB 的会话状态/上下文/查询状态测试已迁至
integration/test_chat_service_state.py（【迁移：真实 PG】第三批）。
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.domain.enums import ChartType, DataSourceType, IntentType
from app.domain.exceptions import LlmClientError, Nl2SqlError, NotFoundError
from app.domain.models import (
    DataSource,
    LlmConfig,
    OntologyClass,
    OntologyMetric,
    OntologyProperty,
)
from app.domain.query_plan import PlanResult, QueryPlan
from app.domain.schemas import (
    ChatRequest,
    HistoryMessage,
    OntologyMetricCreate,
    OntologyPropertyUpdate,
)
from app.services.chat_service import ChatService
from app.services.unanswerable_suggestion import _buildUnanswerableSuggestion
from app.services.nl2sql_service import SqlResult
from app.services.step_query_planner import MultiStepPlan, StepPlan, StepQueryPlanner
from app.services.value_sampler import clearValueSampleCache


@pytest.fixture(autouse=True)
def _isolate_value_sample_cache():
    """值域采样模块缓存按测试隔离，避免跨测试/跨文件命中导致断言失真（2-1）。"""
    clearValueSampleCache()
    yield
    clearValueSampleCache()


# 测试用查询结果（与 test_chat_service_stream.py 同构，供本文件的 affinity 测试使用）
_ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _PipelineLlm:
    """按 prompt 内容路由回复的假客户端：计划 JSON / SQL / 图表 JSON / 回答。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content
        if "图表类型" in user:
            content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            # ReAct 第一阶段：返回一个能通过校验的空计划（classes 为空时无引用可校验）
            content = '{"target":"各供应商的收货数量汇总"}'
        elif "生成 SQL 时必须" in system:
            content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            content = "查询完成，共 2 条记录，各供应商收货量分布如下。"
        return _Resp(content)


class _FakeSession:
    """最小假会话：查询返回空；add_all/add/commit/refresh 为 no-op 并记录。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, stmt):
        class _Scalars:
            def all(self) -> list:
                return []

        class _Result:
            def scalars(self):
                return _Scalars()

            def scalar_one_or_none(self):
                return None

            def all(self) -> list:
                return []

        return _Result()

    def add_all(self, entities: list[object]) -> None:
        self.added.extend(entities)

    def add(self, entity: object) -> None:
        self.added.append(entity)

    async def commit(self) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def refresh(self, entity: object) -> None:
        pass


class _FakeDatasourceService:
    def __init__(self, ds: DataSource) -> None:
        self._ds = ds

    async def get(self, session, datasourceId: int) -> DataSource:
        if datasourceId != self._ds.id:
            raise NotFoundError(f"数据源 {datasourceId} 不存在")
        return self._ds


class _FakeOntologyService:
    """假本体服务：除 listClasses 外，支持领域命令（METRIC/DEFINE/MAP）所需方法并记录调用。"""

    def __init__(
        self,
        classes: list[OntologyClass] | None = None,
        metrics: list[OntologyMetric] | None = None,
        searchHits: list | None = None,
    ) -> None:
        self._classes = classes or []
        self._metrics = metrics or []
        self.searchHits = searchHits or []
        self.createdMetrics: list[OntologyMetricCreate] = []
        self.createdClasses: list = []
        self.updatedProperties: list[tuple[int, OntologyPropertyUpdate]] = []

    async def listClasses(self, session) -> list[OntologyClass]:
        return self._classes

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
        # 1-1：默认为空召回（触发回退全量）；测试可注入 searchHits 验证裁剪
        return self.searchHits

    async def listMetrics(self, session) -> list[OntologyMetric]:
        return self._metrics

    async def listJoins(self, session) -> list:
        # join 目录（运行时 JOIN 唯一真源）；单测默认无 join 边
        return []

    async def createMetric(self, session, dto: OntologyMetricCreate) -> OntologyMetric:
        self.createdMetrics.append(dto)
        return OntologyMetric(
            id=99, metric_name=dto.metric_name, formula=dto.formula, agg_function=dto.agg_function
        )

    async def createClass(self, session, dto) -> OntologyClass:
        self.createdClasses.append(dto)
        return OntologyClass(
            id=98,
            class_name=dto.class_name,
            class_alias=dto.class_alias,
            description=dto.description,
        )

    async def updateProperty(self, session, id: int, dto: OntologyPropertyUpdate) -> OntologyProperty:
        self.updatedProperties.append((id, dto))
        return OntologyProperty(id=id, class_id=1, property_name="x", data_type="STRING")


class _FakeAdapter:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.executedSql: str | None = None

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executedSql = sql
        return self._rows


class _FlakyAdapter(_FakeAdapter):
    """前 failTimes 次执行抛异常，之后成功；记录每次执行（1-3 重试测试用）。"""

    def __init__(self, rows: list[dict], failTimes: int = 1) -> None:
        super().__init__(rows)
        self._failTimes = failTimes
        self.executedSqls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executedSqls.append(sql)
        if self._failTimes > 0:
            self._failTimes -= 1
            raise RuntimeError("ORA-00942: 表或视图不存在")
        return self._rows


class _RecordingAdapter(_FakeAdapter):
    """记录全部 execute_read_only 调用（值域采样 DISTINCT + 真实查询，2-1 用）。"""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__(rows)
        self.sqls: list[str] = []

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.sqls.append(sql)
        return await super().execute_read_only(sql)


class _SamplingFailureAdapter(_FakeAdapter):
    """采样 SQL（含 DISTINCT）抛错、真实查询正常：验证值域采样整体降级（2-1）。"""

    async def execute_read_only(self, sql: str) -> list[dict]:
        if "DISTINCT" in sql:
            raise RuntimeError("业务库不可达")
        return await super().execute_read_only(sql)


class _FakeRouter:
    def __init__(self, config: LlmConfig, fallback: LlmConfig | None = None) -> None:
        self._config = config
        self._fallback = fallback
        self.calls: list[object] = []

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        self.calls.append(ctx)
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return self._fallback


class _UnanswerablePipelineLlm(_PipelineLlm):
    """ReAct 第一阶段返回 target=无法回答 的空计划（模型判定问题超出本体范围）。

    若 SQL 生成被调用，system 含"生成 SQL 时必须"，走 super().complete 追加一次调用，
    因此 len(calls)==1 即证明 SQL 生成被跳过。failForModel 使主模型在计划阶段即失败
    （验证降级后由 fallback 服务的模型名上报）。
    """

    def __init__(self, failForModel: str | None = None) -> None:
        super().__init__()
        self._failForModel = failForModel

    async def complete(self, messages, **kwargs) -> _Resp:
        if self._failForModel is not None and kwargs.get("model") == self._failForModel:
            raise LlmClientError(f"模型 {self._failForModel} 服务不可用")
        system = messages[0].content
        if "解析为查询计划" in system:
            self.calls.append([(m.role, m.content) for m in messages])
            return _Resp('{"target":"无法回答"}')
        return await super().complete(messages, **kwargs)


class _FlakyLlm(_PipelineLlm):
    """指定模型名调用时抛 LlmClientError（模拟主模型不可用），其余正常回复。"""

    def __init__(self, failForModel: str) -> None:
        super().__init__()
        self._failForModel = failForModel

    async def complete(self, messages, **kwargs):
        if kwargs.get("model") == self._failForModel:
            raise LlmClientError(f"模型 {self._failForModel} 服务不可用")
        return await super().complete(messages, **kwargs)


class _FakeEmbeddingService:
    """假 embedding 服务：记录 storeQueryEmbedding 调用，可注入 searchSimilarQueries 命中（1-2）。"""

    def __init__(self, similarQueries: list | None = None, raiseOnSearch: bool = False) -> None:
        self.stored: list[dict] = []
        self.similarQueries = similarQueries or []
        self.raiseOnSearch = raiseOnSearch
        self.searchCalls: list[tuple[str, dict]] = []

    async def storeQueryEmbedding(self, **kwargs) -> None:
        self.stored.append(kwargs)

    async def searchSimilarQueries(self, question: str, **kwargs) -> list:
        self.searchCalls.append((question, kwargs))
        if self.raiseOnSearch:
            raise RuntimeError("Milvus 不可用")
        return self.similarQueries


class _FakeTokenUsage:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.cost = Decimal("0.001")
        self.turnCount = 2
        self.lastModelId = 1

    async def getSessionCost(self, session, sessionId: str) -> Decimal:
        return self.cost

    async def getSessionTurnCount(self, session, sessionId: str) -> int:
        return self.turnCount

    async def getLastModelId(self, session, sessionId: str) -> int | None:
        return self.lastModelId

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


def _buildService(
    *,
    datasource: DataSource | None = None,
    classes: list[OntologyClass] | None = None,
    rows: list[dict] | None = None,
    router: _FakeRouter | None = None,
    llm: _PipelineLlm | None = None,
    ontology: _FakeOntologyService | None = None,
    adapter: _FakeAdapter | None = None,
    embedding: _FakeEmbeddingService | None = None,
) -> tuple[ChatService, _PipelineLlm, _FakeTokenUsage, _FakeAdapter]:
    llm = llm or _PipelineLlm()
    datasourceService = _FakeDatasourceService(datasource or _datasource())
    ontologyService = ontology or _FakeOntologyService(classes or [])
    adapter = adapter or _FakeAdapter(rows or [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}])
    tokenUsage = _FakeTokenUsage()
    service = ChatService(
        datasourceService=datasourceService,
        ontologyService=ontologyService,
        modelRouterService=router or _FakeRouter(_config()),
        tokenUsageService=tokenUsage,
        embeddingService=embedding or _FakeEmbeddingService(),
        llmFactory=lambda config: llm,
        adapterProvider=lambda datasourceId, ds: adapter,
    )
    return service, llm, tokenUsage, adapter


class TestChatService:
    async def test_chitchat_short_circuits_without_llm(self) -> None:
        service, llm, tokenUsage, _ = _buildService()
        session = _FakeSession()
        response = await service.processMessage(_dto("你好"), session)
        assert response.intent == IntentType.CHITCHAT.value
        assert "智能问答助手" in response.answer
        assert response.sql is None
        assert response.tokensUsed == 0
        assert response.modelName is None  # 闲聊未调用任何模型
        assert llm.calls == []
        assert tokenUsage.records == []
        # 4-4：闲聊轮也持久化消息（user + assistant），历史链不断
        assert len(session.added) == 2

    async def test_full_pipeline_returns_complete_response(self) -> None:
        service, llm, tokenUsage, adapter = _buildService()
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert response.intent == IntentType.QUERY.value
        assert "PRECEIPT" in (response.sql or "")
        assert response.chartType == "pie"  # 1 字符串 + 1 数值、2 行 ≤ 6 → PIE
        assert response.chartOption is not None
        assert response.data == [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]
        assert response.tokensUsed == 60  # 4 次调用 × 15（计划/校验 + SQL + 图表 + 回答）
        assert response.cost > 0
        assert response.modelName == "test-model"  # 实际服务的回答模型
        # ReAct 计划随响应返回（前端展示用）
        assert response.queryPlan is not None
        assert response.queryPlan["target"] == "各供应商的收货数量汇总"
        # 流水线顺序：计划 → SQL → 图表 → 回答
        assert len(llm.calls) == 4
        assert "解析为查询计划" in llm.calls[0][0][1]
        assert "生成 SQL 时必须" in llm.calls[1][0][1]
        assert adapter.executedSql is not None
        assert "GROUP BY NAME" in adapter.executedSql

    async def test_records_three_usages_with_purposes(self) -> None:
        service, _, tokenUsage, _ = _buildService()
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert [r["purpose"] for r in tokenUsage.records] == ["nl2sql", "chart", "answer"]
        assert all(r["modelConfigId"] == 1 for r in tokenUsage.records)
        assert all(r["modelName"] == "test-model" for r in tokenUsage.records)

    async def test_nl2sql_prompt_uses_datasource_dialect_and_username(self) -> None:
        """方言 + schema 前缀来自数据源（ORACLE / username=u）。"""
        service, llm, _, _ = _buildService()
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        # calls[1] 为第二阶段 SQL system prompt（calls[0] 为计划 prompt）
        system = llm.calls[1][0][1]
        assert "Oracle 数据库" in system
        assert "FETCH FIRST N ROWS ONLY" in system
        assert "u.表名" in system
        assert "FROM u.PRECEIPTD d JOIN u.PRECEIPT h" in system

    async def test_nl2sql_prompt_uses_mysql_dialect(self) -> None:
        mysqlDs = DataSource(
            id=1,
            name="mysql",
            type=DataSourceType.MYSQL,
            host="h",
            port=3306,
            database_name="db",
            username="root",
            password_encrypted="cipher",
        )
        service, llm, _, _ = _buildService(datasource=mysqlDs)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        system = llm.calls[1][0][1]
        assert "MySQL 数据库" in system
        assert "LIMIT" in system
        assert "FETCH FIRST N ROWS ONLY" not in system
        # MySQL 无 schema 前缀惯例：不注入 prefix 提示，也不限定表名
        assert "表名使用 schema 前缀" not in system
        assert "root." not in system

    async def test_nl2sql_prompt_uses_postgresql_dialect(self) -> None:
        pgDs = DataSource(
            id=1,
            name="pg",
            type=DataSourceType.POSTGRESQL,
            host="h",
            port=5432,
            database_name="db",
            username="postgres",
            password_encrypted="cipher",
        )
        service, llm, _, _ = _buildService(datasource=pgDs)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        system = llm.calls[1][0][1]
        assert "PostgreSQL 数据库" in system
        assert "LIMIT" in system
        assert "FETCH FIRST N ROWS ONLY" not in system
        assert "表名使用 schema 前缀" not in system

    async def test_datasource_not_found_raises(self) -> None:
        service, _, _, _ = _buildService()
        with pytest.raises(NotFoundError):
            await service.processMessage(_dto("各供应商的收货数量汇总", datasourceId=99), _FakeSession())

    # ---- 范围感知行数限制：scopeQuestion 透传（cases 22-23）----

    async def test_single_step_omits_scope_question(self) -> None:
        """23：单步场景下 scopeQuestion 应为 None（避免误把"上一轮历史"当成主问题范围）。"""
        service, _, _, _ = _buildService()

        captured: list[dict] = []

        class _CapturingNl2sql:
            async def generateValidatedPlan(self, question, classes, llmClient, modelConfig, **kwargs):
                captured.append({"stage": "plan", "question": question, **kwargs})
                return PlanResult(plan=QueryPlan(target="t"), promptTokens=6, completionTokens=4)

            async def generateSql(self, *args, **kwargs):
                class _R:
                    sql = "SELECT 1 FROM DUAL"
                    promptTokens = 3
                    completionTokens = 2
                return _R()

        service._nl2sql = _CapturingNl2sql()
        await service.processMessage(_dto("列出所有收货记录"), _FakeSession())
        assert len(captured) == 1
        assert captured[0]["scopeQuestion"] is None

    async def test_multi_step_passes_original_question_as_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """22：多步场景下 scopeQuestion 应透传主问题（主问题∪子问题做范围判定）。

        使用 monkeypatch.setattr（pytest 自动 teardown）保证 step_query_planner
        的 staticmethod 在测试结束后恢复原状——直接赋值 staticmethod(_fakeSplit)
        在 finally 还原时极易因静态/实例方法边界出错而污染其他测试。
        """
        service, _, _, _ = _buildService()

        # 多步路径会触发 _buildDriftWarning → schema introspection；本测试只关心 plan 透传，
        # 用 no-op stub 避免触达真实数据库。
        from app.domain.schemas import OntologyDriftReport

        class _NoDriftIntrospection:
            async def validateOntologyDrift(self, session, ds, classes):
                return OntologyDriftReport(
                    datasourceId=ds.id,
                    hasDrift=False,
                    schemaCached=False,
                    checkedTables=0,
                    missingTables=[],
                    missingColumns=[],
                )

        service._schemaIntrospection = _NoDriftIntrospection()

        captured: list[dict] = []

        class _CapturingNl2sql:
            async def generateValidatedPlan(self, question, classes, llmClient, modelConfig, **kwargs):
                captured.append({"stage": "plan", "question": question, **kwargs})
                # 多步每个子问题会调一次 generateValidatedPlan，scopeQuestion 都应是 dto.question
                return PlanResult(plan=QueryPlan(target="t"), promptTokens=6, completionTokens=4)

            async def generateSql(self, *args, **kwargs):
                class _R:
                    sql = "SELECT 1 FROM DUAL"
                    promptTokens = 3
                    completionTokens = 2
                return _R()

        service._nl2sql = _CapturingNl2sql()

        def _fakeSplit(question: str):
            return MultiStepPlan(
                steps=(
                    StepPlan(index=0, description="第一步", sub_question="查各供应商采购额"),
                    StepPlan(index=1, description="汇总", sub_question="汇总", aggregation_only=True),
                ),
                aggregation_hint="汇总",
                original_question=question,
            )

        # monkeypatch 自动 teardown：恢复原 staticmethod（不再漏到其他测试）
        monkeypatch.setattr(StepQueryPlanner, "rule_based_split", staticmethod(_fakeSplit))
        await service.processMessage(_dto("2025年的采购情况：第一步查各供应商采购额"), _FakeSession())

        assert captured, "expected at least one plan call"
        # 子问题的 question 是 _fakeSplit 的产物，主问题（含年份）应作为 scopeQuestion 传入
        assert all(c["scopeQuestion"] == "2025年的采购情况：第一步查各供应商采购额" for c in captured)

    async def test_nl2sql_exhaustion_raises(self) -> None:
        service, _, _, _ = _buildService()

        class _BrokenNl2sql:
            async def generateValidatedPlan(self, question, classes, llmClient, modelConfig, **kwargs):
                raise Nl2SqlError("无法生成有效的查询计划")

            async def generateSql(self, question, classes, llmClient, modelConfig, **kwargs):
                raise Nl2SqlError("无法生成有效的查询 SQL")

        service._nl2sql = _BrokenNl2sql()
        with pytest.raises(Nl2SqlError):
            await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())

    async def test_routing_context_includes_session_metrics(self) -> None:
        service, _, _, _ = _buildService()
        router = service._modelRouter
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert len(router.calls) == 1
        ctx = router.calls[0]
        assert ctx.sessionId == "s1"
        assert ctx.sessionCost == 0.001
        assert ctx.sessionTurnCount == 2
        assert ctx.priorModelId == 1

    async def test_stores_query_embedding_after_nl2sql(self) -> None:
        service, _, _, _ = _buildService()
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        await asyncio.sleep(0)  # 向量存储为 fire-and-forget：让后台任务有机会完成
        assert len(service._embedding.stored) == 1
        record = service._embedding.stored[0]
        assert record["sessionId"] == "s1"
        assert record["question"] == "各供应商的收货数量汇总"
        assert record["sql"] is not None
        assert record["datasourceId"] == 1

    async def test_chitchat_does_not_store_embedding(self) -> None:
        service, _, _, _ = _buildService()
        await service.processMessage(_dto("你好"), _FakeSession())
        assert service._embedding.stored == []

    async def test_clarify_answers_without_sql(self) -> None:
        """CLARIFY：直接 LLM 解释概念，不执行 SQL、不生成图表、不更新查询状态。"""
        service, llm, tokenUsage, adapter = _buildService()
        response = await service.processMessage(_dto("什么是收货数量"), _FakeSession())
        assert response.intent == IntentType.CLARIFY.value
        assert response.sql is None
        assert response.data is None
        assert response.queryPlan is None
        assert response.chartOption is None
        # 仅一次 LLM 调用（clarify），未走查询流水线
        assert len(llm.calls) == 1
        assert "图表类型" not in llm.calls[0][1][1]
        assert adapter.executedSql is None
        assert [r["purpose"] for r in tokenUsage.records] == ["clarify"]
        assert service._embedding.stored == []

    # =========================================================================
    # 值域采样接线（2-1）
    # =========================================================================

    @staticmethod
    def _sampledClass() -> OntologyClass:
        # 表名不含 schema 限定：Oracle 采样 SQL 应烘焙 u. 前缀（ds.username）
        return OntologyClass(
            class_name="PRECEIPT",
            class_alias="收货单",
            source_table="PRECEIPT",
            properties=[
                OntologyProperty(
                    property_name="STATUS", data_type="STRING", source_column="STATUS_0"
                )
            ],
        )

    async def test_value_samples_flow_into_plan_and_sql_prompts(self) -> None:
        """2-1：查询意图对候选列做 DISTINCT 采样，值域注入计划与 SQL 两阶段 schema。"""
        adapter = _RecordingAdapter([{"NAME": "A"}, {"NAME": "B"}])
        service, llm, _, _ = _buildService(classes=[self._sampledClass()], adapter=adapter)
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        samplingCalls = [s for s in adapter.sqls if "DISTINCT" in s]
        assert samplingCalls, "查询意图应触发值域采样（访问业务库取 DISTINCT 值）"
        assert "DISTINCT STATUS_0" in samplingCalls[0]
        assert "FROM u.PRECEIPT" in samplingCalls[0]  # Oracle 前缀烘焙进采样 SQL
        assert response.sql is not None
        # 计划（calls[0]）与 SQL（calls[1]）两阶段 schema 都注入值域示例
        expected = "STATUS: STRING (column=STATUS_0) 值域示例: ['A', 'B']"
        assert expected in llm.calls[0][0][1]
        assert expected in llm.calls[1][0][1]

    async def test_sampling_failure_degrades_to_no_samples(self) -> None:
        """2-1：采样整体失败不影响流水线，schema 不带值域示例（增强非硬依赖）。"""
        service, llm, _, _ = _buildService(
            classes=[self._sampledClass()], adapter=_SamplingFailureAdapter(_ROWS)
        )
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert response.sql is not None
        assert response.data == _ROWS
        assert "值域示例" not in llm.calls[0][0][1]
        assert "值域示例" not in llm.calls[1][0][1]

    async def test_clarify_skips_value_sampling(self) -> None:
        """2-1：CLARIFY 不生成 WHERE，跳过采样，不访问业务库。"""
        adapter = _RecordingAdapter(_ROWS)
        service, llm, _, _ = _buildService(classes=[self._sampledClass()], adapter=adapter)
        response = await service.processMessage(_dto("什么是收货数量"), _FakeSession())
        assert response.intent == IntentType.CLARIFY.value
        assert adapter.sqls == []  # 未采样也未执行查询
        assert "值域示例" not in llm.calls[0][0][1]

    async def test_invalid_datasource_username_skips_prefix_baking(self) -> None:
        """2-1：非法 schema 前缀（连字符用户名）经白名单拒绝，采样与 schema 展示都不烘焙。"""
        weirdDs = DataSource(
            id=1,
            name="weird",
            type=DataSourceType.ORACLE,
            host="h",
            port=1521,
            database_name="svc",
            username="my-user",  # 含连字符，非合法标识符
            password_encrypted="cipher",
        )
        adapter = _RecordingAdapter([{"NAME": "A"}])
        service, llm, _, _ = _buildService(datasource=weirdDs, classes=[self._sampledClass()], adapter=adapter)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        samplingCalls = [s for s in adapter.sqls if "DISTINCT" in s]
        assert samplingCalls
        assert "my-user." not in samplingCalls[0]
        assert "FROM PRECEIPT" in samplingCalls[0]  # 未烘焙，与 schema 文本一致
        assert "my-user.PRECEIPT" not in llm.calls[0][0][1]

    # =========================================================================
    # 图表类型覆盖 + 抽取实体回传（Phase 2）
    # =========================================================================

    async def test_chart_type_override_from_request(self) -> None:
        service, _, _, _ = _buildService()
        dto = _dto("各供应商的收货数量汇总")
        dto.chartType = ChartType.LINE
        response = await service.processMessage(dto, _FakeSession())
        assert response.chartType == ChartType.LINE.value

    async def test_chart_type_from_intent_keyword(self) -> None:
        """3-3/C3："换成柱状图"的意图抽取 chartType 贯通 _chartStep，不再自动推荐。"""
        service, _, _, _ = _buildService()
        response = await service.processMessage(_dto("换成柱状图"), _FakeSession())
        assert response.chartType == ChartType.BAR.value

    async def test_request_chart_type_wins_over_intent_keyword(self) -> None:
        """3-3：客户端显式 dto.chartType 优先于意图抽取（消息词 vs UI 选择）。"""
        service, _, _, _ = _buildService()
        dto = _dto("换成柱状图")
        dto.chartType = ChartType.LINE
        response = await service.processMessage(dto, _FakeSession())
        assert response.chartType == ChartType.LINE.value

    async def test_response_includes_extracted_entities(self) -> None:
        service, _, _, _ = _buildService()
        response = await service.processMessage(_dto("按地区汇总各供应商的销售额总额"), _FakeSession())
        assert response.extractedEntities is not None
        assert response.extractedEntities.dimension == "地区"
        assert response.extractedEntities.metric == "销售额"

    async def test_query_without_entities_has_none(self) -> None:
        service, _, _, _ = _buildService()
        response = await service.processMessage(_dto("各供应商的收货明细"), _FakeSession())
        assert response.extractedEntities is None

    # =========================================================================
    # 模型降级重试（5.4）
    # =========================================================================

    async def test_falls_back_to_cheapest_model_on_llm_error(self) -> None:
        primary = _config()
        fallback = LlmConfig(
            id=2,
            model_name="cheap-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.0001"),
            cost_per_1k_output=Decimal("0.0002"),
        )
        service, llm, tokenUsage, _ = _buildService(
            router=_FakeRouter(primary, fallback=fallback),
            llm=_FlakyLlm(failForModel=primary.model_name),
        )
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())

        assert response.modelName == "cheap-model"  # 回答经降级后由 fallback 模型实际服务
        purposes = [r["purpose"] for r in tokenUsage.records]
        assert "fallback_nl2sql" in purposes
        assert "fallback_answer" in purposes
        assert "fallback_chart" not in purposes  # 图表内部回退规则，不触发模型降级
        assert "chart" not in purposes  # 图表失败回退规则，零 token 不记录
        # 降级后成功调用按实际服务模型计量
        nl2sqlRow = next(r for r in tokenUsage.records if r["purpose"] == "nl2sql")
        assert nl2sqlRow["modelConfigId"] == 2
        assert nl2sqlRow["modelName"] == "cheap-model"
        answerRow = next(r for r in tokenUsage.records if r["purpose"] == "answer")
        assert answerRow["modelConfigId"] == 2
        # 失败的原始调用记录 zero-token usage 用于审计
        fallbackRow = next(r for r in tokenUsage.records if r["purpose"] == "fallback_nl2sql")
        assert fallbackRow["modelConfigId"] == 1
        assert fallbackRow["promptTokens"] == 0
        assert fallbackRow["completionTokens"] == 0

    async def test_reraises_when_no_fallback_available(self) -> None:
        primary = _config()
        service, _, _, _ = _buildService(
            router=_FakeRouter(primary, fallback=None),
            llm=_FlakyLlm(failForModel=primary.model_name),
        )
        with pytest.raises(LlmClientError):
            await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())

    async def test_no_fallback_usage_on_success(self) -> None:
        service, _, tokenUsage, _ = _buildService()
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert all(not r["purpose"].startswith("fallback_") for r in tokenUsage.records)

    async def test_falls_back_on_nl2sql_error_with_consumed_tokens(self) -> None:
        """主模型两阶段（计划阶段）抛 Nl2SqlError 时，也应触发降级并计量已消耗 token。"""
        primary = _config()
        fallback = LlmConfig(
            id=2,
            model_name="cheap-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.0001"),
            cost_per_1k_output=Decimal("0.0002"),
        )
        service, _, tokenUsage, _ = _buildService(
            router=_FakeRouter(primary, fallback=fallback),
        )

        class _RetryExhaustedNl2sql:
            async def generateValidatedPlan(self, question, classes, llmClient, modelConfig, **kwargs):
                # 主模型（test-model）计划校验耗尽；备选模型（cheap-model）能成功
                if modelConfig.model_name == "test-model":
                    raise Nl2SqlError(
                        "无法生成有效的查询计划",
                        detail="类 GHOST 不在本体",
                        tokens=(20, 10),
                    )
                return PlanResult(plan=QueryPlan(target="t"), promptTokens=6, completionTokens=4)

            async def generateSql(self, question, classes, llmClient, modelConfig, **kwargs):
                if modelConfig.model_name == "test-model":
                    raise Nl2SqlError("无法生成有效的查询 SQL", tokens=(30, 10))
                return SqlResult(sql="SELECT 1 FROM DUAL", promptTokens=5, completionTokens=3)

        service._nl2sql = _RetryExhaustedNl2sql()
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())

        # 降级审计行记录主模型已消耗的 token（来自 Nl2SqlError.tokens），而非 0/0
        fallbackRow = next(r for r in tokenUsage.records if r["purpose"] == "fallback_nl2sql")
        assert fallbackRow["modelConfigId"] == 1
        assert fallbackRow["promptTokens"] == 20
        assert fallbackRow["completionTokens"] == 10
        # 降级后成功调用按备选模型计量：plan(6,4) + sql(5,3) 聚合为单条 nl2sql 行
        nl2sqlRow = next(r for r in tokenUsage.records if r["purpose"] == "nl2sql")
        assert nl2sqlRow["modelConfigId"] == 2
        assert nl2sqlRow["modelName"] == "cheap-model"
        assert nl2sqlRow["promptTokens"] == 11
        assert nl2sqlRow["completionTokens"] == 7
        # tokensUsed 包含主模型浪费的 token：nl2sql 成功 18 + 浪费 30 + chart 15 + answer 15
        assert response.tokensUsed == 78
        # 本轮仅 NL2SQL 降级；回答仍由主模型服务，modelName 反映实际服务模型
        assert response.modelName == "test-model"

    async def test_filters_classes_by_relevant_keyword(self) -> None:
        """1-1：向量检索只召回相关类时，NL2SQL prompt 只渲染被召回的表。"""
        supplier = OntologyClass(
            id=1, class_name="Supplier", class_alias=None, description=None,
            source_table="SUPPLIER", properties=[],
        )
        receipt = OntologyClass(
            id=2, class_name="PRECEIPT", class_alias=None, description=None,
            source_table="PRECEIPT", properties=[],
        )
        ontology = _FakeOntologyService(
            classes=[supplier, receipt],
            searchHits=[SimpleNamespace(id=2)],  # 只召回 receipt
        )
        service, llm, _, _ = _buildService(ontology=ontology)
        await service.processMessage(_dto("收货数量"), _FakeSession())
        systemContent = llm.calls[1][0][1]  # 第二阶段 SQL prompt 的 system
        assert "PRECEIPT" in systemContent
        assert "SUPPLIER" not in systemContent  # 无关表被裁剪

    async def test_falls_back_to_all_classes_when_search_unavailable(self) -> None:
        """1-1：向量检索不可用时回退全量类，仍能回答而非报错。"""
        supplier = OntologyClass(
            id=1, class_name="Supplier", class_alias=None, description=None,
            source_table="SUPPLIER", properties=[],
        )
        receipt = OntologyClass(
            id=2, class_name="PRECEIPT", class_alias=None, description=None,
            source_table="PRECEIPT", properties=[],
        )

        class _BoomSearchOntology(_FakeOntologyService):
            async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
                raise RuntimeError("Milvus 不可用")

        service, llm, _, _ = _buildService(ontology=_BoomSearchOntology([supplier, receipt]))
        await service.processMessage(_dto("收货数量"), _FakeSession())
        systemContent = llm.calls[1][0][1]
        assert "SUPPLIER" in systemContent  # 回退全量 → Supplier 未被裁剪
        assert "PRECEIPT" in systemContent

    @staticmethod
    def _classes(*ids: int) -> list[OntologyClass]:
        return [
            OntologyClass(id=i, class_name=f"C{i}", class_alias=None, description=None,
                          source_table=f"T{i}", properties=[])
            for i in ids
        ]

    async def test_class_filter_logs_search_error_fallback(self, caplog) -> None:
        """1-1：检索抛错时回退全量并记 reason=search_error（供回退率聚合）。"""
        class _BoomSearchOntology(_FakeOntologyService):
            async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
                raise RuntimeError("Milvus 不可用")

        service, _, _, _ = _buildService(ontology=_BoomSearchOntology(self._classes(1)))
        with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
            result = await service._selectRelevantClasses(_FakeSession(), "收货数量", self._classes(1))
        assert [c.id for c in result] == [1]  # 回退全量
        assert "reason=search_error total=1" in caplog.text

    async def test_class_filter_logs_no_hits_fallback(self, caplog) -> None:
        """1-1：检索无命中时回退全量并记 reason=no_hits。"""
        classes = self._classes(1, 2)
        service, _, _, _ = _buildService(ontology=_FakeOntologyService(classes))
        with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
            result = await service._selectRelevantClasses(_FakeSession(), "收货数量", classes)
        assert result == classes
        assert "reason=no_hits total=2" in caplog.text

    async def test_class_filter_logs_no_match_fallback(self, caplog) -> None:
        """1-1：命中但无类 id 命中时回退全量并记 reason=no_match（带命中数）。"""
        classes = self._classes(1, 2)
        ontology = _FakeOntologyService(classes, searchHits=[SimpleNamespace(id=999)])
        service, _, _, _ = _buildService(ontology=ontology)
        with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
            result = await service._selectRelevantClasses(_FakeSession(), "收货数量", classes)
        assert result == classes
        assert "reason=no_match hits=1 total=2" in caplog.text

    async def test_class_filter_logs_successful_pruning(self, caplog) -> None:
        """1-1：命中全部可解析为真实类时 info 记录裁剪结果，不误告警。"""
        classes = self._classes(1, 2)
        ontology = _FakeOntologyService(classes, searchHits=[SimpleNamespace(id=2)])
        service, _, _, _ = _buildService(ontology=ontology)
        with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
            result = await service._selectRelevantClasses(_FakeSession(), "收货数量", classes)
        assert [c.id for c in result] == [2]
        assert "本体类裁剪完成 pruned=1 total=2 hits=1" in caplog.text

    async def test_class_filter_warns_on_low_hit_match(self, caplog) -> None:
        """1-1：命中大多解析不到真实类（检索漂移）时告警，防裁剪悄悄失效。

        用 topK 远小于 schema 规模时的命中比例而非覆盖比例，避免大 schema 误报。
        """
        classes = self._classes(1, 2)
        ontology = _FakeOntologyService(
            classes, searchHits=[SimpleNamespace(id=1)] + [SimpleNamespace(id=i) for i in (50, 51, 52)]
        )
        service, _, _, _ = _buildService(ontology=ontology)
        with caplog.at_level(logging.INFO, logger="app.services.chat_service"):
            result = await service._selectRelevantClasses(_FakeSession(), "收货数量", classes)
        assert [c.id for c in result] == [1]
        assert "本体类裁剪命中率过低 hits=4 matched=1 ratio=0.25" in caplog.text

    async def test_injects_few_shot_from_similar_queries(self) -> None:
        """1-2：语义相似的历史成功 SQL 注入 NL2SQL 的 system prompt（计划 + SQL 两阶段）。"""
        embedding = _FakeEmbeddingService(similarQueries=[
            SimpleNamespace(question="各供应商上月收货数量",
                            sql="SELECT NAME, SUM(QTY) FROM ZJTH.PRECEIPT GROUP BY NAME",
                            similarity=0.9),
            SimpleNamespace(question="本月销量",
                            sql="SELECT SUM(AMT) FROM ZJTH.PRECEIPT", similarity=0.7),
            SimpleNamespace(question="随便聊聊", sql=None, similarity=0.5),  # 无 SQL → 丢弃
        ])
        service, llm, _, _ = _buildService(embedding=embedding)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        sqlSystem = next(c[0][1] for c in llm.calls if any("生成 SQL 时必须" in content for _, content in c))
        assert "SELECT NAME, SUM(QTY) FROM ZJTH.PRECEIPT" in sqlSystem
        assert "SELECT SUM(AMT) FROM ZJTH.PRECEIPT" in sqlSystem
        assert "随便聊聊" not in sqlSystem  # sql=None 的命中被过滤
        # 计划阶段同样注入
        planSystem = next(c[0][1] for c in llm.calls if any("解析为查询计划" in content for _, content in c))
        assert "SELECT NAME, SUM(QTY) FROM ZJTH.PRECEIPT" in planSystem
        # 检索按问题调用的 topK 与 datasourceId 传递正确
        assert embedding.searchCalls[0][1]["topK"] == 3
        assert embedding.searchCalls[0][1]["datasourceId"] == 1

    async def test_skips_few_shot_when_search_unavailable(self) -> None:
        """1-2：检索不可用/无相似命中时跳过注入，流水线不受影响。"""
        embedding = _FakeEmbeddingService(raiseOnSearch=True)
        service, llm, _, _ = _buildService(embedding=embedding)
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert response.sql is not None
        for call in llm.calls:
            for _, content in call:
                assert "历史查询示例" not in content

    async def test_skips_few_shot_when_hits_below_similarity(self) -> None:
        """1-2：相似度过低的命中视为噪音，不注入。"""
        embedding = _FakeEmbeddingService(similarQueries=[
            SimpleNamespace(question="无关问题", sql="SELECT 1 FROM DUAL", similarity=0.3),
        ])
        service, llm, _, _ = _buildService(embedding=embedding)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        for call in llm.calls:
            for _, content in call:
                assert "历史查询示例" not in content

    async def test_execution_retry_keeps_few_shot(self) -> None:
        """1-2：执行报错回灌重试的 SQL 生成同样携带 few-shot 示例。"""
        adapter = _FlakyAdapter([{"NAME": "A", "QTY": Decimal(10)}], failTimes=1)
        embedding = _FakeEmbeddingService(similarQueries=[
            SimpleNamespace(question="上月收货数量",
                            sql="SELECT SUM(QTY) FROM ZJTH.PRECEIPT", similarity=0.9),
        ])
        service, llm, _, _ = _buildService(adapter=adapter, embedding=embedding)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        sqlSystems = [
            c[0][1] for c in llm.calls if any("生成 SQL 时必须" in content for _, content in c)
        ]
        assert len(sqlSystems) == 2  # 首次 + 重试
        for system in sqlSystems:
            assert "SELECT SUM(QTY) FROM ZJTH.PRECEIPT" in system

    async def test_few_shot_truncates_overlong_sql(self) -> None:
        """1-2：过长的历史 SQL 示例被截断，避免无界放大输入 token。"""
        longSql = "SELECT " + "A," * 200 + "X FROM ZJTH.PRECEIPT"  # > 400 字符
        embedding = _FakeEmbeddingService(similarQueries=[
            SimpleNamespace(question="上月收货数量", sql=longSql, similarity=0.9),
        ])
        service, llm, _, _ = _buildService(embedding=embedding)
        await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        sqlSystem = next(
            c[0][1] for c in llm.calls if any("生成 SQL 时必须" in content for _, content in c)
        )
        assert "X FROM ZJTH.PRECEIPT" not in sqlSystem  # 尾部被截断
        assert "..." in sqlSystem  # 截断标记

    async def test_retries_sql_when_execution_fails_then_recovers(self) -> None:
        """1-3：首次执行报错 → 回灌错误重试一轮 → 重试执行成功并审计额外消耗。"""
        adapter = _FlakyAdapter(
            [{"NAME": "A", "QTY": Decimal(10)}], failTimes=1,
        )
        service, _, tokenUsage, _ = _buildService(adapter=adapter)
        response = await service.processMessage(_dto("收货数量"), _FakeSession())
        assert len(adapter.executedSqls) == 2  # 首次失败 + 重试成功
        assert "PRECEIPT" in (response.sql or "")
        # 重试额外消耗已计入总量（4 次调用 60 + 重试 15）并审计（记录在 chart 之后）
        assert response.tokensUsed == 75
        assert [r["purpose"] for r in tokenUsage.records] == ["nl2sql", "chart", "nl2sql", "answer"]


class TestSessionContext:
    """会话上下文持久化 + Prompt 注入（5.2）（纯逻辑，无 IO）。"""

    async def test_answer_stage_omits_history_when_none(self) -> None:
        service, llm, _, _ = _buildService()
        await service.processMessage(_dto("本月销量"), _FakeSession())
        answerUser = llm.calls[-1][1][1]
        assert "查询结果" in answerUser
        assert "对话历史" not in answerUser

    async def test_falls_back_to_client_history_when_no_stored_messages(self) -> None:
        service, llm, _, _ = _buildService()
        dto = _dto("本月销量")
        dto.history = [
            HistoryMessage(role="user", content="上月销量"),
            HistoryMessage(role="assistant", content="上月销量为 1000"),
        ]
        await service.processMessage(dto, _FakeSession())
        systemContent = llm.calls[1][0][1]
        assert "以下是用户之前的对话历史" in systemContent
        assert "用户：上月销量" in systemContent
        assert "助手：上月销量为 1000" in systemContent

    async def test_skips_context_when_no_history(self) -> None:
        service, llm, _, _ = _buildService()
        await service.processMessage(_dto("本月销量"), _FakeSession())
        systemContent = llm.calls[1][0][1]
        assert "以下是用户之前的对话历史" not in systemContent


class TestUnanswerablePlan:
    """模型判定问题超出本体范围（计划 target=无法回答）时，流水线短路为友好回答。"""

    async def test_short_circuits_without_sql_generation_or_execution(self) -> None:
        service, llm, tokenUsage, adapter = _buildService(llm=_UnanswerablePipelineLlm())
        response = await service.processMessage(_dto("各业务线的销售额是多少？"), _FakeSession())
        # 不生成 SQL、不执行查询、不调用回答 LLM
        assert response.sql is None
        assert response.data is None
        assert response.chartType is None
        assert "无法回答" in response.answer
        assert len(llm.calls) == 1  # 仅计划阶段一次调用，SQL/图表/回答均跳过
        assert adapter.executedSql is None
        # 计划仍随响应下发（前端可展示"无法回答"原因）；计划 token 计入用量
        assert response.queryPlan is not None
        assert response.queryPlan["target"] == "无法回答"
        assert response.tokensUsed == 15
        assert response.cost > 0  # 计划阶段按实际服务模型计费
        assert [r["purpose"] for r in tokenUsage.records] == ["nl2sql"]

    async def test_fallback_served_plan_reports_fallback_model(self) -> None:
        """主模型在计划阶段失败 → fallback 生成无法回答计划：modelName 上报实际服务模型。"""
        primary = _config()
        fallback = LlmConfig(
            id=2,
            model_name="cheap-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.0001"),
            cost_per_1k_output=Decimal("0.0002"),
        )
        service, _, tokenUsage, adapter = _buildService(
            router=_FakeRouter(primary, fallback=fallback),
            llm=_UnanswerablePipelineLlm(failForModel=primary.model_name),
        )
        response = await service.processMessage(_dto("各业务线的销售额是多少？"), _FakeSession())
        assert response.sql is None
        assert response.modelName == "cheap-model"  # 由实际服务的 fallback 模型
        assert adapter.executedSql is None
        assert [r["purpose"] for r in tokenUsage.records] == ["fallback_nl2sql", "nl2sql"]


class TestUnanswerableSuggestion:
    """4-2：不可回答建议（缺表/缺术语）推断逻辑。"""

    def _classes(self, properties: list[OntologyProperty] | None = None) -> list[OntologyClass]:
        return [
            OntologyClass(
                class_name="PRECEIPT", class_alias="收货单", source_table="PRECEIPT",
                properties=properties or [],
            )
        ]

    def test_missing_quoted_term_is_flagged(self) -> None:
        """问题引用本体里没有的术语 → 报缺术语，帮助用户定向修正。"""
        classes = self._classes(
            [OntologyProperty(property_name="AMOUNT", data_type="NUMBER", source_column="AMT_0")]
        )
        suggestion = _buildUnanswerableSuggestion("查询『应收账龄』的分布", classes)
        assert "应收账龄" in suggestion
        assert "未匹配到" in suggestion

    def test_known_term_not_flagged_as_missing(self) -> None:
        """命中本体表面词（含业务别名）的术语不误报缺失。"""
        classes = self._classes(
            [OntologyProperty(
                property_name="AMOUNT", data_type="NUMBER", source_column="AMT_0",
                business_aliases=["营业额"],
            )]
        )
        suggestion = _buildUnanswerableSuggestion("查询『营业额』", classes)
        assert "营业额" not in suggestion  # 命中别名，不误报
        assert "PRECEIPT" in suggestion  # 回退列举现有业务类

    def test_unquoted_latin_keywords_are_not_flagged(self) -> None:
        """未框定的拉丁/整句不做切分，不误报缺失（避免 SQL 关键字等噪音）。"""
        classes = self._classes(
            [OntologyProperty(property_name="AMOUNT", data_type="NUMBER", source_column="AMT_0")]
        )
        suggestion = _buildUnanswerableSuggestion("SELECT * FROM PRECEIPT", classes)
        assert "未匹配到" not in suggestion  # 无候选术语，走回退列举
        assert "可查询" in suggestion

    def test_available_classes_listed_when_no_candidates(self) -> None:
        """无候选术语（无法推断缺失）时列举现有业务类引导用户选题。"""
        suggestion = _buildUnanswerableSuggestion("各业务线的销售额是多少？", self._classes())
        assert "可查询" in suggestion
        assert "PRECEIPT" in suggestion

    def test_empty_ontology_returns_empty(self) -> None:
        """本体为空且无候选术语 → 返回空串，由调用方拼兜底引导。"""
        assert _buildUnanswerableSuggestion("你好", []) == ""


class TestDomainCommands:
    """DEFINE / MAP / METRIC 领域命令：调用本体 CRUD，不进入 NL2SQL 流水线。"""

    async def _build(
        self,
        classes: list[OntologyClass] | None = None,
        metrics: list[OntologyMetric] | None = None,
    ) -> tuple[ChatService, _FakeOntologyService, _FakeTokenUsage]:
        ontology = _FakeOntologyService(classes, metrics)
        tokenUsage = _FakeTokenUsage()
        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=ontology,
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=tokenUsage,
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: _PipelineLlm(),
            adapterProvider=lambda datasourceId, ds: _FakeAdapter([]),
        )
        return service, ontology, tokenUsage

    async def test_define_metric_creates_and_replies(self) -> None:
        service, ontology, tokenUsage = await self._build()
        response = await service.processMessage(_dto("定义指标 销售额 = SUM(order.amount)"), _FakeSession())
        assert response.intent == IntentType.DEFINE.value
        assert "销售额" in response.answer
        assert response.sql is None
        assert response.tokensUsed == 0
        assert tokenUsage.records == []
        assert len(ontology.createdMetrics) == 1
        assert ontology.createdMetrics[0].metric_name == "销售额"
        assert ontology.createdMetrics[0].formula == "SUM(order.amount)"

    async def test_define_without_formula_returns_usage_guide(self) -> None:
        service, ontology, _ = await self._build()
        response = await service.processMessage(_dto("定义指标"), _FakeSession())
        assert response.intent == IntentType.DEFINE.value
        assert "格式" in response.answer
        assert ontology.createdMetrics == []

    async def test_metric_lists_existing_metrics(self) -> None:
        metric = OntologyMetric(
            id=1, metric_name="sales_amount", formula="SUM(amount)", agg_function="SUM"
        )
        service, _, _ = await self._build(metrics=[metric])
        response = await service.processMessage(_dto("有哪些指标"), _FakeSession())
        assert response.intent == IntentType.METRIC.value
        assert "sales_amount" in response.answer
        assert "SUM(amount)" in response.answer
        assert response.tokensUsed == 0

    async def test_metric_with_no_metrics(self) -> None:
        service, _, _ = await self._build()
        response = await service.processMessage(_dto("有哪些指标"), _FakeSession())
        assert response.intent == IntentType.METRIC.value
        assert "没有定义" in response.answer

    async def test_map_property_to_class(self) -> None:
        prop = OntologyProperty(id=10, class_id=1, property_name="customer_name", data_type="STRING")
        customer = OntologyClass(id=1, class_name="Customer", properties=[prop])
        order = OntologyClass(id=2, class_name="Order")
        service, ontology, _ = await self._build(classes=[customer, order])
        response = await service.processMessage(_dto("把 customer_name 映射到 Order"), _FakeSession())
        assert response.intent == IntentType.MAP.value
        assert "customer_name" in response.answer
        assert len(ontology.updatedProperties) == 1
        propId, updateDto = ontology.updatedProperties[0]
        assert propId == 10
        assert updateDto.ref_class_id == 2
        assert updateDto.is_foreign_key is True

    async def test_map_matches_business_alias(self) -> None:
        """2-2：MAP 确定性匹配也认 business_aliases（"客户名称"→customer_name）。"""
        prop = OntologyProperty(
            id=10, class_id=1, property_name="customer_name",
            business_aliases=["客户名称", "客户"], data_type="STRING",
        )
        customer = OntologyClass(id=1, class_name="Customer", properties=[prop])
        order = OntologyClass(id=2, class_name="Order")
        service, ontology, _ = await self._build(classes=[customer, order])
        response = await service.processMessage(_dto("把 客户名称 映射到 Order"), _FakeSession())
        assert response.intent == IntentType.MAP.value
        assert len(ontology.updatedProperties) == 1
        propId, updateDto = ontology.updatedProperties[0]
        assert propId == 10
        assert updateDto.ref_class_id == 2

    async def test_map_unknown_entity_replies_guidance(self) -> None:
        service, ontology, _ = await self._build()
        response = await service.processMessage(_dto("把 GHOST 映射到 Order"), _FakeSession())
        assert response.intent == IntentType.MAP.value
        assert "未找到" in response.answer
        assert ontology.updatedProperties == []

    async def test_domain_command_has_no_extracted_entities(self) -> None:
        service, _, _ = await self._build(metrics=[OntologyMetric(id=1, metric_name="m", formula="SUM(x)")])
        response = await service.processMessage(_dto("有哪些指标"), _FakeSession())
        assert response.extractedEntities is None

    # ===== /define 创建类（Phase 5 斜杠指令）=====

    async def test_slash_define_creates_class_and_replies(self) -> None:
        service, ontology, tokenUsage = await self._build()
        response = await service.processMessage(_dto("/define 产品"), _FakeSession())
        assert response.intent == IntentType.DEFINE.value
        assert "产品" in response.answer
        assert response.sql is None
        assert response.tokensUsed == 0
        assert len(ontology.createdClasses) == 1
        assert ontology.createdClasses[0].class_name == "产品"
        assert ontology.createdMetrics == []

    async def test_slash_define_with_alias_and_desc(self) -> None:
        service, ontology, _ = await self._build()
        response = await service.processMessage(
            _dto("/define 产品 alias=Product desc=销售商品"), _FakeSession()
        )
        assert response.intent == IntentType.DEFINE.value
        assert len(ontology.createdClasses) == 1
        assert ontology.createdClasses[0].class_name == "产品"
        assert ontology.createdClasses[0].class_alias == "Product"
        assert ontology.createdClasses[0].description == "销售商品"

    async def test_slash_metric_creates_metric(self) -> None:
        service, ontology, _ = await self._build()
        response = await service.processMessage(
            _dto("/metric 销售额 = SUM(order.amount)"), _FakeSession()
        )
        assert response.intent == IntentType.METRIC.value
        assert "销售额" in response.answer
        assert len(ontology.createdMetrics) == 1
        assert ontology.createdClasses == []

    async def test_slash_map_to_class(self) -> None:
        prop = OntologyProperty(id=10, class_id=1, property_name="customer_name", data_type="STRING")
        customer = OntologyClass(id=1, class_name="Customer", properties=[prop])
        order = OntologyClass(id=2, class_name="Order")
        service, ontology, _ = await self._build(classes=[customer, order])
        response = await service.processMessage(
            _dto("/map customer_name -> Order"), _FakeSession()
        )
        assert response.intent == IntentType.MAP.value
        assert "customer_name" in response.answer
        assert len(ontology.updatedProperties) == 1
        assert ontology.updatedProperties[0][1].ref_class_id == 2


# =============================================================================
# 会话亲和性（Phase 7）：ChatResponse.affinityStatus
# =============================================================================


class TestAffinityStatus:
    """会话亲和性：前 N 轮锁定模型，剩余轮数在响应中返回。

    - turnCount < N 且 lastModelId 有值 → 锁定（返回 AffinityStatus）
    - turnCount >= N 或 lastModelId 为 None → 解锁（返回 None）
    - 领域命令 / 闲聊不返回 affinityStatus
    """

    async def _buildWithAffinity(
        self,
        *,
        turnCount: int = 2,
        lastModelId: int | None = 1,
        affinityTurns: int = 3,
    ):
        tokenUsage = _FakeTokenUsage()
        tokenUsage.turnCount = turnCount
        tokenUsage.lastModelId = lastModelId
        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=_FakeOntologyService([]),
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=tokenUsage,
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: _PipelineLlm(),
            adapterProvider=lambda datasourceId, ds: _FakeAdapter(_ROWS),
            affinityTurns=affinityTurns,
        )
        return service, tokenUsage

    async def test_query_returns_affinity_status_when_locked(self) -> None:
        """turnCount < N 且有 lastModelId：返回锁定状态 + 剩余轮数。"""
        service, _ = await self._buildWithAffinity(turnCount=1, lastModelId=1, affinityTurns=3)
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert response.affinityStatus is not None
        assert response.affinityStatus.lockedModel == "test-model"
        # remainingTurns = N - turnCount = 3 - 1 = 2
        assert response.affinityStatus.remainingTurns == 2

    async def test_query_returns_none_when_turn_count_exceeds_window(self) -> None:
        """turnCount >= N：解锁，不返回 affinityStatus。"""
        service, _ = await self._buildWithAffinity(turnCount=3, lastModelId=1, affinityTurns=3)
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert response.affinityStatus is None

    async def test_query_returns_none_when_no_prior_model(self) -> None:
        """首轮（lastModelId=None）：尚未建立亲和，不返回。"""
        service, _ = await self._buildWithAffinity(turnCount=0, lastModelId=None, affinityTurns=3)
        response = await service.processMessage(_dto("各供应商的收货数量汇总"), _FakeSession())
        assert response.affinityStatus is None

    async def test_chitchat_returns_no_affinity_status(self) -> None:
        """闲聊意图不返回 affinityStatus（未走模型路由）。"""
        service, _ = await self._buildWithAffinity(turnCount=1, lastModelId=1)
        response = await service.processMessage(_dto("你好"), _FakeSession())
        assert response.affinityStatus is None

    async def test_domain_command_returns_no_affinity_status(self) -> None:
        """领域命令不返回 affinityStatus（零 LLM 消耗）。"""
        ontology = _FakeOntologyService()
        tokenUsage = _FakeTokenUsage()
        tokenUsage.turnCount = 1
        tokenUsage.lastModelId = 1
        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=ontology,
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=tokenUsage,
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: _PipelineLlm(),
            adapterProvider=lambda datasourceId, ds: _FakeAdapter([]),
            affinityTurns=3,
        )
        response = await service.processMessage(_dto("/metric 销售额 = SUM(x)"), _FakeSession())
        assert response.affinityStatus is None

    async def test_clarify_returns_affinity_status_when_locked(self) -> None:
        """CLARIFY 走模型路由，应返回 affinityStatus（设计：与查询意图同构）。"""
        service, _ = await self._buildWithAffinity(turnCount=1, lastModelId=1, affinityTurns=3)
        response = await service.processMessage(_dto("订单是什么意思"), _FakeSession())
        assert response.affinityStatus is not None
        assert response.affinityStatus.lockedModel == "test-model"


class TestBuildAnswerPrompt:
    """空结果提示优化：data=[] 时注入「可能未命中」提示，避免 answer LLM 误判为无数据。"""

    def test_empty_data_injects_hint(self) -> None:
        prompt = ChatService._buildAnswerPrompt(
            "统计查询2025年采购量最多的10种物料的采购价格",
            "SELECT * FROM T WHERE 1=0",
            [],
        )
        assert "查询结果" in prompt
        assert "[]" in prompt
        assert "未命中" in prompt
        assert "可能" in prompt

    def test_non_empty_data_omits_hint(self) -> None:
        prompt = ChatService._buildAnswerPrompt(
            "统计查询2025年采购量最多的10种物料的采购价格",
            "SELECT * FROM T",
            [{"物料编号": "A", "采购价格": 1.5}],
        )
        assert "未命中" not in prompt

    def test_hint_appears_after_result_block(self) -> None:
        prompt = ChatService._buildAnswerPrompt("问题", "SELECT 1", [])
        resultIdx = prompt.index("查询结果")
        hintIdx = prompt.index("未命中")
        assert hintIdx > resultIdx


class TestAnswerSystemPromptHardConstraint:
    """answer LLM system prompt 必须禁止反向追问（2026-08-17 真实回归）。

    即使单步路径漏判复合问题让 LLM 拿到"3 件事问题 + 1 件数据"，也不应让 answer
    LLM 输出"需要继续查询吗 / 是否需要进一步分析"这类拟人化追问。
    """

    def test_answer_prompt_forbids_reverse_questions(self) -> None:
        from app.services.chat_stream_output import _ANSWER_SYSTEM_PROMPT

        assert "禁止反向追问" in _ANSWER_SYSTEM_PROMPT
        assert "需要继续查询" in _ANSWER_SYSTEM_PROMPT or "继续查询吗" in _ANSWER_SYSTEM_PROMPT
