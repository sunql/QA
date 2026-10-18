"""REFINE 捷径（从 nl2sql_service 拆出）：纯代码 SQL 改写，不调 LLM。

Phase D 追问改写：行数（前 N/top N/限 N）、排序（按 X 升/降序）、筛选（等于/大于/
日期范围/排除）三类可识别调整叠加，直接改写上一轮 SQL，无法安全识别时返回 None
退回 LLM 两阶段。列名必须来自上一轮计划且过 _SAFE_IDENT_RE 白名单，值过
_quoteFilterValue 校验，不引入 SQL 注入面。

未来扩展：新增一种快捷改写（如 GROUP BY 调整）加一个 _extract* / _rewrite* 对，
在 applyRefineDirect 里登记即可。
"""

from __future__ import annotations

import re
from datetime import date as _date

import sqlglot
from sqlglot import exp

from app.domain.enums import DataSourceType
from app.domain.query_plan import QueryPlan


# 行数调整："前 3 条 / top 5 / 限 3"
_REFINE_LIMIT_RE = re.compile(r"(?:前|top)\s*(\d+)|限\s*(\d+)", re.IGNORECASE)
# 比较运算符（中文词 → SQL 运算符）。顺序决定正则交替的优先级：多字词须排在单字词
# 之前（"大于等于" 在 "大于" 前），否则长运算符会被拆成短运算符 + 残留值。
_REFINE_CMP_OPERATORS: tuple[tuple[str, str], ...] = (
    ("大于等于", ">="),
    ("不小于", ">="),
    ("不低于", ">="),
    ("小于等于", "<="),
    ("不大于", "<="),
    ("超过", ">"),
    ("大于", ">"),
    ("高于", ">"),
    ("少于", "<"),
    ("小于", "<"),
    ("低于", "<"),
    ("不等于", "<>"),
    ("不同于", "<>"),
    ("等于", "="),
    ("为", "="),
    ("是", "="),
)
_REFINE_OP_MAP = dict(_REFINE_CMP_OPERATORS)
# 比较/等值筛选："只看/筛选 {列} {大于|等于|...} {值}"。值为单个词，空格/标点/的 截断。
# 运算符交替顺序复用 _REFINE_CMP_OPERATORS（多字词在前，避免长运算符被拆短）。
_REFINE_CMP_RE = re.compile(
    r"(?:只看|只要|过滤|筛选)?\s*(?P<col>\w+)\s*(?P<op>"
    + "|".join(re.escape(op) for op, _ in _REFINE_CMP_OPERATORS)
    + r")\s*(?P<val>[^\s，。、=的]+)"
)
# 排除筛选："排除/去掉/剔除 {列} {为|是|等于} {值}" → 列 <> 值。连接词必须存在（非可选），
# 否则贪婪列名会把值一并吃掉（"排除状态为已关闭" 的列须在 "为" 处截断）。
_REFINE_EXCLUDE_RE = re.compile(
    r"(?:排除|去掉|剔除|不包含|除开)\s*(?P<col>\w+?)\s*(?:为|是|等于)\s*"
    r"(?P<val>[^\s，。、=的]+)"
)
# 日期范围："筛选 {列} 在 {YYYY-MM-DD} 到 {YYYY-MM-DD} 之间" → BETWEEN（值严格校验）
_REFINE_RANGE_RE = re.compile(
    r"(?:只看|只要|过滤|筛选)?\s*(?P<col>\w+?)\s*(?:在|介于)\s*"
    r"(?P<from>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(?:到|至|~)\s*"
    r"(?P<to>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(?:之间|期间)?"
)
_REFINE_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
# SELECT 别名提取：`AS 别名`，别名须通过标识符白名单
_REFINE_AS_RE = re.compile(r"\bAS\s+([^\s,()]+)", re.IGNORECASE)
# SQL 子句锚点（用于定位插入点；WHERE < GROUP BY < ORDER BY < FETCH/LIMIT）
_CLAUSE_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_CLAUSE_ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
_CLAUSE_FETCH_FIRST = re.compile(r"FETCH\s+FIRST\s+\d+\s+ROWS\s+ONLY", re.IGNORECASE)
_CLAUSE_LIMIT = re.compile(r"\bLIMIT\s+\d+(?:\s*,\s*\d+)?", re.IGNORECASE)
# SQL 标识符白名单：REFINE 排序列/筛选列/别名必须匹配，防止本体属性名引入危险标识符。
# （MEDIUM-2：标识符直接拼入 SQL，需强制为标识符字符。3-5 起放行中文字符——CJK 无法
#  闭合字符串/注释上下文——但分隔符、引号、空白仍拒绝。）
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_㐀-鿿][A-Za-z0-9_㐀-鿿]*$")
# REFINE 行数上限：钳制超大 LIMIT/FETCH，避免拖慢查询规划（LOW-3）。
# 运行期从 system_config.REFINE_MAX_LIMIT 现读（魔数治理 Phase 2 hard tier），
# 缺席/格式错返 _DEFAULT。
_REFINE_MAX_LIMIT_DEFAULT = 1000

# 数据源类型 → sqlglot 方言名（postgresql 在 sqlglot 里叫 postgres）。
_SQLGLOT_DIALECTS: dict[DataSourceType, str] = {
    DataSourceType.MYSQL: "mysql",
    DataSourceType.POSTGRESQL: "postgres",
    DataSourceType.ORACLE: "oracle",
}
# 新建 ORDER BY 节点时显式设 nulls_first 为**方言默认值**，sqlglot 序列化时
# 才会省略 NULLS 子句；否则它会按自己的默认展开成 NULLS LAST 或 CASE WHEN，
# 前者改变 Oracle/PG 的 DESC 语义（两者 DESC 默认 NULLS FIRST），后者让
# MySQL 输出变脏。各方言的「DESC 默认即 NULLS FIRST」情况：
_NULLS_DEFAULT_MATCHES_DESC = frozenset({"oracle", "postgres"})
# MySQL：ASC 默认 NULLS FIRST，DESC 默认 NULLS LAST（与上面相反）。
_NULLS_DEFAULT_MATCHES_ASC = frozenset({"mysql"})


def _nullsFirst(dialect: str | None, desc: bool) -> bool | None:
    """返回该方言下与默认一致的 nulls_first 值；None 表示保持未设置。"""
    if dialect in _NULLS_DEFAULT_MATCHES_ASC:
        return not desc
    if dialect in _NULLS_DEFAULT_MATCHES_DESC:
        return desc
    # 默认方言：ASC 显式置 NULLS FIRST（而非 sqlglot 默认的 NULLS LAST），
    # DESC 不设置即可省略子句。
    return True if not desc else None


def _sqlglotDialect(datasourceType: DataSourceType | str | None) -> str | None:
    """把数据源类型映射为 sqlglot 方言名；未知类型返回 None（默认方言）。

    脏值 fail-fast 由上游 ``coerceDatasourceType`` 负责；本函数只服务
    最佳努力的 REFINE 捷径，未知值退回默认方言，不额外抛错。
    """
    if isinstance(datasourceType, DataSourceType):
        return _SQLGLOT_DIALECTS.get(datasourceType)
    if isinstance(datasourceType, str):
        try:
            return _SQLGLOT_DIALECTS.get(DataSourceType(datasourceType.lower()))
        except ValueError:
            return None
    return None


def _extractLimit(question: str, maxLimit: int = _REFINE_MAX_LIMIT_DEFAULT) -> int | None:
    """从问题中提取行数并钳制到上限；无法识别返回 None。

    maxLimit 运行期从 system_config.REFINE_MAX_LIMIT 现读（魔数治理 Phase 2）。
    """
    match = _REFINE_LIMIT_RE.search(question)
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    return min(int(raw), maxLimit)


def _rewriteLimit(sql: str, n: int, *, dialect: str | None = None) -> str | None:
    """改写顶层 LIMIT/FETCH FIRST 为指定行数。

    AST 化后：仅简单顶层 SELECT（无 CTE / 子查询 / window）走快路径；
    字符串字面量内的 LIMIT 不被改写（AST 自动 quote-aware）；
    嵌套 / CTE / window / 解析失败 → 返 None（让两阶段生成接手）。
    """
    return rewriteTopLimit(sql, n, dialect=dialect)


# Public API
def rewriteTopLimit(sql: str, n: int, *, dialect: str | None = None) -> str | None:
    """公开 API：AST 化 LIMIT 改写（按数据源方言解析与输出）。

    Args:
        sql: 输入 SQL
        n: 目标行数
        dialect: sqlglot 方言名（"mysql"/"postgres"/"oracle"）；None 用默认方言

    Returns:
        改写后的 SQL 字符串（quote-aware）；非简单 SELECT / 无既有行数子句 /
        解析失败 → None。无既有行数子句时不追加——ROWNUM 等非 AST limit 形式
        追加 LIMIT/FETCH 会改变语义或产生方言非法语法，交给 LLM 两阶段。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError:
        return None

    if not isinstance(tree, exp.Select):
        return None
    # 嵌套 / CTE / window 自动回退
    if list(tree.find_all(exp.CTE)):
        return None
    if list(tree.find_all(exp.Subquery)):
        return None
    if list(tree.find_all(exp.Window)):
        return None
    # 无既有行数子句 → 不追加（LOW-2 安全决策）
    if tree.args.get("limit") is None:
        return None

    tree.set("limit", exp.Limit(expression=exp.Literal.number(n)))
    return tree.sql(dialect=dialect)


def _extractSort(question: str, columns: tuple[str, ...]) -> tuple[str, str] | None:
    """提取 (排序列, 方向)。

    列必须来自上一轮查询计划或 SQL 输出别名（3-5/C8），且已通过 _SAFE_IDENT_RE
    白名单（避免任意标识符注入）；列名匹配对大小写不敏感（MEDIUM-3），
    命中后返回匹配到的列名（保持原大小写）。

    列前须为标识符边界（非标识符字符），避免短列名（QTY）命中长别名
    （TOTAL_QTY）的子串（3-5/C8）。
    """
    boundary = r"(?<![A-Za-z0-9_㐀-鿿])"
    for col in columns:
        esc = re.escape(col)
        if re.search(rf"按\s*{esc}\s*降序", question, re.IGNORECASE):
            return col, "DESC"
        if re.search(rf"{boundary}{esc}\s*降序", question, re.IGNORECASE):
            return col, "DESC"
        if re.search(rf"按\s*{esc}\s*升序", question, re.IGNORECASE):
            return col, "ASC"
        if re.search(rf"{boundary}{esc}\s*升序", question, re.IGNORECASE):
            return col, "ASC"
        if re.search(rf"按\s*{esc}\s*排序", question, re.IGNORECASE):
            return col, "ASC"
        if re.search(rf"{boundary}{esc}\s*排序", question, re.IGNORECASE):
            return col, "ASC"
    return None


def _orderColumn(col: str) -> exp.Column:
    """构造排序列节点：安全标识符按原样输出，其余退回带引号形式。

    ``exp.column()`` 会给非 ASCII 标识符自动加引号（"收货数量"），改变输出
    文本且无必要；列名已过安全白名单，可直接用未加引号的 Identifier。
    """
    quoted = not _SAFE_IDENT_RE.fullmatch(col)
    return exp.Column(this=exp.Identifier(this=col, quoted=quoted))


def _rewriteOrderBy(sql: str, col: str, direction: str, *, dialect: str | None = None) -> str | None:
    """改写顶层 ORDER BY（只动顶层；嵌套内层不动）。

    AST 化后：用 sqlglot 解析 → 只对顶层 Select.order 操作 → 序列化。
    解析失败 / 非 Select → 返 None。
    """
    return rewriteOrderBy(sql, col, direction, dialect=dialect)


# Public API
def rewriteOrderBy(sql: str, col: str, direction: str, *, dialect: str | None = None) -> str | None:
    """公开 API：AST 化顶层 ORDER BY 改写（按数据源方言解析与输出）。

    Args:
        sql: 输入 SQL
        col: 排序列（已通过 _SAFE_IDENT_RE 校验的安全标识符）
        direction: "ASC" / "DESC"
        dialect: sqlglot 方言名（"mysql"/"postgres"/"oracle"）；None 用默认方言

    Returns:
        改写后的 SQL；解析失败 / 非 Select → None。
        已有顶层 ORDER BY → 替换为新 ORDER BY col direction；
        无顶层 ORDER BY → 末尾追加。
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.errors.ParseError:
        return None

    if not isinstance(tree, exp.Select):
        return None

    desc = direction.upper() == "DESC"
    ordered = exp.Ordered(this=_orderColumn(col), desc=desc)
    nullsFirst = _nullsFirst(dialect, desc)
    if nullsFirst is not None:
        ordered.set("nulls_first", nullsFirst)
    tree.set("order", exp.Order(expressions=[ordered]))
    return tree.sql(dialect=dialect)


def _quoteFilterValue(val: str) -> str | None:
    """等值筛选的值校验：拒绝引号/分号/注释/反斜杠注入；数值不加引号，其余按字符串字面量。"""
    val = val.rstrip("的")
    if not val:
        return None
    if any(ch in val for ch in ("'", '"', ";", "--", "\\")):
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", val):
        return val
    return f"'{val}'"


def _extractAliases(sql: str) -> tuple[str, ...]:
    """提取顶层 SELECT 列表中的别名（含中文），供排序列匹配。

    单遍扫描 SQL，跟踪字符串字面量/行注释/块注释/括号深度，仅在"括号深度 0、不在
    字符串或注释内"处识别首个顶层 FROM 截断 SELECT 列表，再于其中匹配 `AS 别名`。
    这样 CAST(x AS INT) 内的 AS（在括号内）、字符串里的 FROM、标量子查询内的 FROM
    都不会污染别名集；标量子查询的顶层别名 `(SELECT ...) AS 内部` 仍可被捕获
    （MEDIUM-1/MEDIUM-2：原正则在首个 FROM 处粗截断，CAST 类型名会泄漏为可排序别名）。
    别名须通过 _SAFE_IDENT_RE 白名单，返回去重元组。排序引用输出列别名在 ANSI SQL
    合法（ORDER BY alias），因此别名仅参与排序、不参与筛选（C8）。
    """
    selectChars: list[str] = []
    depth = 0
    inStr = False
    strQuote = ""
    inLine = False
    inBlock = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if inLine:
            if ch == "\n":
                inLine = False
            i += 1
            continue
        if inBlock:
            if ch == "*" and nxt == "/":
                inBlock = False
                i += 2
                continue
            i += 1
            continue
        if inStr:
            if ch == strQuote:
                if nxt == strQuote:  # 成对转义（'' / ""）
                    i += 2
                    continue
                inStr = False
            i += 1
            continue
        if ch == "'" or ch == '"':
            inStr = True
            strQuote = ch
            i += 1
            continue
        if ch == "-" and nxt == "-":
            inLine = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            inBlock = True
            i += 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            # 顶层 FROM（独立词）截断 SELECT 列表
            if (
                sql[i:i + 4].upper() == "FROM"
                and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_"))
                and (i + 4 >= n or not (sql[i + 4].isalnum() or sql[i + 4] == "_"))
            ):
                break
            selectChars.append(ch)
        i += 1
    top = "".join(selectChars)
    aliases: list[str] = []
    for match in _REFINE_AS_RE.finditer(top):
        alias = match.group(1)
        if _SAFE_IDENT_RE.fullmatch(alias):
            aliases.append(alias)
    return tuple(dict.fromkeys(aliases))


def _normalizeDate(raw: str) -> str | None:
    """把日期规范化成 ISO 'YYYY-MM-DD'；格式非法或月/日越界（含非闰年 2-29）返回 None。

    用 datetime.date 做真实日历校验，拒绝 2024-02-31 / 2023-02-29 这类格式合法但
    不存在的日期（MEDIUM-3：原仅校验月 1-12、日 1-31，会放过 2 月 31 号）。
    """
    match = _REFINE_DATE_RE.fullmatch(raw)
    if not match:
        return None
    year, month, day = match.groups()
    try:
        _date(int(year), int(month), int(day))
    except ValueError:
        return None
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _resolveRefineColumn(raw: str, columns: tuple[str, ...]) -> str | None:
    """大小写不敏感地解析筛选/排序列名到计划中的真实列名；不在计划中返回 None。"""
    return next((c for c in columns if c.upper() == raw.upper()), None)


def _extractDateRangeFilter(question: str, columns: tuple[str, ...]) -> str | None:
    """提取日期范围条件 "列 BETWEEN '起' AND '止'"；起止非法或倒置返回 None。"""
    match = _REFINE_RANGE_RE.search(question)
    if not match:
        return None
    actual = _resolveRefineColumn(match.group("col"), columns)
    if actual is None:
        return None
    lo = _normalizeDate(match.group("from"))
    hi = _normalizeDate(match.group("to"))
    if lo is None or hi is None or lo > hi:
        return None
    return f"{actual} BETWEEN '{lo}' AND '{hi}'"


def _extractExcludeFilter(question: str, columns: tuple[str, ...]) -> str | None:
    """提取排除条件 "列 <> 值"（排除/去掉/剔除/不包含）。"""
    match = _REFINE_EXCLUDE_RE.search(question)
    if not match:
        return None
    actual = _resolveRefineColumn(match.group("col"), columns)
    if actual is None:
        return None
    quoted = _quoteFilterValue(match.group("val"))
    if quoted is None:
        return None
    return f"{actual} <> {quoted}"


def _extractFilter(question: str, columns: tuple[str, ...]) -> str | None:
    """提取筛选条件，优先级：日期范围 BETWEEN > 排除 <> > 比较/等值 (=,>,>=,<,<=,<>) 。

    列须来自计划且已通过 _SAFE_IDENT_RE 白名单（3-5 起允许中文列名）；列名大小写
    不敏感（MEDIUM-3），命中后使用计划中的真实列名（保持原大小写）生成 SQL 条件。
    值经 _quoteFilterValue 校验，日期经 _normalizeDate 严格校验，不引入新的 SQL 注入面。
    """
    cond = _extractDateRangeFilter(question, columns)
    if cond is not None:
        return cond
    cond = _extractExcludeFilter(question, columns)
    if cond is not None:
        return cond
    match = _REFINE_CMP_RE.search(question)
    if not match:
        return None
    actual = _resolveRefineColumn(match.group("col"), columns)
    if actual is None:
        return None
    quoted = _quoteFilterValue(match.group("val"))
    if quoted is None:
        return None
    return f"{actual} {_REFINE_OP_MAP[match.group('op')]} {quoted}"


def _rewriteWhere(sql: str, cond: str) -> str:
    """追加等值筛选：已有 WHERE 则 AND；否则插到 GROUP BY / ORDER BY / FETCH / LIMIT 之前。"""
    where = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
    if where:
        end = len(sql)
        for anchor in (_CLAUSE_GROUP_BY, _CLAUSE_ORDER_BY, _CLAUSE_FETCH_FIRST, _CLAUSE_LIMIT):
            match = anchor.search(sql[where.end():])
            if match:
                end = min(end, where.end() + match.start())
        return f"{sql[:end].rstrip()} AND {cond} {sql[end:].lstrip()}"
    for anchor in (_CLAUSE_GROUP_BY, _CLAUSE_ORDER_BY, _CLAUSE_FETCH_FIRST, _CLAUSE_LIMIT):
        match = anchor.search(sql)
        if match:
            return f"{sql[:match.start()].rstrip()} WHERE {cond} {sql[match.start():].lstrip()}"
    return f"{sql.rstrip()} WHERE {cond}"


def applyRefineDirect(
    sql: str,
    plan: QueryPlan | None,
    question: str,
    *,
    maxLimit: int = _REFINE_MAX_LIMIT_DEFAULT,
    datasourceType: DataSourceType | str | None = None,
) -> str | None:
    """REFINE 捷径：纯代码改写上一轮 SQL（行数/排序/筛选），不调 LLM。

    返回改写后的 SQL；无法安全识别时返回 None（流水线退回 LLM 两阶段）。
    筛选列必须来自上一轮查询计划（selectedProperties）且通过 _SAFE_IDENT_RE
    标识符白名单（MEDIUM-2：本体属性名即便含特殊字符也不得拼入 SQL），值经
    _quoteFilterValue 校验，因此不引入新的 SQL 注入面。多个可识别调整可叠加。

    3-5：排序列额外允许顶层 SELECT 别名（ORDER BY alias 在 ANSI SQL 合法），
    让 "按总额降序" 命中 `SUM(金额) AS 总额`；筛选列仍只取计划列（WHERE 引用
    聚合别名非法）。列名匹配对大小写不敏感。

    maxLimit 运行期从 system_config.REFINE_MAX_LIMIT 现读（魔数治理 Phase 2）。
    datasourceType 决定 sqlglot 解析/输出方言：Oracle 的 FETCH FIRST 必须保持
    FETCH FIRST，不能被改写成方言非法的 LIMIT。
    """
    if not sql or not question:
        return None
    dialect = _sqlglotDialect(datasourceType)
    planColumns = (
        tuple(c for c in plan.selectedProperties if _SAFE_IDENT_RE.fullmatch(c))
        if plan
        else ()
    )
    sortColumns = planColumns + _extractAliases(sql)
    transformed = False

    limit = _extractLimit(question, maxLimit)
    if limit is not None:
        rewritten = _rewriteLimit(sql, limit, dialect=dialect)
        if rewritten is None:
            # 用户明确要求行数但当前 SQL 无法安全改写（无既有行数子句/ROWNUM/
            # 嵌套等）→ 整体放弃捷径。否则排序/筛选改写会让用户拿到一份
            # 「忽略了前 N 条」的结果，且 LLM 两阶段兜底也被绕过。
            return None
        sql = rewritten
        transformed = True

    sort = _extractSort(question, sortColumns)
    if sort is not None:
        rewritten = _rewriteOrderBy(sql, sort[0], sort[1], dialect=dialect)
        if rewritten is not None:
            sql = rewritten
            transformed = True

    filterCond = _extractFilter(question, planColumns)
    if filterCond is not None:
        sql = _rewriteWhere(sql, filterCond)
        transformed = True

    return sql if transformed else None
