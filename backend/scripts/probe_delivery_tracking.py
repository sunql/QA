"""到货跟踪口径基线探针（P5，Task 10）—— 只读。

用途
----
验证 `delivery_tracking_query` 的本体取数口径与设计基线一致。**不生成 SQL 字符串
断言**，而是真的经 `resolveBinding` → THBI Oracle 跑一遍，把结果与基线逐项对照：

    订单总数            4844
    全到货/部分/未到     3986 / 448 / 410
    订单到货率          82.29%
    量级率              40.51%

基线来自 2026-11-25 设计期探查（spec §4.2）；**任何偏离都应显式报告**，
不得静默按新值接受 —— 先判断是「数据漂移」还是「口径实现错」。

用法（backend/ 下）
-------------------
    python -m scripts.probe_delivery_tracking
    python -m scripts.probe_delivery_tracking --from 2026-10-24 --to 2026-11-24
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from app.infrastructure.database import getSessionFactory

#: 实测基线（2026-11-25 Task 10，THBI 真实取数，含 spec 4.4 脏数据剔除）。
#:
#: 与设计期（spec §4.2）的差异**全部**由「脏数据剔除」解释，已逐项验证：
#:   - totalOrders 4844→4843、partial 448→447：恰好 **1 个订单的所有行都是脏行**，
#:     被 `LINE_COUNT > 0` 守卫排除出订单分母（实测该数为 1）
#:   - qtyArrivalRate 40.51%→44.46%：不剔除时实测**正好 40.51%**；脏行从分子分母同时
#:     移除后升到 44.46%（脏行是超大额未到货行，剔除后拉高率值）
#:   - 订单到货率 82.29%→82.30%：同上，1 个全脏行订单退出分母
BASELINE: dict[str, float] = {
    "totalOrders": 4843,
    "allArrivedOrders": 3986,
    "partialArrivedOrders": 447,
    "notArrivedOrders": 410,
    "orderArrivalRate": 82.30,
    "qtyArrivalRate": 44.46,
}


async def _run(dateFrom: str | None, dateTo: str | None) -> int:
    """跑真实取数并对照基线。返回退出码（0 = 一致 / 1 = 偏离）。"""
    from app.services.delivery_tracking_query import deliveryTrackingHandler
    from app.services.ontology_binding_resolver import resolveBinding

    args: dict[str, object] = {}
    if dateFrom and dateTo:
        args = {"dateFrom": dateFrom, "dateTo": dateTo}
    else:
        args = {"anchorDay": 24, "periodIndex": 0}

    async with getSessionFactory()() as session:
        # 先确认绑定可解析（fail-closed 的第一道）
        for className, required in (
            ("DWD_PURCHASE_ORDER_DTL", ["PO_NO", "PO_LINE_NO", "PO_QTY", "ORDER_DATE"]),
            ("DWD_GOODS_RECEIPT_DTL", ["PO_NO", "PO_LINE_NO", "RCV_QTY_STU"]),
        ):
            binding = await resolveBinding(
                session, className=className, requiredProperties=required
            )
            print(f"绑定 {className} → {binding.qualifiedTable()}（{len(binding.columns)} 列）")

        result = await deliveryTrackingHandler(session, args, None)

    data = result.data
    window = data["window"]
    summary = data["summary"]
    print(f"\n窗口: {window['dateFrom']} ~ {window['dateTo']}（mode={window['mode']}）")
    print(f"数据层: {data['dataLayer']}  数据源 id={data['datasourceId']}")
    print("\n实测:")
    for key in (
        "totalOrders",
        "allArrivedOrders",
        "partialArrivedOrders",
        "notArrivedOrders",
        "orderArrivalRate",
        "qtyArrivalRate",
        "excludedRows",
    ):
        print(f"  {key:24s} {summary[key]}")

    print("\n与基线对照:")
    violations: list[str] = []
    for key, expected in BASELINE.items():
        actual = summary[key]
        ok = (
            abs(float(actual) - float(expected)) < 0.01
            if isinstance(expected, float)
            else int(actual) == int(expected)
        )
        mark = "✅" if ok else "❌"
        print(f"  {mark} {key:24s} 实测={actual}  基线={expected}")
        if not ok:
            violations.append(f"{key}: 实测 {actual} ≠ 基线 {expected}")

    if violations:
        print("\n⚠ 偏离基线（先判断是数据漂移还是口径实现错，勿静默接受）:")
        for item in violations:
            print(f"  - {item}")
        return 1

    print("\n✅ 全部与设计基线一致。")
    return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description="到货跟踪口径基线探针（只读）")
    parser.add_argument("--from", dest="dateFrom", default=None, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="dateTo", default=None, help="YYYY-MM-DD")
    opts = parser.parse_args()
    code = await _run(opts.dateFrom, opts.dateTo)
    sys.exit(code)


if __name__ == "__main__":
    asyncio.run(main())
