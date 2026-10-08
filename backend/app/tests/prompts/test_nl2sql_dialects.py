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


def test_日期算术规则_Oracle_必须显式禁用_MySQL_函数() -> None:
    """2026-10-08 补：研究复盘实证 LLM 在 Oracle 上仍写 MySQL 语法
    `DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH), '%Y-%m')` → ORA-00907。
    仅禁 `DATEADD` 不够：必须**显式列出 MySQL 常用函数 + 给正确示例**。
    """
    rule = _SQL_DIALECTS_ORACLE_11G.dateArithmeticRule
    # MySQL 专属函数必须出现在禁用清单里
    for banned in ("DATE_FORMAT", "DATE_SUB", "DATE_ADD", "INTERVAL", "CURDATE"):
        assert banned in rule, (
            f"Oracle 规则必须显式禁用 MySQL 函数 {banned}（否则 LLM 仍会写 MySQL 语法，"
            f"实际规则：{rule!r}）"
        )
    # 必须给正确的 Oracle 写法示例
    assert "TO_CHAR" in rule and "ADD_MONTHS" in rule, (
        "Oracle 规则必须给 TO_CHAR(ADD_MONTHS(...)) 这种正确示例（让 LLM 有可抄的模板）"
    )


def test_resolveDialect_Oracle19c_应走_12c_分支() -> None:
    """2026-10-08 补：19c 被 `version.startswith('11')` 误判为 11g 走 ROWNUM 分支。
    19c 实际 ≥ 12c 必须走 FETCH FIRST 12c 路径。
    """
    dialect = resolveDialect(DataSourceType.ORACLE, oracle_version="19c")
    assert dialect is _SQL_DIALECTS_ORACLE_12C, (
        f"19c 必须解析为 12c 方言（实际 {dialect.name}，sampleLimitSql={dialect.sampleLimitSql!r}）"
    )


def test_resolveDialect_Oracle11g_字符串_应走_11g() -> None:
    """11g 字面量 / 11.2.x / 9.x / 10.x 都应解析为 11g。"""
    for ver in ("11g", "11.2.0.1.0", "9i", "10g", "11gR2"):
        d = resolveDialect(DataSourceType.ORACLE, oracle_version=ver)
        assert d is _SQL_DIALECTS_ORACLE_11G, f"{ver} 应走 11g 分支（实际 {d.name}）"


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
