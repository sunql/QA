"""SQL 方言定义（从 nl2sql_service 拆出）。

按数据源类型（Oracle/MySQL/PostgreSQL）声明行数限制、JOIN 模板、schema 前缀、
值域采样、NULL 排序、时间粒度分组等方言规则片段。`resolveDialect` 是运行时解析入口。

未来扩展：新增一种 SQL 方言只需在本模块加一个 `SqlDialect` 实例 + `_SQL_DIALECTS` 表项。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.domain.enums import DataSourceType
from app.domain.exceptions import ValidationError
from app.services.messages_zh import MSG_DATASOURCE_TYPE_UNKNOWN

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SqlDialect:
    """一种业务库 SQL 方言的 prompt 规则片段。

    - limitRule：行数限制规则的完整句子（不含序号），注入 System Prompt。
    - joinTemplate：JOIN 别名用法示例的模板，含 {schema} 占位符（由 schema 前缀
      填充，如 `ZJTH.`；不使用前缀的方言填空串）。表名仅为语法示范，禁止照抄。
    - useSchemaPrefix：该方言是否使用 "schema 前缀" 限定表名（Oracle 的
      username 即 schema owner；MySQL/PostgreSQL 无此惯例，不注入前缀提示）。
    - sampleLimitSql：值域采样去重查询的取前 N 行语法模板（2-1），含 {sql}/{n}
      占位符；Oracle 11g 用 ROWNUM 子查询，12c 用 FETCH FIRST，其余用 LIMIT。
    - timeBucketRule：按时间粒度（月/年/季度）分组的方言写法规则，注入 System Prompt。
    - aggregateRule：SELECT 列表中「聚合函数与标量子查询」能否混排的方言限制，注入 System Prompt。
    """

    name: str
    limitRule: str
    joinTemplate: str
    useSchemaPrefix: bool
    sampleLimitSql: str
    identifierRule: str = ""
    nullOrderingRule: str = ""
    timeBucketRule: str = ""
    aggregateRule: str = ""
    # 日期算术（"最近 N 月/天"、"X 月以来" 等相对时间在 WHERE / 表达式里的写法）
    # 2026-10-08 补：研究 finding 实锤 LLM 在 Oracle 上写出 DATEADD(month, -12, CURRENT_DATE)
    # → ORA-00904 → finding.rows=[] → 报告 data 章节无 chart 块。每种 dialect 必须有非空规则，
    # 且必须**显式禁用**其它方言的日期函数（仅靠推荐函数不足以让 LLM 改习惯）。
    dateArithmeticRule: str = ""

    def boundedDistinct(self, table: str, column: str, n: int) -> str:
        """构造取前 n 行去重值查询（表/列已过标识符白名单校验）。"""
        return self.sampleLimitSql.format(
            sql=f"SELECT DISTINCT {column} FROM {table}", n=n
        )


# 跨年/分组对比的排序 NULL 规则：Oracle 与 PostgreSQL 的 ORDER BY ... DESC 默认
# 将 NULL 排在最前（NULLS FIRST），仅部分分组有数据的行（聚合值为 NULL）会排到
# top-N 之前。MySQL 的 DESC 默认 NULLS LAST，无需此规则。
_NULL_ORDERING_RULE = (
    "按聚合结果或派生列排序时必须处理 NULL：ORDER BY ... DESC 默认将 NULL 排在最前"
    "（NULLS FIRST），跨年/分组对比时仅部分分组有数据的行（聚合值为 NULL）会排到最前，"
    "导致 top-N 取到错误数据。请在排序键后显式加 NULLS LAST，例如 "
    "ORDER BY TOTAL_QTY_2025 DESC NULLS LAST；跨年对比建议先在子查询中按目标年份取 top-N，"
    "再 LEFT JOIN 其他年份的数据。"
)

# 时间粒度分组规则（按月/按年/按季度/按周/按天 分组时对日期列做截断，勿按原始时间戳分组；
# 与计划阶段规则 5 呼应，同一截断表达式须在 SELECT 与 GROUP BY 中一致出现）。
_TIME_BUCKET_RULE_ORACLE = (
    "按时间粒度分组时对日期列做截断，不要按原始时间戳分组：按月用 "
    "TO_CHAR(日期列,'YYYY-MM') 或 EXTRACT(YEAR FROM 日期列)||'-'||EXTRACT(MONTH FROM 日期列)，"
    "按年用 TO_CHAR(日期列,'YYYY') 或 EXTRACT(YEAR FROM 日期列)，"
    "按季度用 TO_CHAR(日期列,'YYYY-Q')；并在 SELECT 输出同一截断表达式作为月份/年份列。"
)
_TIME_BUCKET_RULE_POSTGRESQL = (
    "按时间粒度分组时对日期列做截断，不要按原始时间戳分组：按月用 "
    "DATE_TRUNC('month', 日期列) 或 TO_CHAR(日期列,'YYYY-MM')，"
    "按年用 DATE_TRUNC('year', 日期列) 或 TO_CHAR(日期列,'YYYY')，"
    "按季度用 DATE_TRUNC('quarter', 日期列)；并在 SELECT 输出同一截断表达式作为月份/年份列。"
)
_TIME_BUCKET_RULE_MYSQL = (
    "按时间粒度分组时对日期列做格式化截断，不要按原始时间戳分组：按月用 "
    "DATE_FORMAT(日期列,'%Y-%m')，按年用 DATE_FORMAT(日期列,'%Y')，"
    "按季度用 CONCAT(YEAR(日期列),'-Q',QUARTER(日期列))；并在 SELECT 输出同一表达式作为月份/年份列。"
)

# 日期算术：相对时间（"最近 N 月/天"、"X 月以来"）在 WHERE 子句 / 表达式中的方言写法。
# 2026-10-08 二次补：仅禁 DATEADD 不够——研究复盘实证 LLM 看到 19c Oracle 时仍写
# MySQL 语法 `DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH), '%Y-%m')` → ORA-00907。
# 修复契约：每条规则**必须**显式禁用其它方言的常用函数（DATEADD/DATE_FORMAT/DATE_SUB/
# INTERVAL/CURDATE 等），**并给出可直接抄写的 Oracle 正确示例**（TO_CHAR + ADD_MONTHS），
# 让 LLM 有模板可套，不能光说"不要用"。
_DATE_ARITHMETIC_RULE_ORACLE = (
    "相对时间（'最近 N 个月'、'近 N 天'、'X 个月以来'、'同比/环比'）必须用 Oracle 原生写法。"
    "**正确示例**（直接抄）："
    "① 最近 N 个月（按月截断 + 过滤）："
    "   WHERE date_col >= ADD_MONTHS(TRUNC(CURRENT_DATE, 'MM'), -N)"
    "   或 SELECT TO_CHAR(ADD_MONTHS(CURRENT_DATE, -N), 'YYYY-MM') AS m FROM DUAL;"
    "② 最近 N 天：WHERE date_col >= CURRENT_DATE - N"
    "③ 月度趋势：SELECT TO_CHAR(ADD_MONTHS(CURRENT_DATE, -LEVEL+1), 'YYYY-MM') ... CONNECT BY LEVEL <= N"
    "或：GROUP BY TO_CHAR(ADD_MONTHS(SYSDATE, -rownum+1), 'YYYY-MM') 这种生成序列的方式。"
    "**绝对禁止**以下函数（在 Oracle 上必然报错）："
    "DATEADD（SQL Server，ORA-00904）、DATEDIFF（SQL Server）、DATEPART、DATE_PART；"
    "DATE_FORMAT（MySQL，ORA-00904）、DATE_SUB / DATE_ADD（MySQL，ORA-00904）、"
    "INTERVAL 'N' MONTH（PostgreSQL 语法，Oracle 不支持）、CURDATE（MySQL）；"
    "以及任何 `WHERE date_col >= date_col - INTERVAL N MONTH` 形态（INTERVAL 不是 Oracle 字面量）。"
    "基准时间用 CURRENT_DATE 或 TRUNC(SYSDATE)（视是否需要时分秒）。"
)
_DATE_ARITHMETIC_RULE_POSTGRESQL = (
    "相对时间用 PostgreSQL 原生写法：按月/天加减用 日期列 - INTERVAL 'N' MONTH 或 "
    "日期列 - INTERVAL 'N' DAY；按季度用 日期列 - INTERVAL 'N' QUARTER；"
    "基准时间用 CURRENT_DATE 或 NOW()。"
    "**绝对禁止** DATEADD（SQL Server 语法，PostgreSQL 会报 function dateadd does not exist）、"
    "ADD_MONTHS（Oracle 专属）、DATE_SUB（MySQL 语法）。"
)
_DATE_ARITHMETIC_RULE_MYSQL = (
    "相对时间用 MySQL 原生写法：按月/天加减用 DATE_SUB(日期列, INTERVAL N MONTH) 或 "
    "DATE_ADD(日期列, INTERVAL N DAY)；基准时间用 CURDATE() 或 NOW()。"
    "**绝对禁止** DATEADD（SQL Server 语法，MySQL 会报 FUNCTION db.DATEADD does not exist）、"
    "ADD_MONTHS（Oracle 专属）、INTERVAL 'N' MONTH（PostgreSQL 语法）。"
)

# 聚合与标量子查询不得混排：Oracle 禁止在同一个查询块的 SELECT 列表中把聚合函数
# 与标量子查询**并列**（ORA-00937「不是单组分组函数」），PostgreSQL 允许该写法。
# 真机回归（2026-10-02）：问「5月份供货量最多的三家供应商所供货物总量占比」，
# Top-N 占比被写成 SELECT SUM(t.QTY) / (SELECT SUM(s.QTY) FROM supplier_qty s) FROM topn t。
# 已在生产 Oracle 库实测：两侧都改成标量子查询、外层 FROM DUAL 即通过。
_AGGREGATE_RULE_ORACLE = (
    "SELECT 列表中不得把聚合函数与标量子查询并列混排（Oracle 会报 ORA-00937「不是单组分组函数」；"
    "PostgreSQL 允许但 Oracle 不允许）：例如 "
    "SELECT SUM(t.QTY) / (SELECT SUM(QTY) FROM all_rows) FROM topn t 在 Oracle 必然失败。"
    "改法**仅在分母来自另一个结果集**（全局合计、Top-N 求和）时适用：把分子分母都写成标量子查询、外层用 FROM DUAL，"
    "例如 SELECT (SELECT SUM(QTY) FROM topn) / NULLIF((SELECT SUM(QTY) FROM all_rows), 0) AS RATIO FROM DUAL。"
    "若只是逐组占比（每个供应商、每月各占多少），保持窗口函数 SUM(x) / SUM(SUM(x)) OVER () 形态，"
    "不要为此加 FROM DUAL —— 那会把逐组行塌缩成单行。"
)

_SQL_DIALECTS_ORACLE_11G = SqlDialect(
    name="Oracle",
    limitRule="需要限制行数时使用 ROWNUM，例如 SELECT * FROM (SELECT t.*, ROWNUM rn FROM (...) t WHERE ROWNUM <= 1000)，不要使用 FETCH FIRST，也不要使用 LIMIT。",
    joinTemplate="FROM {schema}PRECEIPTD d JOIN {schema}PRECEIPT h ON h.PTHNUM_0 = d.PTHNUM_0",
    useSchemaPrefix=True,
    sampleLimitSql="SELECT * FROM ({sql}) WHERE ROWNUM <= {n}",
    nullOrderingRule=_NULL_ORDERING_RULE,
    timeBucketRule=_TIME_BUCKET_RULE_ORACLE,
    aggregateRule=_AGGREGATE_RULE_ORACLE,
    dateArithmeticRule=_DATE_ARITHMETIC_RULE_ORACLE,
    identifierRule=(
        "列别名与表别名不得以数字开头（Oracle 标识符规则），否则必须用双引号包裹，"
        "例如 AS 2025采购量 未加引号会报 ORA-00923。建议别名用字母或中文开头，"
        "如 AS AVG_PRICE_2025；按年份分区/跨年对比时用 CASE WHEN 并在别名中带年份，"
        "例如 AVG(CASE WHEN EXTRACT(YEAR FROM d.ORDDAT_0) = 2025 THEN d.CPRPRI_0 END) AS AVG_PRICE_2025。"
    ),
)
_SQL_DIALECTS_ORACLE_12C = SqlDialect(
    name="Oracle",
    limitRule="需要限制行数时使用 Oracle 的 FETCH FIRST N ROWS ONLY，不要使用 LIMIT 或 ROWNUM。",
    joinTemplate="FROM {schema}PRECEIPTD d JOIN {schema}PRECEIPT h ON h.PTHNUM_0 = d.PTHNUM_0",
    useSchemaPrefix=True,
    sampleLimitSql="{sql} FETCH FIRST {n} ROWS ONLY",
    nullOrderingRule=_NULL_ORDERING_RULE,
    timeBucketRule=_TIME_BUCKET_RULE_ORACLE,
    aggregateRule=_AGGREGATE_RULE_ORACLE,
    dateArithmeticRule=_DATE_ARITHMETIC_RULE_ORACLE,
    identifierRule=(
        "列别名与表别名不得以数字开头（Oracle 标识符规则），否则必须用双引号包裹，"
        "例如 AS 2025采购量 未加引号会报 ORA-00923。建议别名用字母或中文开头，"
        "如 AS AVG_PRICE_2025；按年份分区/跨年对比时用 CASE WHEN 并在别名中带年份，"
        "例如 AVG(CASE WHEN EXTRACT(YEAR FROM d.ORDDAT_0) = 2025 THEN d.CPRPRI_0 END) AS AVG_PRICE_2025。"
    ),
)
_SQL_DIALECTS: dict[DataSourceType, SqlDialect] = {
    DataSourceType.ORACLE: _SQL_DIALECTS_ORACLE_11G,  # 默认 11g（保守）；调用方应传入 oracle_version 覆盖
    DataSourceType.MYSQL: SqlDialect(
        name="MySQL",
        limitRule="需要限制行数时使用 MySQL 的 LIMIT 子句，例如 LIMIT N，不要使用 FETCH FIRST。",
        joinTemplate="FROM sales s JOIN customers c ON c.id = s.customer_id",
        useSchemaPrefix=False,
        sampleLimitSql="{sql} LIMIT {n}",
        timeBucketRule=_TIME_BUCKET_RULE_MYSQL,
        dateArithmeticRule=_DATE_ARITHMETIC_RULE_MYSQL,
    ),
    DataSourceType.POSTGRESQL: SqlDialect(
        name="PostgreSQL",
        limitRule="需要限制行数时使用 PostgreSQL 的 LIMIT 子句，例如 LIMIT N，不要使用 FETCH FIRST。",
        joinTemplate="FROM sales s JOIN customers c ON c.id = s.customer_id",
        useSchemaPrefix=False,
        sampleLimitSql="{sql} LIMIT {n}",
        nullOrderingRule=_NULL_ORDERING_RULE,
        timeBucketRule=_TIME_BUCKET_RULE_POSTGRESQL,
        dateArithmeticRule=_DATE_ARITHMETIC_RULE_POSTGRESQL,
    ),
}


# ---------------------------------------------------------------------------
# SQL Guard（2026-10-08 补）：LLM 写的 SQL 与目标 dialect 不符 ⇒ 拦截并喂回反馈
#
# 真机证据：研究复盘 8/8 finding 失败，LLM 在 19c Oracle 上写出
#   `WHERE arr_month >= DATE_FORMAT(DATE_SUB(CURRENT_DATE, INTERVAL 6 MONTH), '%Y-%m')`
# ⇒ ORA-00907。`dateArithmeticRule` 已经禁了这些函数（防御深度第 1 层：prompt 教 LLM），
# 但 LLM 视而不见 ⇒ 必须**执行前**用 sqlparse 反向校验（防御深度第 2 层：守卫拦），
# 检出后把违规列表 + 正确写法喂回 LLM 触发 retry。
#
# 仅靠 `prompt` 教 LLM 是不够的：
# - LLM 训练语料里 MySQL/PostgreSQL 占比远大于 Oracle（19c 商用居多、私有部署），
#   `DATE_FORMAT`/`INTERVAL` 肌肉记忆很强；
# - 禁用清单写得再长，LLM 注意力不够时仍会写错；
# - 执行层 ORA-00907 ⇒ 0 行 ⇒ 报告 data 章节空 ⇒ 用户看不到数据。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DialectViolation:
    """单条方言违规（可序列化喂回 LLM 触发 retry）。"""

    function: str  # 违规的函数/关键字（大写）
    suggestion: str  # 给 LLM 的正确写法提示


@dataclass(frozen=True)
class DialectGuardResult:
    """守卫结果：要么 ok，要么列出全部违规 + 喂回 LLM 的提示。"""

    ok: bool
    violations: tuple[DialectViolation, ...]
    hint: str  # 喂回 LLM 的人类语言提示（仅在 not ok 时有意义）


# Oracle 上不允许的**函数**（大小写不敏感）。这些函数在 Oracle 19c 上必然 ORA-00904 / ORA-00907。
# 与 `dateArithmeticRule` 文本清单保持一致 —— 后者教 LLM，前者反向校验。
_BANNED_FUNCTIONS_ORACLE: frozenset[str] = frozenset({
    # SQL Server 专属
    "DATEADD", "DATEDIFF", "DATEPART", "DATE_PART",
    # MySQL 专属
    "DATE_FORMAT", "DATE_SUB", "DATE_ADD", "CURDATE", "DATE_FORMAT",
    # MySQL 5+ 的 NOW() 在 Oracle 上要换成 SYSDATE；NOW 在 Oracle 不存在
    "NOW",
})

# Oracle 上不允许的**关键字**形态。INTERVAL 是 PostgreSQL/MySQL 的字面量语法，Oracle 不支持；
# 单独的字符串/列名不会触发（正则要求后跟数字或引号字面量）。
_BANNED_KEYWORDS_ORACLE: tuple[tuple[str, str, str], ...] = (
    (r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS(date, -N) 或 date - N 替代"),
)


def _extractFunctionNames(sql: str) -> set[str]:
    """从 SQL 中提取全部函数名（UPPERCASE）。

    不用 sqlparse 的 `Token.Function` —— 它对单引号字符串（如 `DATE_FORMAT(d, '%Y-%m')`）
    会把函数名解析成 `Token.Name` 而非 Function，导致漏检。

    改用**正则**：`NAME + 左括号` 形态的 identifier 即视为函数调用（具体正则见函数体）。这对研究 SQL

    嵌套 / 重复 / 大小写混杂都收敛成大写集合。
    """
    funcs: set[str] = set()
    # 形如 DATE_FORMAT(...) / my_func(...) / "schema"."func"(...) 都命中
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", sql):
        funcs.add(match.group(1).upper())
    return funcs


def _checkBannedKeywords(sql: str, patterns: tuple[tuple[str, str, str], ...]) -> list[DialectViolation]:
    """按正则清单扫描 SQL 中的**关键字**形态违规（INTERVAL 之类）。"""
    violations: list[DialectViolation] = []
    for pattern, name, suggestion in patterns:
        if re.search(pattern, sql, flags=re.IGNORECASE):
            violations.append(DialectViolation(function=name, suggestion=suggestion))
    return violations


def checkSqlDialect(
    sql: str | None, datasourceType: DataSourceType | str | None
) -> DialectGuardResult:
    """反向校验 LLM 生成的 SQL 是否符合目标 dialect；违规则给出 retry 提示。

    Args:
        sql: LLM 生成的 SQL 字符串（None / 空 ⇒ 视为无违规，交给上游 `STEP_MISSING_SQL`）。
        datasourceType: 目标数据源类型（`DataSourceType` 枚举或其 `value` 字符串）。
        研究执行面 `dialectArgs["datasourceType"]` 是 `str`（与 DB 列保持 snake/value 一致），
        这里**双向**接受，调用方不必先做归一化。

    Returns:
        `DialectGuardResult.ok=True` ⇒ 合规，可执行。
        `ok=False` ⇒ 有违规，`violations` 列出全部命中项，`hint` 是拼好的 LLM retry 提示。

    注意：本守卫**不**保证 SQL 100% 正确（只覆盖日期/算术函数层面），数据库执行期的
    ORA-00907 / ORA-00904 等仍由执行面负责；本函数只拦「L100% 在某 dialect 上必然错」的
    高置信度违规，避免无谓的执行失败。
    """
    if not sql or not sql.strip():
        return DialectGuardResult(ok=True, violations=(), hint="")

    # 把字符串归一到 enum（不抛错：失败 = 放行，由上游 `coerceDatasourceType` 负责脏值）
    dsEnum: DataSourceType | None = None
    if isinstance(datasourceType, DataSourceType):
        dsEnum = datasourceType
    elif isinstance(datasourceType, str):
        for t in DataSourceType:
            if t.value == datasourceType.lower():
                dsEnum = t
                break

    if dsEnum == DataSourceType.ORACLE:
        banned = _BANNED_FUNCTIONS_ORACLE
        keywordPatterns = _BANNED_KEYWORDS_ORACLE
    elif dsEnum == DataSourceType.MYSQL:
        # MySQL 自身的 DATE_FORMAT / DATE_SUB / DATE_ADD 是合规的；禁止的是 SQL Server
        # 的 DATEADD 与 Oracle 专属的 ADD_MONTHS（MySQL 没有这个）。
        banned = frozenset({"DATEADD", "DATEDIFF", "DATEPART", "ADD_MONTHS", "EXTRACT"})
        keywordPatterns = ()
    elif dsEnum == DataSourceType.POSTGRESQL:
        # PostgreSQL 用 INTERVAL 字面量是合法的（不要禁）；禁 SQL Server / Oracle 专属。
        banned = frozenset({"DATEADD", "DATEDIFF", "DATEPART", "ADD_MONTHS", "DATE_SUB", "DATE_ADD", "DATE_FORMAT"})
        keywordPatterns = ()
    else:
        # 未知 dialect：放行（不替 resolveDialect 兜底；那是 coerceDatasourceType 的职责）
        return DialectGuardResult(ok=True, violations=(), hint="")

    found = _extractFunctionNames(sql)
    violations: list[DialectViolation] = []
    for fn in sorted(found & banned):
        suggestion = _suggestionFor(dsEnum, fn)
        violations.append(DialectViolation(function=fn, suggestion=suggestion))
    violations.extend(_checkBannedKeywords(sql, keywordPatterns))

    if not violations:
        return DialectGuardResult(ok=True, violations=(), hint="")

    hint = _buildHint(dsEnum, violations)
    return DialectGuardResult(ok=False, violations=tuple(violations), hint=hint)


def _suggestionFor(datasourceType: DataSourceType, fn: str) -> str:
    """给 LLM 的具体函数替代写法（Oracle 为主，其它 dialect 仅禁用未列建议）。"""
    if datasourceType == DataSourceType.ORACLE:
        return {
            "DATE_FORMAT": "用 TO_CHAR(date, 'YYYY-MM') 替代",
            "DATE_SUB": "用 ADD_MONTHS(date, -N) 或 date - N 替代",
            "DATE_ADD": "用 ADD_MONTHS(date, +N) 或 date + N 替代",
            "DATEADD": "用 ADD_MONTHS(date, ±N) 替代",
            "DATEDIFF": "用 date1 - date2 直接相减（返回天数）替代",
            "DATEPART": "用 EXTRACT(YEAR/MONTH/DAY FROM date) 替代",
            "DATE_PART": "用 EXTRACT(YEAR/MONTH/DAY FROM date) 替代",
            "CURDATE": "用 TRUNC(SYSDATE) 或 CURRENT_DATE 替代",
            "NOW": "用 SYSDATE 或 CURRENT_DATE 替代",
        }.get(fn, f"Oracle 不支持 {fn}，请改用 Oracle 原生函数")
    return f"{datasourceType.value} 不支持 {fn}，请改用该 dialect 的原生函数"


def _buildHint(datasourceType: DataSourceType, violations: list[DialectViolation]) -> str:
    """拼好喂回 LLM 的人类语言提示（注入 prompt 让 LLM 重写）。"""
    lines = [
        f"上一轮生成的 SQL 包含 {datasourceType.value} dialect 不允许的函数/语法，已被守卫拦截。",
        "请重新生成 SQL，**只用**目标 dialect 的原生写法，**不要**用以下函数：",
    ]
    for v in violations:
        lines.append(f"- {v.function}：{v.suggestion}")
    return "\n".join(lines)


def coerceDatasourceType(value: str | None, *, name: str = "") -> DataSourceType:
    """数据源类型边界校验（fail fast）：脏值抛 ValidationError，绝不静默回退。

    与 resolveDialect 的「未知回退 Oracle」分工：后者是方言解析的**最后防线**
    （历史行为，测试钉死）；本函数是流水线的**第一反应** —— 数据源类型脏值若
    继续走，会用错误方言生成 SQL（如给 MySQL 生成 ROWNUM），执行必错且用户
    只看到莫名其妙的数据库报错。在 LLM 消费前拒绝，把问题留给能修它的人。
    消息自足：带数据源名（定位是哪个库）+ 脏值原文 + 可操作指引。
    """
    if value is not None:
        try:
            return DataSourceType(value)
        except ValueError:
            lowered = str(value).lower()
            for t in DataSourceType:
                if t.value == lowered:
                    return t
    raise ValidationError(MSG_DATASOURCE_TYPE_UNKNOWN.format(name=name, type=value))


def resolveDialect(datasourceType: DataSourceType | str | None, oracle_version: str | None = None) -> SqlDialect:
    """按数据源类型解析方言；未指定或未知类型回退 Oracle（历史行为）。

    字符串输入先尝试大小写不敏感匹配（"MySQL"/"POSTGRESQL"），避免脏值
    误回退 Oracle 生成错误方言。
    oracle_version 用于区分 Oracle 版本：11g 用 ROWNUM，12c+ 用 FETCH FIRST。
    """
    if datasourceType is None:
        return _SQL_DIALECTS_ORACLE_11G
    try:
        dialect = _SQL_DIALECTS[DataSourceType(datasourceType)]
    except (ValueError, KeyError):
        if isinstance(datasourceType, str):
            for dialectType, dialect in _SQL_DIALECTS.items():
                if datasourceType.lower() == dialectType.value.lower():
                    return dialect
        logger.warning("未知数据源类型 %r，NL2SQL 回退到 Oracle 11g 方言", datasourceType)
        return _SQL_DIALECTS_ORACLE_11G

    # Oracle 版本判断：12c 及以上用 FETCH FIRST，否则用 ROWNUM
    if dialect.name == "Oracle" and datasourceType == DataSourceType.ORACLE:
        version = (oracle_version or "").lower()
        # 点分版本（探测落库原文，如 "11.2.0.1.0"）与 "11g" 字样都识别为 11g ——
        # 否则 11.2 会三个规则都不命中而落 12c 分支，给 11g 库生成 FETCH FIRST。
        if "11g" in version or version.startswith(("9", "10", "11")):
            return _SQL_DIALECTS_ORACLE_11G
        return _SQL_DIALECTS_ORACLE_12C

    return dialect
