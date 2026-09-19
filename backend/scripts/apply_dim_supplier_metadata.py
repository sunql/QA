"""修复 DIM_SUPPLIER 元数据可读性 + 收敛 DWD_BUSINESS_PARTNER 类描述。

背景（2026-09-18 诊断）：问「公司有多少供应商」时，LLM 概率性倒向
DWD_BUSINESS_PARTNER 而非 DIM_SUPPLIER：
- DIM_SUPPLIER 229 列全是 Sage X3 裸物理码（BPSNUM_0…），无别名/业务别名/说明，
  LLM 读不懂 → schema prompt 里「可读的表」只剩 DWD_BUSINESS_PARTNER
  （8 列 PARTNER_CODE/PARTNER_NAME…，类描述还自夸「是供应商和客户的汇总数据」）。

本脚本（幂等，可重复执行）：
1. 经 OntologyService.updateProperty 给 DIM_SUPPLIER 核心列补中文别名
   （别名渲染进 schema prompt 列名位 `name (alias)`，同时进入合法引用名集合）。
2. 经 OntologyService.updateClass 收敛 DWD_BUSINESS_PARTNER 类描述，
   明示「统计供应商主数据优先 DIM_SUPPLIER」。

同步链路：updateProperty/updateClass 自动写 audit + Neo4j 节点 + Milvus 类向量
（best-effort 后台任务），三库一致，无需额外对账。

用法：cd backend && uv run python scripts/apply_dim_supplier_metadata.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Iterable

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
if env.exists():  # 宿主机布局；容器内 SECRET_KEY 已由 compose 注入环境
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app.dependencies import CurrentUser  # noqa: E402
from app.domain.models import OntologyClass, OntologyProperty  # noqa: E402
from app.domain.schemas import OntologyClassUpdate, OntologyPropertyUpdate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.acl_service import ADMIN_ROLE  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

SUPPLIER_SOURCE_TABLE = "DIM_SUPPLIER"
PARTNER_CLASS_NAME = "DWD_BUSINESS_PARTNER"

# DIM_SUPPLIER（Sage X3 BPSUPPLIER 口径）核心列 → 中文别名。
# 仅收录语义确定的列；UPDTICK_0（乐观锁）等运维列不设别名。
ALIAS_MAP: dict[str, str] = {
    "BPSNUM_0": "供应商编号",
    "BPSNAM_0": "供应商名称",
    "BPSSHO_0": "供应商简称",
    "BPSTYP_0": "供应商类型",
}

PARTNER_DESCRIPTION = (
    "合作伙伴宽表（供应商与客户的统一视图）。统计供应商主数据"
    "（数量、名称、编号）请优先使用 DIM_SUPPLIER；"
    "仅当需要伙伴统一编码、税号、币种等信息时使用本表。"
)


async def apply(
    session,
    service: OntologyService,
    *,
    actor: CurrentUser,
) -> dict:
    """幂等应用元数据修复，返回摘要供调用方/测试断言。

    摘要结构：
    - aliased: 本轮实际补了别名的列名
    - skipped: 别名已正确的列名（幂等跳过）
    - missing: 目标列在本体中不存在（记入摘要继续处理，不中断）
    - descriptionUpdated: 类描述本轮是否实际更新
    """
    aliased: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []

    dim = (
        await session.execute(
            select(OntologyClass).where(OntologyClass.source_table == SUPPLIER_SOURCE_TABLE)
        )
    ).scalars().first()
    if dim is not None:
        props = (
            await session.execute(
                select(OntologyProperty).where(OntologyProperty.class_id == dim.id)
            )
        ).scalars().all()
        propsByName = {p.property_name: p for p in props}
        for propName, alias in ALIAS_MAP.items():
            prop = propsByName.get(propName)
            if prop is None:
                missing.append(propName)
                continue
            if prop.property_alias == alias:
                skipped.append(propName)
                continue
            await service.updateProperty(
                session,
                prop.id,
                OntologyPropertyUpdate(property_alias=alias),
                actor=actor.userId,
                actor_departments=actor.departments and ",".join(actor.departments) or None,
            )
            aliased.append(propName)
    else:
        missing.extend(ALIAS_MAP)

    descriptionUpdated = False
    partner = (
        await session.execute(
            select(OntologyClass).where(OntologyClass.class_name == PARTNER_CLASS_NAME)
        )
    ).scalars().first()
    if partner is not None and partner.description != PARTNER_DESCRIPTION:
        await service.updateClass(
            session,
            partner.id,
            OntologyClassUpdate(description=PARTNER_DESCRIPTION),
            actor=actor,
        )
        descriptionUpdated = True

    return {
        "aliased": aliased,
        "skipped": skipped,
        "missing": missing,
        "descriptionUpdated": descriptionUpdated,
    }


def _formatSummary(summary: dict) -> Iterable[str]:
    yield f"aliased={summary['aliased']}"
    yield f"skipped={summary['skipped']}"
    yield f"missing={summary['missing']}"
    yield f"descriptionUpdated={summary['descriptionUpdated']}"


async def main() -> None:
    factory = getSessionFactory()
    service = OntologyService()
    actor = CurrentUser(userId="ops-script", roles=(ADMIN_ROLE,))
    async with factory() as session:
        summary = await apply(session, service, actor=actor)
    for line in _formatSummary(summary):
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
