"""修正 live DB 的 PPRICLIST 物料列映射：CPNITMREF_0（空列）→ PLICRI2_0（真物料列）。

数据实证：CPNITMREF_0 全表恒为空格，join PORDERQ.ITMREF_0 命中 0 行；
PLICRI2_0 命中 18888 物料 / 601 万行。故：
1. 把既有「价格条件3」(PLICRI2_0) 属性改造成「物料编码」：改名 + fk=ITMMASTER + 别名/说明；
2. 删除错误的「物料编号」(CPNITMREF_0) 属性及其 PG/Neo4j/Milvus 记录；
3. 删除两条过期 join 边：PPRICLIST.CPNITMREF_0→ITMMASTER、PORDERQ→PPRICLIST.CPNITMREF_0
   （seed 只增不删，必须显式清）；
4. 重跑 seed：物化正确的 PPRICLIST.PLICRI2_0→ITMMASTER.ITMREF_0 FK 边与
   PORDERQ.ITMREF_0→PPRICLIST.PLICRI2_0 直达边；
5. 刷新「物料编码」的 Milvus 向量（含新别名/说明文本）。

用法：cd backend && uv run python scripts/apply_price_join_fix.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy import select

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

import seed_ontology  # noqa: E402
from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty  # noqa: E402
from app.domain.schemas import OntologyPropertyUpdate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402


async def main() -> None:
    factory = getSessionFactory()
    service = OntologyService()

    async with factory() as session:
        ppr = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "PPRICLIST")
            )
        ).scalar_one()
        itm = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "ITMMASTER")
            )
        ).scalar_one()
        props = (
            await session.execute(
                select(OntologyProperty).where(OntologyProperty.class_id == ppr.id)
            )
        ).scalars().all()

        cond3 = next((p for p in props if p.property_alias == "PLICRI2_0"), None)
        wrong = next((p for p in props if p.property_alias == "CPNITMREF_0"), None)
        if cond3 is None or wrong is None:
            raise RuntimeError(
                f"前置缺失: PLICRI2_0 属性={cond3 is not None}, CPNITMREF_0 属性={wrong is not None}"
            )
        print(f"改造前: 价格条件3(id={cond3.id}, alias={cond3.property_alias}, name={cond3.property_name})")
        print(f"删除前: 物料编号(id={wrong.id}, alias={wrong.property_alias}, name={wrong.property_name})")

    # 1) 改造 PLICRI2_0 属性为 物料编码 + FK
    async with factory() as session:
        dto = OntologyPropertyUpdate(
            property_name="物料编码",
            is_foreign_key=True,
            ref_class_id=itm.id,
            business_aliases=["价格条件3", "物料编号"],
            description="价格条件3列在本库实际存放物料编码（实证：CPNITMREF_0 恒为空格），接 PORDERQ.ITMREF_0 取报价/比价",
        )
        updated = await service.updateProperty(session, cond3.id, dto)
        print(f"改造后: 物料编码(id={updated.id}) alias={updated.property_alias} fk={updated.is_foreign_key}")

        # 1b) Milvus 向量刷新
        text = " ".join(
            filter(
                None,
                [
                    updated.property_name,
                    *(updated.business_aliases or []),
                    updated.description or "",
                ],
            )
        )
        embedding = await service._ensureEmbedding().generateEmbedding(text)
        service.syncEmbedding(
            ontologyId=updated.id,
            type="property",
            name=updated.property_name,
            alias=updated.property_alias,
            description=updated.description,
            embedding=embedding,
        )
        print(f"物料编码 embedding 已刷新: {text[:40]}…")

    # 2) 删除错误的 CPNITMREF_0 属性（含 Neo4j 节点 + Milvus 向量）
    async with factory() as session:
        await service.deleteProperty(session, wrong.id)
        print(f"已删除错误属性 物料编号(id={wrong.id})")

    # 3) 删除两条过期 join 边（seed 只增不删，须显式清）
    async with factory() as session:
        all_joins = (
            await session.execute(select(OntologyJoin))
        ).scalars().all()
        doomed = [
            j for j in all_joins
            if (
                # PPRICLIST.CPNITMREF_0 → ITMMASTER.ITMREF_0（错误 FK 物化边）
                j.source_class_id == ppr.id and j.source_columns == ["CPNITMREF_0"]
            )
            or (
                # PORDERQ.ITMREF_0 → PPRICLIST.CPNITMREF_0（上一轮错误直达边）
                j.target_class_id == ppr.id and j.target_columns == ["CPNITMREF_0"]
            )
        ]
        for j in doomed:
            await service.deleteJoin(session, j.id)
            print(f"已删除过期 join id={j.id} {j.source_columns}->{j.target_columns}")

    # 4) 重跑 seed：物化正确 FK 边与直达边（幂等）
    await seed_ontology.seed()
    print("seed 完成：PLICRI2_0 FK 边 + PORDERQ→PPRICLIST.PLICRI2_0 直达边已物化")


if __name__ == "__main__":
    asyncio.run(main())
