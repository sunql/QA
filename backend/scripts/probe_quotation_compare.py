"""真数据探针：年度报价比价（`quotation_compare` 的 Oracle 侧验收）。

背景：`quotation_compare` 的**代码与单测**已完成（纯确定性、无 LLM），
但**真数据验收**缺一块——需要连 THBI Oracle 实跑，确认：
1. 表 `DWD_PURCHASE_QUOTATION_YEARLY_DTL` 能取到数（16.7 万行级别）；
2. `UNIT_PRICE_EX_TAX` / `SEG_YEAR` 的实际分布；
3. 比价矩阵在真实数据上的表现（最低价、中位价、偏差标记）。

**这个脚本只读**：走业务数据源的只读 adapter（SQL Guard + 只读账号双闸），
不写任何业务库，也不调用 embedding。

用法（凭据放环境变量，不要写进文件）::

    ORACLE_DSN=<host>:<port>/<service> \\
    ORACLE_USER=<qa_readonly> \\
    ORACLE_PASSWORD=<...> \\
    python -m scripts.probe_quotation_compare --year 2026 --item AJ0100684

退出码：0 = 取到数据且比价矩阵可算；2 = 连不上/无数据（附原因）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.procurement_quotation_compare import (  # noqa: E402
    buildComparison,
    quotesFromRows,
)


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026, help="报价年度（SEG_YEAR）")
    parser.add_argument(
        "--item",
        action="append",
        default=None,
        help="物料编码（可重复）；不给则取该年度全部报价的前 N 个物料",
    )
    parser.add_argument("--limit", type=int, default=200, help="取多少行")
    parser.add_argument("--top-items", type=int, default=5, help="无 --item 时取几个物料")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.domain.models import DataSource
    from app.infrastructure.database import getSessionFactory
    from app.services.agent_tools_procurement import _adapterFor

    factory = getSessionFactory()
    async with factory() as session:
        ds = (
            await session.execute(
                select(DataSource)
                .where(DataSource.is_default.is_(True))
                .where(DataSource.is_active.is_(True))
                .limit(1)
            )
        ).scalars().first()
        if ds is None:
            print("[probe] 无默认活跃业务数据源（先 seed_oracle_datasource）", file=sys.stderr)
            return 2
        print(f"[probe] 数据源 id={ds.id} name={ds.name} type={ds.type}")

        adapter = _adapterFor(ds.id, ds)

        # 1. 年度总览
        overviewSql = (
            "SELECT COUNT(*) AS n, COUNT(DISTINCT ITEM_CODE) AS items, "
            "COUNT(DISTINCT SUPPLIER_CODE) AS sups FROM "
            "DWD_PURCHASE_QUOTATION_YEARLY_DTL WHERE SEG_YEAR = "
            f"{int(args.year)}"
        )
        overview = (await adapter.execute_read_only(overviewSql)) or [{}]
        print(f"[probe] {args.year} 年度报价行数={overview[0].get('n')} "
              f"物料数={overview[0].get('items')} 供应商数={overview[0].get('sups')}")

        # 2. 取样本
        if args.item:
            items = list(args.item)
            inList = ", ".join(f"'{code}'" for code in items)
            where = f"WHERE SEG_YEAR = {int(args.year)} AND ITEM_CODE IN ({inList})"
        else:
            where = f"WHERE SEG_YEAR = {int(args.year)}"
        sampleSql = (
            "SELECT SUPPLIER_CODE, ITEM_CODE, SEG_YEAR, UNIT_PRICE_EX_TAX "
            "FROM DWD_PURCHASE_QUOTATION_YEARLY_DTL "
            f"{where} ORDER BY ITEM_CODE, SUPPLIER_CODE "
            f"FETCH FIRST {int(args.limit)} ROWS ONLY"
        )
        rows = await adapter.execute_read_only(sampleSql)
        quotes = quotesFromRows(rows)
        print(f"[probe] 取到 {len(rows)} 行（有效报价 {len(quotes)} 条）")

        if not quotes:
            print("[probe] 无有效报价（价格为空或编码缺失）", file=sys.stderr)
            return 2

        # 3. 比价矩阵
        groups = buildComparison(quotes)
        print(f"[probe] 比价组数={len(groups)}")
        for group in groups[: args.top_items]:
            flags: dict[str, int] = {}
            for row in group.rows:
                flags[row.flag] = flags.get(row.flag, 0) + 1
            print(
                f"  - {group.itemCode}@{group.segmentYear} "
                f"供应商 {len(group.rows)} 家 "
                f"最低 {group.lowestPrice} / 中位 {group.medianPrice} "
                f"价差 {group.spreadPct}% "
                f"最低价供应商 {group.bestSupplierCode} "
                f"标记 {flags}"
            )
    return 0


def main() -> None:
    args = _parseArgs()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
