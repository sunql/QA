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


# ---------------------------------------------------------------------------
# SQL Guard（2026-10-08 补）：LLM 写的 SQL 与目标 dialect 不符 ⇒ 拦截并喂回反馈
#
# 真机证据：研究复盘 8/8 finding 失败，LLM 在 19c Oracle 上写出
#   `WHERE arr_month >= DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH), '%Y-%m')`
# ⇒ ORA-00907。`dateArithmeticRule` 已经禁了这些函数，但 LLM 视而不见 —— 必须**执行前**
# 用 sqlparse 反向校验（防御深度）。详见 `checkSqlDialect` 文档。
# ---------------------------------------------------------------------------


def test_checkSqlDialect_Oracle_禁用_DATE_FORMAT() -> None:
    """2026-10-08 复盘 1：LLM 把 MySQL `DATE_FORMAT` 写进 Oracle SQL → ORA-00904。

    守卫必须在执行前检出，给 retry 通道喂回违规列表 + 正确写法提示。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT DATE_FORMAT(CURRENT_DATE, '%Y-%m') AS m FROM DUAL",
        DataSourceType.ORACLE,
    )
    assert not result.ok, f"Oracle 不允许 DATE_FORMAT，但守卫放行: {result!r}"
    assert "DATE_FORMAT" in {v.function for v in result.violations}


def test_checkSqlDialect_Oracle_ADD_MONTHS_合规_放行() -> None:
    """守卫不能误伤：Oracle 原生函数 ADD_MONTHS / TO_CHAR / TRUNC / EXTRACT 都不能标违规。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT TO_CHAR(ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -6), 'YYYY-MM') AS m FROM DUAL",
        DataSourceType.ORACLE,
    )
    assert result.ok, f"Oracle 原生函数应放行，实际: {result!r}"


def test_checkSqlDialect_Oracle_复盘实证_完整_MySQL_SQL() -> None:
    """2026-10-08 复盘 2：LLM 真机写出的整条 SQL —— DATE_FORMAT + DATE_SUB + INTERVAL
    三件套同时出现。守卫必须一次检出全部三个违规，hint 喂回 LLM 后能触发 retry。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    badSql = (
        "SELECT TO_CHAR(arr_month, 'YYYY-MM') AS m, SUM(qty) AS total "
        "FROM t1 "
        "WHERE arr_month >= DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH), '%Y-%m') "
        "GROUP BY TO_CHAR(arr_month, 'YYYY-MM')"
    )
    result = checkSqlDialect(badSql, DataSourceType.ORACLE)
    assert not result.ok
    found = {v.function for v in result.violations}
    assert {"DATE_FORMAT", "DATE_SUB", "INTERVAL"} <= found, (
        f"应同时检出 DATE_FORMAT / DATE_SUB / INTERVAL，实际: {found}"
    )
    # hint 必须能让 LLM 知道怎么改
    assert "TO_CHAR" in result.hint or "ADD_MONTHS" in result.hint
    assert "DATE_FORMAT" in result.hint
    assert "DATE_SUB" in result.hint
    assert "INTERVAL" in result.hint


def test_checkSqlDialect_MySQL_原生_DATE_FORMAT_合规_放行() -> None:
    """MySQL 自身的 `DATE_FORMAT` / `DATE_SUB` 是合规的，守卫不能误伤。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT DATE_FORMAT(t.dt, '%Y-%m') AS m FROM t1 t",
        DataSourceType.MYSQL,
    )
    assert result.ok, f"MySQL 原生 DATE_FORMAT 应放行: {result!r}"


def test_checkSqlDialect_MySQL_禁用_Oracle_ADD_MONTHS() -> None:
    """LLM 把 Oracle `ADD_MONTHS` 写进 MySQL ⇒ MySQL 报 function not found。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT ADD_MONTHS(t.dt, -1) AS m FROM t1 t",
        DataSourceType.MYSQL,
    )
    assert not result.ok
    assert "ADD_MONTHS" in {v.function for v in result.violations}


def test_checkSqlDialect_PostgreSQL_原生_INTERVAL_合规_放行() -> None:
    """PostgreSQL 用 `INTERVAL '6' MONTH` 是合规的；守卫禁 Oracle 语法即可。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT t.dt - INTERVAL '6' MONTH AS six_months_ago FROM t1 t",
        DataSourceType.POSTGRESQL,
    )
    assert result.ok, f"PostgreSQL 原生 INTERVAL 应放行: {result!r}"


def test_checkSqlDialect_PostgreSQL_禁用_Oracle_ADD_MONTHS() -> None:
    """PostgreSQL 同样不允许 Oracle 专属的 `ADD_MONTHS`。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT ADD_MONTHS(t.dt, -1) AS m FROM t1 t",
        DataSourceType.POSTGRESQL,
    )
    assert not result.ok
    assert "ADD_MONTHS" in {v.function for v in result.violations}


def test_checkSqlDialect_空_SQL_不_违规() -> None:
    """空 SQL / None 不应报违规（由上游 `STEP_MISSING_SQL` 处理，无 SQL 守卫不参与）。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    for empty in (None, "", "   ", "\n  \t"):
        result = checkSqlDialect(empty, DataSourceType.ORACLE)
        assert result.ok, f"空 SQL 应放行（{empty!r}）：{result!r}"


def test_checkSqlDialect_大小写_不敏感() -> None:
    """LLM 写 `date_format` / `Date_Format` 都应被检出（正则转大写比对）。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    for variant in ("date_format", "Date_Format", "DATE_FORMAT"):
        result = checkSqlDialect(
            f"SELECT {variant}(CURRENT_DATE, '%Y-%m') AS m FROM DUAL",
            DataSourceType.ORACLE,
        )
        assert not result.ok, f"变体 {variant} 应被检出：{result!r}"


def test_checkSqlDialect_列名含_关键词_不_误伤() -> None:
    """防御：列名 `interval_id` / `format_col` 不应被当作 INTERVAL 关键字违规。
    INTERVAL 关键字必须**后跟数字或引号**才算违规（regex 见 nl2sql_dialects 中的 _BANNED_KEYWORDS_ORACLE）。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT t.interval_id, t.format_col FROM t1 t",
        DataSourceType.ORACLE,
    )
    assert result.ok, f"列名中的 interval / format 不应误伤：{result!r}"
