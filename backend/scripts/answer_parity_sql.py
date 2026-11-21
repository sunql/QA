"""A5 比较层的 SQL 部分：L1 规范化 + L3 口径元素集合。

两条硬要求决定了这里的写法：

1. **别名不是身份**：`s.BPSNUM_0` 与 `f.BPSNUM_0` 指向同一张表时必须相等；
   但**自连接**里两个别名就是两个角色（`s1.PARENT = s2.CODE`），绝不能坍缩。
2. **空集不许算通过**：L3 比的是「查数时的限定条件集合」，集合必须包含
   **FROM 基表（含 schema）、度量表达式、JOIN 边与类型、WHERE / HAVING / 非内连接 ON、
   GROUP BY、DISTINCT、分页（LIMIT / FETCH / OFFSET，以及分页时的 ORDER BY）** ——
   少收一类，就等于把「换表 / 换度量 / 换取行范围」判成一致。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

DIALECT = "oracle"


class UnparsableSql(RuntimeError):
    """SQL 解析不了 ⇒ 该样本判「无法判定」（退出码 6），**不许**退回文本比较蒙结论。"""


def _parse(sql: str) -> exp.Expression:
    try:
        return sqlglot.parse_one(sql, read=DIALECT)
    except Exception as exc:  # noqa: BLE001 - 解析器异常一律视为「判不了」
        raise UnparsableSql(f"SQL 无法按 {DIALECT} 方言解析：{exc}") from exc


def _tableIdentity(name: str, alias: str, occurrences: Counter[str]) -> str:
    """同一张表在一次查询里出现多次 ⇒ 用别名当身份（自连接的两个角色必须可区分）。"""
    if not alias or not name:
        return name or alias
    if alias == name:
        return name
    return name if occurrences[name] == 1 else alias


def _buildAliasMap(tree: exp.Expression) -> dict[str, str]:
    tables = list(tree.find_all(exp.Table))
    occurrences: Counter[str] = Counter((table.name or "").upper() for table in tables)
    aliasMap: dict[str, str] = {}
    for table in tables:
        alias = (table.alias_or_name or "").upper()
        real = (table.name or "").upper()
        if alias and real:
            aliasMap[alias] = _tableIdentity(real, alias, occurrences)
    return aliasMap


def _resolveTableAliases(tree: exp.Expression) -> exp.Expression:
    """把 `s.COL` 解析成 `DIM_SUPPLIER.COL`；自连接保持别名，别名声明一并去掉。

    为什么不是「直接删掉表前缀」：`d.SUPPLIER_CODE` 与 `s.BPSNUM_0` 删前缀后
    只剩 `SUPPLIER_CODE` / `BPSNUM_0`，两个不同表上的同名列会**碰巧相等**
    ⇒ L3 假绿。
    """
    aliasMap = _buildAliasMap(tree)

    def _resolve(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            alias = (node.alias_or_name or "").upper()
            real = (node.name or "").upper()
            # 列引用都已换成解析后的身份，表声明上的别名只剩噪声；
            # 但自连接的别名就是身份，保留。
            if alias and real and aliasMap.get(alias) == real and alias != real:
                node.set("alias", None)
            return node
        if not isinstance(node, exp.Column) or not node.table:
            return node
        resolved = aliasMap.get(node.table.upper())
        if not resolved or resolved == node.table.upper():
            return node
        return exp.column(node.this.copy(), table=exp.to_identifier(resolved))

    return tree.transform(_resolve)


def _normalizeOperators(node: exp.Expression) -> exp.Expression:
    """把等价写法归一：单值 `IN (x)` → `= x`、`NOT IN (x)` → `<> x`、IN 值排序。

    **不解括号**：`(A+B)*C` 与 `A+B*C` 是两种口径，删掉 `Paren` 会把它们判成一致。
    AND 链的包裹括号由 `_flattenAnd` 在合取项层面处理。
    """

    def _normalize(current: exp.Expression) -> exp.Expression:
        # transform 是前序遍历：父节点 Not 先于子节点 In 被看到 ⇒ 必须在这里显式识别。
        if isinstance(current, exp.Not) and isinstance(current.this, exp.In):
            inner = current.this
            values = list(inner.expressions)
            if len(values) == 1:
                return exp.NEQ(this=inner.this.copy(), expression=values[0].copy())
            inner.set(
                "expressions", sorted(values, key=lambda item: item.sql(dialect=DIALECT))
            )
            return current
        if isinstance(current, exp.Not) and isinstance(current.this, exp.EQ):
            inner = current.this
            return exp.NEQ(this=inner.this.copy(), expression=inner.expression.copy())
        if isinstance(current, exp.In):
            values = list(current.expressions)
            if len(values) == 1:
                return exp.EQ(this=current.this.copy(), expression=values[0].copy())
            current.set(
                "expressions", sorted(values, key=lambda item: item.sql(dialect=DIALECT))
            )
            return current
        return current

    return node.transform(_normalize)


def _canonicalExpression(node: exp.Expression) -> str:
    normalized = normalize_identifiers(_normalizeOperators(node), dialect=DIALECT)
    return normalized.sql(dialect=DIALECT, comments=False)


def _flattenAnd(node: exp.Expression) -> list[exp.Expression]:
    """把顶层 AND 链摊平成合取项列表（`a AND b AND c` → [a, b, c]）。"""
    if isinstance(node, exp.Paren):
        return _flattenAnd(node.this)
    if isinstance(node, exp.And):
        return _flattenAnd(node.left) + _flattenAnd(node.right)
    return [node]


def canonicalSql(sql: str) -> str:
    """L1 用的规范化 SQL：别名解析 + 标识符大小写 + 空白/注释归一。"""
    tree = _normalizeOperators(_resolveTableAliases(_parse(sql)))
    return normalize_identifiers(tree, dialect=DIALECT).sql(
        dialect=DIALECT, comments=False
    )


def _joinKind(join: exp.Join) -> str:
    """JOIN 类型归一：`LEFT OUTER JOIN` 与 `LEFT JOIN` 是同一种口径。"""
    side = (join.side or "").upper()
    kind = (join.kind or "").upper()
    if kind == "OUTER" and side:
        kind = ""
    parts = [part for part in (side, kind) if part]
    return " ".join(parts) if parts else "INNER"


_CLAUSE_WRAPPERS = (exp.Where, exp.Having)


def _conjuncts(clause: exp.Expression | None) -> list[str]:
    if clause is None:
        return []
    # `find_all` 给的是 Where/Having 包装节点；JOIN 的 ON 是裸表达式。
    # 不剥掉包装，整条 AND 链会变成**一个**元素 ⇒ 丢一个谓词只算「换了一整块」，
    # 量化率失真，且差异定位不可读。
    body = clause.this if isinstance(clause, _CLAUSE_WRAPPERS) else clause
    return [_canonicalExpression(conjunct) for conjunct in _flattenAnd(body)]


def _qualifiedName(table: exp.Table) -> str:
    """表身份带 schema 限定：`ODS.T` 与 `DWD.T` 是两张表，不能都塌成 `T`。"""
    parts = [table.catalog, table.db, table.name]
    return ".".join(part for part in parts if part).upper()


def _tableElements(tree: exp.Expression) -> set[str]:
    elements: set[str] = set()
    for table in tree.find_all(exp.Table):
        elements.add(f"FROM:{_qualifiedName(table) or '?'}")
    return elements


def _measureElements(tree: exp.Expression) -> set[str]:
    elements: set[str] = set()
    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            # 别名只影响展示，不影响「算的是什么」
            elements.add(f"MEASURE:{_canonicalExpression(projection.unalias())}")
        if select.args.get("distinct") is not None:
            elements.add("DISTINCT")
    return elements


_PAGING_NODES = (exp.Limit, exp.Fetch, exp.Offset)


def _pagingElements(tree: exp.Expression) -> set[str]:
    """分页与「取哪一段」必须被看见：Oracle 惯用 `FETCH FIRST n ROWS ONLY` / `OFFSET`。

    只收 `exp.Limit` 会把 `FETCH FIRST 10` vs `FETCH FIRST 20` 判成一致（L3 假绿）；
    并且**有分页时 `ORDER BY` 决定取到哪些行**，排序键也必须进集合。
    """
    elements: set[str] = set()
    hasPaging = False
    for clause in tree.find_all(*_PAGING_NODES):
        hasPaging = True
        elements.add(f"PAGE:{_canonicalExpression(clause)}")
    if hasPaging:
        for order in tree.find_all(exp.Order):
            for ordered in order.expressions:
                elements.add(f"ORDERBY:{_canonicalExpression(ordered)}")
    return elements


def predicateSet(sql: str) -> frozenset[str]:
    """口径元素集合；L3 的硬红线比的就是它。

    元素构成（每一项都是一条会改变「查到什么」的限定）：
    - `FROM:<表>`：换基表必须被发现（否则纯换表会让集合两边都空 ⇒ 假绿）；
    - `MEASURE:<投影>`：换度量（COUNT → SUM、换列）必须被发现；
    - WHERE 合取项：裸表达式（与 INNER JOIN 的 ON 搬家视为等价）；
    - `HAVING:<谓词>`：聚合后过滤，与 WHERE 不可互换，单独命名空间；
    - `ON[<右表>]:<谓词>`：**非内连接**的 ON 与 WHERE 语义不同，不能合并；
    - `JOIN[<类型>]:<右表>`：丢 JOIN / 改 JOIN 类型（含 OUTER 归一）；
    - `GROUPBY:<键>`：聚合粒度；
    - `DISTINCT` / `PAGE:<分页子句>` / `ORDERBY:<键>`（仅分页时）：直接改变结果集。
    """
    tree = _resolveTableAliases(_parse(sql))
    elements: set[str] = set()
    elements |= _tableElements(tree)
    elements |= _measureElements(tree)
    elements |= _pagingElements(tree)

    for clause in tree.find_all(exp.Where):
        elements.update(_conjuncts(clause))

    for clause in tree.find_all(exp.Having):
        elements.update(f"HAVING:{item}" for item in _conjuncts(clause))

    for join in tree.find_all(exp.Join):
        right = join.this
        rightName = (
            _qualifiedName(right) if isinstance(right, exp.Table) else (right.alias_or_name or "")
        )
        kind = _joinKind(join)
        elements.add(f"JOIN[{kind}]:{(rightName or '?').upper()}")
        onConjuncts = _conjuncts(join.args.get("on"))
        if kind == "INNER":
            elements.update(onConjuncts)
        else:
            for item in onConjuncts:
                elements.add(f"ON[{kind}]:{item}")

    for group in tree.find_all(exp.Group):
        for grouped in group.expressions:
            elements.add(f"GROUPBY:{_canonicalExpression(grouped)}")

    return frozenset(elements)


@dataclass(frozen=True)
class CoverageDiff:
    """两态口径集合的对称差；`rate` 就是 M0.4 §4.3 的「口径覆盖差异率」。"""

    offOnly: tuple[str, ...]
    onOnly: tuple[str, ...]
    unionSize: int

    @property
    def isEmpty(self) -> bool:
        return not self.offOnly and not self.onOnly

    @property
    def rate(self) -> float:
        if self.unionSize == 0:
            return 0.0
        return (len(self.offOnly) + len(self.onOnly)) / self.unionSize


def coverageDiff(offSql: str, onSql: str) -> CoverageDiff:
    off = predicateSet(offSql)
    on = predicateSet(onSql)
    return CoverageDiff(
        offOnly=tuple(sorted(off - on)),
        onOnly=tuple(sorted(on - off)),
        unionSize=len(off | on),
    )
