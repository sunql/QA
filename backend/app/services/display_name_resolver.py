"""显示名解析：列名/值 → 中文标签。

数据源（列名，按优先级）：
1. OntologyProperty.property_alias（DB 查；真实库填充率低，但一旦治理即自动生效）
2. DISPLAY_COLUMN_DICT 代码内字典（键 = (大写表名, 大写列名)）
3. 原列名

数据源（值）：DISPLAY_VALUE_DICT 代码内字典（键 = (大写表名, 大写列名, 原值)）。

失败全部降级：DB 不可达 / property 缺失 / 不在 dict → 返回原 column/value。

派生：`deriveSourceTable` 从 finding 的支撑 SQL 的 FROM/JOIN 子句派生来源表 ——
R3 从未把 sourceTable 写进 supporting_data（实测：真实会话的 supporting_data 只有
rows/error/stats/rowCount），故列名翻译在真实数据上完全无效；读侧派生是唯一能同时
修复历史会话的方式。

大小写：行数据键是小写（site_code），本体列/字典键是大写（SITE_CODE）——查表前
一律 `.upper()` 归一。本体层的 DB 比较同样两侧归一（`func.upper(...) == 归一值`）：
真实库 `ontology_class.source_table` 是小写（见 ontology_service 的类级索引），
而 `source_column` 来自数仓列名（大写），两侧大小写不齐 —— 不归一就永远查不中。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from sqlalchemy import func, select

from app.domain.models import OntologyClass, OntologyProperty

log = logging.getLogger(__name__)

__all__ = [
    "DISPLAY_COLUMN_DICT",
    "DISPLAY_VALUE_DICT",
    "deriveSourceTable",
    "resolveColumnName",
    "resolveValueName",
]

# (大写 source_table, 大写 source_column) → 中文列名
# 初始集 = R2 真机 e2e 报告里出现的全部结果集列名（含 LLM 的聚合别名 bill_cnt/amt）。
DISPLAY_COLUMN_DICT: Final[dict[tuple[str, str], str]] = {
    ("DWD_RECONCILIATION_BILL_DTL", "SITE_CODE"): "基地",
    ("DWD_RECONCILIATION_BILL_DTL", "PERIOD_MONTH"): "月份",
    ("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE"): "确认阶段",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_STATE"): "账单状态",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_TAG"): "供应商标记",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_CODE"): "供应商编码",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_NAME"): "供应商名称",
    ("DWD_RECONCILIATION_BILL_DTL", "TOTAL_AMOUNT"): "含税金额",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_CNT"): "账单数",
    ("DWD_RECONCILIATION_BILL_DTL", "AMT"): "金额",
    ("DWD_RECONCILIATION_BILL_DTL", "PAYABLE_AMOUNT"): "应付金额",
}

# (大写 source_table, 大写 source_column, raw_value) → 中文值
DISPLAY_VALUE_DICT: Final[dict[tuple[str, str, str], str]] = {
    ("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE", "QTY"): "数量确认",
    ("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE", "PRICE"): "价格确认",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_STATE", "20"): "已确认",
    ("DWD_RECONCILIATION_BILL_DTL", "BILL_STATE", "10"): "未确认",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_TAG", "UNCONFIRMED"): "未确认供应商",
    ("DWD_RECONCILIATION_BILL_DTL", "SUPPLIER_TAG", "CONFIRMED"): "已确认供应商",
    # 基地值（C1 / T1 / T7 …）需 site 主数据才能译名，暂不做 —— 列名「基地」已让
    # 「基地=C1」可读。
}

# FROM / JOIN 后的标识符：可选 schema 前缀、可带 ANSI 双引号。
_TABLE_REF_RE: Final = re.compile(
    r"\b(?:FROM|JOIN)\s+("
    r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
    r'(?:\.(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*))*'
    r")",
    re.IGNORECASE,
)


def deriveSourceTable(sql: str | None) -> str | None:
    """从支撑 SQL 派生来源表名（大写）。

    只取**第一个** FROM/JOIN 的标识符：报告翻译不做多表消歧，而事实表在 LLM 生成的
    聚合 SQL 里总是首个 FROM（CTE 体内也算 —— 那正是真实业务表）。取不到 → None，
    调用方据此把翻译整体降级为原样（与 R3 行为一致，不会更糟）。

    刻意**不查本体白名单**：下游 dict / DB 查询都是精确匹配，误判（如 CTE 名）自然
    落空 → 降级原样，无副作用；而查库会让本函数从纯函数退化。
    """
    if not sql:
        return None
    match = _TABLE_REF_RE.search(sql)
    if match is None:
        return None
    ident = match.group(1).split(".")[-1]
    ident = ident.strip().strip('"').strip()
    return ident.upper() or None


async def resolveColumnName(
    db: Any,
    sourceTable: str | None,
    sourceColumn: str,
) -> str:
    """列名 → 显示名。

    本体 `property_alias` 优先；落空则查 `DISPLAY_COLUMN_DICT`；再落空返原列名。
    `db=None`（降级路径）跳过本体层。DB 异常只 warning，不影响主流程。
    """
    if not sourceColumn:
        return sourceColumn
    table = (sourceTable or "").upper()
    column = str(sourceColumn).upper()
    if db is not None and table:
        try:
            result = await db.execute(
                select(OntologyProperty.property_alias)
                .join(OntologyClass, OntologyProperty.class_id == OntologyClass.id)
                .where(func.upper(OntologyClass.source_table) == table)
                .where(func.upper(OntologyProperty.source_column) == column)
                .limit(1)
            )
            alias = result.scalar_one_or_none()
            if alias:
                return str(alias)
        except Exception as exc:  # noqa: BLE001 —— 翻译只是显示装饰，读失败不该炸报告
            log.warning("resolveColumnName 本体别名查询失败 %s.%s: %s", table, column, exc)
    return DISPLAY_COLUMN_DICT.get((table, column), sourceColumn)


def resolveValueName(
    sourceTable: str | None,
    sourceColumn: str | None,
    value: str | int | float | None,
) -> str:
    """值 → 显示名。值不在 dict → 返回原 str(value)。None → '—'。

    表/列归一为大写后查表；**值本身不做大小写归一**（值是数据，大小写敏感）。
    """
    if value is None:
        return "—"
    raw = str(value)
    if sourceTable and sourceColumn:
        key = (sourceTable.upper(), sourceColumn.upper(), raw)
        if key in DISPLAY_VALUE_DICT:
            return DISPLAY_VALUE_DICT[key]
    return raw
