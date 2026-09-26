"""查询计划解析丢弃报告（M3）。

`QueryPlan.from_dict` 刻意「绝不抛错」：损坏字段静默变成空集合 / None，好处是读取
历史 JSONB 或解析 LLM 回复时不会炸，代价是**丢掉了什么无从诊断**（11 处丢弃点全部
静默）。本模块提供丢弃的分类记录，由调用方单点写日志；domain 层保持无日志、无 IO。

只记录**被丢弃**的数据。两类既有刻意行为不计入：
- 缺键 / None：LLM 省略可选字段是正常形态，不是损坏；
- `join.columns` 的 `'A = B'` 拆分：既有自愈归一（`_normalizeJoinColumnToken`），
  是修复不是丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass

# 丢弃原因（与日志 reason= 口径一致；新增原因须同步 test_plan_drop.py 的逐点断言）
DROP_NOT_A_DICT = "PLAN_NOT_A_DICT"  # 整个载荷不是 dict
DROP_TARGET_NOT_STR = "PLAN_TARGET_NOT_STR"  # target 非字符串
DROP_FIELD_NOT_A_LIST = "PLAN_FIELD_NOT_A_LIST"  # 集合字段的值不是列表
DROP_ITEM_NOT_STR = "PLAN_ITEM_NOT_STR"  # 集合字段里的非字符串条目
DROP_NESTED_NOT_A_DICT = "PLAN_NESTED_NOT_A_DICT"  # 嵌套条目不是对象
DROP_NESTED_INVALID = "PLAN_NESTED_INVALID"  # 嵌套条目缺必填字段 / 类型不符
DROP_POSITIVE_INT_INVALID = "PLAN_POSITIVE_INT_INVALID"  # 正整数语义字段非法
DROP_INTERPRETATION_NOT_STR = "PLAN_INTERPRETATION_NOT_STR"  # interpretation 非字符串

# 整载荷丢弃时的占位字段名
PAYLOAD_FIELD = "<payload>"

# 单条丢弃记录
PlanDropKey = tuple[str, str, str]  # (field, reason, rawType)


@dataclass(frozen=True)
class PlanDrop:
    """一条被丢弃的计划片段（不可变）。

    只存**类型名**不存原值：LLM 输出可能很长，且原始串可能带用户数据，进日志
    只保留可诊断的结构信息（哪个字段、什么原因、什么类型、几条）。
    """

    field: str
    reason: str
    rawType: str
    count: int = 1


def formatPlanDrops(drops: tuple[PlanDrop, ...]) -> str:
    """把丢弃记录渲染成**单行**文本（日志用，便于 grep 与限长）。"""
    if not drops:
        return ""
    parts = []
    for drop in drops:
        suffix = f"x{drop.count}" if drop.count > 1 else ""
        parts.append(f"{drop.field}:{drop.reason}({drop.rawType}){suffix}")
    return ", ".join(parts)
