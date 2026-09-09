"""查询计划（QueryPlan）领域结构 — ReAct NL2SQL 推理阶段产物。

不可变 frozen dataclass：QueryPlan 描述一次查询的选表/选列/聚合/JOIN/排序，
从 LLM 回复解析后经 validatePlan 校验引用，再序列化为 JSONB 存储或前端展示。

设计约束：
- 所有 dataclass frozen=True，杜绝原地修改。
- from_dict 容忍缺失字段、非列表值、未知键与损坏的嵌套对象，
  保证读取历史状态（DB JSONB）或解析 LLM 回复时绝不抛错。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 模型无法从本体匹配到任何表时的 target 约定值（见 nl2sql _buildPlanSystemPrompt 规则 2）
UNANSWERABLE_TARGET = "无法回答"


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
    def from_dict(cls, data: dict[str, Any]) -> "QueryPlan":
        """从 dict 反序列化，容忍一切损坏输入，绝不抛错。

        - 非 dict 输入 → 空计划
        - 非列表值 → 空集合；列表中的非字符串 → 过滤
        - 嵌套对象：仅取已知字段，缺必填字段或类型不符 → 跳过该条目
        """
        if not isinstance(data, dict):
            return cls(target="")

        def _list(raw: Any) -> list[Any]:
            return raw if isinstance(raw, list) else []

        def _strings(raw: Any) -> tuple[str, ...]:
            return tuple(s for s in _list(raw) if isinstance(s, str))

        def _nested(
            raw: Any,
            fieldNames: set[str],
            factory: type,
            tupleFields: tuple[str, ...] = (),
        ) -> tuple[Any, ...]:
            out: list[Any] = []
            for entry in _list(raw):
                if not isinstance(entry, dict):
                    continue
                filtered = {k: v for k, v in entry.items() if k in fieldNames}
                # 元组类型字段：JSON 里是 list，统一转 tuple 以保持类型契约
                for tf in tupleFields:
                    if tf in filtered and isinstance(filtered[tf], list):
                        filtered[tf] = tuple(filtered[tf])
                try:
                    out.append(factory(**filtered))
                except TypeError:
                    continue  # 缺必填字段等：跳过损坏条目
            return tuple(out)

        target = data.get("target", "")
        interpretation = data.get("interpretation")
        return cls(
            target=target if isinstance(target, str) else "",
            selectedClasses=_strings(data.get("selectedClasses")),
            selectedProperties=_strings(data.get("selectedProperties")),
            conditions=_strings(data.get("conditions")),
            aggregations=_nested(data.get("aggregations"), set(Aggregation.__dataclass_fields__), Aggregation),
            groupBy=_strings(data.get("groupBy")),
            joins=_nested(data.get("joins"), set(JoinSpec.__dataclass_fields__), JoinSpec, ("columns",)),
            sortBy=_nested(data.get("sortBy"), set(SortSpec.__dataclass_fields__), SortSpec),
            rowLimit=data.get("rowLimit"),
            partitionBy=_strings(data.get("partitionBy")),
            perGroupLimit=_coercePositiveInt(data.get("perGroupLimit")),
            interpretation=interpretation if isinstance(interpretation, str) else None,
        )


@dataclass(frozen=True)
class PlanResult:
    """查询计划生成结果（ReAct 推理阶段，不可变）。"""

    plan: QueryPlan
    promptTokens: int
    completionTokens: int


def planToText(plan: QueryPlan) -> str:
    """将查询计划渲染为 prompt 中的人类可读段落。

    嵌套元素可能是 frozen dataclass 或 dict（防御性兼容），统一取值。
    """

    def _aggText(a: Any) -> str:
        d = a.__dict__ if not isinstance(a, dict) else a
        formula = d.get("formula")
        if formula:
            # 派生指标：直接渲染公式（如 SUM(数量) / SUM(SUM(数量)) OVER ()）
            alias = d.get("alias")
            return f"{formula} AS {alias}" if alias else str(formula)
        text = f"{d.get('function', '?')}({d.get('property', '?')})"
        alias = d.get("alias")
        return f"{text} AS {alias}" if alias else text

    def _sortText(s: Any) -> str:
        d = s.__dict__ if not isinstance(s, dict) else s
        return f"{d.get('property', '?')} {d.get('direction', 'asc')}"

    lines = [f"- 目标：{plan.target or '（未描述）'}"]
    if plan.interpretation:
        lines.append(f"- 理解：{plan.interpretation}")
    if plan.selectedClasses:
        lines.append(f"- 涉及表：{', '.join(plan.selectedClasses)}")
    if plan.selectedProperties:
        lines.append(f"- 涉及列：{', '.join(plan.selectedProperties)}")
    if plan.conditions:
        lines.append(f"- 过滤条件：{'; '.join(plan.conditions)}")
    if plan.aggregations:
        lines.append("- 聚合：" + "; ".join(_aggText(a) for a in plan.aggregations))
    if plan.groupBy:
        lines.append(f"- 分组：{', '.join(plan.groupBy)}")
    if plan.sortBy:
        lines.append("- 排序：" + "; ".join(_sortText(s) for s in plan.sortBy))
    if plan.rowLimit is not None:
        lines.append(f"- 行数限制：{plan.rowLimit}")
    if plan.partitionBy and plan.perGroupLimit is not None:
        # 逐组 Top-N：分区维 + 组内排序（复用 sortBy 文本）+ 每组行数。
        # SQL 阶段据此生成 ROW_NUMBER() OVER (PARTITION BY ...)，不是全局截断。
        order = "、".join(_sortText(s) for s in plan.sortBy) if plan.sortBy else ""
        bullet = f"- 每组 Top-N：按 {'、'.join(plan.partitionBy)} 分区"
        if order:
            bullet += f"，组内按 {order} 排序"
        bullet += f"，每组取前 {plan.perGroupLimit} 行"
        lines.append(bullet)
    return "\n".join(lines)
