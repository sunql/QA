"""Phase 1.4 Chat × DataQuality 集成测试。

真实 PG + 完整 API 链路：
- 直接 INSERT data_quality_score 历史（模拟已跑过 compute 的真实业务库）
- chat 请求触发 selectedClasses=[PORDER] 的 NL2SQL pipeline
- 断言响应中 ChatResponse.dataQuality 含正确的 PORDER badge

覆盖：
- 已有 score：evaluated=True + overall_score 准确
- 无 score：evaluated=False badge（不阻断 chat）
- DQ service 异常：dataQuality=None + WARN 日志（chat 主链路 200）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text

from app.domain.enums import ScoreType
from app.domain.models import (
    DataQualityScore,
    LlmConfig,
    OntologyClass,
    OntologyProperty,
)


async def _insertScore(dbSession, *, target_table: str, overall: str = "76.84", rules_count: int = 5) -> None:
    score = DataQualityScore(
        target_table=target_table,
        score_type=ScoreType.TABLE,
        completeness_score=Decimal("84.21"),
        validity_score=Decimal("78.95"),
        uniqueness_score=Decimal("94.74"),
        consistency_score=Decimal("52.63"),
        timeliness_score=None,
        referential_score=Decimal("73.68"),
        overall_score=Decimal(overall),
        evaluated_at=datetime.now(timezone.utc),
        evaluation_duration_ms=22,
        rules_count=rules_count,
    )
    dbSession.add(score)
    await dbSession.commit()


async def _ensurePorderClass(dbSession) -> None:
    """确保 ontology_class + ontology_property 包含 PORDER（最小必要子集）。"""
    existing = (
        await dbSession.execute(text("SELECT id FROM ontology_class WHERE class_name='PORDER'"))
    ).first()
    if existing:
        return
    cls = OntologyClass(
        class_name="PORDER",
        class_alias="采购订单",
        source_table="PORDER",
        description="采购订单",
    )
    dbSession.add(cls)
    await dbSession.flush()
    dbSession.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="PONUM",
            property_alias="订单号",
            data_type="VARCHAR",
            source_column="PONUM",
        )
    )
    await dbSession.commit()


def _chat_payload(question: str, datasourceId: int, sessionId: str | None = None) -> dict:
    return {
        "sessionId": sessionId or f"s-{uuid.uuid4().hex[:8]}",
        "question": question,
        "datasourceId": datasourceId,
    }


def _makePipelineLlm(plan: dict, sql: str, answer: str = "ok"):
    """生成可控的 fake LLM，按 prompt 阶段返回 plan / sql / chart / answer。"""

    class _Llm:
        async def complete(self, messages, **kwargs):
            class _Resp:
                content = ""
                modelName = "test-model"
                promptTokens = 10
                completionTokens = 5

            user = messages[1].content
            system = messages[0].content
            if "图表类型" in user:
                _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1]}]}'
            elif "解析为查询计划" in system:
                _Resp.content = (
                    '{"target":"' + plan.get("target", "x") + '",'
                    '"selectedClasses":' + str(plan["selectedClasses"]).replace("'", '"') + ","
                    '"selectedProperties":' + str(plan["selectedProperties"]).replace("'", '"') + ","
                    '"groupBy":[]}'
                )
            elif "生成 SQL 时必须" in system:
                _Resp.content = "```sql\n" + sql + "\n```"
            else:
                _Resp.content = answer
            return _Resp()

    return _Llm()


class _StubEmbeddingService:
    async def embed(self, text):
        return [0.0] * 8

    async def storeQueryEmbedding(self, **kwargs) -> None:  # noqa: ARG002
        return None


class _FakeAdapter:
    async def execute_read_only(self, sql):
        return [{"PONUM": "PO00001"}]


class _RouterForConfig:
    """固定返回预置 LlmConfig 的假路由。"""

    def __init__(self, config) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx):  # noqa: ARG002
        return self._config

    def selectFallbackModel(self, configs, excludeId):  # noqa: ARG002
        return None


async def _seedLlmConfig(dbSession) -> LlmConfig:
    """确保 DB 里有至少一个 active LlmConfig。"""
    existing = (await dbSession.execute(select(LlmConfig))).first()
    if existing:
        return existing[0]
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    dbSession.add(config)
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


async def _setupChatFakes(monkeypatch, dbSession, *, llm, ds_id) -> None:
    """注入模型路由 + LLM + 适配器 + embedding fakes。"""
    config = await _seedLlmConfig(dbSession)
    import app.api.v1.chat as chatModule

    monkeypatch.setattr(chatModule._service, "_modelRouter", _RouterForConfig(config))
    monkeypatch.setattr(chatModule._service, "_llmFactory", lambda cfg: llm)
    monkeypatch.setattr(chatModule._service, "_adapterProvider", lambda dsId, ds: _FakeAdapter())
    monkeypatch.setattr(chatModule._service, "_embedding", _StubEmbeddingService())


class TestChatDataQualityIntegration:
    async def test_chat_response_includes_dq_badges_for_selected_classes(
        self, client, dbSession, monkeypatch
    ) -> None:
        """已评估过的表 → ChatResponse.dataQuality 含 evaluated=True badge 且分数准确。"""
        await _insertScore(dbSession, target_table="PORDER", overall="76.84", rules_count=5)
        await _ensurePorderClass(dbSession)

        from app.tests.integration.test_data_quality_api import _createTestDatasource

        ds_id = await _createTestDatasource(client)

        llm = _makePipelineLlm(
            plan={"target": "采购订单", "selectedClasses": ["PORDER"], "selectedProperties": ["PONUM"]},
            sql="SELECT PONUM FROM PORDER FETCH FIRST 10 ROWS ONLY",
        )
        await _setupChatFakes(monkeypatch, dbSession, llm=llm, ds_id=ds_id)

        resp = await client.post("/api/v1/chat", json=_chat_payload("采购订单号查询", ds_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["queryPlan"]["selectedClasses"] == ["PORDER"]
        assert "dataQuality" in body
        assert body["dataQuality"] is not None
        assert len(body["dataQuality"]) == 1
        badge = body["dataQuality"][0]
        assert badge["targetTable"] == "PORDER"
        assert badge["evaluated"] is True
        assert badge["overallScore"] == "76.84"
        assert badge["rulesCount"] == 5
        assert badge["evaluatedAt"] is not None

    async def test_chat_response_evaluates_false_when_table_not_scored(
        self, client, dbSession, monkeypatch
    ) -> None:
        """未评估过的表 → evaluated=False badge（其余分数字段 None）。"""
        await _ensurePorderClass(dbSession)

        from app.tests.integration.test_data_quality_api import _createTestDatasource

        ds_id = await _createTestDatasource(client)

        llm = _makePipelineLlm(
            plan={"target": "采购订单", "selectedClasses": ["PORDER"], "selectedProperties": ["PONUM"]},
            sql="SELECT PONUM FROM PORDER",
        )
        await _setupChatFakes(monkeypatch, dbSession, llm=llm, ds_id=ds_id)

        resp = await client.post("/api/v1/chat", json=_chat_payload("查询采购订单号", ds_id))
        body = resp.json()

        assert body["dataQuality"] is not None
        badge = body["dataQuality"][0]
        assert badge["targetTable"] == "PORDER"
        assert badge["evaluated"] is False
        assert badge["overallScore"] is None
        assert badge["evaluatedAt"] is None
        assert badge["rulesCount"] is None

    async def test_chat_silently_degrades_when_dq_service_raises(
        self, client, dbSession, monkeypatch
    ) -> None:
        """DQ service 抛异常 → chat 主链路仍 200，dataQuality=None，不挂。"""
        await _ensurePorderClass(dbSession)

        from app.tests.integration.test_data_quality_api import _createTestDatasource
        from app.services.data_quality_score_service import DataQualityScoreService

        ds_id = await _createTestDatasource(client)

        class _ExplodingDqService(DataQualityScoreService):
            async def getLatestTableScores(self, session, target_tables):
                raise RuntimeError("simulated DQ outage")

        llm = _makePipelineLlm(
            plan={"target": "采购订单", "selectedClasses": ["PORDER"], "selectedProperties": ["PONUM"]},
            sql="SELECT PONUM FROM PORDER",
        )
        await _setupChatFakes(monkeypatch, dbSession, llm=llm, ds_id=ds_id)
        # 必须在 _setupChatFakes 之后注入（_setupChatFakes 不会动 dqScoreService）；
        # 但 chat_service 的 _buildDataQualityBadges 走 lazy import 默认实例，
        # 因此直接 monkeypatch 默认实例的同名方法。
        import app.api.v1.chat as chatModule
        monkeypatch.setattr(chatModule._service, "_dqScoreService", _ExplodingDqService())

        resp = await client.post("/api/v1/chat", json=_chat_payload("查询采购订单号", ds_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dataQuality"] is None