"""对话接口集成测试。

验证 HTTP 契约：
- POST /api/v1/chat 完整流水线返回 camelCase 响应（answer/sql/chartType/chartOption/data/tokensUsed/cost）
- 闲聊返回问候、无 SQL、不消耗 Token
- 数据源不存在返回 404 错误包络
- 参数校验失败返回 422

说明：为避免依赖 tiktoken 编码下载与真实外部调用，模型路由与 LLM/适配器均以 fake 注入。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.domain.models import DataSource, LlmConfig, OntologyClass, OntologyProperty
from app.infrastructure.security.crypto import encryptApiKey

ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]


class _PipelineLlm:
    """按 prompt 内容路由回复：计划 / SQL / 图表 JSON / 回答；记录每次调用消息。"""

    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> object:
        self.calls.append([(m.role, m.content) for m in messages])
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            # ReAct 第一阶段：返回对本体 PRECEIPT 类合法的计划（NAME/QTY 属性存在）
            _Resp.content = (
                '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"],'
                '"selectedProperties":["NAME","QTY"],"groupBy":["NAME"]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 2 条记录，各供应商收货量分布如下。"
        return _Resp()


class _FakeAdapter:
    async def execute_read_only(self, sql: str) -> list[dict]:
        return ROWS


async def _seed(session) -> tuple[LlmConfig, DataSource]:
    """写入模型配置、数据源与本体类，返回 (config, ds)。"""
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    ds = DataSource(
        name="ZJTH",
        type="oracle",
        host="h",
        port=1521,
        database_name="svc",
        username="u",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
        is_default=True,
    )
    cls = OntologyClass(
        class_name="PRECEIPT",
        class_alias="收货单",
        source_table="ZJTH.PRECEIPT",
        properties=[
            OntologyProperty(property_name="NAME", data_type="STRING", source_column="NAME"),
            OntologyProperty(property_name="QTY", data_type="DECIMAL", source_column="QTY"),
        ],
    )
    session.add_all([config, ds, cls])
    await session.commit()
    await session.refresh(config)
    await session.refresh(ds)
    return config, ds


class _RouterFor:
    """固定返回指定模型配置的假路由。"""

    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None


class _StubEmbeddingService:
    """fire-and-forget 存储占位：不触达真实 embedding API。"""

    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None


def _installFakes(monkeypatch, config: LlmConfig) -> None:
    """用 fake 替换 ChatService 的模型路由 / LLM 工厂 / 适配器 / embedding。"""
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: _PipelineLlm())
    monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbeddingService())


def _chat_payload(question: str, datasourceId: int = 1, sessionId: str = "s1") -> dict:
    return {"sessionId": sessionId, "question": question, "datasourceId": datasourceId}


class TestChatApi:
    async def test_full_pipeline_returns_camel_case(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)

        resp = await client.post(
            "/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "query"
        assert "PRECEIPT" in body["sql"]
        assert body["chartType"] == "pie"
        assert body["chartOption"] is not None
        assert body["chartOption"]["series"][0]["type"] == "bar"
        assert len(body["data"]) == 2
        assert body["data"][0]["NAME"] == "A"
        assert body["tokensUsed"] == 60  # 计划/校验 + SQL + 图表 + 回答 = 4 次调用 × 15
        assert body["cost"] > 0
        assert body["modelName"] == "test-model"  # camelCase 输出实际使用的大模型名称
        # ReAct 计划随响应返回（前端展示用）
        assert body["queryPlan"] is not None
        assert body["queryPlan"]["selectedClasses"] == ["PRECEIPT"]
        assert body["queryPlan"]["groupBy"] == ["NAME"]
        # 无密码/敏感字段泄漏
        assert "password" not in body

    async def test_full_pipeline_records_usage_rows(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)
        await client.post("/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id))

        from app.domain.models import SessionTokenUsage

        result = await dbSession.execute(select(SessionTokenUsage))
        rows = list(result.scalars().all())
        assert len(rows) == 3
        assert sorted(r.purpose for r in rows) == ["answer", "chart", "nl2sql"]
        by_purpose = {r.purpose: r for r in rows}
        # ReAct 计划 + SQL 生成两次调用合并到 nl2sql 用途
        assert by_purpose["nl2sql"].prompt_tokens == 20
        assert by_purpose["nl2sql"].completion_tokens == 10
        assert by_purpose["nl2sql"].total_tokens == 30
        for purpose in ("answer", "chart"):
            assert by_purpose[purpose].prompt_tokens == 10
            assert by_purpose[purpose].completion_tokens == 5
            assert by_purpose[purpose].total_tokens == 15

    async def test_query_persists_session_messages(self, client, dbSession, monkeypatch) -> None:
        config, ds = await _seed(dbSession)
        _installFakes(monkeypatch, config)
        await client.post("/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id))

        from app.domain.models import SessionMessage

        result = await dbSession.execute(select(SessionMessage).order_by(SessionMessage.id))
        rows = list(result.scalars().all())
        assert len(rows) == 2
        assert [r.role for r in rows] == ["user", "assistant"]
        assert rows[0].content == "各供应商的收货数量汇总"
        assert rows[1].sql_generated is not None

    async def test_follow_up_query_injects_stored_context(self, client, dbSession, monkeypatch) -> None:
        import app.api.v1.chat as chat_module

        config, ds = await _seed(dbSession)
        llm = _PipelineLlm()
        monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
        monkeypatch.setattr(chat_module._service, "_llmFactory", lambda c: llm)
        monkeypatch.setattr(chat_module._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())

        await client.post("/api/v1/chat", json=_chat_payload("各供应商的收货数量汇总", ds.id))
        llm.calls.clear()  # 仅保留第二轮调用记录
        await client.post("/api/v1/chat", json=_chat_payload("本月销量如何", ds.id))
        systemContent = llm.calls[0][0][1]
        assert "以下是用户之前的对话历史" in systemContent
        assert "用户：各供应商的收货数量汇总" in systemContent
        assert "助手：查询完成" in systemContent

    async def test_chitchat_returns_greeting_without_sql(self, client, dbSession) -> None:
        resp = await client.post("/api/v1/chat", json=_chat_payload("你好"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["intent"] == "chitchat"
        assert "智能问答助手" in body["answer"]
        assert body["sql"] is None
        assert body["chartType"] is None
        assert body["tokensUsed"] == 0
        assert body["cost"] == 0
        assert body["modelName"] is None  # 闲聊不调用模型
        # 4-4：闲聊轮也持久化消息（user + assistant），历史链不断
        from sqlalchemy import select

        from app.domain.models import SessionMessage

        msgs = list((await dbSession.execute(select(SessionMessage))).scalars().all())
        assert len(msgs) == 2
        assert {m.role for m in msgs} == {"user", "assistant"}

    async def test_datasource_not_found_returns_404(self, client) -> None:
        resp = await client.post(
            "/api/v1/chat", json=_chat_payload("查询各供应商收货数量", 99999)
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["success"] is False
        assert "不存在" in body["error"]

    async def test_empty_question_returns_422(self, client) -> None:
        resp = await client.post(
            "/api/v1/chat", json={"sessionId": "s1", "question": "", "datasourceId": 1}
        )
        assert resp.status_code == 422


class _FakeSuggestService:
    """假相似问答检索服务，供 /suggest 集成测试注入。"""

    def __init__(self, suggestions: list | None = None) -> None:
        self.suggestions = suggestions if suggestions is not None else []
        self.calls: list[dict] = []

    async def searchSimilarQueries(self, question: str, *, topK: int = 5, datasourceId: int | None = None):
        self.calls.append({"question": question, "topK": topK, "datasourceId": datasourceId})
        return self.suggestions


class TestSuggestApi:
    async def test_suggest_returns_camel_case_suggestions(self, client, monkeypatch) -> None:
        import app.api.v1.chat as chat_module

        from app.domain.schemas import SimilarQuery

        fake = _FakeSuggestService([
            SimilarQuery(question="各供应商的收货数量汇总", sql="SELECT ...", similarity=0.9),
        ])
        monkeypatch.setattr(chat_module, "_embeddingService", fake)

        resp = await client.post("/api/v1/chat/suggest", json={"question": "供应商收货数量"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["suggestions"][0]["question"] == "各供应商的收货数量汇总"
        assert body["suggestions"][0]["sql"] == "SELECT ..."
        assert body["suggestions"][0]["similarity"] == 0.9
        assert fake.calls[0]["datasourceId"] is None

    async def test_suggest_passes_datasource_id(self, client, monkeypatch) -> None:
        import app.api.v1.chat as chat_module

        fake = _FakeSuggestService()
        monkeypatch.setattr(chat_module, "_embeddingService", fake)
        await client.post(
            "/api/v1/chat/suggest", json={"question": "库存", "datasourceId": 2}
        )
        assert fake.calls[0]["datasourceId"] == 2

    async def test_suggest_returns_empty_when_no_hits(self, client, monkeypatch) -> None:
        import app.api.v1.chat as chat_module

        monkeypatch.setattr(chat_module, "_embeddingService", _FakeSuggestService([]))
        resp = await client.post("/api/v1/chat/suggest", json={"question": "不存在的语义"})
        assert resp.status_code == 200
        assert resp.json()["suggestions"] == []

    async def test_suggest_empty_question_returns_422(self, client) -> None:
        resp = await client.post("/api/v1/chat/suggest", json={"question": ""})
        assert resp.status_code == 422
