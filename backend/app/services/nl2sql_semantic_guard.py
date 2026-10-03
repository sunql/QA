"""Top-N 占比分母守卫（feat-nl2sql-share-denominator-guard）。

2026-10-02 真机回归：同一问题两次生成 SQL，一次用独立 CTE 从过滤前明细算分母（对），
一次在 Top-N 过滤后的行集上用窗口函数算分母（分母=分子，占比恒 100%，错）。prompt
负向约束是概率性的（LLM 非确定性），形态级拦截才是确定性的。三层职责：

- L1 ``findShareDenominatorIssues``：生成出口的形态拦截（generateSql 重试循环内，
  与 _assert_read_only 同构——拦截原因回灌 errors 重试）。
- L3 ``checkShareInvariants``：执行后的数学不变量（占比 > 100% 在 Top-N 场景不可能
  成立——Top-N 是总量的子集），确定性判错。
- L2 ``shareAmbiguityWarning``：全部恒 100% 无法数学判错（也可能是组内明细恰好 ≤ N），
  诚实示警而非静默返回。

判据核心（SQL 求值顺序）：WHERE 先于 SELECT 求值，因此**同一块**里「排名列过滤 +
窗口函数占比分母」必然是陷阱，不存在合法的同块形态；合法形态（内层窗口 + 外层过滤、
独立 CTE 分母 JOIN）都不同块或无窗口。块作用域 = 顶层语句 + 每个 SELECT/WITH 子查询体。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

from app.domain.query_plan import QueryPlan
from app.services.messages_zh import (
    MSG_NL2SQL_SHARE_AMBIGUOUS_WARNING,
    MSG_NL2SQL_SHARE_DENOMINATOR_FEEDBACK,
)

logger = logging.getLogger(__name__)

# 占比与 1 比较的容差：浮点占比（0.9999999999）不误报
_SHARE_EPS = Decimal("0.000000001")

# 子查询块起点：( 之后第一个词是 SELECT / WITH
_SUBQUERY_START_RE = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)

# WHERE 子句终止关键字（相对深度 0 处）
_CLAUSE_END_RE = re.compile(
    r"\b(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|FETCH|WINDOW)\b", re.IGNORECASE
)

# 排名列过滤：可选「别名.」前缀 + 排名列名 + <= / < / = + 数字。
# 词边界防止 return / toprank 之类误命中。
_RANK_FILTER_RE = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*\s*\.\s*)?"
    r"(?:rn|rnk|rank|row_number|rownum|row_num|seq)"
    r"\s*(?:<=|<|=)\s*\d+",
    re.IGNORECASE,
)

# 比率 + 窗口分母：`/` 之后不跨逗号/分号即遇到 OVER(。逗号是 SELECT 列表项边界，
# 保证「/ 与 OVER 同属一个表达式」；穿透 NULLIF(x, 0) 之类包裹（逗号在 OVER 之后）。
_RATIO_WINDOW_RE = re.compile(r"/[^,;]*?\bOVER\s*\(", re.IGNORECASE | re.DOTALL)

_STRING_LITERAL_RE = re.compile(r"'[^']*'")
_DQUOTE_LITERAL_RE = re.compile(r'"[^"]*"')
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _stripLiteralsAndComments(sql: str) -> str:
    """去掉字符串字面量与注释，替换为空格（保持位置，块扫描不依赖原偏移）。"""
    text = _LINE_COMMENT_RE.sub(" ", sql)
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _STRING_LITERAL_RE.sub(" ", text)
    return _DQUOTE_LITERAL_RE.sub(" ", text)


def _findSubquerySpans(text: str) -> list[tuple[int, int]]:
    """找出所有 SELECT/WITH 子查询体的 (start, end) 半开区间（text[start+1:end]）。

    单趟扫描：遇 ( 判断其后第一个词，是子查询则记录并按括号配对跳过整个体
    （体内不再嵌套找——嵌套体由 _iterBlocks 对体文本递归负责）。
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            if _SUBQUERY_START_RE.match(text[i + 1 :]):
                # 配对闭合（从 i 起重新计深度）
                j = i + 1
                inner = 1
                while j < n and inner:
                    if text[j] == "(":
                        inner += 1
                    elif text[j] == ")":
                        inner -= 1
                    j += 1
                spans.append((i, j - 1))  # text[i+1 : j-1] 为体
                i = j
                continue
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return spans


def _blankSpans(text: str, spans: list[tuple[int, int]]) -> str:
    """把给定区间替换为等长空格（位置保持，方便统一扫描）。"""
    chars = list(text)
    for start, end in spans:
        for k in range(start, min(end, len(chars))):
            chars[k] = " "
    return "".join(chars)


def _whereClauseText(body: str) -> str:
    """取块内相对深度 0 的 WHERE 子句文本（到下一个子句关键字或块尾）。

    前提：body 已把嵌套子查询体置空——深度 >0 的括号只剩函数调用，关键字
    匹配只在深度 0 生效，函数实参里的 GROUP/ORDER 不会截断子句。
    """
    match = re.search(r"\bWHERE\b", body, re.IGNORECASE)
    if match is None:
        return ""
    i = match.end()
    n = len(body)
    depth = 0
    while i < n:
        ch = body[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break  # 块边界
            depth -= 1
        elif depth == 0 and _CLAUSE_END_RE.match(body, i):
            break
        i += 1
    return body[match.start() : i]


def _iterBlocks(sql: str) -> Iterator[tuple[str, str]]:
    """产出 (localText, whereText) 对：顶层语句 + 每个 SELECT/WITH 子查询体。

    localText 已把嵌套子查询体置空（跨块内容不参与本块判定）、字面量已剥离。
    """
    text = _stripLiteralsAndComments(sql)
    pending: list[str] = [text]
    while pending:
        body = pending.pop(0)
        spans = _findSubquerySpans(body)
        localText = _blankSpans(body, spans)
        yield localText, _whereClauseText(localText)
        for start, end in spans:
            pending.append(body[start + 1 : end])


def findShareDenominatorIssues(sql: str | None) -> tuple[str, ...]:
    """L1：检测「Top-N 过滤后的行集上用窗口函数算占比分母」形态。

    返回问题列表（空 = 通过）。已知漏报边界：排名列别名不在词表内（rn/rnk/rank/
    row_number/rownum/row_num/seq）时漏检，由 L3 结果不变量兜底；UNION 两侧视为
    同块（从不在本仓生成风格中出现，接受）。
    """
    if not sql:
        return ()
    for localText, whereText in _iterBlocks(sql):
        if _RANK_FILTER_RE.search(whereText) and _RATIO_WINDOW_RE.search(localText):
            logger.warning(
                "占比分母守卫命中：同块存在排名过滤与窗口分母 where=%s",
                whereText[:120],
            )
            return (MSG_NL2SQL_SHARE_DENOMINATOR_FEEDBACK,)
    return ()


@dataclass(frozen=True)
class ShareCheckResult:
    """L3/L2 检查结果：violations 非空 = 数学上必然错误；warning = 歧义示警。"""

    violations: tuple[str, ...] = ()
    warning: str | None = None


def _toDecimal(value: Any) -> Decimal | None:
    """结果值 → Decimal；bool/None/非数值/NaN 返回 None（宽松跳过，不误报）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _findKey(row: dict, name: str) -> str | None:
    """行内列名匹配：精确优先，其次大小写不敏感（DB 返回列名大小写不定）。"""
    if name in row:
        return name
    lowered = name.lower()
    for key in row:
        if isinstance(key, str) and key.lower() == lowered:
            return key
    return None


def _shareAliases(plan: QueryPlan) -> tuple[str, ...]:
    """占比别名：formula 含除法的聚合的 alias（Top-N 场景 LLM 会沿用 plan 别名命名输出列）。"""
    return tuple(
        agg.alias
        for agg in plan.aggregations
        if agg.alias and agg.formula and "/" in agg.formula
    )


def checkShareInvariants(plan: QueryPlan | None, data: list[dict] | None) -> ShareCheckResult:
    """L3 + L2：Top-N 占比的数学不变量核验（纯函数，结果行内计算，零额外查询）。

    - violations：任一占比值 > 100%（Top-N 是总量子集，单行不可能）；或组列可解析时
      同组占比之和 > 100%。
    - warning：所有占比值恒等于 100%——陷阱签名，但组内明细恰好 ≤ N 时也合法，
      不可数学判错，交由上层示警。
    - 宽松契约：无 plan / 无数据 / 非 Top-N / 无占比别名 / 组列对不上行键 → 空结果
      （守卫宁漏不误报，漏报由其他层兜底）。
    """
    if plan is None or not data:
        return ShareCheckResult()
    if plan.perGroupLimit is None and plan.rowLimit is None:
        return ShareCheckResult()
    aliases = _shareAliases(plan)
    if not aliases:
        return ShareCheckResult()

    violations: list[str] = []
    sawValue = False
    allOnes = True
    for row in data:
        for alias in aliases:
            key = _findKey(row, alias)
            if key is None:
                continue
            value = _toDecimal(row[key])
            if value is None:
                continue
            sawValue = True
            if value > 1 + _SHARE_EPS:
                violations.append(f"占比列 {alias} 出现 {value}（> 100%）")
            if abs(value - 1) > _SHARE_EPS:
                allOnes = False
    if violations:
        return ShareCheckResult(violations=tuple(violations))

    # 组列可解析时按组求和：单行 ≤1 但同组多行加起来 >1 也判错（行粒度 ≠ 组粒度时）
    groupKey = _resolveGroupKey(plan, data[0])
    if groupKey is not None:
        for alias in aliases:
            sums: dict[Any, Decimal] = {}
            for row in data:
                value = _toDecimal(row.get(_findKey(row, alias) or ""))
                if value is None:
                    continue
                groupValue = row.get(groupKey)
                sums[groupValue] = sums.get(groupValue, Decimal(0)) + value
            for groupValue, total in sums.items():
                if total > 1 + _SHARE_EPS:
                    violations.append(
                        f"占比列 {alias} 在组 {groupValue!r} 的合计为 {total}（> 100%）"
                    )
        if violations:
            return ShareCheckResult(violations=tuple(violations))

    warning = MSG_NL2SQL_SHARE_AMBIGUOUS_WARNING if (sawValue and allOnes) else None
    return ShareCheckResult(warning=warning)


def _resolveGroupKey(plan: QueryPlan, sampleRow: dict) -> str | None:
    """把 plan 的分组维（partitionBy/groupBy）对到结果行列名；对不上返回 None。"""
    for name in (*plan.partitionBy, *plan.groupBy):
        key = _findKey(sampleRow, name)
        if key is not None:
            return key
    return None


def shareAmbiguityWarning(plan: QueryPlan | None, data: list[dict] | None) -> str | None:
    """L2 薄封装：只取歧义示警文本，供答案渲染层注入（violations 由执行层抛错处理）。"""
    return checkShareInvariants(plan, data).warning
