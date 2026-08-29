"""集成测试：ReceiptDetail「入库量/收货数量」场景（真实回归 2026-08-17）。

用户问题：问「今年 3、4、5 三个月入库量趋势，从 ReceiptDetail 类查询」时反馈
「选中的属性 收货数量 不属于选定的任何类；聚合属性 收货数量 不属于选定的任何类」。

根因：本体里 ReceiptDetail（PRECEIPTD）的数量列 QTYUOM_0 名为「库存数量」，
没有「收货数量」；「收货数量」只存在于 PurchaseOrderDetail / ArrivalNoticeDetail。
LLM 把「入库量」映射成「收货数量」，而 validatePlan 是类作用域校验，
ReceiptDetail 的属性引用集不含「收货数量」→ 两条报错。

修复：给 ReceiptDetail.库存数量 补业务别名 ["收货数量", "入库数量"]
（seed_ontology.py + 实时 DB 同步）。本测试锁定：
- 别名存在时，同一 plan 走完整 chat 链路校验通过（200）；
- 别名缺失时，复现原始报错（400 + 两条确切消息），防回归。

说明：为避免依赖 tiktoken 编码下载与真实外部调用，模型路由 / LLM / 适配器 /
embedding 均以 fake 注入（与 test_chat_api.py 同模式）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

import seed_ontology
from app.domain.models import DataSource, LlmConfig, OntologyClass, OntologyProperty
from app.infrastructure.security.crypto import encryptApiKey

ROWS = [{"RCPDAT": "2026-03-01", "TOTAL_QTY": Decimal(100)}]


class _PipelineLlm:
    """按 prompt 内容路由回复：计划 / SQL / 回答；记录每次调用消息。

    ReAct 第一阶段固定返回引用「收货数量」的计划（真实回归场景：
    LLM 把「入库量」映射成全库真实存在但不属于 ReceiptDetail 的属性名）。
    """

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

        if "解析为查询计划" in system:
            # selectedClasses=ReceiptDetail（合法类）+ 收货数量（仅在别名存在时属于该类）
            _Resp.content = (
                '{"target":"3、4、5 月入库量趋势对比（ReceiptDetail）",'
                '"selectedClasses":["ReceiptDetail"],'
                '"selectedProperties":["收货数量","收货日期"],'
                '"aggregations":[{"function":"SUM","property":"收货数量","alias":"TOTAL_QTY"}],'
                '"groupBy":["收货日期"]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT RCPDAT, SUM(QTYUOM_0) AS TOTAL_QTY FROM ZJTH.PRECEIPTD "
                "GROUP BY RCPDAT FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 1 条记录，入库量趋势如下。"
        return _Resp()


class _FakeAdapter:
    async def execute_read_only(self, sql: str) -> list[dict]:
        return ROWS


async def _seed(session, *, withQtyAlias: bool) -> tuple[LlmConfig, DataSource]:
    """写入模型配置、数据源与 ReceiptDetail 本体类，返回 (config, ds)。

    withQtyAlias=True：库存数量带业务别名 ["收货数量", "入库数量"]（修复后）。
    withQtyAlias=False：不带别名（修复前，复现原始报错）。
    """
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
    qty = OntologyProperty(
        property_name="库存数量",
        data_type="DECIMAL",
        source_column="QTYUOM_0",
        business_aliases=["收货数量", "入库数量"] if withQtyAlias else None,
    )
    cls = OntologyClass(
        class_name="ReceiptDetail",
        class_alias="收货明细",
        source_table="ZJTH.PRECEIPTD",
        properties=[
            qty,
            OntologyProperty(
                property_name="收货日期", data_type="DATETIME", source_column="RCPDAT_0"
            ),
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


def _chat_payload(question: str, datasourceId: int) -> dict:
    return {"sessionId": "s-receiptdetail", "question": question, "datasourceId": datasourceId}


_QUESTION = "今年3、4、5三个月的入库量趋势做个对比，需要从ReceiptDetail这个类查询"


class TestReceiptDetailQtyAlias:
    async def test_plan_referencing_shouhuo_qty_passes_when_alias_present(
        self, client, dbSession, monkeypatch
    ) -> None:
        """别名存在时：plan 引用「收货数量」→ 校验通过，完整链路返回 200。"""
        config, ds = await _seed(dbSession, withQtyAlias=True)
        _installFakes(monkeypatch, config)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["sql"] is not None
        assert body["answer"]

    async def test_plan_referencing_shouhuo_qty_rejected_without_alias(
        self, client, dbSession, monkeypatch
    ) -> None:
        """别名缺失时：复现原始报错（400 + 两条确切消息），防回归。"""
        config, ds = await _seed(dbSession, withQtyAlias=False)
        _installFakes(monkeypatch, config)

        resp = await client.post("/api/v1/chat", json=_chat_payload(_QUESTION, ds.id))

        assert resp.status_code == 400
        body = resp.json()
        assert "选中的属性 收货数量 不属于选定的任何类" in body["detail"]
        assert "聚合属性 收货数量 不属于选定的任何类" in body["detail"]

    async def test_seed_syncs_aliases_for_existing_property(
        self, dbSession, monkeypatch
    ) -> None:
        """seed 重跑对已存在属性增量同步业务别名（幂等可重放），别名改动不静默丢失。

        真实回归 2026-08-17：_seedProperties 的 existing 分支原只 continue 跳过，
        重跑 seed 时 PRECEIPTD.库存数量 新补的 aliases 不会落到 PG。本测试模拟
        「旧环境已存在无别名属性 + seed 已补别名」→ 重跑后别名被同步。
        """
        # Arrange：旧环境状态——库存数量 已存在但无别名
        cls = OntologyClass(
            class_name="ReceiptDetail",
            class_alias="收货明细",
            source_table="PRECEIPTD",
        )
        dbSession.add(cls)
        await dbSession.flush()
        dbSession.add(
            OntologyProperty(
                class_id=cls.id,
                property_name="库存数量",
                data_type="DECIMAL",
                source_column="QTYUOM_0",
                business_aliases=None,
            )
        )
        await dbSession.commit()

        # seed 里该属性已补别名；只喂 PRECEIPTD 一个表避免全量本体（执行快且聚焦）
        monkeypatch.setattr(
            seed_ontology,
            "PROPERTIES",
            {
                "PRECEIPTD": [
                    seed_ontology.P(
                        "库存数量", "QTYUOM_0", "DECIMAL", aliases=["收货数量", "入库数量"]
                    )
                ],
            },
        )

        # Act：重跑 seed 的属性阶段
        pid = await seed_ontology._seedProperties(dbSession, {"PRECEIPTD": cls.id})

        # Assert：别名被增量同步
        stmt = select(OntologyProperty).where(OntologyProperty.class_id == cls.id)
        prop = (await dbSession.execute(stmt)).scalar_one()
        assert prop.business_aliases == ["收货数量", "入库数量"]
        assert (cls.id, "库存数量") in pid
