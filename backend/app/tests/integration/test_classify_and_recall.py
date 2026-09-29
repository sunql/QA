"""v3.1 A7 集成守卫：classifyAndRecall needRecall=True 路径 + 单步 QUERY LLM 基线。

真实 PostgreSQL（独立容器 qa-pg-a1 / host 5434）+ 完整 ChatService 链路。
复用 test_chat_service_state.py 同款 _PipelineLlm / 假依赖模式（不重复造）；
focus 在通道 1 入口的集成语义：

1. needRecall=True 走真实本体类召回（fake ontology 注入 searchHits，验证
   classes/recallInfo 正常产出且 LLM 不被触达）；
2. 单步 QUERY 链路（plan + SQL 生成）LLM 调用 == 2，且 classify/recall 路径
   LLM 调用 == 0（既有 test_chat_service_state.py 与 test_chat_service.py 已
   覆盖 len(llm.calls)==4 的全量断言；本测试聚焦 plan+SQL 的零漂移基线）。
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import ChartType, DataSourceType, IntentType
from app.domain.exceptions import NotFoundError
from app.domain.models import DataSource, LlmConfig, OntologyClass, SessionQueryState
from app.domain.schemas import ChatRequest
from app.services.chat_service import ChatService
from app.services.intent_service import IntentService


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _PipelineLlm:
    """按 prompt 内容路由回复：plan JSON / SQL / chart JSON / answer。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages, **kwargs) -> _Resp:  # noqa: ANN001, ANN202
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content
        if "图表类型" in user:
            return _Resp('{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}')
        if "解析为查询计划" in system:
            # selectedClasses 必须与召回到的 OntologyClass.class_name 一致，
            # 否则 plan 校验因引用不可解析而判 PLAN_EMPTY
            return _Resp(
                '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"]}'
            )
        if "生成 SQL 时必须" in system:
            return _Resp(
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        return _Resp("查询完成，共 2 条记录。")


class _FakeDatasource:
    def __init__(self, ds: DataSource) -> None:
        self._ds = ds

    async def get(self, session, datasourceId: int) -> DataSource:  # noqa: ANN001
        if datasourceId != self._ds.id:
            raise NotFoundError(f"数据源 {datasourceId} 不存在")
        return self._ds


class _FakeOntology:
    """注入召回命中——验证 classifyAndRecall needRecall=True 真实召回路径。"""

    def __init__(self, hits: list, allClasses: list[OntologyClass]) -> None:
        self._hits = hits
        self._classes = allClasses

    async def listClasses(self, session) -> list[OntologyClass]:  # noqa: ANN001
        return self._classes

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:  # noqa: ANN001
        return self._hits

    async def listJoins(self, session) -> list:  # noqa: ANN001
        return []


class _FakeAdapter:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def execute_read_only(self, sql: str) -> list[dict]:  # noqa: ANN001
        return self._rows


class _FakeRouter:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:  # noqa: ANN001
        return self._config

    def selectFallbackModel(self, configs, excludeId):  # noqa: ANN001
        return None


class _FakeTokenUsage:
    async def getSessionCost(self, session, sessionId):  # noqa: ANN001
        return Decimal("0")

    async def getSessionTurnCount(self, session, sessionId):  # noqa: ANN001
        return 0

    async def getLastModelId(self, session, sessionId):  # noqa: ANN001
        return None

    async def recordUsage(self, session, **kwargs) -> None:  # noqa: ANN001
        pass


def _config() -> LlmConfig:
    return LlmConfig(
        id=1, model_name="test-model", provider="openai",
        cost_per_1k_input=Decimal("0.001"), cost_per_1k_output=Decimal("0.002"),
    )


def _datasource() -> DataSource:
    return DataSource(
        id=1, name="ZJTH", type=DataSourceType.ORACLE,
        host="h", port=1521, database_name="svc",
        username="u", password_encrypted="cipher",
    )


def _dto(question: str) -> ChatRequest:
    return ChatRequest(sessionId="a7-session", question=question, datasourceId=1)


def _buildService(*, llm: _PipelineLlm, ontology: _FakeOntology) -> ChatService:
    return ChatService(
        intentService=IntentService(),
        datasourceService=_FakeDatasource(_datasource()),
        ontologyService=ontology,  # type: ignore[arg-type]
        modelRouterService=_FakeRouter(_config()),  # type: ignore[arg-type]
        tokenUsageService=_FakeTokenUsage(),  # type: ignore[arg-type]
        llmFactory=lambda c: llm,
        adapterProvider=lambda did, ds: _FakeAdapter(  # noqa: ANN202
            [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]
        ),
    )


class _FakeHit:
    def __init__(self, clsId: int, score: float = 0.9) -> None:
        self.id = clsId
        self.score = score


def _class(clsId: int, table: str) -> OntologyClass:
    return OntologyClass(
        id=clsId, class_name="PRECEIPT",
        source_table=table, valid_from=None, valid_to=None,
    )


class TestClassifyAndRecallIntegration:
    async def test_need_recall_path_returns_classes_and_recall_info(
        self, dbSession,
    ) -> None:
        """needRecall=True + QUERY 意图：召回真实跑通，LLM 工厂零调用。"""
        classes = [
            _class(1, "DWD_PRECEIPT"),
            _class(2, "DWD_PRECEIPT_DETAIL"),
        ]
        hits = [_FakeHit(1, 0.95), _FakeHit(2, 0.8)]
        llm = _PipelineLlm()
        service = _buildService(
            llm=llm,
            ontology=_FakeOntology(hits=hits, allClasses=classes),
        )

        classified = await service.classifyAndRecall(
            dbSession, "各供应商的收货数量汇总",
            sessionId="a7-recall-test", needRecall=True,
        )

        assert classified.intentResult.intent is IntentType.QUERY
        assert classified.intentResult.semanticState is not None
        # 召回产物来自真实 _selectRelevantClasses（mode="recall"，未扩边）
        assert classified.recallInfo is not None
        assert classified.recallInfo.hitCount == 2
        assert {c.id for c in classified.classes} == {1, 2}
        # 无状态记录 → state=None
        assert classified.state is None
        # 通道 1 召回路径零 LLM 调用
        assert llm.calls == []

    async def test_non_recall_intent_with_need_recall_skips_recall(
        self, dbSession,
    ) -> None:
        """CHITCHAT 等不进 NL2SQL 的意图：needRecall=True 也跳过召回（省检索）。"""
        llm = _PipelineLlm()
        service = _buildService(
            llm=llm,
            ontology=_FakeOntology(hits=[], allClasses=[]),
        )
        classified = await service.classifyAndRecall(
            dbSession, "你好", sessionId="a7-chitchat", needRecall=True,
        )
        assert classified.intentResult.intent is IntentType.CHITCHAT
        assert classified.classes == []
        assert classified.recallInfo is None
        assert llm.calls == []

    async def test_reclassify_with_prior_state(self, dbSession) -> None:
        """有会话状态时重分类为 REFINE（会话状态经真实 PG）。"""
        await service_saveState(dbSession)
        llm = _PipelineLlm()
        service = _buildService(
            llm=llm,
            ontology=_FakeOntology(hits=[], allClasses=[]),
        )
        classified = await service.classifyAndRecall(
            dbSession, "按数量降序排序",
            sessionId="a7-prior", needRecall=False,
        )
        assert classified.intentResult.intent is IntentType.REFINE
        assert classified.state is not None
        assert classified.state.last_question == "各供应商的收货数量汇总"
        assert llm.calls == []


async def service_saveState(session) -> None:
    """为重分类测试准备 SessionQueryState（真实 PG upsert）。"""
    service = ChatService()
    await service._saveQueryState(  # type: ignore[attr-defined]
        session,
        "a7-prior",
        question="各供应商的收货数量汇总",
        plan=None, sql=None, resultColumns=[],
    )
    # 验证建好（防止无 DB 时悬空）
    from sqlalchemy import select
    row = (await session.execute(
        select(SessionQueryState).where(SessionQueryState.session_id == "a7-prior")
    )).scalar_one()
    assert row.last_question == "各供应商的收货数量汇总"


class TestSingleStepQueryLlmBaseline:
    """单步 QUERY 链路的 LLM 调用基线（plan + SQL == 2；classify/recall == 0）。

    既有 unit/test_chat_service.py:test_full_pipeline_returns_complete_response 已
    覆盖 ``len(llm.calls)==4`` 与 plan/SQL 位置断言。本测试在真实 PG 下重复一遍，
    并显式以 system prompt 标记筛 plan/SQL 调用次数，验证 A7 收口未漂移。
    """

    async def test_single_step_query_makes_two_llm_calls_plan_and_sql(
        self, dbSession,
    ) -> None:
        """验收：单步 QUERY 链路 plan + SQL 生成共 2 次 LLM 调用，classify/recall 路径 0 次。"""
        llm = _PipelineLlm()
        # 注入一个本体类 → plan 阶段能产出 selected_classes → 链路跑通
        classes = [_class(1, "DWD_PRECEIPT")]
        hits = [_FakeHit(1, 0.95)]
        service = _buildService(
            llm=llm,
            ontology=_FakeOntology(hits=hits, allClasses=classes),
        )
        await service.processMessage(_dto("各供应商的收货数量汇总"), dbSession)

        # 按 system prompt 标记筛选计划/SQL 两阶段调用
        planCalls = [
            c for c in llm.calls if "解析为查询计划" in c[0][0][1]
        ]
        sqlCalls = [
            c for c in llm.calls if "生成 SQL 时必须" in c[0][0][1]
        ]
        assert llm.calls, "LLM 完全未被调用——前置拦截（L1/L4/分类）可能异常"
        # 按 system prompt 标记筛选计划/SQL 两阶段调用（c[0][1]=content of first message）
        planCalls = [
            c for c in llm.calls if "解析为查询计划" in c[0][1]
        ]
        sqlCalls = [
            c for c in llm.calls if "生成 SQL 时必须" in c[0][1]
        ]
        assert len(planCalls) == 1, f"计划阶段调用次数={len(planCalls)}，期望 1"
        assert len(sqlCalls) == 1, f"SQL 阶段调用次数={len(sqlCalls)}，期望 1"
        # 完整流水线（参考既有 unit/test_chat_service.py:test_full_pipeline_returns_complete_response
        # 的 len(llm.calls)==4）保持 4 次总调用。
        assert len(llm.calls) == 4, f"单步 QUERY 流水线总调用={len(llm.calls)}，期望 4"
