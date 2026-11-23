"""只读探针：「待到货 / 逾期未到」的数据口径能不能成立。

场景目标：给定时间窗（或全部），列出**欠交**的采购订单行 + 逾期天数 + 供应商，
为后续 `pending_delivery_query` 工具定口径。

只读，全部 SELECT。要回答四个问题：

    P1  订单行状态字段怎么用（只统计"未关闭"）
    P2  欠交量怎么算：QTYSTU_0（订购，库存单位） - RCPQTYSTU_0（已收，库存单位）？
    P3  到货日期用哪个字段：EXTRCPDAT_0 / DEMRCPDAT_0，空值多少，逾期怎么算
    P4  收到的量与到货明细（YPRECEIPTD）对不对得上（抽样对账）
    P5  样例：TOP 欠交行（供应商 / 物料 / 欠交量 / 逾期天数）

用法（`backend/` 下）：

    python -m scripts.probe_pending_delivery
    python -m scripts.probe_pending_delivery --datasource-id 1 --top 20
    python -m scripts.probe_pending_delivery --since 2026-01-01   # 只看该日期后下的单

依据：2026-11-23 讨论「Copilot 应有固定场景而非自由问答」——待到货是 P0 场景，
数据在 ODS_PORDERQ（订购/已收）+ ODS_YPRECEIPTD（到货明细）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.domain.models import DataSource
from app.infrastructure.business_db_pool import get_adapter
from app.infrastructure.database import getSessionFactory
from scripts.probe_material_category import (  # noqa: E402 — 复用只读探针的公共件
    _get,
    _section,
    _try_query,
)

Q = "ZJTH.PORDERQ"
R = "ZJTH.YPRECEIPTD"


def _distinctSql(table: str, column: str, top: int = 10) -> str:
    return (
        f"SELECT {column} AS V, COUNT(*) AS CNT FROM {table} "
        f"GROUP BY {column} ORDER BY CNT DESC FETCH FIRST {top} ROWS ONLY"
    )


async def _probeCounts(adapter, since: str | None) -> None:
    _section("P0 规模")
    where = f" WHERE ORDDAT_0 >= DATE '{since}'" if since else ""
    rows = await _try_query(adapter, f"SELECT COUNT(*) CNT FROM {Q}{where}", label="订单行")
    if rows:
        print(f"  订单行数（{since or '全部'}）: {_get(rows[0], 'CNT')}")


async def _probeStatuses(adapter) -> None:
    _section("P1 状态字段分布（判断「未关闭」的口径）")
    for table, column in (
        (Q, "LINSTA_0"),
        (Q, "WIPSTA_0"),
        ("ZJTH.PORDER", "VALSTA_0"),
    ):
        rows = await _try_query(adapter, _distinctSql(table, column), label=f"{table}.{column}")
        for row in rows:
            print(f"    {column} = {_get(row, 'V')!s:<6} {_get(row, 'CNT')}")


async def _probeOpenQty(adapter, since: str | None) -> None:
    _section("P2 欠交量口径：QTYSTU_0 − RCPQTYSTU_0（同为库存单位）")
    where = f" WHERE ORDDAT_0 >= DATE '{since}'" if since else ""
    sql = f"""
    SELECT
        COUNT(*) AS LINES,
        SUM(CASE WHEN QTYSTU_0 - RCPQTYSTU_0 > 0 THEN 1 ELSE 0 END) AS POSITIVE,
        SUM(CASE WHEN QTYSTU_0 - RCPQTYSTU_0 = 0 THEN 1 ELSE 0 END) AS ZERO,
        SUM(CASE WHEN QTYSTU_0 - RCPQTYSTU_0 < 0 THEN 1 ELSE 0 END) AS NEGATIVE,
        SUM(CASE WHEN QTYSTU_0 - RCPQTYSTU_0 > 0 THEN QTYSTU_0 - RCPQTYSTU_0 ELSE 0 END) AS OPEN_QTY
    FROM {Q}{where}
    """
    rows = await _try_query(adapter, sql, label="分布")
    if rows:
        row = rows[0]
        print(
            "   行数={} 欠交>0={} 恰好为0={} 负数(超收?)={} 欠交总量={}".format(
                _get(row, "LINES"), _get(row, "POSITIVE"), _get(row, "ZERO"),
                _get(row, "NEGATIVE"), _get(row, "OPEN_QTY"),
            )
        )
        if (_get(row, "NEGATIVE") or 0) > 0:
            print("   ⚠️ 存在负数 ⇒ 口径/单位可能不一致，需人工看样例再定。")


async def _probeDates(adapter) -> None:
    _section("P3 到货日期字段可用性")
    sql = f"""
    SELECT
        COUNT(*) AS LINES,
        SUM(CASE WHEN EXTRCPDAT_0 IS NULL THEN 1 ELSE 0 END) AS EXTRCP_NULL,
        SUM(CASE WHEN DEMRCPDAT_0 IS NULL THEN 1 ELSE 0 END) AS DEMRCP_NULL,
        SUM(CASE WHEN EXTRCPDAT_0 IS NOT NULL AND EXTRCPDAT_0 < TRUNC(SYSDATE) THEN 1 ELSE 0 END) AS EXTRCP_OVERDUE
    FROM {Q}
    """
    rows = await _try_query(adapter, sql, label="日期")
    if rows:
        row = rows[0]
        print(
            "   行数={} 预计到货(EXTRCPDAT)空={} 需求到货(DEMRCPDAT)空={} 已逾期(EXTRCPDAT<今天)={}".format(
                _get(row, "LINES"), _get(row, "EXTRCP_NULL"),
                _get(row, "DEMRCP_NULL"), _get(row, "EXTRCP_OVERDUE"),
            )
        )


async def _probeReconcile(adapter) -> None:
    _section("P4 对账：订单行已收量 vs 到货明细汇总（抽样 10 行）")
    sql = f"""
    SELECT q.POHNUM_0, q.POPLIN_0, q.QTYSTU_0, q.RCPQTYSTU_0, r.RCP_SUM
    FROM {Q} q
    JOIN (
        SELECT POHNUM_0, POPLIN_0, SUM(QTYUOM_0) AS RCP_SUM
        FROM {R} GROUP BY POHNUM_0, POPLIN_0
    ) r ON r.POHNUM_0 = q.POHNUM_0 AND r.POPLIN_0 = q.POPLIN_0
    WHERE q.RCPQTYSTU_0 > 0
    FETCH FIRST 10 ROWS ONLY
    """
    rows = await _try_query(adapter, sql, label="抽样")
    mismatched = 0
    for row in rows:
        ordered = float(_get(row, "RCPQTYSTU_0") or 0)
        receipt = float(_get(row, "RCP_SUM") or 0)
        if abs(ordered - receipt) > 0.001:
            mismatched += 1
        print(
            f"    {_get(row, 'POHNUM_0')}/{_get(row, 'POPLIN_0')}: "
            f"订购={_get(row, 'QTYSTU_0')} 已收字段={ordered} 到货明细合计={receipt}"
        )
    if rows:
        print(
            f"  抽样不一致 {mismatched}/{len(rows)} 行；"
            "不一致多 ⇒ 已收字段不能直接当到货真值（要考虑退货/单位）。"
        )


async def _probeTop(adapter, top: int, since: str | None) -> None:
    _section(f"P5 样例：TOP {top} 欠交行")
    where = f" AND q.ORDDAT_0 >= DATE '{since}'" if since else ""
    sql = f"""
    SELECT * FROM (
        SELECT
            q.POHNUM_0, q.POPLIN_0, q.ITMREF_0, q.BPSNUM_0,
            q.QTYSTU_0 AS ORDERED, q.RCPQTYSTU_0 AS RECEIVED,
            (q.QTYSTU_0 - q.RCPQTYSTU_0) AS OPEN_QTY,
            q.EXTRCPDAT_0, q.LINSTA_0,
            TRUNC(SYSDATE) - TRUNC(q.EXTRCPDAT_0) AS OVERDUE_DAYS
        FROM {Q} q
        WHERE q.QTYSTU_0 - q.RCPQTYSTU_0 > 0{where}
        ORDER BY (q.QTYSTU_0 - q.RCPQTYSTU_0) DESC
    ) WHERE ROWNUM <= {top}
    """
    rows = await _try_query(adapter, sql, label="TOP")
    for row in rows:
        print(
            f"    {_get(row, 'POHNUM_0')}/{_get(row, 'POPLIN_0')} "
            f"物料={_get(row, 'ITMREF_0')} 供应商={_get(row, 'BPSNUM_0')} "
            f"订购={_get(row, 'ORDERED')} 已收={_get(row, 'RECEIVED')} "
            f"欠交={_get(row, 'OPEN_QTY')} 预计到货={_get(row, 'EXTRCPDAT_0')} "
            f"逾期天={_get(row, 'OVERDUE_DAYS')} 行状态={_get(row, 'LINSTA_0')}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="待到货/逾期未到 只读探针")
    parser.add_argument("--datasource-id", type=int, default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--since", default=None, help="只看该日期之后下的单，如 2026-01-01")
    args = parser.parse_args()

    async with getSessionFactory()() as session:
        if args.datasource_id is not None:
            ds = (
                await session.execute(
                    select(DataSource).where(DataSource.id == args.datasource_id)
                )
            ).scalar_one()
        else:
            rows = (
                await session.execute(
                    select(DataSource)
                    .where(DataSource.is_active.is_(True))
                    .where(DataSource.type.in_(("oracle", "ORACLE")))
                    .order_by(DataSource.id)
                )
            ).scalars().all()
            if not rows:
                raise SystemExit("没有 active 的 Oracle 数据源，请显式 --datasource-id")
            ds = rows[0]

    print(f"数据源: #{ds.id} {ds.name} ({ds.type} @ {ds.host}/{ds.database_name})")
    adapter = get_adapter(ds.id, ds)

    await _probeCounts(adapter, args.since)
    await _probeStatuses(adapter)
    await _probeOpenQty(adapter, args.since)
    await _probeDates(adapter)
    await _probeReconcile(adapter)
    await _probeTop(adapter, args.top, args.since)

    _section("口径判定")
    print(
        "  • P2 负数≈0 且 P4 抽样基本一致 ⇒ 欠交量 = QTYSTU_0 − RCPQTYSTU_0 可直接用；\n"
        "  • P3 EXTRCPDAT 空值多 ⇒ 逾期判定要退回 DEMRCPDAT 或「下单日+标准交期」；\n"
        "  • P1 选一个「未关闭」口径（LINSTA_0 / WIPSTA_0），并写进工具契约。"
    )


if __name__ == "__main__":
    if sys.platform == "darwin":
        pass
    asyncio.run(main())
