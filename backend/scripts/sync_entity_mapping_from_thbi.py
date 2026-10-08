"""THBI 数据仓库 → entity_mapping 同步脚本（幂等 + 可 dry-run）。

解决痛点：合成 seed（SUP000001 / V000001 等）和真实供应商/物料编码完全无关，
Supplier360Page 的 360° 视图对不上 THBI DWS 表的 supplier_code。

设计：
- 数据源：`THBI.DIM_SUPPLIER(BPSNUM_0/BPSNAM_0)`、`THBI.DIM_IMATERIAL(ITMREF_0/ITMDES1_0..3)`
  （THBI 数仓主数据）。**2026-09-30 改**：原写 `THBI.DWD_SUPPLIER` / `THBI.DWD_MATERIAL`，
  这两张表在库里已不存在（实跑 ORA-00942），DIM_* 才是现役主数据表 ——
  行数 3500 / 350922 与 2026-09-16 那次成功同步落库的数字一致。
- enterprise_code = THBI 原编码（与 DWS supplier_code 自然 JOIN，无需转换层）
- enterprise_key 派生见 `app/domain/enterprise_key.py`（**唯一 SSOT**；本脚本与
  entity_mapping_service 不再各存一份）
    - 同一 code 必得同 key → 重跑由 ON CONFLICT (entity_type, enterprise_key, source_system) 兜底幂等
    - **2026-09-30**：原 2³² 键空间对 35 万条太小，MATERIAL 实测 12 个 key 各含两个编码
      → DO UPDATE 静默折叠 12 行（后者编码消失、前者 name 被覆盖）。已扩到 2⁴⁸，
      碰撞期望 14.3 → 2.2e-4。
- source_system = ERP（THBI 即 ERP 数仓）；source_code = enterprise_code
- match_rule = MDM_MASTER
- 仅写 entity_mapping；不动 entity 主表、不动 ontology、不动 feature_values

依赖：
- data_source 表里已注册默认活跃 THBI 数据源（is_default=true, is_active=true）
- 密码由 DataSource.password_encrypted 提供，启动时 `decryptApiKey` 解密
- 需 PG 元数据库可达（默认 DATABASE_URL）
- THBI-Oracle 端需 DIM_SUPPLIER / DIM_IMATERIAL 表有数据

用法：
    python -m scripts.sync_entity_mapping_from_thbi --dry-run     # 只打印计划
    python -m scripts.sync_entity_mapping_from_thbi               # 真写（幂等）

环境：
    QUERY_TIMEOUT_SECONDS=600  # DIM_IMATERIAL 35w 行默认 30s 不够，
                              # 同步必须显式拉大到 ≥600；小查询可省略。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.domain.enterprise_key import (  # noqa: E402
    MATERIAL_KEY_OFFSET,
    SUPPLIER_KEY_OFFSET,
    stableKey,
)
from app.domain.enums import MatchRule, SourceSystem  # noqa: E402
from app.domain.models import DataSource, EntityMapping  # noqa: E402
from app.infrastructure.business_db_pool import (  # noqa: E402
    dispose_adapter,
    get_adapter,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402

# asyncpg 单次查询参数上限 32767；按 11 列/行反推 batch 上限 32767/11 ≈ 2978，
# 取保守值 2500，留余量给 ORM 可能补的额外参数。
_CHUNK_ROWS = 2_500

_DEFAULT_EFFECTIVE = __import__("datetime").date(2026, 1, 1)


def _mapping(
    entity_type: str,
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
        enterprise_key=stableKey(code, offset=offset),
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
    """返回 [(supplier_code, supplier_name)]，按 supplier_code 排序去重。

    adapter.execute_read_only 统一把列名转小写（business_db_pool.py:422），
    所以这里取 r.get('supplier_code') 而不是 r.get('SUPPLIER_CODE')。
    2026-09-16 复盘：曾因大写键读取而所有 supplier 被空 code 过滤掉，
    表现为 dry-run 显示 suppliers=0 / materials=0（实测 DWD_SUPPLIER 有 3500 行）。
    """
    rows = await adapter.execute_read_only(
        "SELECT BPSNUM_0 AS supplier_code, BPSNAM_0 AS supplier_name "
        "FROM THBI.DIM_SUPPLIER ORDER BY BPSNUM_0",
    )
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for r in rows:
        code = (r.get("supplier_code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        name = (r.get("supplier_name") or "").strip() or None
        out.append((code, name))
    return out


async def _fetchMaterialCodes(adapter: Any) -> list[tuple[str, str | None]]:
    """返回 [(material_code, description)]：description 取 description_1 + 2 + 3 拼接。

    X3 物料通常 description_1 是短名，description_2/3 是补充规格；按非空顺序拼接，
    给 AutoComplete 完整信息。列名取小写键（与 _fetchSupplierCodes 同源）。
    """
    rows = await adapter.execute_read_only(
        "SELECT ITMREF_0 AS material_code, ITMDES1_0 AS description_1, "
        "ITMDES2_0 AS description_2, ITMDES3_0 AS description_3 "
        "FROM THBI.DIM_IMATERIAL ORDER BY ITMREF_0",
    )
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for r in rows:
        code = (r.get("material_code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        parts = [
            (r.get("description_1") or "").strip(),
            (r.get("description_2") or "").strip(),
            (r.get("description_3") or "").strip(),
        ]
        name = " ".join(p for p in parts if p) or None
        out.append((code, name))
    return out


def _buildAllRows(
    suppliers: list[tuple[str, str | None]],
    materials: list[tuple[str, str | None]],
) -> list[dict[str, Any]]:
    rows = [
        _mapping("SUPPLIER", code, offset=SUPPLIER_KEY_OFFSET, name=name)
        for code, name in suppliers
    ]
    rows += [
        _mapping("MATERIAL", code, offset=MATERIAL_KEY_OFFSET, name=name)
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

    幂等：ON CONFLICT (entity_type, enterprise_key, source_system) DO UPDATE SET name。
    重跑时 supplier_code / material_code 不变 → enterprise_key 不变 → 命中唯一约束，
    只刷新 name（THBI 改名 / 物料描述修正），其它列保持原值。
    """
    suppliers = await _fetchSupplierCodes(adapter)
    materials = await _fetchMaterialCodes(adapter)
    rows = _buildAllRows(suppliers, materials)

    if dryRun:
        for m in rows[:10]:
            print(
                f"  [plan] {m['entity_type']} key={m['enterprise_key']} "
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
    """按 (is_default=true, is_active=true) 取当前默认活跃数据源。

    不再硬编码 name：2026-09-16 复盘发现脚本查 'THBI-Oracle'（连字符），
    但 data_source 表实际注册名是 'THBI Oracle'（空格），导致脚本每次
    跑都抛 RuntimeError，SUPPLIER 同步从未真实运行过。改为按业务属性
    (default+active) 查，name 仍记入断言日志方便人眼核对。
    """
    ds = (
        await session.execute(
            select(DataSource).where(
                DataSource.is_default == True,  # noqa: E712
                DataSource.is_active == True,  # noqa: E712
            ),
        )
    ).scalars().first()
    if ds is None:
        raise RuntimeError(
            "未找到默认活跃数据源（is_default=true AND is_active=true）；"
            "请到 /admin/datasources 把 THBI Oracle 设为默认并激活"
        )
    if not ds.is_active:
        raise RuntimeError(f"默认数据源 {ds.name!r} 已禁用（is_active=false）")
    # name 仅做断言日志（人眼核对），不再作为查询键
    print(f"[sync] 使用默认活跃数据源: name={ds.name!r} id={ds.id} host={ds.host}")
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
