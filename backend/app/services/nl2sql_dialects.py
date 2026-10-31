"""SQL 方言定义（从 nl2sql_service 拆出）。

按数据源类型（Oracle/MySQL/PostgreSQL）声明行数限制、JOIN 模板、schema 前缀、
值域采样、NULL 排序、时间粒度分组等方言规则片段。`resolveDialect` 是运行时解析入口。

未来扩展：新增一种 SQL 方言只需在本模块加一个 `SqlDialect` 实例 + `_DIALECT_SEEDS` 表项。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from types import MappingProxyType

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
    # 2026-10-08 dialect-registry refactor：banned set 从模块级 dict 搬到实例上
    bannedFunctions: tuple[str, ...] = ()
    keywordPatterns: tuple[tuple[str, str, str], ...] = ()
    intervalAllowed: bool = True
    notes: str = ""
    # 用于 check_sql 生成**该 dialect 专属**的 LLM retry 提示（如 Oracle 把
    # DATE_FORMAT 提示成「用 TO_CHAR(date, 'YYYY-MM') 替代」）。None 时退回
    # 通用提示「改用 {name} 原生函数」，仅用于纯单测构造的 SqlDialect。
    datasourceType: DataSourceType | None = None

    def boundedDistinct(self, table: str, column: str, n: int) -> str:
        """构造取前 n 行去重值查询（表/列已过标识符白名单校验）。"""
        return self.sampleLimitSql.format(
            sql=f"SELECT DISTINCT {column} FROM {table}", n=n
        )

    def check_sql(self, sql: str | None) -> DialectGuardResult:
        """按本方言的 bannedFunctions + keywordPatterns + intervalAllowed 反向校验 SQL。

        2026-10-08 refactor：从模块级 checkSqlDialect 抽到实例方法；行为一致。
        """
        if not sql or not sql.strip():
            return DialectGuardResult(ok=True, violations=(), hint="")

        violations: list[DialectViolation] = []
        if self.bannedFunctions:
            found = _extractFunctionNames(sql)
            for fn in sorted(found & set(self.bannedFunctions)):
                # 有 datasourceType 时用专属提示（Oracle 给出 "用 TO_CHAR..."），
                # 否则退到通用提示（纯单测场景不期望强方言语义）
                suggestion = (
                    _suggestionFor(self.datasourceType, fn)
                    if self.datasourceType is not None
                    else f"改用 {self.name} 原生函数"
                )
                violations.append(DialectViolation(function=fn, suggestion=suggestion))
        if self.keywordPatterns:
            violations.extend(_checkBannedKeywords(sql, self.keywordPatterns))
        elif not self.intervalAllowed:
            # Oracle / MySQL 不允许独立 INTERVAL 字面量；即使 keywordPatterns 为空
            # 也启用 INTERVAL 默认守卫（与 refactor 前 _BANNED_KEYWORDS_ORACLE 行为一致）。
            violations.extend(
                _checkBannedKeywords(
                    sql,
                    (
                        (
                            r"\bINTERVAL\s+(\d|['\"])",
                            "INTERVAL",
                            "用 ADD_MONTHS(date, -N) 或 date - N 替代",
                        ),
                    ),
                )
            )

        if not violations:
            return DialectGuardResult(ok=True, violations=(), hint="")
        # 有 datasourceType 时透传 enum，_buildHint 会取 .value 拼成 "oracle"；
        # 否则退到 self.name（纯单测场景）
        hint = _buildHint(
            self.datasourceType if self.datasourceType is not None else self.name,
            violations,
        )
        return DialectGuardResult(ok=False, violations=tuple(violations), hint=hint)


# ---------------------------------------------------------------------------
# DialectRegistry（2026-10-08 补）：按 (DataSourceType, versionBucket) 注册/解析方言。
# 取代原 `_SQL_DIALECTS` dict + Oracle-only if 分支；fallback 链 exact → major → "default"。
# ---------------------------------------------------------------------------


def _candidateBuckets(db: DataSourceType, version: str | None) -> list[str]:
    """按 db 类型生成候选 bucket 列表（fallback 链前缀）。

    Oracle：保留历史 "9/10/11 开头 → 11g" 行为；其它版本按命名规则（N[g/c/i] 或 N）解析。
    MySQL：按 major.minor / major 解析。
    PostgreSQL：按 major 解析。
    未识别版本 → ["default"]。
    """
    if not version:
        return ["default"]
    v = version.lower().strip()
    if db is DataSourceType.ORACLE:
        # 历史行为（原 line 411）：9/10/11 开头或含 "11g" → 11g
        if v.startswith(("9", "10", "11")) or "11g" in v:
            return ["11g"]
        # 命名版本（"19c", "21c", "12c"）→ 直接命中
        m = re.match(r"(\d+)([gci])", v)
        if m:
            return [f"{m.group(1)}{m.group(2)}", "default"]
        # 纯数字 major（如 "19", "21"）→ 试 major+"c" 后缀
        m = re.match(r"(\d+)", v)
        if m:
            return [f"{m.group(1)}c", m.group(1), "default"]
        return ["default"]
    if db is DataSourceType.MYSQL:
        m = re.match(r"(\d+)\.(\d+)", v)
        if m:
            return [f"{m.group(1)}_{m.group(2)}", m.group(1), "default"]
        m = re.match(r"(\d+)", v)
        if m:
            return [m.group(1), "default"]
        return ["default"]
    if db is DataSourceType.POSTGRESQL:
        m = re.match(r"(\d+)", v)
        if m:
            return [m.group(1), "default"]
        return ["default"]
    return ["default"]


@dataclass(frozen=True)
class DialectRegistry:
    """按 (dbType, versionBucket) 注册/解析方言；版本不匹配走 fallback 链。

    register 是「创建新实例」语义（frozen），不 mutate；resolve 走 fallback：
    exact bucket → major bucket → "default" → KeyError。
    """

    _entries: MappingProxyType  # type: ignore[type-arg]

    @staticmethod
    def default() -> DialectRegistry:
        """返回系统内置注册表（所有 known 版本方言已填入）。"""
        return _BUILTIN_DIALECT_REGISTRY

    def withEntry(
        self,
        dbType: DataSourceType | str,
        bucket: str,
        dialect: SqlDialect,
    ) -> DialectRegistry:
        """注册一条 (dbType, bucket) → 方言；同 key 重复注册抛 ValueError。返回新实例。"""
        db = DataSourceType(dbType)
        key = (db, bucket)
        if key in self._entries:
            raise ValueError(f"dialect already registered: {db.value}/{bucket}")
        new_entries = dict(self._entries)
        new_entries[key] = dialect
        return DialectRegistry(_entries=MappingProxyType(new_entries))

    def resolve(
        self,
        dbType: DataSourceType | str,
        version: str | None = None,
    ) -> SqlDialect:
        """按 fallback 链解析方言。version 解析失败 / 未注册 → 抛 KeyError。

        字符串大小写不敏感匹配（如 "MySQL" / "POSTGRESQL" 都视作 DataSourceType.MYSQL/POSTGRESQL）；
        不能匹配时抛 ValueError。调用方应在边界（`coerceDatasourceType`）做脏值校验，
        本函数只对已校验/已归一的输入做方言解析。
        """
        if isinstance(dbType, str):
            dbType = dbType.lower()
        db = DataSourceType(dbType)
        for bucket in _candidateBuckets(db, version):
            key = (db, bucket)
            if key in self._entries:
                return self._entries[key]
        raise KeyError(f"no dialect registered for {db.value} (version={version!r})")


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
    "按季度用 TO_CHAR(日期列,'YYYY-\"Q\"Q')（输出如 2026-Q4；"
    "格式串中的 Q 必须用双引号转义为字面量，否则只输出季度序号如 2026-4）；"
    "并在 SELECT 输出同一截断表达式作为月份/年份列。"
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
    "④ 按季度取范围（本季度/上季度/指定季度，一律按日历年、含起不含止）："
    "本季度 WHERE date_col >= TRUNC(SYSDATE,'Q') AND date_col < ADD_MONTHS(TRUNC(SYSDATE,'Q'),3)；"
    "上季度下界 ADD_MONTHS(TRUNC(SYSDATE,'Q'),-3)、上界 TRUNC(SYSDATE,'Q')；"
    "指定年月的季度直接写起止日（第 N 季度起始月为 3*(N-1)+1，"
    "如 2026 年第 3 季度为 2026-07-01 至 2026-10-01）。"
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
_DIALECT_SEEDS: dict[DataSourceType, SqlDialect] = {
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
# Task 3：内置方言注册表（2026-10-08 dialect-registry refactor）
# 在 _DIALECT_SEEDS dict 之外，构造 12 条版本特定的方言实例；保留既有 11g/12c 实例
# （用 replace 加 bannedFunctions 等新字段）以保证既有 chat 调用方语义不变。
# ---------------------------------------------------------------------------

# Oracle banned 集合（refactor 前在 _BANNED_FUNCTIONS_ORACLE 模块常量里）
_ORACLE_BANNED_FUNCTIONS = (
    "DATEADD",
    "DATEDIFF",
    "DATEPART",
    "DATE_PART",
    "DATE_FORMAT",
    "DATE_SUB",
    "DATE_ADD",
    "CURDATE",
    "NOW",
)
_ORACLE_KEYWORD_PATTERNS = (
    (r"\bINTERVAL\s+(\d|['\"])", "INTERVAL", "用 ADD_MONTHS(date, -N) 或 date - N 替代"),
    # 2026-10-08 复盘 4：研究 finding #1 写 `ORDER BY ... LIMIT 12` → ORA-00933
    # （Oracle 不支持 LIMIT 关键字；11g 用 ROWNUM 子查询，12c+ 用 FETCH FIRST）。
    # 强制后跟数字避免误伤列名 `limit_count` / 字符串字面量 `'LIMIT'`。
    (r"\bLIMIT\s+\d+", "LIMIT", "用 FETCH FIRST N ROWS ONLY（12c+）或 ROWNUM 子查询（11g）替代"),
)
# MySQL banned 集合（refactor 前 inline 在 checkSqlDialect 函数里）
_MYSQL_BANNED_FUNCTIONS = ("DATEADD", "DATEDIFF", "DATEPART", "ADD_MONTHS", "EXTRACT")
# PG banned 集合（refactor 前 inline 在 checkSqlDialect 函数里）
_POSTGRES_BANNED_FUNCTIONS = (
    "DATEADD",
    "DATEDIFF",
    "DATEPART",
    "ADD_MONTHS",
    "DATE_SUB",
    "DATE_ADD",
    "DATE_FORMAT",
)

# Oracle 12c+ default（基础方言；含 banned 集合 + 12c 风格 notes）
_SQL_DIALECTS_ORACLE_DEFAULT = replace(
    _SQL_DIALECTS_ORACLE_12C,
    bannedFunctions=_ORACLE_BANNED_FUNCTIONS,
    keywordPatterns=_ORACLE_KEYWORD_PATTERNS,
    intervalAllowed=False,
    notes="Oracle 12c+：使用 FETCH FIRST N ROWS ONLY 做行数限制。",
    datasourceType=DataSourceType.ORACLE,
)

# Oracle 11g：保留既有 11g 实例 + 加 banned 集合 + 11g 特定 notes
_SQL_DIALECTS_ORACLE_11G_REGISTERED = replace(
    _SQL_DIALECTS_ORACLE_11G,
    bannedFunctions=_ORACLE_BANNED_FUNCTIONS,
    keywordPatterns=_ORACLE_KEYWORD_PATTERNS,
    intervalAllowed=False,
    notes="Oracle 11g：使用 ROWNUM 做行数限制（无 FETCH FIRST 语法）。",
    datasourceType=DataSourceType.ORACLE,
)

# Oracle 19c（clone default + 19c 特定 notes）
_SQL_DIALECTS_ORACLE_19C = replace(
    _SQL_DIALECTS_ORACLE_DEFAULT,
    notes="Oracle 19c：ADD_MONTHS 优先；MERGE INTO 可用；JSON_TABLE 在 19c 可用。",
)

# Oracle 21c（clone default + 21c 特定 notes）
_SQL_DIALECTS_ORACLE_21C = replace(
    _SQL_DIALECTS_ORACLE_DEFAULT,
    notes="Oracle 21c：MERGE INTO 完整支持；JSON_DATAGUIDE / 21c 自治特性。",
)

# MySQL default（8.0 通用，含 banned 集合）
_SQL_DIALECTS_MYSQL_DEFAULT = replace(
    _DIALECT_SEEDS[DataSourceType.MYSQL],
    bannedFunctions=_MYSQL_BANNED_FUNCTIONS,
    intervalAllowed=False,
    notes="MySQL：使用 LIMIT N 做行数限制。",
    datasourceType=DataSourceType.MYSQL,
)

# MySQL 8.0
_SQL_DIALECTS_MYSQL_8_0 = replace(
    _SQL_DIALECTS_MYSQL_DEFAULT,
    notes="MySQL 8.0：CTE / 窗口函数 / JSON_TABLE 可用。",
)

# MySQL 5.7
_SQL_DIALECTS_MYSQL_5_7 = replace(
    _SQL_DIALECTS_MYSQL_DEFAULT,
    notes="MySQL 5.7：无原生 CTE（WITH 子句需 8.0+）；递归用临时表；无窗口函数。",
)


def _replace_pg_default(notes: str = "PostgreSQL：使用 LIMIT N 做行数限制。"):
    """PG default + 任意 notes（replace 不能 mutate frozen instance）。"""
    return replace(
        _DIALECT_SEEDS[DataSourceType.POSTGRESQL],
        bannedFunctions=_POSTGRES_BANNED_FUNCTIONS,
        notes=notes,
        datasourceType=DataSourceType.POSTGRESQL,
    )


def _build_default_registry() -> DialectRegistry:
    """构造系统内置方言注册表（12 条）。"""
    entries = (
        # Oracle
        (DataSourceType.ORACLE, "default", _SQL_DIALECTS_ORACLE_DEFAULT),
        (DataSourceType.ORACLE, "11g", _SQL_DIALECTS_ORACLE_11G_REGISTERED),
        (DataSourceType.ORACLE, "19c", _SQL_DIALECTS_ORACLE_19C),
        (DataSourceType.ORACLE, "21c", _SQL_DIALECTS_ORACLE_21C),
        # MySQL
        (DataSourceType.MYSQL, "default", _SQL_DIALECTS_MYSQL_DEFAULT),
        (DataSourceType.MYSQL, "8_0", _SQL_DIALECTS_MYSQL_8_0),
        (DataSourceType.MYSQL, "5_7", _SQL_DIALECTS_MYSQL_5_7),
        (DataSourceType.MYSQL, "8", _SQL_DIALECTS_MYSQL_DEFAULT),
        (DataSourceType.MYSQL, "5", _SQL_DIALECTS_MYSQL_DEFAULT),
        # MariaDB 复用 MySQL default（fallback chain 兜底）
        # PostgreSQL
        (DataSourceType.POSTGRESQL, "default", _replace_pg_default()),
        (DataSourceType.POSTGRESQL, "14", _replace_pg_default("PostgreSQL 14：MERGE INTO 语法可用。")),
        (DataSourceType.POSTGRESQL, "15", _replace_pg_default("PostgreSQL 15：MERGE / 15+ 完整支持。")),
        (DataSourceType.POSTGRESQL, "16", _replace_pg_default("PostgreSQL 16：MERGE / JSON_TABLE / 16+ 完整支持。")),
    )
    reg: DialectRegistry = DialectRegistry(_entries=MappingProxyType({}))
    for db, bucket, dialect in entries:
        reg = reg.withEntry(db, bucket, dialect)
    return reg


# 在 _DIALECT_SEEDS dict 定义之后才构造 registry（依赖既有实例做 replace seed）
_BUILTIN_DIALECT_REGISTRY = _build_default_registry()


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

    2026-10-08 dialect-registry refactor：薄壳化，内部委托给
    `DialectRegistry.default().resolve(dsType).check_sql(sql)`。
    行为等价于 refactor 前（banned set 按 DataSourceType 一刀切）。

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
    # 字符串归一到 enum（与 refactor 前一致：脏值放行，由 coerceDatasourceType 兜底）
    dsEnum: DataSourceType | None = None
    if isinstance(datasourceType, DataSourceType):
        dsEnum = datasourceType
    elif isinstance(datasourceType, str):
        for t in DataSourceType:
            if t.value == datasourceType.lower():
                dsEnum = t
                break
    if dsEnum is None:
        return DialectGuardResult(ok=True, violations=(), hint="")
    try:
        dialect = DialectRegistry.default().resolve(dsEnum)
    except KeyError:
        return DialectGuardResult(ok=True, violations=(), hint="")
    return dialect.check_sql(sql)


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


def _buildHint(datasourceType: DataSourceType | str, violations: list[DialectViolation]) -> str:
    """拼好喂回 LLM 的人类语言提示（注入 prompt 让 LLM 重写）。"""
    name = (
        datasourceType.value
        if isinstance(datasourceType, DataSourceType)
        else datasourceType
    )
    lines = [
        f"上一轮生成的 SQL 包含 {name} dialect 不允许的函数/语法，已被守卫拦截。",
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
    """按数据源类型解析方言；未指定或未知类型回退 Oracle 11g（历史行为）。

    2026-10-08 dialect-registry refactor：薄壳化，内部委托给 DialectRegistry。
    字符串输入由 registry 做大小写不敏感匹配（"MySQL"/"POSTGRESQL" 等）。
    Oracle 版本通过 candidate bucket 链解析：11g / 19c / 21c / default 各自有专属方言。
    """
    if datasourceType is None:
        return DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
    try:
        return DialectRegistry.default().resolve(datasourceType, oracle_version)
    except (KeyError, ValueError):
        logger.warning("未知数据源类型 %r，NL2SQL 回退到 Oracle 11g 方言", datasourceType)
        return DialectRegistry.default().resolve(DataSourceType.ORACLE, "11g")
