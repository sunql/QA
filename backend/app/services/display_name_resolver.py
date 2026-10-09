"""显示名解析：列名/值 → 中文标签。

数据源：
1. 列名：OntologyProperty.property_alias（DB 查；未命中 → 原 column）
2. 值：DISPLAY_VALUE_DICT 模块内字典（key = (table, column, value)）

失败全部降级：DB 不可达 / property 缺失 / value 不在 dict → 返回原 column/value。

实现注：实际 ORM 模型里 `source_table` 在 `OntologyClass` 上，`OntologyProperty`
通过 `class_id` 外键引用类。查 alias 时走 JOIN。

调用约定：`resolveColumnName` 是 `async` —— 测试用 `AsyncSession`（real PG），
调用点 `report_planner.compose` 也走 async path；`resolveValueName` 是纯函数（dict 查）。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from sqlalchemy import select

from app.domain.models import OntologyClass, OntologyProperty

log = logging.getLogger(__name__)

# (source_table, source_column, raw_value) → display name
# 初始集覆盖 R2 真机 e2e 命中的 value code 字典。
DISPLAY_VALUE_DICT: Final[dict[tuple[str, str, str], str]] = {
    ("dwd_reconciliation_bill_dtl", "confirm_stage", "QTY"): "数量确认",
    ("dwd_reconciliation_bill_dtl", "confirm_stage", "PRICE"): "价格确认",
    ("dwd_reconciliation_bill_dtl", "bill_state", "20"): "已确认",
    ("dwd_reconciliation_bill_dtl", "bill_state", "10"): "未确认",
    ("dwd_reconciliation_bill_dtl", "supplier_tag", "UNCONFIRMED"): "未确认供应商",
    ("dwd_reconciliation_bill_dtl", "supplier_tag", "CONFIRMED"): "已确认供应商",
    # base/region 维度值（C1, T1, T2, ...）暂未翻译，等 ontology 接 site 表
}


async def resolveColumnName(
    db: Any,
    sourceTable: str | None,
    sourceColumn: str,
) -> str:
    """列名 → 显示名（async）。

    优先查 OntologyProperty（join OntologyClass by class_id）by (source_table, source_column)；
    命中且 property_alias 非空 → 返回 alias；否则返回原 column。
    失败降级：DB 不可达 / property 缺失 → 返回原 column。
    """
    if not sourceTable or not sourceColumn:
        return sourceColumn
    try:
        result = await db.execute(
            select(OntologyProperty.property_alias)
            .join(OntologyClass, OntologyProperty.class_id == OntologyClass.id)
            .where(OntologyClass.source_table == sourceTable)
            .where(OntologyProperty.source_column == sourceColumn)
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row:
            return str(row)
    except Exception as exc:
        log.warning("resolveColumnName failed for %s.%s: %s", sourceTable, sourceColumn, exc)
    return sourceColumn


def resolveValueName(
    sourceTable: str | None,
    sourceColumn: str | None,
    value: str | int | float | None,
) -> str:
    """值 → 显示名。值不在 dict → 返回原 str(value)。None → '—'。"""
    if value is None:
        return "—"
    raw = str(value)
    if sourceTable and sourceColumn:
        key = (sourceTable, sourceColumn, raw)
        if key in DISPLAY_VALUE_DICT:
            return DISPLAY_VALUE_DICT[key]
    return raw