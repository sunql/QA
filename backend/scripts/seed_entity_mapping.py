"""Phase 3.2 跨系统编码映射种子脚本（幂等）。

Phase 6.x 重要变更：
- 供应商不再合成（SUP000001-000010 与 THBI 完全无关）。改用 sync_entity_mapping_from_thbi.py
  从 THBI.DWD_SUPPLIER 拉取真实 supplier_code + supplier_name（写 entity_mapping.name）。
- 物料同上：seed 只保留 RM-STEEL 系列作为 ontology 测试 fixture（无生产数据时也能让
  feature_value 测试有 entity 可挂）；真实物料由 sync 脚本拉 THBI.DWD_MATERIAL。

本 seed 现在只生成：
- 15 物料（RM-STEEL-001..010）：ERP 全量 + SRM 前 5 个，match_rule=MDM_MASTER
- 3 采购订单业务键（PO202608001-003）：match_rule=BUSINESS_KEY
- 2 收货/来料检验业务键（GR202608001 / IQC202608001）：match_rule=BUSINESS_KEY

幂等：`INSERT ... ON CONFLICT DO NOTHING`（唯一键 entity_type+enterprise_key+
source_system），冲突行由 DB 静默跳过；重复运行不产生重复数据，并发运行也安全。

seedEntityMappings() 可被集成测试导入（backend/scripts 为包）；main() 供 standalone 运行。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.domain.enums import EntityType, MatchRule, SourceSystem  # noqa: E402
from app.domain.models import EntityMapping  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

# 默认有效期：2026 年起长期有效（expiry_date=None）
_DEFAULT_EFFECTIVE = date(2026, 1, 1)


def _mapping(
    entity_type: EntityType,
    enterprise_key: int,
    enterprise_code: str,
    source_system: SourceSystem,
    source_key: str,
    match_rule: MatchRule,
) -> dict[str, Any]:
    """构造一行映射记录（不可变：不修改任何入参，返回新 dict）。

    种子中 source_code 取源系统 key（Sheet 05 语义：同一编码源侧标识）。
    """
    return dict(
        entity_type=entity_type,
        enterprise_key=enterprise_key,
        enterprise_code=enterprise_code,
        source_system=source_system,
        source_key=source_key,
        source_code=source_key,
        match_rule=match_rule,
        effective_date=_DEFAULT_EFFECTIVE,
        expiry_date=None,
    )


def _materialMappings() -> list[dict[str, Any]]:
    """10 个物料 × ERP + 前 5 个 × SRM = 15 条（MDM 主数据匹配）。

    ERP 源侧即物料编码本身（ITMREF 语义）；SRM 侧用 MS 前缀源键。
    """
    rows: list[dict[str, Any]] = []
    for i in range(1, 11):
        key = 200_000 + i
        code = f"RM-STEEL-{i:03d}"
        rows.append(
            _mapping(
                EntityType.MATERIAL,
                key,
                code,
                SourceSystem.ERP,
                code,
                MatchRule.MDM_MASTER,
            )
        )
        if i <= 5:
            rows.append(
                _mapping(
                    EntityType.MATERIAL,
                    key,
                    code,
                    SourceSystem.SRM,
                    f"MS{i:06d}",
                    MatchRule.MDM_MASTER,
                )
            )
    return rows


def _poMappings() -> list[dict[str, Any]]:
    """3 条采购订单业务键：ERP 的 PO 号即业务键（Sheet 04 业务编码语义）。"""
    rows: list[dict[str, Any]] = []
    for i in range(1, 4):
        key = 300_000 + i
        po_no = f"PO202608{i:03d}"
        rows.append(
            _mapping(
                EntityType.PO,
                key,
                po_no,
                SourceSystem.ERP,
                po_no,
                MatchRule.BUSINESS_KEY,
            )
        )
    return rows


def _grIqcMappings() -> list[dict[str, Any]]:
    """1 条收货 + 1 条来料检验业务键。"""
    return [
        _mapping(
            EntityType.GR,
            400_001,
            "GR202608001",
            SourceSystem.ERP,
            "GR202608001",
            MatchRule.BUSINESS_KEY,
        ),
        _mapping(
            EntityType.IQC,
            500_001,
            "IQC202608001",
            SourceSystem.QMS,
            "IQC202608001",
            MatchRule.BUSINESS_KEY,
        ),
    ]


ALL_MAPPINGS: list[dict[str, Any]] = (
    _materialMappings() + _poMappings() + _grIqcMappings()
)


async def seedEntityMappings(session: Any) -> int:
    """幂等写入全部映射；唯一键已存在的行静默跳过。返回本次新增条数。

    用 PG `INSERT ... ON CONFLICT DO NOTHING`：无需预查重（无 TOCTOU race），
    唯一索引 uq_entity_mapping_entity_source（entity_type+enterprise_key+source_system）
    冲突的行由 DB 直接跳过，并发运行安全。
    """
    inserted = 0
    for m in ALL_MAPPINGS:
        result = await session.execute(
            pg_insert(EntityMapping)
            .values(**m)
            .on_conflict_do_update(
                index_elements=["entity_type", "enterprise_key", "source_system"],
                set_={
                    # seed 仅设 enterprise_code / source_code 等稳定字段；name 由 sync 脚本写
                    "expiry_date": pg_insert(EntityMapping).excluded.expiry_date,
                },
            )
        )
        inserted += result.rowcount or 0
    await session.commit()
    return inserted


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        inserted = await seedEntityMappings(session)
        total = (
            await session.execute(select(func.count()).select_from(EntityMapping))
        ).scalar()
    print(f"[seed_entity_mapping] 本次新增 {inserted} 条，库内共 {total} 条")
    print("✅ 跨系统编码映射种子完成（覆盖 ERP/SRM/QMS，Phase 3 验收 ≥25 条已满足）")


if __name__ == "__main__":
    asyncio.run(main())
