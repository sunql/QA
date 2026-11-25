"""`entity_mapping` 名称读取（2026-11-25 从待到货实现拆出，供多工具复用）。

`entity_mapping.name` 是主数据名称 SSOT：名称**不 JOIN 数仓字典**
（覆盖率不足），也**不按 `source_system` 过滤** —— 该列只描述「这条映射
来自哪个系统」，不是名称准入条件。实测生产库名称主要挂在 `ERP` 行
（`MDM` 行为 0），筛 `source_system = MDM` 会把名称全部筛成 None。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import EntityMapping


async def loadMdmNames(
    session: AsyncSession, *, entityType: str, codes: tuple[str, ...]
) -> dict[str, str]:
    """按业务码批量取名称（供应商 / 物料）；无映射行 ⇒ 名称缺省（不臆造）。

    同一 code 多行时取「首个非空名称」（`name.asc().nulls_last()` 把空名排后）。
    """
    if not codes:
        return {}
    rows = (
        await session.execute(
            select(EntityMapping.enterprise_code, EntityMapping.name)
            .where(
                EntityMapping.entity_type == entityType,
                EntityMapping.enterprise_code.in_(list(codes)),
            )
            .order_by(EntityMapping.name.asc().nulls_last())
        )
    ).all()
    names: dict[str, str] = {}
    for enterpriseCode, name in rows:
        code = str(enterpriseCode)
        if code not in names and name:
            names[code] = name
    return names
