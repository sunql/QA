"""属性/类引用归一化 + 校验提示（从 nl2sql_service 拆出）。

纯函数模块：把 LLM 输出的属性/类引用归一化（拆复合 '业务名 (alias)'、收集合法引用名、
识别时间粒度词、派生指标 alias 判定、公式属性提取），供 validatePlan 做存在性校验与
可操作报错。无 LLM 调用，无 I/O。

未来扩展：新增一条校验/归一化规则只需在本模块加一个纯函数，validatePlan 调它。
"""

from __future__ import annotations

import re
from dataclasses import replace

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.query_plan import Aggregation, QueryPlan


def _propertyRefNames(prop: OntologyProperty) -> set[str]:
    """单个属性的全部合法引用名：业务名、别名、物理列、近义词别名。

    schema 文本把这些都呈现给 LLM（`属性名 (别名): 类型 (column=物理列)` +
    `业务别名: [...]`，其中 column 段仅在物理列无法从左侧 token 读出时渲染），
    LLM 可能写任一合法引用名（如"到货行号"命中"行号"的别名）；
    校验只按业务名会误杀正确计划。source_column/property_alias/business_aliases
    可空，过滤空串。
    """
    return {
        token
        for token in (
            prop.property_name,
            prop.property_alias,
            prop.source_column,
            *(prop.business_aliases or ()),
        )
        if token
    }


def _classRefNames(cls: OntologyClass) -> set[str]:
    """一个类的全部合法属性引用名并集（供类的属性/分组/JOIN 列校验）。

    除未限定名（业务名/别名/物理列/近义词）外，额外收纳表限定与类名限定形式
    （`表.列` / `类.列` / `表.属性` / `类.属性`）。schema 以 `### 类名 (别名): table=表名`
    呈现类、以 `属性名 (别名): 类型` 呈现列，LLM 在 groupBy / JOIN 列
    等位置常写 `PORDERQ.ITMREF_0` 这类完整限定名，只按未限定名校验会误杀正确计划
    （真实回归 2026-08-15）。
    """
    if not cls.properties:
        return set()
    names = set().union(*(_propertyRefNames(p) for p in cls.properties))
    qualifiers = {q for q in (cls.source_table, cls.class_name) if q}
    if not qualifiers:
        return names
    for prop in cls.properties:
        for ref in _propertyRefNames(prop):
            for qualifier in qualifiers:
                names.add(f"{qualifier}.{ref}")
    return names


# 复合形式 '业务名 (alias)' 拆分（仅匹配一对 ASCII 括号；中文括号（）不算）
_COMPOUND_REF_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")


def _splitCompoundRef(prop: str) -> tuple[str, str | None]:
    """拆 '业务名 (alias)' → (业务名, alias)；无括号返回 (原值, None)。

    LLM 偶尔从 schema 渲染文本（业务名 (alias): 类型）原样抄
    property_name；校验按单 token 严格匹配必拒，统一在此处把复合形式还原成单 token。
    空字符串 / 非字符串 / 中文括号 / 嵌套括号均原样保留（不在本函数改造范围）。
    """
    if not isinstance(prop, str):
        return (prop, None)
    m = _COMPOUND_REF_RE.match(prop.strip())
    if not m:
        return (prop.strip(), None)
    return (m.group(1).strip(), m.group(2).strip())


def _normalizePlanProperties(
    plan: QueryPlan,
    classes: list[OntologyClass],
) -> QueryPlan:
    """把 plan 里所有 prop 字段中的复合形式 'name (alias)' 替换成首个合法 token。

    合法 token 取自 _classRefNames(classes)（业务名/别名/物理列/限定形式）。
    优先业务名（拆出前半段），都不在则原样保留交 validatePlan 报错。
    返回新 plan（frozen dataclass 不允许原地修改）；空 classes 时直接返回。

    触发场景：LLM 偶尔把 schema 渲染格式 '供应商 (BPSNUM_0)' 原样抄进 property_name，
    validatePlan 严格 token 匹配永远 false → 整轮失败；本函数在 _parsePlanOutcome
    之后 / SQL 生成之前替换，让后续链路不感知复合形式（2026-09-18 真实回归）。
    """
    if not classes:
        return plan
    refs = set().union(*(_classRefNames(c) for c in classes))
    if not refs:
        return plan

    def _pick(token: str, alias: str | None) -> str:
        if token in refs:
            return token
        if alias and alias in refs:
            return alias
        return token  # 双都不在：保留业务名，原有错误链路接管

    def _normProp(p: str) -> str:
        token, alias = _splitCompoundRef(p)
        return _pick(token, alias)

    return replace(
        plan,
        selectedProperties=tuple(_normProp(p) for p in plan.selectedProperties),
        aggregations=tuple(
            replace(a, property=_normProp(a.property)) for a in plan.aggregations
        ),
        groupBy=tuple(_normProp(p) for p in plan.groupBy),
        partitionBy=tuple(_normProp(p) for p in plan.partitionBy),
        sortBy=tuple(
            replace(s, property=_normProp(s.property)) for s in plan.sortBy
        ),
        joins=tuple(
            replace(j, columns=tuple(_normProp(c) for c in j.columns))
            for j in plan.joins
        ),
    )


def _aggregationAliases(aggregations: list[Aggregation]) -> set[str]:
    """聚合别名集合（含派生公式别名），供 ORDER BY alias 排序校验。"""
    return {agg.alias for agg in aggregations if agg.alias}


# 时间粒度词：问题含「按月/按年/按季度/按周/按天 分组、变化趋势」时，LLM 常把
# 「月份」「月」等裸词直接写进 groupBy。这些是粒度概念而非属性，须引导改用
# DATE/DATETIME 属性作为 groupBy（真实回归 2026-08-15）。
_TIME_BUCKET_TOKENS: frozenset[str] = frozenset({
    "月份", "月", "按月", "月分", "年月", "年度", "年", "按年", "年份",
    "季度", "季", "按季度", "周", "按周", "星期", "天", "按天", "日", "按日",
    "时间", "日期", "期间", "周期",
})

# DATE/DATETIME/TIMESTAMP 数据类型（供时间粒度分组提示收集日期属性）。
_DATE_TYPES: frozenset[str] = frozenset({
    "DATE", "DATETIME", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE",
    "TIMESTAMP WITHOUT TIME ZONE",
})


def _datePropertyNames(classes: list[OntologyClass]) -> list[str]:
    """收集各类中 DATE/DATETIME/TIMESTAMP 类型属性的业务名（去重保序，供时间粒度提示）。"""
    seen: list[str] = []
    for cls in classes:
        for prop in cls.properties:
            name = prop.property_name
            if name and name not in seen and (prop.data_type or "").upper() in _DATE_TYPES:
                seen.append(name)
    return seen


def _timeBucketGroupHint(token: str, classes: list[OntologyClass]) -> str:
    """分组属性是时间粒度词时的可操作提示；非粒度词返回空串。

    让重试自愈：引导把 groupBy 改成选中的日期属性（如「订单日期」），
    按月/按年的粒度截断交给后续 SQL 生成阶段处理。
    """
    if token.strip() not in _TIME_BUCKET_TOKENS:
        return ""
    dateProps = _datePropertyNames(classes)
    if not dateProps:
        return "这是时间粒度概念而非属性，请改用选中的日期属性作为 groupBy"
    return (
        f"这是时间粒度概念而非属性，请把 groupBy 改为选中的日期属性"
        f"（如 {dateProps[0]}）；按月/按年/按季度的粒度截断由后续 SQL 生成阶段用"
        f" TO_CHAR/EXTRACT 处理"
    )


# 属性归属提示最多列出的拥有类数量（避免 schema 类多时提示过长挤占重试 token）。
# 运行期从 system_config.OWNER_HINT_MAX_CLASSES 现读（魔数治理 Phase 2 hard tier），
# 缺席/格式错返 _DEFAULT。
_OWNER_HINT_MAX_CLASSES_DEFAULT = 3


def _propertyOwnerHint(
    prop: str,
    propsByClass: dict[str, set[str]],
    *,
    maxClasses: int = _OWNER_HINT_MAX_CLASSES_DEFAULT,
) -> str:
    """属性不在选定类时的可操作提示：说明归属或如实告知不存在。

    生产回归（2026-09-16）：LLM 引用跨类属性（「供应商名称」属于供应商主表，
    但 selectedClasses 只选了收货明细类），原「不属于选定的任何类」无指引，
    重试两次仍犯同错 → 整轮失败。有归属类时列出（截断到 maxClasses），
    引导把类加入 selectedClasses 并经 JOIN 目录关联；schema 中完全不存在时如实
    说明（含别名/物理列口径），避免重试继续幻觉同一属性名。

    maxClasses 运行期从 system_config.OWNER_HINT_MAX_CLASSES 现读（魔数治理 Phase 2）。
    """
    owners = sorted(cn for cn, refs in propsByClass.items() if prop in refs)
    if not owners:
        return (
            "本体 schema 中不存在该属性（已比对全部类的业务名/别名/物理列），"
            "请改用选中类的已有属性或修正命名"
        )
    shown = ", ".join(owners[:maxClasses])
    more = f" 等 {len(owners)} 个类" if len(owners) > maxClasses else ""
    return (
        f"该属性属于类 {shown}{more}（均已在 schema 中），"
        f"请把对应类加入 selectedClasses 并按 JOIN 目录关联后再引用"
    )


def _extractFormulaProperties(formula: str) -> set[str]:
    """从公式中提取候选属性名，供 validatePlan 做存在性校验。

    先去掉单/双引号字符串字面量（避免把字面量中的词当属性），再剥离函数调用名
    （TO_DATE / NVL / COALESCE 等，实参中的列名仍保留继续校验），最后用标识符正则
    提取 token，过滤 SQL 关键字/函数名后返回。数值字面量与运算符不被标识符正则匹配，天然忽略。
    """
    # 去掉字符串字面量：替换为空格，保持位置但不产生 token
    text = re.sub(r"'[^']*'", " ", formula)
    text = re.sub(r'"[^"]*"', " ", text)
    # 剥离函数调用名，避免把 TO_DATE 等 SQL 函数名误判为属性
    text = _FORMULA_FUNC_CALL_RE.sub(" ", text)
    tokens = _SAFE_FORMULA_IDENT_RE.findall(text)
    return {t for t in tokens if t.upper() not in _FORMULA_SQL_KEYWORDS}


# 公式中候选属性名提取：与 _SAFE_IDENT_RE 一致支持 ASCII + CJK 标识符（属性名多为中文）。
_SAFE_FORMULA_IDENT_RE = re.compile(r"[A-Za-z_㐀-鿿][A-Za-z0-9_㐀-鿿]*")

# 函数调用名（标识符紧跟 `(`，如 TO_DATE(、NVL(、SUM(）。SQL 函数名不是属性，
# 剥离后仅保留实参中的真实列引用继续做存在性校验，避免把函数名误判为属性。
_FORMULA_FUNC_CALL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s*\(")

# SQL 关键字/函数名集合：公式引用属性校验时忽略这些 token，避免把 SUM/OVER 等误判为属性。
# 大写存放，匹配时对 token 大写化比较。含裸用（不带括号）的日期伪列/关键字。
#
# 不变式（2026-09-28 线上 400 回归）：本集合必须**覆盖** formula_parser._SQL_KEYWORDS
# ——两处都回答同一个问题「公式里出现的这个词是 SQL 词还是属性」。曾经 ASC/DESC 只在
# formula_parser 那侧存在，导致公式 OVER (ORDER BY SUM(x) DESC) 里的 DESC 被当成属性上报
# （「公式中的属性 DESC 不属于选定的任何类」），排名/累计占比类问题整轮 400。
# 守卫测试：test_query_plan_validation.py::TestSqlKeywordSetsStayInSync。
_FORMULA_SQL_KEYWORDS: frozenset[str] = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "BETWEEN", "LIKE", "IS", "NULL",
    "ANY", "SOME", "EXISTS",
    "AS", "BY", "GROUP", "ORDER", "HAVING", "JOIN", "ON", "LEFT", "RIGHT", "INNER", "OUTER",
    "CROSS", "FULL", "UNION", "ALL", "DISTINCT", "LIMIT", "FETCH", "ROWNUM", "WITH",
    "ASC", "DESC",
    "CASE", "WHEN", "THEN", "ELSE", "END", "CAST", "COALESCE", "NULLIF", "IF", "CONVERT",
    "ABS", "ROUND", "FLOOR", "CEIL", "CEILING", "MOD", "CONCAT", "SUBSTR", "SUBSTRING",
    "TRUNC", "TRUNCATE", "LENGTH", "LEAST", "GREATEST",
    "DATE", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND",
    "TRUE", "FALSE",
    "SUM", "AVG", "COUNT", "MAX", "MIN", "STDDEV", "VARIANCE",
    "OVER", "PARTITION", "ROWS", "RANGE", "UNBOUNDED", "PRECEDING", "FOLLOWING",
    "CURRENT", "ROW", "RESPECT", "IGNORE", "NULLS", "FIRST", "LAST",
    "SYSDATE", "SYSTIMESTAMP", "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "INTERVAL",
})


# 派生指标别名关键词：命中时 Aggregation.formula 必填（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()）。
# 大小写不敏感（英文关键词 alias lower() 后比对）。中英文都覆盖，
# 适用于 PORDERQ/采购/销售 等业务域常见命名（2026-08-17 真实回归）：
# 用户问"top10 物料的占比"时 LLM 倾向 alias="占比" 无 formula → SQL 不算百分比
# → validatePlan 重试耗尽 → 步骤被静默标"无法回答"。
_DERIVED_METRIC_ALIAS_KEYWORDS: tuple[str, ...] = (
    "占比",
    "比率",
    "比例",
    "百分比",
    "ratio",
    "percent",
    "share",
    "pct",
)


def _aliasRequiresFormula(alias: str | None) -> bool:
    """聚合 alias 暗示派生指标时必须 formula（占比/比率/百分比 等）。

    大小写不敏感：英文关键词 lower() 后比对。alias 为 None/空时返回 False
    （让普通 SUM/COUNT 聚合无 alias 命名时不受打扰）。
    """
    if not alias:
        return False
    lowered = alias.lower()
    return any(kw in lowered for kw in _DERIVED_METRIC_ALIAS_KEYWORDS)
