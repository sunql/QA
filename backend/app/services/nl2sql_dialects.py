"""SQL 方言定义（从 nl2sql_service 拆出）。

按数据源类型（Oracle/MySQL/PostgreSQL）声明行数限制、JOIN 模板、schema 前缀、
值域采样、NULL 排序、时间粒度分组等方言规则片段。`resolveDialect` 是运行时解析入口。

未来扩展：新增一种 SQL 方言只需在本模块加一个 `SqlDialect` 实例 + `_SQL_DIALECTS` 表项。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.domain.enums import DataSourceType

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
    """

    name: str
    limitRule: str
    joinTemplate: str
    useSchemaPrefix: bool
    sampleLimitSql: str
    identifierRule: str = ""
    nullOrderingRule: str = ""
    timeBucketRule: str = ""

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

_SQL_DIALECTS_ORACLE_11G = SqlDialect(
    name="Oracle",
    limitRule="需要限制行数时使用 ROWNUM，例如 SELECT * FROM (SELECT t.*, ROWNUM rn FROM (...) t WHERE ROWNUM <= 1000)，不要使用 FETCH FIRST，也不要使用 LIMIT。",
    joinTemplate="FROM {schema}PRECEIPTD d JOIN {schema}PRECEIPT h ON h.PTHNUM_0 = d.PTHNUM_0",
    useSchemaPrefix=True,
    sampleLimitSql="SELECT * FROM ({sql}) WHERE ROWNUM <= {n}",
    nullOrderingRule=_NULL_ORDERING_RULE,
    timeBucketRule=_TIME_BUCKET_RULE_ORACLE,
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
    ),
    DataSourceType.POSTGRESQL: SqlDialect(
        name="PostgreSQL",
        limitRule="需要限制行数时使用 PostgreSQL 的 LIMIT 子句，例如 LIMIT N，不要使用 FETCH FIRST。",
        joinTemplate="FROM sales s JOIN customers c ON c.id = s.customer_id",
        useSchemaPrefix=False,
        sampleLimitSql="{sql} LIMIT {n}",
        nullOrderingRule=_NULL_ORDERING_RULE,
        timeBucketRule=_TIME_BUCKET_RULE_POSTGRESQL,
    ),
}


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
        if "11g" in version or version.startswith("10") or version.startswith("9"):
            return _SQL_DIALECTS_ORACLE_11G
        return _SQL_DIALECTS_ORACLE_12C

    return dialect
