"""查询计划（QueryPlan）领域结构 — ReAct NL2SQL 推理阶段产物。

不可变 frozen dataclass：QueryPlan 描述一次查询的选表/选列/聚合/JOIN/排序，
从 LLM 回复解析后经 validatePlan 校验引用，再序列化为 JSONB 存储或前端展示。

设计约束：
- 所有 dataclass frozen=True，杜绝原地修改。
- from_dict 容忍缺失字段、非列表值、未知键与损坏的嵌套对象，
  保证读取历史状态（DB JSONB）或解析 LLM 回复时绝不抛错。
- from_dictWithReport 与之语义相同，额外返回被丢弃片段的分类报告（M3）：
  domain 层仍无日志 / 无 IO，由调用方决定怎么记。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.domain.plan_drop import (
    DROP_FIELD_NOT_A_LIST,
    DROP_INTERPRETATION_NOT_STR,
    DROP_ITEM_NOT_STR,
    DROP_NESTED_INVALID,
    DROP_NESTED_NOT_A_DICT,
    DROP_NOT_A_DICT,
    DROP_POSITIVE_INT_INVALID,
    DROP_TARGET_NOT_STR,
    PAYLOAD_FIELD,
    PlanDrop,
    PlanDropKey,
)

# 模型无法从本体匹配到任何表时的 target 约定值（见 nl2sql _buildPlanSystemPrompt 规则 2）
UNANSWERABLE_TARGET = "无法回答"

# 复合形式 '业务名 (alias)' 拆分（与 nl2sql_service._splitCompoundRef 同口径，
# 此处复刻以避免 query_plan 反向依赖 nl2sql_service 形成循环导入）。
_COMPOUND_REF_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")


def _stripCompoundRef(prop: str) -> str:
    """拆 'name (alias)' → name（取业务名）；无括号 / 中文括号 / 嵌套括号原样保留。

    用于 planToText 渲染：剥离复合形式以防 state 回灌 prompt 时诱导 LLM 持续使用
    复合写法（2026-09-18 真实回归）。无 ontology 上下文，无法判别哪个 token 合法，
    一律取拆出的业务名作为人类可读 token。
    """
    if not isinstance(prop, str):
        return prop
    m = _COMPOUND_REF_RE.match(prop.strip())
    if not m:
        return prop.strip()
    return m.group(1).strip()


def _normalizeJoinColumnToken(token: str) -> tuple[str, ...]:
    """把 "A = B" 等式 token 拆成 (A, B)；普通列名原样返回单元素 tuple。

    LLM 偶发把 join.columns 写成等式字符串（如 "SUPPLIER_CODE = PARTNER_CODE"），
    而契约是列名数组（2026-09-18 真实回归：等式整体不匹配任何属性 → 校验必挂）。
    在 from_dict 解析出口一次性自愈：仅当恰好一个 "=" 且两侧 strip 后均非空才拆分，
    其余（缺一侧、多个等号）保留原 token 交由 validatePlan 拒绝并提示正确写法。
    """
    if "=" not in token:
        return (token,)
    parts = [p.strip() for p in token.split("=")]
    if len(parts) == 2 and parts[0] and parts[1]:
        return (parts[0], parts[1])
    return (token,)


def _coercePositiveInt(value: Any) -> int | None:
    """把值归一为正整数或 None（与 nl2sql_service._coerceRowLimit 同口径，此处避免导入环）。

    bool/非 int/非纯数字串/<=0 一律返回 None，保证 perGroupLimit 字段类型洁净。
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@dataclass(frozen=True)
class Aggregation:
    """聚合表达式（不可变）。

    function/property 描述基础聚合（如 SUM(数量)）。
    formula 为派生指标表达式（如占比：`SUM(数量) / SUM(SUM(数量)) OVER ()`），
    存在时由渲染与 SQL 生成阶段优先使用；function/property 仍须填写，
    property 取公式主要引用的属性，供 validatePlan 做存在性校验。
    """

    function: str
    property: str
    alias: str | None = None
    formula: str | None = None


@dataclass(frozen=True)
class JoinSpec:
    """JOIN 关系描述（不可变）。"""

    sourceClass: str
    targetClass: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SortSpec:
    """排序规则（不可变）。"""

    property: str
    direction: str = "asc"  # asc | desc


class _DropCollector:
    """按 (field, reason, rawType) 聚合丢弃计数。

    有意的可变累加器：生命周期仅限单次 from_dictWithReport 调用，不跨调用共享，
    聚合是为了让日志单行且不随垃圾条目数量膨胀（LLM 返回 50 条垃圾 → 一行 x50）。
    """

    def __init__(self) -> None:
        self._counts: dict[PlanDropKey, int] = {}

    def record(self, field: str, reason: str, value: Any) -> _DropCollector:
        key = (field, reason, type(value).__name__)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self

    def freeze(self) -> tuple[PlanDrop, ...]:
        return tuple(
            PlanDrop(field=field, reason=reason, rawType=rawType, count=count)
            for (field, reason, rawType), count in self._counts.items()
        )


@dataclass(frozen=True)
class QueryPlan:
    """NL2SQL 查询计划（ReAct 推理阶段的产物，不可变）。

    从 LLM 回复解析，经 validatePlan 校验引用是否在本体 schema 中，
    校验通过后才用于 SQL 生成。所有集合字段默认空 tuple。
    """

    target: str
    selectedClasses: tuple[str, ...] = ()
    selectedProperties: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    aggregations: tuple[Aggregation, ...] = ()
    groupBy: tuple[str, ...] = ()
    joins: tuple[JoinSpec, ...] = ()
    sortBy: tuple[SortSpec, ...] = ()
    rowLimit: int | None = None
    # 「分别/各/每个 X 的 top N」逐组取前 N（2026-09-09）：
    # partitionBy 为分区维（如 供应商代码），perGroupLimit 为每组保留行数（N）。
    # 与全局 rowLimit 互斥（validatePlan 强制）；SQL 阶段据此生成
    # ROW_NUMBER() OVER (PARTITION BY ...) + rn<=N，而不是把 N×组数折成全局 top。
    partitionBy: tuple[str, ...] = ()
    perGroupLimit: int | None = None
    interpretation: str | None = None

    @property
    def isUnanswerable(self) -> bool:
        """模型判定问题超出本体可回答范围（target=无法回答）。

        此时没有可查询的语义目标：流水线须短路为友好回答，严禁再生成 SQL
        （空计划会让模型自由编造表名 → 执行报错被包装成"服务内部错误"）。
        """
        return self.target == UNANSWERABLE_TARGET

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（供 JSONB 存储 / 前端展示）。

        嵌套元素可能是 frozen dataclass 或 dict，统一转 dict。
        """

        def _asDict(item: Any) -> dict[str, Any]:
            return item.__dict__ if not isinstance(item, dict) else item

        return {
            "target": self.target,
            "selectedClasses": list(self.selectedClasses),
            "selectedProperties": list(self.selectedProperties),
            "conditions": list(self.conditions),
            "aggregations": [_asDict(a) for a in self.aggregations],
            "groupBy": list(self.groupBy),
            "joins": [_asDict(j) for j in self.joins],
            "sortBy": [_asDict(s) for s in self.sortBy],
            "rowLimit": self.rowLimit,
            "partitionBy": list(self.partitionBy),
            "perGroupLimit": self.perGroupLimit,
            "interpretation": self.interpretation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueryPlan:
        """从 dict 反序列化，容忍一切损坏输入，绝不抛错（丢弃细节见 from_dictWithReport）。"""
        plan, _ = cls.from_dictWithReport(data)
        return plan

    @classmethod
    def from_dictWithReport(
        cls, data: dict[str, Any]
    ) -> tuple[QueryPlan, tuple[PlanDrop, ...]]:
        """同 from_dict，但额外返回**被丢弃片段**的分类报告（M3）。

        语义与 from_dict 完全一致（同一实现，from_dict 只是丢弃报告），报告供调用方
        单点写日志：静默丢弃掩盖根因，而抛错会破坏「历史 JSONB 可读」的既有契约，
        故取「照旧容错 + 分类上报」而非二选一。

        - 非 dict 输入 → 空计划 + 一条整载荷丢弃
        - 非列表值 → 空集合 + 丢弃记录；列表中的非字符串 → 过滤并按类型聚合计数
        - 嵌套对象：仅取已知字段，非 dict / 缺必填字段 → 跳过该条目 + 丢弃记录
        """
        drops = _DropCollector()
        if not isinstance(data, dict):
            return cls(target=""), drops.record(
                PAYLOAD_FIELD, DROP_NOT_A_DICT, data
            ).freeze()

        def _list(key: str) -> list[Any]:
            raw = data.get(key)
            if raw is None:
                return []
            if not isinstance(raw, list):
                drops.record(key, DROP_FIELD_NOT_A_LIST, raw)
                return []
            return raw

        def _strings(key: str) -> tuple[str, ...]:
            out: list[str] = []
            for item in _list(key):
                if isinstance(item, str):
                    out.append(item)
                else:
                    drops.record(key, DROP_ITEM_NOT_STR, item)
            return tuple(out)

        def _nested(
            key: str,
            factory: type,
            tupleFields: tuple[str, ...] = (),
        ) -> tuple[Any, ...]:
            fieldNames = set(factory.__dataclass_fields__)
            out: list[Any] = []
            for entry in _list(key):
                if not isinstance(entry, dict):
                    drops.record(key, DROP_NESTED_NOT_A_DICT, entry)
                    continue
                filtered = {k: v for k, v in entry.items() if k in fieldNames}
                # 元组类型字段：JSON 里是 list，统一转 tuple 以保持类型契约
                for tf in tupleFields:
                    if tf in filtered and isinstance(filtered[tf], list):
                        filtered[tf] = tuple(filtered[tf])
                # join.columns 契约是列名数组，但 LLM 偶发写成 "A = B" 等式 ——
                # 解析出口一次性拆分自愈（详见 _normalizeJoinColumnToken；
                # 这是归一不是丢弃，故不上报）
                if factory is JoinSpec and "columns" in filtered:
                    filtered["columns"] = tuple(
                        part
                        for token in filtered["columns"]
                        if isinstance(token, str)
                        for part in _normalizeJoinColumnToken(token)
                    )
                try:
                    out.append(factory(**filtered))
                except TypeError:
                    drops.record(key, DROP_NESTED_INVALID, None)
                    continue  # 缺必填字段等：跳过损坏条目
            return tuple(out)

        target = data.get("target", "")
        if not isinstance(target, str):
            drops.record("target", DROP_TARGET_NOT_STR, target)
        interpretation = data.get("interpretation")
        if interpretation is not None and not isinstance(interpretation, str):
            drops.record("interpretation", DROP_INTERPRETATION_NOT_STR, interpretation)
        perGroupLimit = _coercePositiveInt(data.get("perGroupLimit"))
        if perGroupLimit is None and data.get("perGroupLimit") is not None:
            drops.record("perGroupLimit", DROP_POSITIVE_INT_INVALID, data["perGroupLimit"])

        plan = cls(
            target=target if isinstance(target, str) else "",
            selectedClasses=_strings("selectedClasses"),
            selectedProperties=_strings("selectedProperties"),
            conditions=_strings("conditions"),
            aggregations=_nested("aggregations", Aggregation),
            groupBy=_strings("groupBy"),
            joins=_nested("joins", JoinSpec, ("columns",)),
            sortBy=_nested("sortBy", SortSpec),
            # rowLimit 不做类型校验是既有设计：下游 _coerceRowLimit 会归一化
            # （nl2sql_service.py 的 _coerceRowLimit 明示依赖此契约），故这里既不过滤也不上报。
            rowLimit=data.get("rowLimit"),
            partitionBy=_strings("partitionBy"),
            perGroupLimit=perGroupLimit,
            interpretation=interpretation if isinstance(interpretation, str) else None,
        )
        return plan, drops.freeze()


@dataclass(frozen=True)
class PlanResult:
    """查询计划生成结果（ReAct 推理阶段，不可变）。"""

    plan: QueryPlan
    promptTokens: int
    completionTokens: int
    # 4-1（feat-token-cache，2026-09-28）：DeepSeek prompt cache 命中 token 数。
    # None = 字段缺失/不支持（OpenAI/MOONSHOT/AZURE）。由 plan 阶段 LLM 响应
    # 累计；与 SQL 阶段 cachedTokens 合并后用于 _costFor 按差额计费。
    cachedTokens: int | None = None


def planToText(plan: QueryPlan) -> str:
    """将查询计划渲染为 prompt 中的人类可读段落。

    嵌套元素可能是 frozen dataclass 或 dict（防御性兼容），统一取值。
    prop 字段（selectedProperties/groupBy/partitionBy/aggregations/sortBy/joins）
    经 _stripCompoundRef 拆分，去掉 'name (alias)' 复合形式的括号与别名部分，
    只保留业务名，避免 state 回灌 prompt 时诱导 LLM 持续使用复合写法。
    """

    def _aggText(a: Any) -> str:
        d = a.__dict__ if not isinstance(a, dict) else a
        formula = d.get("formula")
        if formula:
            # 派生指标：直接渲染公式（如 SUM(数量) / SUM(SUM(数量)) OVER ()）
            alias = d.get("alias")
            return f"{formula} AS {alias}" if alias else str(formula)
        text = f"{d.get('function', '?')}({_stripCompoundRef(d.get('property', '?'))})"
        alias = d.get("alias")
        return f"{text} AS {alias}" if alias else text

    def _sortText(s: Any) -> str:
        d = s.__dict__ if not isinstance(s, dict) else s
        return f"{_stripCompoundRef(d.get('property', '?'))} {d.get('direction', 'asc')}"

    lines = [f"- 目标：{plan.target or '（未描述）'}"]
    if plan.interpretation:
        lines.append(f"- 理解：{plan.interpretation}")
    if plan.selectedClasses:
        lines.append(f"- 涉及表：{', '.join(plan.selectedClasses)}")
    if plan.selectedProperties:
        lines.append(f"- 涉及列：{', '.join(_stripCompoundRef(p) for p in plan.selectedProperties)}")
    if plan.conditions:
        lines.append(f"- 过滤条件：{'; '.join(plan.conditions)}")
    if plan.aggregations:
        lines.append("- 聚合：" + "; ".join(_aggText(a) for a in plan.aggregations))
    if plan.groupBy:
        lines.append(f"- 分组：{', '.join(_stripCompoundRef(p) for p in plan.groupBy)}")
    if plan.sortBy:
        lines.append("- 排序：" + "; ".join(_sortText(s) for s in plan.sortBy))
    if plan.rowLimit is not None:
        lines.append(f"- 行数限制：{plan.rowLimit}")
    if plan.partitionBy and plan.perGroupLimit is not None:
        # 逐组 Top-N：分区维 + 组内排序（复用 sortBy 文本）+ 每组行数。
        # SQL 阶段据此生成 ROW_NUMBER() OVER (PARTITION BY ...)，不是全局截断。
        order = "、".join(_sortText(s) for s in plan.sortBy) if plan.sortBy else ""
        bullet = f"- 每组 Top-N：按 {'、'.join(_stripCompoundRef(p) for p in plan.partitionBy)} 分区"
        if order:
            bullet += f"，组内按 {order} 排序"
        bullet += f"，每组取前 {plan.perGroupLimit} 行"
        lines.append(bullet)
    return "\n".join(lines)
