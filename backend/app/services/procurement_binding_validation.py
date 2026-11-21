"""采购源绑定的**纯校验**规则（M1-1.1）。

这里是「库层约束」之外的**业务层**闸门。库层（`0154` 迁移）已经挡住：
角色字面量、有效期方向、候选 ⇔ 确认人、键序号唯一。库层挡不住的是：

1. **过滤白名单**：库层只有一列 ``filter_allowlist``，不检查表达式里到底用了谁。
   过滤表达式将来会被拼进读路径的 WHERE ⇒ 用到白名单外的列 = 越权读。
2. **键序号的连续性**：库层只保证「不重复」，不保证「1..n 连续」。
   复合主键缺位会让下游把「第 2 键」当成「第 1 键」用。
3. **角色/列名字面量**：早失败好过让 SQL 报错。
4. **列级变换表达式**：``transform_expression`` 将来会被拼进读路径的投影列表 ⇒
   与过滤表达式**同级的闸门**（禁 DML/UNION/注释 + 标识符白名单）。

⚠️ **白名单条目的形状也是闸门**：白名单项会被当作「列名」看待（读路径按它放行列），
非标识符 / 超长的条目会**被 PostgreSQL 静默截断**（数组元素超长是截断不是报错）⇒
「写时用于校验的白名单」与「落库的白名单」会不是同一个对象 ⇒ 必须响亮失败。

**授权面分三张互不重叠的清单**（安全审查 HIGH-1 / HIGH-2）：

======================  ==========================================  ==================
清单                    授权什么                                     谁来填
======================  ==========================================  ==================
绑定白名单              **列**（数据）—— 行级过滤能引用哪些列        写入方自报
``SAFE_SCALAR_FUNCTIONS`` **函数**（代码）—— 表达式能调用哪些函数    系统内置，不可自报
保留字禁表              **结构词** —— 表达式里不许出现的 SQL 结构     系统内置
======================  ==========================================  ==================

为什么函数**不能**用绑定白名单：绑定白名单是「这一列」的授权清单，让它兼任
函数清单 ⇒ 一个函数名进了白名单就等于**永久授权** ⇒ `pg_sleep` / `UTL_HTTP`
这类能阻塞 / 能外联的函数随时可能被启用。函数是**代码**，列是**数据** ⇒ 分开。

**过滤表达式的语法（行级过滤必须是「依赖行」的谓词）**：

    <裸列> <比较符> <常量>            （必须至少有一处）
    以 AND / NOT / 括号连接（OR 禁）
    函数只能出现在受控清单里，且**不能单独**构成一条过滤

为什么这么定：行级过滤的**唯一目的**是收窄行集 ⇒ 谓词必须依赖行。
恒真谓词（`1=1` / `A = A` / `ABS(A) >= 0` / `CASE ... ELSE 1 END = 1`）
会把行级过滤抹平成**全表读** —— 与「不给白名单」是同一类越权。
只禁 `OR` 堵不住这一族（`OR` 只是其中一种写法）。

**纯函数、无 IO**：全部规则都能在 unit 层直接断言（不碰数据库、不开 session），
服务层只负责把这些规则用在具体的写入路径上。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.domain.exceptions import ValidationError
from app.services.data_quality_evaluators._common import validate_expression

#: 合法绑定角色（与迁移 0154 的 CHECK `ck_procurement_source_binding_role` 同源）。
VALID_BINDING_ROLES: frozenset[str] = frozenset(
    {"BASE", "NUMERATOR", "DENOMINATOR", "FILTER"}
)

#: 过滤表达式里**不算标识符**的 SQL 关键字（函数名不算关键字 —— 函数必须进受控清单）。
#:
#: ⚠️ `EXISTS` **刻意不在这里**：它是结构词（子查询）⇒ 必须能被
#: `_assertNoReservedWords` 看见。若把它算作关键字，它会被 `extractIdentifiers`
#: 提前滤掉 ⇒ 那条禁令就成了**死代码**（只有加引号的 `"EXISTS"` 才能命中）。
_SQL_KEYWORDS: frozenset[str] = frozenset(
    {
        "ALL",
        "AND",
        "ANY",
        "AS",
        "BETWEEN",
        "CASE",
        "DISTINCT",
        "ELSE",
        "END",
        "FALSE",
        "IN",
        "IS",
        "LIKE",
        "NOT",
        "NULL",
        "OR",
        "THEN",
        "TRUE",
        "WHEN",
    }
)

#: 字符串字面量（SQL 单引号，'' 为转义）——先剥掉，否则字面量里的词会被当成标识符。
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
#: 双引号标识符（`"SALARY NET"`）——**整段是一个标识符**，不能按词切分。
_QUOTED_IDENTIFIER_RE = re.compile(r'"([^"]*)"')
#: 标识符（列名 / 函数名）—— **含 `$`**。
#:
#: 为什么含 `$`：Oracle / PostgreSQL 的未加引号标识符**允许** `$`（只能出现在
#: 首字符之后）。若抽取时不认 `$`，`status$1` 会被切成 `status` ⇒ 白名单里的
#: `STATUS` 就把它放行了 ⇒ 而 `status$1` 这一列**永远无法被合法列入白名单**
#: （白名单模式不允许 `$`）⇒ 稳定的白名单外列绕过通道。
#: 含 `$` 后它被**整体抽出**，自然不在白名单里 ⇒ 被拒。
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: 列名必须是合法 SQL 标识符 —— **不含 `$`**（与 DTO 的 `_IDENTIFIER` 同源）。
#: 用 `fullmatch` 语义：尾随换行也不行。
_COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: **函数调用形态**的标识符（`NAME(`）—— 函数是另一类对象，走受控函数清单。
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*\(")
#: 顶层 `OR`（禁）—— 只在**剥掉字面量与引号标识符之后**的文本上匹配，
#: 否则 `STATUS = 'A OR B'` 会被误杀（数据里的 OR 不是逻辑或）。
_OR_RE = re.compile(r"\bOR\b", re.IGNORECASE)

#: 白名单条目长度上限（与库列 `ARRAY(String(100))` 同宽）。
#:
#: 为什么必须在**校验层**也卡：PG 对数组元素超长是**静默截断**（不报错）⇒
#: 只在 DTO 层卡 ⇒ 任何不经 HTTP 的写入方（导入作业 / 脚本 / 内部服务）
#: 都能写进一条超长白名单 ⇒ 「写时校验的白名单」与「落库的白名单」不是同一个对象。
MAX_ALLOWLIST_ITEM_LENGTH = 100

#: 表达式**记号化**（判定「比较的一侧是裸列 + 常量」用）。
#:
#: 为什么需要记号化而不是正则：判断「某个标识符是否**紧邻**比较运算符」必须
#: 知道它是不是函数调用的名字（`UPPER(A) = 'x'` 里的 `A` 不紧邻运算符，
#: 而 `A = 'x'` 里的 `A` 紧邻）⇒ 需要区分「标识符」「函数名」「字面量」。
_TOKEN_RE = re.compile(
    r"""
      (?P<string>'(?:''|[^'])*')
    | (?P<quoted>"[^"]*")
    | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<op><=|>=|<>|!=|=|<|>)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)

#: 记号里代表**常量**的关键字（其余关键字是运算符或连接词）。
_CONSTANT_KEYWORDS: frozenset[str] = frozenset({"NULL", "TRUE", "FALSE"})

#: 记号里代表**比较运算符**的关键词（它们不是符号形态的比较符）。
_COMPARISON_WORDS: frozenset[str] = frozenset({"LIKE", "IN", "BETWEEN", "IS"})

#: 允许出现在表达式里的**受控标量函数**（清单）。
#:
#: 为什么不放开任意函数：表达式会被拼进读路径 ⇒ 函数名也是注入面。
#: `pg_sleep` / `DBMS_LOCK.SLEEP` 一类能逐行阻塞 ⇒ 业务库 DoS；
#: `UTL_HTTP` 一类能外联 ⇒ 数据外泄。
#:
#: 为什么**函数走独立清单**而不是绑定白名单：绑定白名单是「**这一列**」
#: 的授权清单，让它兼任函数清单 ⇒ 一个函数名进了白名单就等于永久授权。
#: 函数是**代码**，列是**数据**，两者授权面不同 ⇒ 分开。
SAFE_SCALAR_FUNCTIONS: frozenset[str] = frozenset(
    {
        "ABS",
        "CEIL",
        "COALESCE",
        "FLOOR",
        "GREATEST",
        "INSTR",
        "LEAST",
        "LENGTH",
        "LOWER",
        "LTRIM",
        "MOD",
        "NVL",
        "POWER",
        "ROUND",
        "RTRIM",
        "SUBSTR",
        "TO_CHAR",
        "TO_DATE",
        "TO_NUMBER",
        "TRIM",
        "TRUNC",
        "UPPER",
    }
)

#: **表达式里禁止出现的 SQL 结构词**（本模块私有，不动 DQ 侧共享表 ——
#: DQ 的规则表达式在用同一张表，改了会误伤）。
#:
#: 为什么必须禁：`SELECT` / `FROM` / `WHERE` 等不在 `_SQL_KEYWORDS` 里 ⇒
#: 它们会被当成普通标识符 ⇒ 而白名单是**写入方自报**的 ⇒ 只要把它们写进白名单，
#: 就能在过滤表达式里塞子查询读别的表（跨表读）或调任意函数。
#: 禁掉这些词 ⇒ 「白名单自报」这条绕道被堵死。
#:
#: 刻意**不**禁的：`ROWNUM` / `LEVEL` / `ROW` / `IN` / `BETWEEN` / `LIKE` ——
#: 它们是 Oracle 里合法的行级过滤要素。
EXPRESSION_RESERVED_WORDS: frozenset[str] = frozenset(
    {
        "SELECT",
        "FROM",
        "WHERE",
        "JOIN",
        "INNER",
        "OUTER",
        "CROSS",
        "GROUP",
        "HAVING",
        "ORDER",
        "INTO",
        "OUTFILE",
        "COPY",
        "DO",
        "CALL",
        "EXISTS",
        "INTERSECT",
        "MINUS",
        "PIVOT",
        "CONNECT",
        "START",
        "WITH",
        "MERGE",
        "RETURNING",
        "UNION",
        "VALUES",
        "DECLARE",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "SET",
        # DML / DDL：DQ 侧 `validate_expression` 已经禁过（防多语句），
        # 这里再禁一遍是为了**白名单条目**也干净 —— 白名单项会被拼进
        # `SELECT <列清单>`，一个叫 `DROP` 的「列名」会让读路径直接拼坏。
        "DROP",
        "DELETE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
        # `TABLE` / `LATERAL`：PostgreSQL 的 `TABLE relation` 等价于
        # `SELECT * FROM relation` ⇒ `EXISTS (TABLE other_tbl)` 可以在**不出现
        # SELECT / FROM** 的情况下读别的表 ⇒ 跨表读绕过。
        "TABLE",
        "LATERAL",
    }
)

#: **只对过滤表达式生效**的禁词（变换表达式不受影响 —— 那里 `CASE` 只是常量投影）。
#:
#: 为什么禁 `CASE`：`CASE WHEN 条件 THEN 1 ELSE 1 END = 1` 在任何方言下都**恒真**
#: （NULL 也走 ELSE）⇒ 行级过滤被抹平成全表读；而 `CASE` 是 SQL 关键字 ⇒
#: 它不会被当成标识符 ⇒ 「白名单外的标识符」这条闸门看不到它 ⇒ 必须单独禁。
_FILTER_ONLY_FORBIDDEN_WORDS: frozenset[str] = frozenset({"CASE"})

#: **白名单条目**不许取的词（SQL 关键字 ∪ 结构词）。
#: 白名单项会被读路径当列名拼进 SQL ⇒ 一个叫 `DROP` / `SELECT` 的「列名」
#: 会让拼出来的 SQL 直接是另一条语句。
_RESERVED_FOR_ALLOWLIST: frozenset[str] = _SQL_KEYWORDS | EXPRESSION_RESERVED_WORDS


def _maskNonAsciiInLiterals(expression: str) -> str:
    """把**字符串字面量内部**的非 ASCII 字符替换成 ASCII 占位符。

    为什么需要：共享的 `validate_expression`（数据质量侧）只允许 ASCII token
    ⇒ 而本项目的业务数据是中文（`STATUS = '有效'`、`ORDER_STATUS <> '已作废'`）
    ⇒ 直接用它会把中文字面量**全部拒掉** ⇒ 业务上不可用。

    为什么这样做是安全的：只动**字面量内部**（单引号之间）⇒ 字面量内容
    本来就不参与任何判定（标识符抽取 / 关键字匹配 / 函数抽取都先把整段字面量
    剥掉）⇒ 掩码不改变判定结果，只让共享校验器接受非 ASCII 字面量。
    引号**之外**的非 ASCII 字符不在掩码范围 ⇒ 仍被共享校验器拒掉。
    """
    return _STRING_LITERAL_RE.sub(
        lambda m: "".join("x" if not ch.isascii() else ch for ch in m.group()),
        expression,
    )


def _validateExpressionSafety(expression: str) -> None:
    """跑共享的表达式安全校验（禁 DML / UNION / 注释 / 分号）。

    共享校验器（数据质量侧）只认 ASCII ⇒ 中文字面量先掩码再送检（见
    `_maskNonAsciiInLiterals`）。报错时把**原文**一并带上 ⇒ 用户看到的
    是自己写的表达式，而不是掩码后的样子。
    """
    masked = _maskNonAsciiInLiterals(expression)
    try:
        validate_expression(masked)
    except ValidationError as exc:
        if masked == expression:
            raise
        raise ValidationError(f"{exc}（原文：{expression!r}）") from exc


def _stripLiteralsAndQuoted(expression: str) -> tuple[str, set[str]]:
    """剥掉字符串字面量与双引号标识符。

    返回 ``(剩余文本, 双引号标识符集合)``：

    - **字面量整段剥掉**：`STATUS = 'ORDER_QTY'` 里的 ORDER_QTY 是数据不是标识符；
    - **引号标识符整段剥掉**：它要**整体**参与白名单比对，不能按词切分
      （`"SALARY NET"` 是一个列名，按词切分会变成 SALARY / NET 两个 token）。
    """
    quoted: set[str] = set()

    def _takeQuoted(match: re.Match[str]) -> str:
        quoted.add(match.group(1))
        return " "

    withoutLiterals = _STRING_LITERAL_RE.sub(" ", expression)
    return _QUOTED_IDENTIFIER_RE.sub(_takeQuoted, withoutLiterals), quoted


def extractIdentifiers(expression: str) -> set[str]:
    """抽出表达式里出现的**全部**标识符（列名 + 函数名）。

    - **字符串字面量整段剥掉**：`STATUS = 'ORDER_QTY'` 里 ORDER_QTY 是数据不是标识符，
      否则会要求用户为字面量里的每个词都开白名单。
    - **双引号标识符整段抽出**：`"SALARY NET"` 是一个带空格的列名 ⇒ 必须整体进白名单；
      `"IN"` 是被引号包住的保留字 ⇒ 同样要进白名单（关键字豁免只对未加引号的部分生效）。
    - **SQL 关键字不算标识符**：`IS` / `AND` / `NULL` 不该进白名单。
    - **函数名算标识符**：函数能改变行为（如 `UPPER`）⇒ 必须显式放行。
    """
    text, quoted = _stripLiteralsAndQuoted(expression)
    words = {
        token
        for token in _IDENTIFIER_RE.findall(text)
        if token.upper() not in _SQL_KEYWORDS
    }
    return words | quoted


def extractCallIdentifiers(expression: str) -> set[str]:
    """抽出**函数调用形态**的标识符（`NAME(`）。

    与普通标识符分开的原因：函数走 `SAFE_SCALAR_FUNCTIONS`，列走绑定白名单 ⇒
    两套授权面不能混。SQL 关键字（`IN (`）与结构词（`EXISTS (`）不算函数调用。
    """
    text, _ = _stripLiteralsAndQuoted(expression)
    return {
        name
        for name in _CALL_RE.findall(text)
        if name.upper() not in _SQL_KEYWORDS
        and name.upper() not in EXPRESSION_RESERVED_WORDS
    }


def extractPlainIdentifiers(expression: str) -> set[str]:
    """抽出**非函数调用**的标识符 —— 这才是要进绑定白名单的东西。"""
    calls = {name.upper() for name in extractCallIdentifiers(expression)}
    return {name for name in extractIdentifiers(expression) if name.upper() not in calls}


def _assertNoOr(expression: str) -> None:
    """表达式里不许出现 `OR`。

    为什么必须禁：`STATUS = 'x' OR 1=1` 里被引用的标识符仍然只有 `STATUS`
    （`1=1` 没有标识符）⇒ 白名单校验会**放行** ⇒ 而这条过滤恒真 ⇒
    行级过滤被抹平成全表读 —— 与「不给白名单」是同一类越权。

    只在**剥掉字面量之后**的文本上匹配 ⇒ `STATUS = 'A OR B'` 不误杀。
    """
    text, _ = _stripLiteralsAndQuoted(expression)
    if _OR_RE.search(text):
        raise ValidationError(
            f"表达式不允许使用 OR（顶层或嵌套都会让行级过滤失去约束）：{expression!r}"
        )


def _assertNoReservedWords(identifiers: set[str], expression: str) -> None:
    """表达式里不许出现 SQL 结构词（子查询 / DML / 多语句）。"""
    hits = sorted(
        {name for name in identifiers if name.upper() in EXPRESSION_RESERVED_WORDS}
    )
    if hits:
        raise ValidationError(
            f"表达式包含禁止的结构词：{hits}（当前表达式：{expression!r}）"
        )


def _assertFunctionsAllowed(expression: str) -> None:
    """函数调用必须落在受控标量函数清单内。"""
    calls = extractCallIdentifiers(expression)
    unknown = sorted(name for name in calls if name.upper() not in SAFE_SCALAR_FUNCTIONS)
    if unknown:
        raise ValidationError(
            f"表达式调用了未授权的函数：{unknown}；"
            f"只允许受控标量函数 {sorted(SAFE_SCALAR_FUNCTIONS)}"
        )


def _assertNoFilterOnlyWords(expression: str) -> None:
    """过滤表达式里不许出现**只对过滤生效**的禁词（当前是 `CASE`）。

    为什么单独一条：`CASE` 是 SQL 关键字 ⇒ 不会被当成标识符 ⇒
    「白名单外的标识符」那条闸门看不见它 ⇒ 必须按**原文文本**匹配。

    为什么禁：`CASE WHEN 条件 THEN 1 ELSE 1 END = 1` 恒真（NULL 也走 ELSE）
    ⇒ 行级过滤被抹平成全表读。
    """
    text, _ = _stripLiteralsAndQuoted(expression)
    hits = sorted(
        word for word in _FILTER_ONLY_FORBIDDEN_WORDS if re.search(rf"\b{word}\b", text, re.IGNORECASE)
    )
    if hits:
        raise ValidationError(
            f"过滤表达式包含禁止的结构词：{hits}（当前表达式：{expression!r}）"
        )


def _tokenize(expression: str) -> list[tuple[str, str]]:
    """把表达式切成 ``(kind, text)`` 记号序列（**丢弃空白**）。

    丢弃空白是必须的：比较符的「紧邻操作数」要靠记号相邻判定 ⇒
    `STATUS = 'X'` 里 `=` 的左邻记号必须是 `STATUS`，而不是一个空格。
    """
    return [
        (match.lastgroup or "other", match.group())
        for match in _TOKEN_RE.finditer(expression)
        if not match.group().isspace()
    ]


def _isConstantToken(token: tuple[str, str]) -> bool:
    """记号是不是**常量**（字符串 / 数字 / `NULL` / `TRUE` / `FALSE`）。"""
    kind, text = token
    if kind in ("string", "number"):
        return True
    return kind == "ident" and text.upper() in _CONSTANT_KEYWORDS


def _isBareColumnToken(tokens: list[tuple[str, str]], index: int) -> bool:
    """记号是不是**裸列**（标识符 / 引号标识符，且不是函数调用的名字）。

    ⚠️ **SQL 关键字不算裸列**：`END = 1` 里的 `END` 是 `CASE` 的收尾关键字，
    不是列 ⇒ 若把关键字算成列，`CASE ... END = 1` 就能靠 `END` 满足
    「裸列↔常量」这条闸门 ⇒ 恒真谓词又从侧门进来了。
    """
    if index < 0 or index >= len(tokens):
        return False
    kind, text = tokens[index]
    if kind not in ("ident", "quoted"):
        return False
    if kind == "ident" and text.upper() in _SQL_KEYWORDS:
        return False
    # 函数调用形态：`NAME(` ⇒ 这个记号是函数名，不是裸列。
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    return not (following is not None and following[0] == "other" and following[1] == "(")


def _isConstantList(tokens: list[tuple[str, str]], start: int) -> bool:
    """`(` 开始的括号内是否**全是常量**（`IN ('a','b')` 这种）。"""
    if start >= len(tokens) or tokens[start] != ("other", "("):
        return False
    depth = 0
    for index in range(start, len(tokens)):
        kind, text = tokens[index]
        if kind == "other" and text == "(":
            depth += 1
            continue
        if kind == "other" and text == ")":
            depth -= 1
            if depth == 0:
                return True
            continue
        if depth == 1 and not (_isConstantToken(tokens[index]) or (kind == "other" and text == ",")):
            return False
    return False


def _isColumnConstantComparison(
    tokens: list[tuple[str, str]], index: int, operator: str
) -> bool:
    """这个比较是不是「**裸列 与 常量**」的比较。"""
    if index == 0:
        return False
    left = tokens[index - 1]
    right = tokens[index + 1] if index + 1 < len(tokens) else None
    leftIsColumn = _isBareColumnToken(tokens, index - 1)
    rightIsColumn = right is not None and _isBareColumnToken(tokens, index + 1)

    if operator == "IN":
        return leftIsColumn and right == ("other", "(") and _isConstantList(tokens, index + 1)
    if operator == "BETWEEN":
        if not leftIsColumn or right is None:
            return False
        after = tokens[index + 2] if index + 2 < len(tokens) else None
        afterAfter = tokens[index + 3] if index + 3 < len(tokens) else None
        return (
            _isConstantToken(right)
            and after is not None
            and after[0] == "ident"
            and after[1].upper() == "AND"
            and afterAfter is not None
            and _isConstantToken(afterAfter)
        )
    if operator == "IS":
        if not leftIsColumn or right is None:
            return False
        if right[0] == "ident" and right[1].upper() in _CONSTANT_KEYWORDS:
            return True
        # `IS NOT NULL`
        after = tokens[index + 2] if index + 2 < len(tokens) else None
        return (
            right[0] == "ident"
            and right[1].upper() == "NOT"
            and after is not None
            and after[0] == "ident"
            and after[1].upper() in _CONSTANT_KEYWORDS
        )
    if operator == "LIKE":
        return leftIsColumn and right is not None and right[0] == "string"

    # 其余是符号形态的比较符（= / <> / != / < / <= / > / >=）
    if leftIsColumn and right is not None and _isConstantToken(right):
        return True
    return rightIsColumn and _isConstantToken(left)


def _assertHasColumnConstantComparison(expression: str) -> None:
    """过滤表达式里必须至少有一处「**裸列 与 常量**」的比较。

    为什么需要这条：行级过滤的**唯一目的是收窄行集** ⇒ 过滤谓词必须**依赖行**。
    恒真谓词（`1=1` / `A = A` / `ABS(A) >= 0` / `CASE ... ELSE 1 END = 1`）
    会把行级过滤抹平成**全表读** —— 与「不给白名单」是同一类越权。

    为什么「禁 OR」不够：`OR` 只是恒真的**一种**写法。禁掉 OR 之后，
    `ABS(A) >= 0`（函数值恒非负）、`A = A`、`CASE ... ELSE 1 END = 1`
    都能绕过 —— 而它们的标识符引用都合法 ⇒ 白名单闸门放行。

    这条规则从**语法**上堵死恒真：只要存在一处「裸列 与 常量」的比较，
    且顶层只有 `AND` / `NOT`（`OR` 已禁）⇒ 整个合取式**依赖行** ⇒ 不可能恒真。

    代价（刻意取舍）：**只有函数比较**的过滤表达式不被接受
    （如 `UPPER(A) = 'X'`）⇒ 需要函数时，另配一处裸列比较
    （如 `STATUS = '有效' AND UPPER(NAME) = 'X'`）。
    """
    tokens = _tokenize(expression)
    for index, (kind, text) in enumerate(tokens):
        operator: str | None = None
        if kind == "op":
            operator = text
        elif kind == "ident" and text.upper() in _COMPARISON_WORDS:
            operator = text.upper()
        if operator is None:
            continue
        if _isColumnConstantComparison(tokens, index, operator):
            return
    raise ValidationError(
        "过滤表达式必须至少有一处「列 与 常量」的直接比较"
        "（否则整条过滤与行无关，等同于把行级过滤抹成全表读）："
        f"{expression!r}"
    )


def validateFilterExpression(
    expression: str | None,
    allowlist: Sequence[str] | None,
) -> None:
    """校验行级过滤表达式与其白名单。

    规则（顺序即短路顺序）：

    1. 没有表达式 ⇒ 放行（没有过滤就没有越权面）；
    2. 有表达式 ⇒ 白名单必须非空（**不给白名单 = 默认放行 = 越权**，必须拒绝）；
    3. 表达式本身必须安全（复用数据质量侧的表达式白名单：禁 DML / UNION / 注释）；
    4. **不许 `OR`**（否则 `X = 1 OR 1=1` 恒真 ⇒ 行级过滤失效）；
    5. **不许 `CASE`**（`CASE ... ELSE 1 END = 1` 恒真 ⇒ 同上）；
    6. **函数调用**必须落在 `SAFE_SCALAR_FUNCTIONS`（堵 `pg_sleep` / `UTL_HTTP`）；
    7. **结构词**（`SELECT` / `FROM` / `TABLE` / `UNION` …）不许出现（堵子查询跨表读）；
    8. 表达式**必须引用至少一个白名单内的列**（`1=1` 这种零列引用 = 把行级过滤
       抹成全表读，与「不给白名单」是同一类越权）；
    9. 表达式用到的每个**列**标识符都必须出现在白名单里（大小写不敏感）。
       函数名**不在**这条比对里（函数走第 6 条的受控清单）；
    10. 必须至少有一处「**裸列 与 常量**」的直接比较（否则整条过滤与行无关 ⇒
        恒真 ⇒ 行级过滤被抹平成全表读）。
    """
    if not expression or not expression.strip():
        return

    allowed = {item.strip().upper() for item in (allowlist or []) if item.strip()}
    if not allowed:
        raise ValidationError(
            f"过滤表达式必须给出白名单（当前表达式：{expression!r}；白名单为空）"
        )

    _validateExpressionSafety(expression)
    _assertNoOr(expression)
    _assertNoFilterOnlyWords(expression)
    _assertFunctionsAllowed(expression)

    plain = extractPlainIdentifiers(expression)
    _assertNoReservedWords(plain, expression)
    if not plain:
        raise ValidationError(
            f"过滤表达式必须引用白名单内的列（当前表达式不含任何列引用：{expression!r}）"
        )
    missing = sorted(name for name in plain if name.upper() not in allowed)
    if missing:
        raise ValidationError(
            f"过滤表达式使用了白名单外的标识符：{missing}；白名单={sorted(allowed)}"
        )
    _assertHasColumnConstantComparison(expression)


def validateKeyOrdinals(ordinals: Sequence[int | None]) -> None:
    """校验复合键序号：非空项必须**不重复**且**从 1 开始连续**。

    - ``None`` = 非键列，忽略；
    - 重复 ⇒ 库层部分唯一索引会拒，但提前给出可读文案；
    - 不连续（缺位）⇒ 库层放行但下游会把序号当序位用 ⇒ 这里必须挡。
    """
    present = sorted({ordinal for ordinal in ordinals if ordinal is not None})
    if not present:
        return
    if len(present) != len([o for o in ordinals if o is not None]):
        raise ValidationError(f"复合键序号重复：{present}")
    if present[0] < 1:
        raise ValidationError(f"复合键序号必须 ≥ 1：{present}")
    if present != list(range(1, len(present) + 1)):
        raise ValidationError(f"复合键序号必须从 1 起连续：{present}")


def validateBindingRole(role: str) -> None:
    """绑定角色必须是受控字面量（与迁移 0154 的 CHECK 同源）。"""
    if role not in VALID_BINDING_ROLES:
        raise ValidationError(
            f"bindingRole 非法：{role!r}；应为 {sorted(VALID_BINDING_ROLES)}"
        )


def validateColumnName(column: str) -> None:
    """物理列名必须是合法 SQL 标识符（禁止拼接式注入）。

    用 `fullmatch` 语义：`$` 在 Python 正则里允许**尾随换行** ⇒ `"ABC\n"` 会被
    `re.match` 放行，而拼进 SQL 时是另一回事。
    """
    if not _COLUMN_RE.fullmatch(column or ""):
        raise ValidationError(f"物理列名非法（必须 ^[A-Za-z_][A-Za-z0-9_]*$）：{column!r}")


def validateAllowlistItems(allowlist: Sequence[str] | None) -> None:
    """白名单条目必须逐条是合法列标识符、**长度合规**、且**不是 SQL 保留字**。

    为什么单独一条规则：白名单项会被读路径当作「可放行的列名」看待；
    非标识符 / 超长的条目要么拼出注入面，要么被 PostgreSQL **静默截断**
    （数组元素超长是截断不是报错）⇒ 校验对象与落库对象会不是同一个。

    为什么要在这里也卡长度：DTO 层有 `max_length=100`，但**任何不经 HTTP 的
    写入方**（导入作业 / 脚本 / 内部服务）都绕得过 DTO ⇒ 长度闸门必须住在
    纯校验层（共享常量 `MAX_ALLOWLIST_ITEM_LENGTH`，与库列 `ARRAY(String(100))` 同宽）。

    为什么还要禁保留字：白名单项会被拼进 SQL 的列清单 ⇒ 一个叫 `SELECT` 的
    「列名」会让拼出来的 SQL 变成另一条语句（`SELECT ... , SELECT`）。
    """
    for item in allowlist or []:
        validateColumnName(item)
        if len(item) > MAX_ALLOWLIST_ITEM_LENGTH:
            raise ValidationError(
                f"白名单条目过长（{len(item)} > {MAX_ALLOWLIST_ITEM_LENGTH} 字符）：{item!r}"
            )
        if item.strip().upper() in _RESERVED_FOR_ALLOWLIST:
            raise ValidationError(f"白名单条目不能是 SQL 保留字：{item!r}")


def validateTransformExpression(expression: str | None, sourceColumn: str) -> None:
    """列级变换表达式校验（与过滤表达式**同级**的闸门）。

    规则：

    1. 没有表达式 ⇒ 放行；
    2. 表达式本身必须安全（禁 DML / UNION / 注释 / 分号 —— 复用数据质量侧
       ``validate_expression``）；
    3. **不许 `OR`**；
    4. **函数调用**必须落在 `SAFE_SCALAR_FUNCTIONS`（函数能改变行为 ⇒ 独立授权）；
    5. **列**标识符只允许本属性自己的物理列（跨列引用要显式开白名单，本期不做）；
    6. **结构词**不许出现。
    """
    if not expression or not expression.strip():
        return

    _validateExpressionSafety(expression)
    _assertNoOr(expression)
    _assertFunctionsAllowed(expression)

    plain = extractPlainIdentifiers(expression)
    _assertNoReservedWords(plain, expression)

    allowed = {(sourceColumn or "").strip().upper()}
    missing = sorted(name for name in plain if name.upper() not in allowed)
    if missing:
        raise ValidationError(
            f"列级变换表达式使用了未授权的标识符：{missing}；"
            f"只允许本属性的物理列 {sourceColumn!r} 与受控标量函数 "
            f"{sorted(SAFE_SCALAR_FUNCTIONS)}"
        )
