"""`entity_mapping` 名称读取（2026-11-25 从待到货实现拆出，供多工具复用）。

`entity_mapping.name` 是主数据名称 SSOT：名称**不 JOIN 数仓字典**
（覆盖率不足），也**不按 `source_system` 过滤** —— 该列只描述「这条映射
来自哪个系统」，不是名称准入条件。实测生产库名称主要挂在 `ERP` 行
（`MDM` 行为 0），筛 `source_system = MDM` 会把名称全部筛成 None。

名称 / 企业键只有**一份**读取实现（I4）：待到货用 `loadMdmNames`，
到货跟踪用 `loadMdmDirectory`（多返回 `enterpriseKey`）。两处各行一份
SQL 曾各自漂移过（`source_system` 过滤与否）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import EntityMapping


async def loadMdmDirectory(
    session: AsyncSession, *, entityType: str, codes: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """按业务码批量取名称与企业键；无映射行 ⇒ 缺省（不臆造）。

    同一 code 多行时取「首个非空名称」（`name.asc().nulls_last()` 把空名排后）。
    返回 `{code: {"name": str | None, "enterpriseKey": ...}}`。
    """
    if not codes:
        return {}
    rows = (
        await session.execute(
            select(
                EntityMapping.enterprise_code,
                EntityMapping.name,
                EntityMapping.enterprise_key,
            )
            .where(
                EntityMapping.entity_type == entityType,
                EntityMapping.enterprise_code.in_(list(codes)),
            )
            .order_by(EntityMapping.name.asc().nulls_last())
        )
    ).all()
    directory: dict[str, dict[str, Any]] = {}
    for enterpriseCode, name, enterpriseKey in rows:
        code = str(enterpriseCode)
        current = directory.get(code)
        if current is None:
            directory[code] = {"name": name or None, "enterpriseKey": enterpriseKey}
        elif current["name"] is None and name:
            directory[code] = {"name": name, "enterpriseKey": enterpriseKey}
    return directory


async def loadMdmNames(
    session: AsyncSession, *, entityType: str, codes: tuple[str, ...]
) -> dict[str, str]:
    """按业务码批量取名称（供应商 / 物料）；无映射行 ⇒ 名称缺省（不臆造）。"""
    directory = await loadMdmDirectory(
        session, entityType=entityType, codes=codes
    )
    return {
        code: str(info["name"])
        for code, info in directory.items()
        if info["name"]
    }
