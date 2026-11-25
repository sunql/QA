"""采购只读工具的通用 SQL 碎片（2026-11-25 从 `agent_tools_procurement` 抽出）。

只放**纯函数 / 常量**：不读写数据库、不依赖模块内状态，供
`agent_tools_procurement` 与 `pending_delivery_query` 共用，避免拆模块时
复制一份「方言 / 转义 / 月份偏移」而慢慢漂移。
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.ontology_binding_resolver import ResolvedBinding

#: 单次只读查询的行数上限（防大结果集拖垮只读适配器）。
MAX_LIMIT = 50
MONTHS_BACK_MIN, MONTHS_BACK_MAX = 1, 36

#: 行级欠交量上限（spec 4.4）：≥ 1e6 判为源侧录入错误的脏行，剔除并显式计数。
#:
#: ⚠ 判据是**欠交量**（`PO_QTY - RECEIVED`），不是**订购量**：大额订购是真实业务
#: （spec 4.2 明确拒绝按订购量剔行）。待到货与到货跟踪两个工具**共用**本常量，
#: 两侧各写一份会让「同口径」的承诺慢慢漂移。
OPEN_QTY_SANITY_MAX = 1_000_000


def escapeLiteral(value: str) -> str:
    """字面量转义（单引号翻倍；模板参数只接受标识符级输入）。"""
    return value.replace("'", "''")


def shiftMonths(base: date, months: int) -> date:
    """date 偏移 N 月（无 dateutil 依赖；月末夹齐：11-30 减 3 月 = 08-31；N 可负）。"""
    total = base.year * 12 + (base.month - 1) + months
    year, monthIndex = divmod(total, 12)
    month = monthIndex + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def minusMonths(base: date, months: int) -> date:
    """date - N 月（`shiftMonths` 的减法形态，保留既有调用点语义）。"""
    return shiftMonths(base, -months)


def monthsBackWindow(today: date, monthsBack: int) -> tuple[date, date]:
    """近 N 月窗口：`[today - N 月, today)`（左闭右开，spec 4.3）。"""
    return minusMonths(today, monthsBack), today


def dateWindowPredicate(column: str, dateFrom: date, dateTo: date) -> str:
    """时间窗谓词：`column` 落在 `[dateFrom, dateTo)`（左闭右开，spec 4.3）。

    两个采购工具共用同一构造：待到货曾是闭区间、到货跟踪是左闭右开 ⇒
    末日订单在两者间被算进/算不进，同一份数据两个数字。
    """
    return (
        f"{column} >= DATE '{dateFrom.isoformat()}'"
        f" AND {column} < DATE '{dateTo.isoformat()}'"
    )


def colValue(row: Any, name: str) -> Any:
    """**大小写不敏感**地取列值。

    ⚠️ Oracle 的 ``cursor.description`` 返回大写标识符，而 ``business_db_pool``
    统一把它们**小写化** ⇒ 用大写 key 去 get **永远拿到 None**
    （2026-11-22 实测踩到，见 `agent_tools_procurement._col` 的完整说明）。
    """
    if not isinstance(row, dict):
        return None
    if name in row:
        return row[name]
    lowered = name.lower()
    for key, value in row.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


class SqlDialect:
    """SQL 的方言碎片（2026-11-24 生产实测：切 MySQL 源 500）。

    根因：builder 硬编码 Oracle 方言，而场景槽位（datasourceId）可切到
    MySQL 源——派生表无别名（MySQL 1248）、NVL/TRUNC 不存在。
    只分叉差异构造，其余模板两边共用；派生表别名统一用**无 AS 裸别名**
    （Oracle 不允许 ``AS`` 表别名，裸别名两边合法）。
    其余方言（如 postgresql）回落 Oracle 分支——不预期出现在采购影子表；
    真出现时错误语义同现在（fail loud），不做静默适配。
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def overdueDays(self, todayLit: str, col: str) -> str:
        """今天 - 预期到货日的天数差。"""
        if self.name == "mysql":
            return f"DATEDIFF({todayLit}, {col})"
        return f"TRUNC({todayLit}) - TRUNC({col})"

    def coalesce(self, expr: str, default: str) -> str:
        if self.name == "mysql":
            return f"COALESCE({expr}, {default})"
        return f"NVL({expr}, {default})"

    def tail(self, limit: int, offset: int = 0) -> str:
        """行数限制（+ 可选偏移）：MySQL 8.0.19 以下不支持 FETCH FIRST，统一 LIMIT 更稳。

        Oracle：``FETCH FIRST n ROWS ONLY OFFSET m``；
        MySQL：``LIMIT n OFFSET m``（注意顺序与 Oracle 相反）。
        """
        if self.name == "mysql":
            base = f"LIMIT {int(limit)}"
            return f"{base} OFFSET {int(offset)}" if offset else base
        base = f"FETCH FIRST {int(limit)} ROWS ONLY"
        return f"{base} OFFSET {int(offset)}" if offset else base

    @staticmethod
    def fromAdapter(adapter: Any) -> SqlDialect:
        """从 adapter 读方言；Oracle 适配器（或测试桩）缺属性 ⇒ 回落 oracle。"""
        name = str(getattr(adapter, "dialect", "") or "oracle").lower()
        if name not in ("mysql", "oracle"):
            name = "oracle"
        return SqlDialect(name)


def receiptAggregate(
    receipt: ResolvedBinding,
    *,
    includeItemDesc: bool = False,
    includeRcvDate: bool = False,
    poNoSubquery: str | None = None,
) -> str:
    """收货子查询：按 PO_NO+PO_LINE_NO 聚合已收量（两个采购工具**共用**）。

    - `includeItemDesc` / `includeRcvDate`：明细展示用的物料名与最后收货日
      （只有到货跟踪需要；待到货只取数量 ⇒ 聚合并集更小）；
    - `poNoSubquery`：`SELECT ... PO_NO FROM <订单行> WHERE <窗口>`，把聚合
      收敛到窗口内出现过的订单（semi-join）——否则每次汇总都全表 GROUP BY
      整张收货明细（欢迎态热路径）。
    """
    poNo = receipt.column("PO_NO")
    poLine = receipt.column("PO_LINE_NO")
    selects = [
        f"{poNo} AS PO_NO",
        f"{poLine} AS PO_LINE_NO",
        f"SUM({receipt.column('RCV_QTY_STU')}) AS RECEIVED_QTY",
    ]
    if includeItemDesc:
        selects.append(f"MAX({receipt.column('ITEM_DESC')}) AS ITEM_DESC")
    if includeRcvDate:
        selects.append(f"MAX({receipt.column('RCV_DATE')}) AS RECEIPT_DATE")
    where = "" if poNoSubquery is None else f" WHERE {poNo} IN ({poNoSubquery})"
    return (
        f"(SELECT {', '.join(selects)}"
        f" FROM {receipt.qualifiedTable()}{where}"
        f" GROUP BY {poNo}, {poLine})"
    )
