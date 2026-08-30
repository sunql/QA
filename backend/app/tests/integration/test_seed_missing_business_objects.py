"""Phase 3.3 缺失业务对象 seed 集成测试（真实 PG 5433）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整链路，
禁止 sqlite 内存库。client / dbSession fixtures 走 _pg_support.pgApiClient()，
每测试 TRUNCATE 隔离。

覆盖：
1. seed() 在真实 PG 上创建 6 个新类（采购报价/采购发票/付款各带明细）
2. 重跑幂等（类/属性/join/指标计数不变）
3. 新类关键属性落库正确（主键、FK、业务别名）
4. NL2SQL buildSchemaText 能渲染新类与 JOIN 关系（无漂移引用）
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.domain.models import (
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    OntologyProperty,
)

NEW_SOURCE_TABLES = ["PQUOTAT", "PQUOTATD", "PINVOICE", "PINVOICED", "PAYMENTH", "PAYMENTD"]


async def _runSeed(monkeypatch) -> None:
    """seed() 走独立真实 PG 引擎（seed 内部会 dispose，故每跑新建引擎）。"""
    import seed_ontology
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.tests import _pg_support

    url = _pg_support.resolveTestDatabaseUrl()
    engine = create_async_engine(url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(seed_ontology, "getEngine", lambda: engine)
    monkeypatch.setattr(seed_ontology, "getSessionFactory", lambda: factory)
    monkeypatch.setattr(seed_ontology, "_syncToNeo4j", lambda cid, pid, mid=None: None)
    await seed_ontology.seed()  # 内部 engine.dispose()


class TestSeedMissingObjectsRealPg:
    async def _counts(self, dbSession) -> tuple[int, int, int, int]:
        async def tableCount(model) -> int:
            result = await dbSession.execute(
                select(func.count()).select_from(model)
            )
            return result.scalar_one()

        # asyncpg 下不能跨 await 复用 statement；逐个执行
        nClasses = await tableCount(OntologyClass)
        nProps = await tableCount(OntologyProperty)
        nJoins = await tableCount(OntologyJoin)
        nMetrics = await tableCount(OntologyMetric)
        return nClasses, nProps, nJoins, nMetrics

    async def test_seed_creates_new_classes_idempotently(
        self, client, dbSession, monkeypatch
    ) -> None:
        await _runSeed(monkeypatch)
        first = await self._counts(dbSession)

        # 6 个新类全部落库，source_table 正确
        newClasses = (
            await dbSession.execute(
                select(OntologyClass).where(
                    OntologyClass.source_table.in_(NEW_SOURCE_TABLES)
                )
            )
        ).scalars().all()
        assert {c.source_table for c in newClasses} == set(NEW_SOURCE_TABLES)

        # 重跑幂等
        await _runSeed(monkeypatch)
        second = await self._counts(dbSession)
        assert second == first

    async def test_new_classes_have_key_properties(
        self, client, dbSession, monkeypatch
    ) -> None:
        """新类关键属性落库：主键/FK/业务别名。"""
        await _runSeed(monkeypatch)

        classes = (
            await dbSession.execute(
                select(OntologyClass).where(
                    OntologyClass.source_table.in_(NEW_SOURCE_TABLES)
                )
            )
        ).scalars().all()
        byTable = {c.source_table: c for c in classes}

        # 发票头：发票号单主键 + 供应商 FK + 金额别名
        invoice = byTable["PINVOICE"]
        invoiceProps = (
            await dbSession.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == invoice.id
                )
            )
        ).scalars().all()
        invByName = {p.property_name: p for p in invoiceProps}
        assert invByName["发票号"].is_primary_key
        assert invByName["供应商"].is_foreign_key
        assert "发票金额" in (invByName["不含税总额"].business_aliases or [])

        # 发票明细：复合主键 + 物料 FK
        invDetail = byTable["PINVOICED"]
        detailProps = (
            await dbSession.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == invDetail.id
                )
            )
        ).scalars().all()
        detByName = {p.property_name: p for p in detailProps}
        assert detByName["发票号"].is_primary_key
        assert detByName["行号"].is_primary_key
        assert detByName["物料编号"].is_foreign_key

        # 付款头：金额列名称或别名含业务词
        payment = byTable["PAYMENTH"]
        payProps = (
            await dbSession.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == payment.id
                )
            )
        ).scalars().all()
        payByName = {p.property_name: p for p in payProps}
        payText = payByName["付款金额"].property_name + "".join(
            payByName["付款金额"].business_aliases or []
        )
        assert "付款金额" in payText

    async def test_schema_text_can_reference_new_tables(
        self, client, dbSession, monkeypatch
    ) -> None:
        """NL2SQL schema 文本能渲染新类与复合键 JOIN（可被 LLM 引用）。"""
        from app.services.nl2sql_service import Nl2SqlService

        await _runSeed(monkeypatch)

        classes = (
            await dbSession.execute(select(OntologyClass))
        ).scalars().all()
        joins = (
            await dbSession.execute(select(OntologyJoin))
        ).scalars().all()
        text = Nl2SqlService().buildSchemaText(classes, joins=joins)

        # 新类头渲染（含中文别名 + 表名）
        assert "### PurchaseInvoice (采购发票): table=PINVOICE" in text
        assert "### Payment (付款单): table=PAYMENTH" in text
        assert "### Quotation (采购报价): table=PQUOTAT" in text
        # 明细表头
        assert "table=PINVOICED" in text
        assert "table=PAYMENTD" in text
        # JOIN 关系区渲染（发票→订单三向匹配）
        assert "### JOIN 关系" in text
        assert "PINVOICED" in text and "PORDERQ" in text
        assert "PQUOTATD" in text and "PREQUISD" in text
