"""采购只读工具的通用 SQL 碎片（2026-11-25 从 `agent_tools_procurement` 抽出）。

只放**纯函数 / 常量**：不读写数据库、不依赖模块内状态，供
`agent_tools_procurement` 与 `pending_delivery_query` 共用，避免拆模块时
复制一份「方言 / 转义 / 月份偏移」而慢慢漂移。
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

#: 单次只读查询的行数上限（防大结果集拖垮只读适配器）。
MAX_LIMIT = 50
MONTHS_BACK_MIN, MONTHS_BACK_MAX = 1, 36


def escapeLiteral(value: str) -> str:
    """字面量转义（单引号翻倍；模板参数只接受标识符级输入）。"""
    return value.replace("'", "''")


def minusMonths(base: date, months: int) -> date:
    """date - N 月（无 dateutil 依赖；月末夹齐：11-30 减 3 月 = 08-31）。"""
    year = base.year
    month = base.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


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
