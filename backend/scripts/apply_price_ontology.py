"""把 PRI_0 业务别名/说明 + PORDERQ→PPRICLIST 直达 join 应用到运行时本体。

背景：seed_ontology._seedProperties 对已存在属性是 skip（不更新别名/说明），
所以 PRI_0 的新增元数据需经 OntologyService.updateProperty 定向落库；
join 目录走 seed_ontology.seed()（幂等，按 join_key 去重，补齐缺失边）。

用法：cd backend && uv run python scripts/apply_price_ontology.py
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
from app.domain.models import OntologyClass, OntologyProperty  # noqa: E402
from app.domain.schemas import OntologyPropertyUpdate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402


async def main() -> None:
    factory = getSessionFactory()
    service = OntologyService()

    async with factory() as session:
        cls = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "PPRICLIST")
            )
        ).scalar_one()
        prop = (
            await session.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == cls.id,
                    OntologyProperty.property_name == "单价",
                )
            )
        ).scalar_one()
        dto = OntologyPropertyUpdate(
            business_aliases=["报价", "供应商报价", "采购报价"],
            description="供应商报价单明细中的单价（含价格条件/生效失效日期约束，取数前须校验有效期）",
        )
        updated = await service.updateProperty(session, prop.id, dto)
        print(
            f"PRI_0 id={updated.id}: aliases={updated.business_aliases} "
            f"desc={'set' if updated.description else 'MISSING'}"
        )

        # Milvus 向量刷新：PRI_0 的 embedding 仍是旧文本（无别名/新说明），
        # 按 中文名+业务别名+说明 重建，使「报价」类语义检索能命中该属性。
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
        print(f"PRI_0 embedding 已刷新（文本: {text[:50]}…）")

    # join 目录幂等补齐（PORDERQ.ITMREF_0 → PPRICLIST.CPNITMREF_0）
    await seed_ontology.seed()
    print("seed 完成：join 目录已幂等补齐")


if __name__ == "__main__":
    asyncio.run(main())
