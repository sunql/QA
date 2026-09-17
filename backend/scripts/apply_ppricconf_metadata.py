"""修复 PPRICCONF 类元数据 + 清理残留原始属性（一次性数据修复）。

背景：seed_ontology 按 source_table 复用已有类，PPRICCONF 类早已存在（id=30，
class_name/class_alias='PPRICCONF'，description='含税价格说明'，created_by=None），
seed 只补了 22 个建模属性 + 2 条 join，未覆盖类名/别名/说明，且残留 1 条原始
重复属性（PLI_0 <- PLI_0，非主键无别名，与新增的 价格表号 重复）。

本脚本：
1. 经 OntologyService.updateClass 修正为友好名/别名/说明（原地更新，id 稳定，
   Neo4j 按原 id 重同步）。
2. 经 deleteProperty 删除残留原始 PLI_0 属性（PG + Neo4j + Milvus 同步清理）。

用法：cd backend && uv run python scripts/apply_ppricconf_metadata.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.models import OntologyClass, OntologyProperty  # noqa: E402
from app.domain.schemas import OntologyClassUpdate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.acl_service import ADMIN_ROLE  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402


async def main() -> None:
    factory = getSessionFactory()
    service = OntologyService()
    async with factory() as session:
        cls = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "PPRICCONF")
            )
        ).scalar_one()
        dto = OntologyClassUpdate(
            class_name="SupplierPriceConf",
            class_alias="供应商价格配置",
            description="供应商价格配置表，定义价格清单的取价条件维度（供应商/物料等字段组合）、取价优先级与价格类型。",
        )
        # 运维脚本走 admin 角色执行（updateClass 现收完整 CurrentUser 做 ACL）
        updated = await service.updateClass(
            session, cls.id, dto,
            actor=CurrentUser(userId="ops-script", roles=(ADMIN_ROLE,)),
        )
        print(f"class id={updated.id} name={updated.class_name} alias={updated.class_alias}")

        # 删除残留原始重复属性（property_name == 物理列名、非主键、无业务别名）
        dup = (
            await session.execute(
                select(OntologyProperty).where(
                    OntologyProperty.class_id == cls.id,
                    OntologyProperty.property_name == "PLI_0",
                    OntologyProperty.is_primary_key.is_(False),
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            await service.deleteProperty(session, dup.id)
            print(f"删除残留重复属性 id={dup.id} PLI_0")
        else:
            print("无残留重复 PLI_0 属性，跳过")

    # 校验结果
    async with factory() as session:
        c = (
            await session.execute(
                select(OntologyClass).where(OntologyClass.source_table == "PPRICCONF")
            )
        ).scalar_one()
        props = (
            await session.execute(
                select(OntologyProperty)
                .where(OntologyProperty.class_id == c.id)
                .order_by(OntologyProperty.id)
            )
        ).scalars().all()
        print(f"校验: name={c.class_name} alias={c.class_alias} properties={len(props)}")
        print("  " + ", ".join(p.property_name for p in props[:5]) + " ...")


if __name__ == "__main__":
    asyncio.run(main())
