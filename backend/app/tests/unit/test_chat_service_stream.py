"""ChatService 流式输出单元测试（5.6）。

mock 全部依赖，验证 processMessageStream 的事件序列与降级语义：
- 闲聊：meta(chitchat) → token(问候) → done(0 token)
- 查询：meta → plan → sql → chart → token×N → done（含累计 token/成本 + 后台向量存储）
- 数据源不存在：meta → error 事件（连接不中断）
- 回答流主模型失败且未产出 token：降级到最便宜模型，记录 fallback_answer
- 回答流已产出 token 后中断：不降级，记 answer_stream_failed 失败标记 + error 事件
- 未预期异常（非领域异常）：结构化 error 事件而非中断连接
- 回答流块间超时（LLM 挂起）：结构化 error 事件，不无限占用连接
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.domain.exceptions import LlmClientError
from app.domain.models import LlmConfig, OntologyClass, OntologyProperty
from app.domain.schemas import HistoryMessage
from app.infrastructure.llm.base_client import StreamChunk
from app.services.chat_service import ChatService
from app.services.stream_events import (
    EVENT_CHART,
    EVENT_CLASS_RECALL,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_MULTI_STEP_PLAN,
    EVENT_PLAN,
    EVENT_SQL,
    EVENT_STEP_PLAN,
    EVENT_STEP_RESULT,
    EVENT_TOKEN,
)
from app.services.value_sampler import clearValueSampleCache
from app.tests.unit.test_chat_service import (
    _config,
    _datasource,
    _dto,
    _FakeAdapter,
    _FakeDatasourceService,
    _FakeEmbeddingService,
    _FakeOntologyService,
    _FakeRouter,
    _FakeSession,
    _FakeTokenUsage,
    _FlakyAdapter,
    _RecordingAdapter,
    _Resp,
)

_ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]


@pytest.fixture(autouse=True)
def _isolate_value_sample_cache():
    """值域采样模块缓存按测试隔离，避免跨测试/跨文件命中导致断言失真（2-1）。"""
    clearValueSampleCache()
    yield
    clearValueSampleCache()


class _StreamPipelineLlm:
    """complete() 供 nl2sql/chart；completeStream() 供回答（可配置失败）。"""

    def __init__(
        self,
        failStreamModel: str | None = None,
        failAfterChunks: int | None = None,
    ) -> None:
        self._failStreamModel = failStreamModel
        self._failAfterChunks = failAfterChunks
        self.streamCalls = 0
        self.completeCalls = 0

    async def complete(self, messages, **kwargs) -> _Resp:
        self.completeCalls += 1
        system = messages[0].content
        user = messages[1].content
        if "图表类型" in user:
            return _Resp('{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}')
        if "解析为查询计划" in system:
            # ReAct 第一阶段：空计划（classes 为空时无引用可校验）
            return _Resp('{"target":"各供应商的收货数量汇总"}')
        if "生成 SQL 时必须" in system:
            return _Resp(
                "```sql\nSELECT NAME, SUM(QTY) FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        return _Resp("查询完成，共 2 条记录。")

    async def completeStream(self, messages, **kwargs):
        self.streamCalls += 1
        if kwargs.get("model") == self._failStreamModel:
            raise LlmClientError(f"模型 {kwargs.get('model')} 服务不可用")
        pieces = ("查询完成，", "共 2 条记录。")
        for index, piece in enumerate(pieces):
            if self._failAfterChunks is not None and index >= self._failAfterChunks:
                raise LlmClientError("回答流中断")
            yield StreamChunk(
                content=piece, isDone=False, promptTokens=0, completionTokens=0, modelName="test-model"
            )
        yield StreamChunk(
            content="", isDone=True, promptTokens=10, completionTokens=5, modelName="test-model"
        )


class _UnanswerableStreamPipelineLlm(_StreamPipelineLlm):
    """ReAct 第一阶段返回 target=无法回答 的空计划；SQL/回答不被调用则 complete 仅 1 次。"""

    async def complete(self, messages, **kwargs) -> _Resp:
        self.completeCalls += 1
        system = messages[0].content
        if "解析为查询计划" in system:
            return _Resp('{"target":"无法回答"}')
        return _Resp("查询完成，共 2 条记录。")


class _RecordingStreamLlm(_StreamPipelineLlm):
    """在 _StreamPipelineLlm 基础上记录 complete() 的 system prompt（calls[1] 为 SQL 阶段）。"""

    def __init__(self) -> None:
        super().__init__()
        self.systemPrompts: list[str] = []

    async def complete(self, messages, **kwargs) -> _Resp:
        self.systemPrompts.append(messages[0].content)
        return await super().complete(messages, **kwargs)


class _RecordingAnswerLlm(_StreamPipelineLlm):
    """在 _StreamPipelineLlm 基础上记录 completeStream() 的 user prompt（1-5 回答历史验证）。"""

    def __init__(self) -> None:
        super().__init__()
        self.answerUserPrompts: list[str] = []

    async def completeStream(self, messages, **kwargs):
        self.answerUserPrompts.append(messages[1].content)
        async for chunk in super().completeStream(messages, **kwargs):
            yield chunk


def _fallbackConfig() -> LlmConfig:
    return LlmConfig(
        id=2,
        model_name="cheap-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.0001"),
        cost_per_1k_output=Decimal("0.0002"),
    )


def _buildStreamService(
    *,
    router: _FakeRouter | None = None,
    llm: _StreamPipelineLlm | None = None,
    adapter: _FakeAdapter | None = None,
) -> tuple[ChatService, _StreamPipelineLlm, _FakeTokenUsage, _FakeEmbeddingService]:
    llm = llm or _StreamPipelineLlm()
    tokenUsage = _FakeTokenUsage()
    embedding = _FakeEmbeddingService()
    service = ChatService(
        datasourceService=_FakeDatasourceService(_datasource()),
        ontologyService=_FakeOntologyService([]),
        modelRouterService=router or _FakeRouter(_config()),
        tokenUsageService=tokenUsage,
        embeddingService=embedding,
        llmFactory=lambda config: llm,
        adapterProvider=lambda datasourceId, ds: adapter or _FakeAdapter(_ROWS),
    )
    return service, llm, tokenUsage, embedding


async def _collect(
    service: ChatService, dto, *, session: _FakeSession | None = None,
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    session = session or _FakeSession()
    async for event in service.processMessageStream(dto, session):
        events.append((event.event, event.data))
    await asyncio.sleep(0)  # 让后台查询向量任务有机会完成，保证测试确定性
    return events


class TestStreamingPipeline:
    async def test_chitchat_streams_greeting(self) -> None:
        service, llm, tokenUsage, _ = _buildStreamService()
        session = _FakeSession()
        events = await _collect(service, _dto("你好"), session=session)
        assert [e for e, _ in events] == [EVENT_META, EVENT_TOKEN, EVENT_DONE]
        assert events[0][1]["intent"] == "chitchat"
        assert "智能问答助手" in events[1][1]["content"]
        assert events[2][1]["tokensUsed"] == 0
        assert llm.streamCalls == 0
        assert tokenUsage.records == []
        # 4-4：闲聊轮也持久化消息（user + assistant），历史链不断
        assert len(session.added) == 2

    async def test_query_stream_emits_full_event_sequence(self) -> None:
        service, _, tokenUsage, embedding = _buildStreamService()
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        types = [e for e, _ in events]

        assert types[0] == EVENT_META
        assert events[0][1]["intent"] == "query"
        # 2026-09-16 类召回诊断（e028ec6）：query 流先下发 class_recall，再进执行计划
        assert types[1] == EVENT_CLASS_RECALL
        # 2026-08-16：单步查询也下发执行计划事件（multi_step_plan → step_plan → plan → sql → ... → step_result）
        assert types[2] == EVENT_MULTI_STEP_PLAN
        assert events[2][1]["steps"][0]["stepIndex"] == 0
        assert types[3] == EVENT_STEP_PLAN
        assert types[4] == EVENT_PLAN
        assert events[4][1]["plan"]["target"] == "各供应商的收货数量汇总"
        assert types[5] == EVENT_SQL
        assert "PRECEIPT" in events[5][1]["sql"]
        assert types[6] == EVENT_CHART
        assert events[6][1]["chartType"] == "pie"
        assert events[6][1]["chartOption"] is not None
        assert len(events[6][1]["data"]) == 2

        tokenContents = [d["content"] for t, d in events if t == EVENT_TOKEN]
        assert tokenContents == ["查询完成，", "共 2 条记录。"]
        # step_result 事件夹在 token 与 done 之间（携带 sql/summary 给前端卡片）
        stepResultTypes = [t for t in types if t == EVENT_STEP_RESULT]
        assert len(stepResultTypes) == 1
        assert types[-1] == EVENT_DONE
        assert events[-1][1]["tokensUsed"] == 60  # plan+sql 30 + chart 15 + answer 15
        assert events[-1][1]["cost"] > 0
        assert events[-1][1]["modelName"] == "test-model"  # 实际服务的回答模型
        assert [r["purpose"] for r in tokenUsage.records] == ["nl2sql", "chart", "answer"]
        # 查询向量后台存储（fire-and-forget）已记录
        assert len(embedding.stored) == 1
        assert embedding.stored[0]["sql"] == events[5][1]["sql"]
        assert embedding.stored[0]["sessionId"] == "s1"

    async def test_stream_chart_type_from_intent_keyword(self) -> None:
        """3-3：流式路径"换成柱状图"的意图 chartType 贯通，chart 事件不再自动推荐。"""
        service, _, _, _ = _buildStreamService()
        events = await _collect(service, _dto("换成柱状图"))
        chartEvent = next(d for t, d in events if t == EVENT_CHART)
        assert chartEvent["chartType"] == "bar"

    async def test_nl2sql_prompt_uses_datasource_dialect_in_stream(self) -> None:
        llm = _RecordingStreamLlm()
        service, _, _, _ = _buildStreamService(llm=llm)
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        assert events[0][1]["intent"] == "query"
        # calls[0] 为计划 prompt（无方言）；calls[1] 为 SQL system prompt，含方言与 username 前缀
        assert "解析为查询计划" in llm.systemPrompts[0]
        system = llm.systemPrompts[1]
        assert "Oracle 数据库" in system
        assert "u.表名" in system

    async def test_query_stream_sampling_flows_into_sql_prompt(self) -> None:
        """2-1：流式查询同样做值域采样，SQL 阶段 schema 注入值域示例。"""
        cls = OntologyClass(
            class_name="PRECEIPT",
            class_alias="收货单",
            source_table="PRECEIPT",
            properties=[
                OntologyProperty(
                    property_name="STATUS", data_type="STRING", source_column="STATUS_0"
                )
            ],
        )
        adapter = _RecordingAdapter([{"NAME": "A"}, {"NAME": "B"}])
        llm = _RecordingStreamLlm()
        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=_FakeOntologyService([cls]),
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=_FakeTokenUsage(),
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: llm,
            adapterProvider=lambda datasourceId, ds: adapter,
        )
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        assert events[0][1]["intent"] == "query"
        assert any("DISTINCT STATUS_0" in s for s in adapter.sqls), "流式查询应触发值域采样"
        # systemPrompts[0] 为计划、[1] 为 SQL 阶段 schema
        expected = "STATUS: STRING (column=STATUS_0) 值域示例: ['A', 'B']"
        assert expected in llm.systemPrompts[0]
        assert expected in llm.systemPrompts[1]

    async def test_datasource_missing_emits_error_event(self) -> None:
        service, _, _, _ = _buildStreamService()
        session = _FakeSession()
        events = await _collect(service, _dto("各供应商的收货数量汇总", datasourceId=99), session=session)
        assert events[0][0] == EVENT_META
        assert events[1][0] == EVENT_ERROR
        assert "不存在" in events[1][1]["error"]
        assert events[1][1]["errorType"] == "domain"  # 4-1：NotFoundError 属领域错误
        assert len(session.added) == 2  # 4-4：出错轮也持久化消息

    async def test_domain_command_streams_token_and_done(self) -> None:
        """DEFINE 领域命令流式：meta(define) → token(结果) → done（零 LLM 消耗）。"""
        service, llm, tokenUsage, embedding = _buildStreamService()
        events = await _collect(service, _dto("定义指标 销售额 = SUM(order.amount)"))
        types = [e for e, _ in events]
        assert types[0] == EVENT_META
        assert events[0][1]["intent"] == "define"
        assert types[1] == EVENT_TOKEN
        assert "销售额" in events[1][1]["content"]
        assert types[-1] == EVENT_DONE
        assert events[-1][1]["tokensUsed"] == 0
        assert llm.completeCalls == 0
        assert llm.streamCalls == 0
        assert tokenUsage.records == []
        assert embedding.stored == []

    async def test_slash_define_streams_token_and_done(self) -> None:
        """斜杠 /define 创建类流式：meta(define) → token(结果) → done（零 LLM 消耗）。"""
        service, llm, tokenUsage, embedding = _buildStreamService()
        events = await _collect(service, _dto("/define 产品 alias=Product desc=销售商品"))
        types = [e for e, _ in events]
        assert types[0] == EVENT_META
        assert events[0][1]["intent"] == "define"
        assert types[1] == EVENT_TOKEN
        assert "产品" in events[1][1]["content"]
        assert "Product" in events[1][1]["content"]
        assert types[-1] == EVENT_DONE
        assert events[-1][1]["tokensUsed"] == 0
        assert llm.completeCalls == 0
        assert llm.streamCalls == 0
        assert tokenUsage.records == []
        assert embedding.stored == []

    async def test_slash_metric_streams_token_and_done(self) -> None:
        """斜杠 /metric 创建指标流式：meta(metric) → token(结果) → done（零 LLM 消耗）。"""
        service, llm, tokenUsage, embedding = _buildStreamService()
        events = await _collect(service, _dto("/metric 销售额 = SUM(order.amount)"))
        types = [e for e, _ in events]
        assert types[0] == EVENT_META
        assert events[0][1]["intent"] == "metric"
        assert types[1] == EVENT_TOKEN
        assert "销售额" in events[1][1]["content"]
        assert types[-1] == EVENT_DONE
        assert events[-1][1]["tokensUsed"] == 0
        assert llm.completeCalls == 0
        assert llm.streamCalls == 0
        assert tokenUsage.records == []

    async def test_slash_map_streams_token_and_done(self) -> None:
        """斜杠 /map 属性映射流式：meta(map) → token(结果) → done（零 LLM 消耗）。"""
        from app.domain.models import OntologyClass, OntologyProperty

        prop = OntologyProperty(id=10, class_id=1, property_name="customer_name", data_type="STRING")
        order = OntologyClass(id=2, class_name="Order", properties=[prop])
        ontology = _FakeOntologyService([order])
        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=ontology,
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=_FakeTokenUsage(),
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: _StreamPipelineLlm(),
            adapterProvider=lambda datasourceId, ds: _FakeAdapter(_ROWS),
        )
        events = await _collect(service, _dto("/map customer_name -> Order"))
        types = [e for e, _ in events]
        assert types[0] == EVENT_META
        assert events[0][1]["intent"] == "map"
        assert types[1] == EVENT_TOKEN
        assert "customer_name" in events[1][1]["content"]
        assert types[-1] == EVENT_DONE
        assert events[-1][1]["tokensUsed"] == 0
        assert len(ontology.updatedProperties) == 1
        assert ontology.updatedProperties[0][1].ref_class_id == 2

    async def test_answer_stream_falls_back_when_primary_fails_before_tokens(self) -> None:
        primary = _config()
        service, llm, tokenUsage, _ = _buildStreamService(
            router=_FakeRouter(primary, fallback=_fallbackConfig()),
            llm=_StreamPipelineLlm(failStreamModel=primary.model_name),
        )
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        types = [e for e, _ in events]

        answer = "".join(d["content"] for t, d in events if t == EVENT_TOKEN)
        assert answer == "查询完成，共 2 条记录。"
        assert types[-1] == EVENT_DONE
        # 主模型 stream 失败 → 降级记录 fallback_answer，成功回答按 fallback 计量
        assert [r["purpose"] for r in tokenUsage.records] == [
            "nl2sql", "chart", "fallback_answer", "answer",
        ]
        answerRow = next(r for r in tokenUsage.records if r["purpose"] == "answer")
        assert answerRow["modelConfigId"] == 2
        assert answerRow["modelName"] == "cheap-model"
        assert events[-1][1]["modelName"] == "cheap-model"  # 降级后实际服务的模型
        assert llm.streamCalls == 2  # 主模型失败 + 备选成功

    async def test_answer_stream_emits_error_after_partial_tokens(self) -> None:
        service, llm, tokenUsage, _ = _buildStreamService(
            llm=_StreamPipelineLlm(failAfterChunks=1),
        )
        session = _FakeSession()
        events = await _collect(service, _dto("各供应商的收货数量汇总"), session=session)
        types = [e for e, _ in events]

        # 已产出 1 个 token 后中断 → 不降级（客户端已收到部分内容），发 error 事件
        tokenContents = [d["content"] for t, d in events if t == EVENT_TOKEN]
        assert tokenContents == ["查询完成，"]
        assert types[-1] == EVENT_ERROR
        assert "中断" in events[-1][1]["error"]
        assert events[-1][1]["errorType"] == "llm"  # 4-1：回答流中断属 LLM 失败
        assert len(session.added) == 2  # 4-4：出错轮也持久化消息
        # 已产出 token 后中断：不降级，但记一条失败标记审计行（answer_stream_failed）
        assert [r["purpose"] for r in tokenUsage.records] == [
            "nl2sql", "chart", "answer_stream_failed",
        ]
        assert llm.streamCalls == 1

    async def test_unexpected_error_emits_generic_error_event(self) -> None:
        """非领域异常（DB 连接中断等）也须产出结构化 error 事件，而非强行中断连接。"""

        class _BoomAdapter:
            async def execute_read_only(self, sql: str) -> list[dict]:
                raise RuntimeError("连接池耗尽")

        service = ChatService(
            datasourceService=_FakeDatasourceService(_datasource()),
            ontologyService=_FakeOntologyService([]),
            modelRouterService=_FakeRouter(_config()),
            tokenUsageService=_FakeTokenUsage(),
            embeddingService=_FakeEmbeddingService(),
            llmFactory=lambda config: _StreamPipelineLlm(),
            adapterProvider=lambda datasourceId, ds: _BoomAdapter(),
        )
        session = _FakeSession()
        events = await _collect(service, _dto("各供应商的收货数量汇总"), session=session)
        assert events[0][0] == EVENT_META
        assert events[-1][0] == EVENT_ERROR
        assert "服务内部错误" in events[-1][1]["error"]
        assert events[-1][1]["errorType"] == "internal"  # 4-1：未预期异常
        assert len(session.added) == 2  # 4-4：出错轮也持久化消息

    async def test_retries_execution_error_and_emits_corrected_sql(self) -> None:
        """1-3（流式）：执行报错回灌重试成功后，补发更正后的 SQL 事件。"""

        class _ChangingSqlStreamLlm(_StreamPipelineLlm):
            """SQL 生成第二次返回不同 SQL（模拟回灌错误后修正）。"""

            def __init__(self) -> None:
                super().__init__()
                self._sqlCalls = 0

            async def complete(self, messages, **kwargs) -> _Resp:
                self.completeCalls += 1
                system = messages[0].content
                if "生成 SQL 时必须" in system:
                    self._sqlCalls += 1
                    if self._sqlCalls == 1:
                        return _Resp("```sql\nSELECT NAME, SUM(QTY) FROM ZJTH.PRECEIPT GROUP BY NAME\n```")
                    return _Resp(
                        "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT GROUP BY NAME\n```"
                    )
                return await super().complete(messages, **kwargs)

        adapter = _FlakyAdapter(_ROWS, failTimes=1)
        service, llm, tokenUsage, _ = _buildStreamService(
            adapter=adapter, llm=_ChangingSqlStreamLlm(),
        )
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        sqlEvents = [payload["sql"] for evType, payload in events if evType == EVENT_SQL]
        # 首次下发 + 重试修正后补发，共 2 条且 SQL 不同
        assert len(sqlEvents) == 2
        assert sqlEvents[0] != sqlEvents[1]
        assert events[-1][0] == EVENT_DONE
        # 重试额外消耗已审计（nl2sql 重试记录在 chart 之前）
        assert [r["purpose"] for r in tokenUsage.records] == ["nl2sql", "nl2sql", "chart", "answer"]

    async def test_stream_answer_includes_recent_history(self) -> None:
        """1-5（流式）：回答流阶段 user prompt 注入最近对话历史。"""
        dto = _dto("本月销量")
        dto.history = [
            HistoryMessage(role="user", content="上月销量"),
            HistoryMessage(role="assistant", content="上月销量为 1000"),
        ]
        service, llm, _, _ = _buildStreamService(llm=_RecordingAnswerLlm())
        events = await _collect(service, dto)
        assert events[-1][0] == EVENT_DONE
        assert len(llm.answerUserPrompts) == 1
        answerUser = llm.answerUserPrompts[0]
        assert "查询结果" in answerUser
        assert "上月销量为 1000" in answerUser
        assert "上月销量" in answerUser

    async def test_stream_chunk_timeout_emits_error(self, monkeypatch) -> None:
        """回答流块间超时（LLM 挂起）转为结构化 error 事件，不无限占用连接。"""

        class _HangingStreamLlm(_StreamPipelineLlm):
            async def completeStream(self, messages, **kwargs):
                self.streamCalls += 1
                # 永不返回 → 触发块间超时；yield 保证其为 async generator（而非协程）
                await asyncio.Event().wait()
                yield StreamChunk(
                    content="", isDone=False, promptTokens=0, completionTokens=0, modelName="test-model",
                )  # 不可达，仅维持生成器形态

        import app.services.chat_stream_output as stream_output_module

        monkeypatch.setattr(stream_output_module, "_STREAM_CHUNK_TIMEOUT_SECONDS", 0.05)
        service, llm, tokenUsage, _ = _buildStreamService(llm=_HangingStreamLlm())
        session = _FakeSession()
        events = await _collect(service, _dto("各供应商的收货数量汇总"), session=session)
        assert events[-1][0] == EVENT_ERROR
        assert "超时" in events[-1][1]["error"]
        assert events[-1][1]["errorType"] == "llm"  # 4-1：块间超时即 LLM 挂起
        # 主模型未产出 token 时超时 → 与 LlmClientError 同等降级；无备选则上抛为 error 事件
        assert llm.streamCalls == 1
        assert not any(r["purpose"].startswith("fallback_") for r in tokenUsage.records)
        assert len(session.added) == 2  # 4-4：出错轮也持久化消息

    def test_stream_chunk_timeout_within_documented_range(self) -> None:
        """4-3：块间超时锁定在 30-45s 文档区间，防止回退到旧值 120s。"""
        import app.services.chat_stream_output as stream_output_module

        assert 30.0 <= stream_output_module._STREAM_CHUNK_TIMEOUT_SECONDS <= 45.0


class TestUnanswerablePlanStream:
    """计划 target=无法回答 时流式短路：plan + 友好回答 + done，无 sql/chart，不执行查询。"""

    async def test_short_circuits_without_sql_or_execution(self) -> None:
        service, llm, tokenUsage, embedding = _buildStreamService(
            llm=_UnanswerableStreamPipelineLlm()
        )
        events = await _collect(service, _dto("各业务线的销售额是多少？"))
        types = [e for e, _ in events]

        assert types[0] == EVENT_META
        # 2026-09-16 类召回诊断（e028ec6）：class_recall 先于执行计划下发
        assert types[1] == EVENT_CLASS_RECALL
        # 2026-08-16：单步不可答也下发执行计划事件（multi_step_plan → step_plan → plan → token → step_result）
        assert types[2] == EVENT_MULTI_STEP_PLAN
        assert events[2][1]["steps"][0]["description"] == "无法回答"
        assert types[3] == EVENT_STEP_PLAN
        assert types[4] == EVENT_PLAN
        assert events[4][1]["plan"]["target"] == "无法回答"
        assert types[5] == EVENT_TOKEN
        assert "无法回答" in events[5][1]["content"]
        # step_result 在 done 之前（带 error 字段）
        stepResultTypes = [t for t in types if t == EVENT_STEP_RESULT]
        assert len(stepResultTypes) == 1
        assert types[-1] == EVENT_DONE
        # 无 sql / chart 事件；SQL 生成与回答流均未调用
        assert EVENT_SQL not in types
        assert EVENT_CHART not in types
        assert llm.completeCalls == 1  # 仅计划阶段一次调用
        assert llm.streamCalls == 0  # 回答流未调用
        # 无查询向量存储、无 SQL 执行
        assert embedding.stored == []
        # 计划阶段 token 已计量并计费（按实际服务模型）
        assert events[-1][1]["tokensUsed"] == 15
        assert events[-1][1]["cost"] > 0
        assert [r["purpose"] for r in tokenUsage.records] == ["nl2sql"]


class TestAffinityStream:
    """Phase 7：流式 done 事件携带 affinityStatus；解锁/闲聊/领域命令不携带。"""

    async def test_query_stream_done_carries_affinity_status(self) -> None:
        """turnCount < N：done 事件携带 lockedModel + remainingTurns。"""
        from app.tests.unit.test_chat_service import _PipelineLlm  # noqa: F401
        from app.tests.unit.test_chat_service import _buildService

        # 走 _FakeTokenUsage 默认 turnCount=2、lastModelId=1，affinityTurns=3
        service, llm, tokenUsage, _ = _buildStreamService()
        # 注入 affinityTurns
        service._affinityTurns = 3  # type: ignore[attr-defined]
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        done = events[-1][1]
        assert "affinityStatus" in done
        assert done["affinityStatus"]["lockedModel"] == "test-model"
        assert done["affinityStatus"]["remainingTurns"] == 1  # 3 - 2 = 1

    async def test_query_stream_done_omits_when_unlocked(self) -> None:
        """turnCount >= N：done 不携带 affinityStatus。"""
        service, llm, tokenUsage, _ = _buildStreamService()
        service._affinityTurns = 3  # type: ignore[attr-defined]
        # 默认 turnCount=2 < 3，强制改成 3（解锁）
        from app.tests.unit.test_chat_service import _FakeTokenUsage
        # 替换 turnCount：直接覆盖 _FakeTokenUsage 实例
        service._tokenUsage.turnCount = 3  # type: ignore[attr-defined]
        events = await _collect(service, _dto("各供应商的收货数量汇总"))
        done = events[-1][1]
        assert "affinityStatus" not in done or done.get("affinityStatus") is None

    async def test_chitchat_stream_done_omits_affinity(self) -> None:
        """闲聊不走路由：done 不携带 affinityStatus。"""
        service, llm, tokenUsage, _ = _buildStreamService()
        service._affinityTurns = 3  # type: ignore[attr-defined]
        events = await _collect(service, _dto("你好"))
        done = events[-1][1]
        assert "affinityStatus" not in done or done.get("affinityStatus") is None

    async def test_domain_command_stream_done_omits_affinity(self) -> None:
        """领域命令零 LLM：done 不携带 affinityStatus。"""
        service, llm, tokenUsage, _ = _buildStreamService()
        service._affinityTurns = 3  # type: ignore[attr-defined]
        events = await _collect(service, _dto("/metric 销售额 = SUM(x)"))
        done = events[-1][1]
        assert "affinityStatus" not in done or done.get("affinityStatus") is None


class TestSingleStepExecutionPlanHelpers:
    """单步执行计划事件辅助（_singleStepOverview / _singleStepStart）。

    2026-08-16：单步也下发 multi_step_plan + step_plan，使前端 MultiStepPlanCard 始终
    渲染 1 步。本类覆盖事件 payload 的关键不变量（stepIndex/aggregationOnly/subQuestion）。
    """

    def test_overview_emits_single_step_overview_event(self) -> None:
        """_singleStepOverview：步骤索引=0，aggregationOnly 必须为 False（前端过滤要求）。"""
        evt = ChatService._singleStepOverview("执行查询", "查 2025 年供应商")
        assert evt.event == EVENT_MULTI_STEP_PLAN
        assert len(evt.data["steps"]) == 1
        step = evt.data["steps"][0]
        assert step["stepIndex"] == 0
        assert step["description"] == "执行查询"
        assert step["subQuestion"] == "查 2025 年供应商"
        assert step["aggregationOnly"] is False  # 必须 boolean（isStepPlanOverviewItem 过滤）

    def test_start_emits_step_plan_event(self) -> None:
        """_singleStepStart：步骤进入事件，subQuestion 必须保留原问题全文。"""
        evt = ChatService._singleStepStart("执行查询", "查 2025 年供应商")
        assert evt.event == EVENT_STEP_PLAN
        assert evt.data["stepIndex"] == 0
        assert evt.data["description"] == "执行查询"
        assert evt.data["subQuestion"] == "查 2025 年供应商"

    def test_unanswerable_description(self) -> None:
        """不可答场景：description="无法回答"，与正常路径区分。"""
        overview = ChatService._singleStepOverview("无法回答", "某用户问题")
        start = ChatService._singleStepStart("无法回答", "某用户问题")
        assert overview.data["steps"][0]["description"] == "无法回答"
        assert start.data["description"] == "无法回答"
