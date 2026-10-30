"""数据域 + 数据层规则种子（feat-data-domain-layer）。

- data_domain：3 行（PROCUREMENT/QUALITY/LOGISTICS），主管理组织按 organizations.code
  尽力解析（缺失置 NULL）。
- data_layer_rule：7 行，与迁移 0134 seed 及 data_layer_defaults.DEFAULT_LAYER_RULES
  三方逐字对齐（迁移管 prod 首装；本脚本管测试库 truncate 后重建 + 幂等兜底）。

幂等：全部 ON CONFLICT DO NOTHING / NOT EXISTS 守卫，可重放。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataDomain, DataLayerRule

_DOMAIN_ROWS = (
    {"code": "PROCUREMENT", "name": "采购域", "org_code": "procurement",
     "description": "采购/供应商/到货等主数据与业务过程"},
    {"code": "QUALITY", "name": "质量域", "org_code": "quality",
     "description": "来料检验/不合格处理等质量过程"},
    {"code": "LOGISTICS", "name": "物流域", "org_code": None,
     "description": "仓储/运输/库存等物流过程"},
)

# (pattern, layer, priority)；与 0134 迁移 _SEED_RULES / DEFAULT_LAYER_RULES 对齐
_LAYER_RULES = (
    ("ODS_DIM_", "ODS_DICT", 10),
    ("ADS_", "ADS", 20),
    ("DWS_", "DWS", 30),
    ("DWD_", "DWD", 40),
    ("DIM_", "DIM", 50),
    ("ODS_", "ODS_BUSINESS", 60),
    (None, "FEATURE", 90),
)


async def _orgIdByCode(session: AsyncSession, code: str | None) -> int | None:
    if code is None:
        return None
    row = (
        await session.execute(
            text("SELECT id FROM organizations WHERE code = :code"), {"code": code}
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None


async def seedDataDomains(session: AsyncSession) -> int:
    """幂等 seed 数据域；返回新增行数。"""
    inserted = 0
    for row in _DOMAIN_ROWS:
        orgId = await _orgIdByCode(session, row["org_code"])
        stmt = pg_insert(DataDomain).values(
            code=row["code"],
            name=row["name"],
            primary_org_id=orgId,
            description=row["description"],
            enabled=True,
            created_by="seed-data-domain",
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["code"])
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    await session.commit()
    return inserted


async def seedLayerRules(session: AsyncSession) -> int:
    """幂等 seed 数据层规则；返回新增行数。"""
    inserted = 0
    for pattern, layer, priority in _LAYER_RULES:
        stmt = pg_insert(DataLayerRule).values(
            layer=layer, pattern=pattern, priority=priority, enabled=True,
        )
        if pattern is None:
            # 手工声明层：partial unique index uq_layer_rule_manual (layer)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["layer"],
                index_where=text("pattern IS NULL"),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["layer", "pattern"],
                index_where=text("pattern IS NOT NULL"),
            )
        result = await session.execute(stmt)
        inserted += result.rowcount or 0
    await session.commit()
    return inserted
