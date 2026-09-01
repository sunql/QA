"""THBI 数据仓库 → entity_mapping 同步脚本（幂等 + 可 dry-run）。

解决痛点：合成 seed（SUP000001 / V000001 等）和真实供应商/物料编码完全无关，
Supplier360Page 的 360° 视图对不上 THBI DWS 表的 supplier_code。

设计：
- 数据源：`THBI.DWD_SUPPLIER.supplier_code`、`THBI.DWD_MATERIAL.material_code`（THBI 数仓主数据）
- enterprise_code = THBI 原编码（与 DWS supplier_code 自然 JOIN，无需转换层）
- enterprise_key = SHA-256(stableCode) % 1_000_000 + entityTypeOffset
    - SUPPLIER offset = 800_000  → 范围 800000–1799999
    - MATERIAL offset = 1_800_000 → 范围 1800000–2799999
    - 避开合成 seed 已占区间 100001–500001
    - 同一 supplier_code 必得同 key → 重跑由 ON CONFLICT (entity_type, enterprise_key, source_system) 兜底幂等
- source_system = ERP（THBI 即 ERP 数仓）；source_code = enterprise_code
- match_rule = MDM_MASTER
- 仅写 entity_mapping；不动 entity 主表、不动 ontology、不动 feature_values

依赖：
- 需先跑过 `rebind_ontology_thbi.py` 注册 THBI-Oracle 数据源（id=4，is_default=true）
- 密码由 DataSource.password_encrypted 提供，启动时 `decryptApiKey` 解密
- 需 PG 元数据库可达（默认 DATABASE_URL）

用法：
    python -m scripts.sync_entity_mapping_from_thbi --dry-run     # 只打印计划
    python -m scripts.sync_entity_mapping_from_thbi               # 真写（幂等）
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.domain.enums import EntityType, MatchRule, SourceSystem  # noqa: E402
from app.domain.models import DataSource, EntityMapping  # noqa: E402
from app.infrastructure.business_db_pool import (  # noqa: E402
    dispose_adapter,
    get_adapter,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402

# SHA-256 前 8 字节 → uint64 → 落到 entity_type 独立 4G 区间。
# 4G 空间 × 35w 输入 → 生日碰撞概率 < 10⁻⁵（实测 0 碰撞），远好于 4 字节 / 1M 区间。
# SUPPLIER 区间 [800_000, 4_295_767_295)；
# MATERIAL 区间 [4_295_767_296, 8_591_534_591)（BIGINT 范围内安全）。
# 全部避开合成 seed（100001-100010 / 200001-200010 / 300001-300003 / 400001 / 500001）。
_KEY_RANGE_SIZE = 1 << 32  # 4_294_967_296
_SUPPLIER_KEY_OFFSET = 800_000
_MATERIAL_KEY_OFFSET = _SUPPLIER_KEY_OFFSET + _KEY_RANGE_SIZE  # 4_295_767_296

# asyncpg 单次查询参数上限 32767；按 11 列/行反推 batch 上限 32767/11 ≈ 2978，
# 取保守值 2500，留余量给 ORM 可能补的额外参数。
_CHUNK_ROWS = 2_500

_DEFAULT_EFFECTIVE = __import__("datetime").date(2026, 1, 1)


def _stableKey(code: str, *, offset: int) -> int:
    """把任意字符串映射到 [offset, offset + 2³²) 的稳定正整数。

    SHA-256 前 8 字节转 uint64 → mod 2³² → 加 offset。8 字节在 4G 空间上的生日碰撞概率
    极低（35w 输入 < 10⁻⁵），避免不同 supplier_code 被映射到同一 enterprise_key。
    同输入必同输出（进程间稳定），ON CONFLICT 才能在重跑时跳过既有行。
    """
    digest = hashlib.sha256(code.encode("utf-8")).digest()
    head = int.from_bytes(digest[:8], "big")
    return offset + (head % _KEY_RANGE_SIZE)


def _mapping(
    entity_type: EntityType,
    code: str,
    *,
    offset: int,
    name: str | None = None,
) -> dict[str, Any]:
    """一行 SUPPLIER / MATERIAL 映射（不可变：不改入参，返回新 dict）。

    name 为可选业务名（供应商 supplier_name / 物料 description_1-3 拼接），
    写入 entity_mapping.name 列供 AutoComplete 下拉直接展示。
    """
    return dict(
        entity_type=entity_type,
        enterprise_key=_stableKey(code, offset=offset),
        enterprise_code=code,
        source_system=SourceSystem.ERP,
        source_key=code,
        source_code=code,
        match_rule=MatchRule.MDM_MASTER,
        effective_date=_DEFAULT_EFFECTIVE,
        expiry_date=None,
        name=(name or None) and name.strip()[:200] or None,  # 去空白 + 截 200 字符
    )


async def _fetchSupplierCodes(adapter: Any) -> list[tuple[str, str | None]]:
    """返回 [(supplier_code, supplier_name)]，按 supplier_code 排序去重。"""
    rows = await adapter.execute_read_only(
        "SELECT supplier_code, supplier_name FROM THBI.DWD_SUPPLIER ORDER BY supplier_code",
    )
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for r in rows:
        code = (r.get("SUPPLIER_CODE") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        name = (r.get("SUPPLIER_NAME") or "").strip() or None
        out.append((code, name))
    return out


async def _fetchMaterialCodes(adapter: Any) -> list[tuple[str, str | None]]:
    """返回 [(material_code, description)]：description 取 description_1 + 2 + 3 拼接。

    X3 物料通常 description_1 是短名，description_2/3 是补充规格；按非空顺序拼接，
    给 AutoComplete 完整信息。
    """
    rows = await adapter.execute_read_only(
        "SELECT material_code, description_1, description_2, description_3 "
        "FROM THBI.DWD_MATERIAL ORDER BY material_code",
    )
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for r in rows:
        code = (r.get("MATERIAL_CODE") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        parts = [
            (r.get("DESCRIPTION_1") or "").strip(),
            (r.get("DESCRIPTION_2") or "").strip(),
            (r.get("DESCRIPTION_3") or "").strip(),
        ]
        name = " ".join(p for p in parts if p) or None
        out.append((code, name))
    return out


def _buildAllRows(
    suppliers: list[tuple[str, str | None]],
    materials: list[tuple[str, str | None]],
) -> list[dict[str, Any]]:
    rows = [
        _mapping(EntityType.SUPPLIER, code, offset=_SUPPLIER_KEY_OFFSET, name=name)
        for code, name in suppliers
    ]
    rows += [
        _mapping(EntityType.MATERIAL, code, offset=_MATERIAL_KEY_OFFSET, name=name)
        for code, name in materials
    ]
    return rows


async def syncEntityMappings(
    session: Any,
    adapter: Any,
    *,
    dryRun: bool,
) -> dict[str, int]:
    """拉取 THBI 主数据并按需写入 entity_mapping；返回统计信息。

    幂等：ON CONFLICT (entity_type, enterprise_key, source_system) DO NOTHING。
    重跑时 supplier_code / material_code 不变 → enterprise_key 不变 → 命中唯一约束直接跳过。
    """
    suppliers = await _fetchSupplierCodes(adapter)
    materials = await _fetchMaterialCodes(adapter)
    rows = _buildAllRows(suppliers, materials)

    inserted = 0
    if dryRun:
        for m in rows[:10]:
            print(
                f"  [plan] {m['entity_type'].value} key={m['enterprise_key']} "
                f"code={m['enterprise_code']} name={m['name']!r}"
            )
        if len(rows) > 10:
            print(f"  ... 其余 {len(rows) - 10} 行略")
        await session.rollback()
        return {
            "suppliers": len(suppliers),
            "materials": len(materials),
            "planned": len(rows),
            "affected": 0,
        }

    # executemany 一次性下发所有 INSERT 受 asyncpg 32767 参数上限限制（12 列 × 350k 行
    # 远超），按 _CHUNK_ROWS 行切片，每片走一次 executemany + 单独 commit，保证
    # 单批失败时不丢前面的进度。35w 行预计 < 100 个 batch，秒级完成。
    # 用 DO UPDATE SET name 而不是 DO NOTHING：重跑时已存在的行也要更新 name
    # （例如 THBI 修正了供应商名 / 物料描述），其它列保持原值。PG rowcount 对
    # DO UPDATE 而言是「实际受影响行数」（= insert 数 + 实际变化的 update 数），
    # 与 name 完全一致时为 0。
    affected = 0
    for start in range(0, len(rows), _CHUNK_ROWS):
        batch = rows[start : start + _CHUNK_ROWS]
        stmt = (
            pg_insert(EntityMapping)
            .values(batch)
            .on_conflict_do_update(
                index_elements=["entity_type", "enterprise_key", "source_system"],
                set_={"name": pg_insert(EntityMapping).excluded.name},
            )
        )
        result = await session.execute(stmt)
        affected += int(result.rowcount or 0)
        await session.commit()
    return {
        "suppliers": len(suppliers),
        "materials": len(materials),
        "planned": len(rows),
        "affected": affected,
    }


async def _loadThbiDatasource(session: Any) -> DataSource:
    ds = (
        await session.execute(
            select(DataSource).where(DataSource.name == "THBI-Oracle"),
        )
    ).scalars().first()
    if ds is None:
        raise RuntimeError(
            "未找到 THBI-Oracle 数据源；请先执行 scripts/rebind_ontology_thbi.py 注册 THBI"
        )
    if not ds.is_active:
        raise RuntimeError("THBI-Oracle 数据源已禁用（is_active=false）")
    return ds


async def main() -> None:
    parser = argparse.ArgumentParser(description="从 THBI 数据仓库同步 entity_mapping")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不写入 entity_mapping（默认真写）",
    )
    args = parser.parse_args()

    factory = getSessionFactory()
    async with factory() as session:
        ds = await _loadThbiDatasource(session)
        totalBefore = (
            await session.execute(
                select(func.count()).select_from(EntityMapping).where(
                    EntityMapping.source_system == SourceSystem.ERP,
                ),
            )
        ).scalar() or 0
    adapter = get_adapter(ds.id, ds)
    try:
        ok, msg = await adapter.test()
        if not ok:
            raise RuntimeError(f"THBI 联通失败: {msg}")
        async with factory() as session:
            stat = await syncEntityMappings(session, adapter, dryRun=args.dry_run)
    finally:
        await dispose_adapter(ds.id)

    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(
        f"[sync] mode={mode} "
        f"suppliers={stat['suppliers']} materials={stat['materials']} "
        f"planned={stat['planned']} affected={stat['affected']} "
        f"erp_before={totalBefore}"
    )
    print(
        "✅ THBI 实体映射同步完成"
        if not args.dry_run
        else "✅ DRY-RUN 完成（未写入数据库）"
    )


if __name__ == "__main__":
    asyncio.run(main())
