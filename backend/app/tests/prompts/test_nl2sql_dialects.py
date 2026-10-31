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
    _SQL_DIALECTS_ORACLE_11G,
    _SQL_DIALECTS_ORACLE_12C,
    DialectRegistry,
    SqlDialect,
    resolveDialect,
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
    19c 实际 ≥ 12c 必须走 FETCH FIRST 12c 路径（且 dialect-registry refactor 后有专属 19c notes）。
    """
    dialect = resolveDialect(DataSourceType.ORACLE, oracle_version="19c")
    # 不能用 `is` identity（refactor 后 19c 走 _SQL_DIALECTS_ORACLE_19C 克隆，不再返回原 12c）
    # 改用语义断言：sampleLimitSql 必须是 12c 风格（FETCH FIRST），且 19c 专属 notes 已注入
    assert "FETCH FIRST" in dialect.sampleLimitSql, (
        f"19c sampleLimitSql 必须用 FETCH FIRST（实际 {dialect.sampleLimitSql!r}）"
    )
    assert "ROWNUM" not in dialect.sampleLimitSql, (
        f"19c sampleLimitSql 不能误判为 11g 走 ROWNUM（实际 {dialect.sampleLimitSql!r}）"
    )
    assert "ADD_MONTHS" in dialect.notes, (
        f"19c 应有专属 notes（实际 {dialect.notes!r}）"
    )


def test_resolveDialect_Oracle11g_字符串_应走_11g() -> None:
    """11g 字面量 / 11.2.x / 9.x / 10.x 都应解析为 11g。"""
    for ver in ("11g", "11.2.0.1.0", "9i", "10g", "11gR2"):
        d = resolveDialect(DataSourceType.ORACLE, oracle_version=ver)
        # 不用 `is` identity（refactor 后返回 _SQL_DIALECTS_ORACLE_11G_REGISTERED 克隆）
        assert "ROWNUM" in d.sampleLimitSql, (
            f"{ver} sampleLimitSql 应走 11g 分支用 ROWNUM（实际 {d.sampleLimitSql!r}）"
        )
        assert "FETCH FIRST" not in d.sampleLimitSql, (
            f"{ver} sampleLimitSql 不应误判为 12c 走 FETCH FIRST（实际 {d.sampleLimitSql!r}）"
        )


def test_日期算术规则_Oracle_最近N个月_模板必须带上下界() -> None:
    """off-by-one 回归（2026-10-31）：`>= ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'), -N)`
    会把「最近 N 个月」算成 N+1 个自然月（本月再往前数 N 个月），永远多算一个月。
    模板必须给出带下界（本月及前 N-1 个月）与上界（< 下月 1 号）的完整示例。
    """
    for dialect in (_SQL_DIALECTS_ORACLE_11G, _SQL_DIALECTS_ORACLE_12C):
        rule = dialect.dateArithmeticRule
        # 旧的 off-by-one 写法必须消失（否则 LLM 直接照抄就多算一个月）
        assert "ADD_MONTHS(TRUNC(CURRENT_DATE, 'MM'), -N)" not in rule, (
            f"Oracle 日期算术模板仍含 off-by-one 写法（实际：{rule!r}）"
        )
        # 6 个自然月的正确上下界必须出现：>= 本月-5，且 < 下月 1 号
        assert "ADD_MONTHS(TRUNC(CURRENT_DATE, 'MM'), -5)" in rule, (
            f"Oracle 模板必须给出 6 个自然月的下界示例 -5（实际：{rule!r}）"
        )
        assert "ADD_MONTHS(TRUNC(CURRENT_DATE, 'MM'), 1)" in rule, (
            f"Oracle 模板必须给出上界 < 下月 1 号（实际：{rule!r}）"
        )


def test_日期算术规则_Oracle12c_同样_必须含_ADD_MONTHS() -> None:
    """Oracle 12c+ 与 11g 共用日期算术规则（区别只在 LIMIT 子句）。"""
    rule = _SQL_DIALECTS_ORACLE_12C.dateArithmeticRule
    assert rule
    assert "ADD_MONTHS" in rule


def test_日期算术规则_PostgreSQL_必须含_INTERVAL() -> None:
    """PostgreSQL 用 `date - INTERVAL 'N' MONTH` / `date + INTERVAL 'N' day`。"""
    dialect = DialectRegistry.default().resolve(DataSourceType.POSTGRESQL)
    rule = dialect.dateArithmeticRule
    assert rule, "PostgreSQL 必须定义 dateArithmeticRule"
    assert "INTERVAL" in rule
    assert "DATEADD" in rule, "PostgreSQL 规则必须显式禁用 DATEADD"


def test_日期算术规则_MySQL_必须含_DATE_SUB_或_DATE_ADD() -> None:
    """MySQL 用 `DATE_SUB(date, INTERVAL N MONTH)` / `DATE_ADD(...)`。"""
    dialect = DialectRegistry.default().resolve(DataSourceType.MYSQL)
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


# ---------------------------------------------------------------------------
# A1 (2026-10-08 续)：Oracle 守卫扩 LIMIT 关键字
#
# 真机证据：研究复盘 finding #1 写 `ORDER BY rcv_month DESC LIMIT 12` → ORA-00933
# （"SQL 命令未正确结束"）。Oracle 不支持 LIMIT 关键字（11g 用 ROWNUM，12c+ 用
# FETCH FIRST），LLM 在 plan 阶段没意识到这一点，又没守卫拦，于是带病落地。
# 修法：在 _ORACLE_KEYWORD_PATTERNS 增 `\bLIMIT\s+\d+`（强制后跟数字，避免误伤
# 列名 `limit_count` / 字符串字面量 `'LIMIT'`）。MySQL/PG 保留 LIMIT，**不**扩。
# ---------------------------------------------------------------------------


def test_checkSqlDialect_Oracle_LIMIT_12_被拦() -> None:
    """2026-10-08 复盘 4：LLM 在 19c Oracle 上写 `LIMIT 12`（MySQL/PG 习惯），
    守卫之前**没拦**这条规则 → ORA-00933 → 0 行 → 报告 data 章节无 chart。
    守卫必须检出 LIMIT 并给出 FETCH FIRST/ROWNUM 替代提示。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    bad_sql = (
        "SELECT rcv_month, SUM(rcv_qty) AS total "
        "FROM DWD_GOODS_RECEIPT_DTL "
        "GROUP BY rcv_month ORDER BY rcv_month DESC LIMIT 12"
    )
    result = checkSqlDialect(bad_sql, DataSourceType.ORACLE)
    assert not result.ok, f"Oracle 不允许 LIMIT 关键字，但守卫放行：{result!r}"
    assert "LIMIT" in {v.function for v in result.violations}
    # hint 必须能让 LLM 知道怎么改（给两个版本的写法让 LLM 选）
    assert "FETCH FIRST" in result.hint or "ROWNUM" in result.hint
    # 喂回 LLM 用的标准化前缀（与现有 INTERVAL 守卫同款）
    assert "oracle dialect 不允许" in result.hint


def test_checkSqlDialect_Oracle_LIMIT_后_无数字_不误伤() -> None:
    r"""防御：`LIMIT` 作为别名 / 单独出现时不应误伤（字符串字面量内的 LIMIT 12
    会被误拦——这与 INTERVAL 守卫行为一致，是已知限制；测试对齐 INTERVAL 风格）。
    正则强制 `LIMIT\s+\d+`，**不**要求所有 LIMIT 出现都拦——只拦"行数限制语义"。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    cases = [
        "SELECT 'LIMIT' AS tag FROM dual",                        # 字符串字面量
        "SELECT 1 AS LIMIT FROM dual",                            # 别名
    ]
    for sql in cases:
        result = checkSqlDialect(sql, DataSourceType.ORACLE)
        assert result.ok, f"`{sql}` 不应误伤（actual: {result!r}）"


def test_checkSqlDialect_列名_limit_count_不误伤() -> None:
    r"""防御：列名 `limit_count` 不应被误伤为 LIMIT 关键字违规。
    正则 `\bLIMIT\s+\d+` 强制 LIMIT 边界 + 后跟数字，列名末尾的 `_count` 不构成数字。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT t.limit_count, t.limit_total FROM t1 t WHERE t.limit_count > 0",
        DataSourceType.ORACLE,
    )
    assert result.ok, f"列名 limit_count / limit_total 不应误伤：{result!r}"


def test_checkSqlDialect_MySQL_LIMIT_N_合规_放行() -> None:
    """MySQL 自身的 `LIMIT N` 是合规的，守卫**不**应在 MySQL 上拦 LIMIT。
    防止"加了 Oracle 守卫后误伤其他 dialect"。
    """
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT id, name FROM t1 ORDER BY id LIMIT 12",
        DataSourceType.MYSQL,
    )
    assert result.ok, f"MySQL 原生 LIMIT 应放行：{result!r}"


def test_checkSqlDialect_PostgreSQL_LIMIT_N_合规_放行() -> None:
    """PostgreSQL 自身的 `LIMIT N` 是合规的，守卫**不**应在 PG 上拦 LIMIT。"""
    from app.services.nl2sql_dialects import checkSqlDialect

    result = checkSqlDialect(
        "SELECT id, name FROM t1 ORDER BY id LIMIT 12",
        DataSourceType.POSTGRESQL,
    )
    assert result.ok, f"PostgreSQL 原生 LIMIT 应放行：{result!r}"
