"""nl2sql_dialects 单测。

2026-10-08 补：dateArithmeticRule 字段。
- 背景：research_finding.supporting_data 实锤 LLM 在 Oracle 上写出
  `DATEADD(month, -12, CURRENT_DATE)`（SQL Server 语法）→ ORA-00904 →
  finding.rows=[] → 报告 data 章节无 chart 块、只剩 bullet_list。
- 修复契约：每个 dialect 必须有非空 `dateArithmeticRule`，prompt 注入器
  必须把它当作第 11+ 条规则拼到 System Prompt。
"""
from __future__ import annotations

from app.domain.enums import DataSourceType
from app.services.nl2sql_dialects import (
    SqlDialect,
    resolveDialect,
    _SQL_DIALECTS_ORACLE_11G,
    _SQL_DIALECTS_ORACLE_12C,
    _SQL_DIALECTS,
)


def test_日期算术规则_Oracle_必须含_ADD_MONTHS_或_INTERVAL() -> None:
    """Oracle 没有 DATEADD/DATE_SUB，必须用 ADD_MONTHS 或 INTERVAL。"""
    rule = _SQL_DIALECTS_ORACLE_11G.dateArithmeticRule
    assert rule, "Oracle 11g 必须定义 dateArithmeticRule"
    assert "ADD_MONTHS" in rule, (
        f"Oracle 规则必须教 LLM 用 ADD_MONTHS（实际：{rule!r}）"
    )
    assert "DATEADD" in rule, (
        f"Oracle 规则必须**显式禁用** DATEADD（实际：{rule!r}）"
    )


def test_日期算术规则_Oracle12c_同样_必须含_ADD_MONTHS() -> None:
    """Oracle 12c+ 与 11g 共用日期算术规则（区别只在 LIMIT 子句）。"""
    rule = _SQL_DIALECTS_ORACLE_12C.dateArithmeticRule
    assert rule
    assert "ADD_MONTHS" in rule


def test_日期算术规则_PostgreSQL_必须含_INTERVAL() -> None:
    """PostgreSQL 用 `date - INTERVAL 'N' MONTH` / `date + INTERVAL 'N' day`。"""
    dialect = _SQL_DIALECTS[DataSourceType.POSTGRESQL]
    rule = dialect.dateArithmeticRule
    assert rule, "PostgreSQL 必须定义 dateArithmeticRule"
    assert "INTERVAL" in rule
    assert "DATEADD" in rule, "PostgreSQL 规则必须显式禁用 DATEADD"


def test_日期算术规则_MySQL_必须含_DATE_SUB_或_DATE_ADD() -> None:
    """MySQL 用 `DATE_SUB(date, INTERVAL N MONTH)` / `DATE_ADD(...)`。"""
    dialect = _SQL_DIALECTS[DataSourceType.MYSQL]
    rule = dialect.dateArithmeticRule
    assert rule, "MySQL 必须定义 dateArithmeticRule"
    assert "DATE_SUB" in rule or "DATE_ADD" in rule
    assert "DATEADD" in rule, "MySQL 规则必须显式禁用 DATEADD"


def test_resolveDialect_返回的方言必须带日期算术规则() -> None:
    """运行时入口必须返回的方言都带非空 dateArithmeticRule（防御未来新增 dialect 漏配）。"""
    for ds_type in (DataSourceType.ORACLE, DataSourceType.POSTGRESQL, DataSourceType.MYSQL):
        dialect = resolveDialect(ds_type)
        assert dialect.dateArithmeticRule, (
            f"{ds_type.value} 的 SqlDialect.dateArithmeticRule 不可为空，"
            f"否则 LLM 仍可能写出 DATEADD 等错误方言函数"
        )


def test_SqlDialect_dateArithmeticRule_可实例化() -> None:
    """field 可选且默认空串（向后兼容；不传时旧代码不受影响）。"""
    d = SqlDialect(
        name="X",
        limitRule="",
        joinTemplate="",
        useSchemaPrefix=False,
        sampleLimitSql="",
    )
    assert d.dateArithmeticRule == ""

    d2 = SqlDialect(
        name="X",
        limitRule="",
        joinTemplate="",
        useSchemaPrefix=False,
        sampleLimitSql="",
        dateArithmeticRule="use ADD_MONTHS",
    )
    assert d2.dateArithmeticRule == "use ADD_MONTHS"
