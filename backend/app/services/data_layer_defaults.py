"""数据层默认规则（feat-data-domain-layer 方案 2）。

前缀识别的**代码侧 fallback SSOT**：DB（data_layer_rule，迁移 0134 seed 与此
逐字对齐）未 warmUp 或查询失败时使用。规则语义：

- pattern 非空 = 表名前缀规则，**长前缀必须先匹配**（ODS_DIM_ 先于 ODS_，
  用 priority 保证，小者先）；
- pattern 为空 = 手工声明层（不可由前缀探测，如 FEATURE 特征库层——存量
  agent_definition / agent_access_policy 在用）。

与 chat_recall._getClassLayer 历史：ADS_/DWS_/DWD_/DIM_/ODS_DIM_/ODS_ 六前缀
按原优先级迁入；本模块是收敛后的单一 fallback，chat_recall 与 layerRuleRegistry
共用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LayerRule:
    """单条层规则；pattern=None 表示手工声明层（前缀匹配跳过）。"""

    layer: str
    pattern: str | None
    priority: int


DEFAULT_LAYER_RULES: tuple[LayerRule, ...] = (
    LayerRule(layer="ODS_DICT", pattern="ODS_DIM_", priority=10),
    LayerRule(layer="ADS", pattern="ADS_", priority=20),
    LayerRule(layer="DWS", pattern="DWS_", priority=30),
    LayerRule(layer="DWD", pattern="DWD_", priority=40),
    LayerRule(layer="DIM", pattern="DIM_", priority=50),
    LayerRule(layer="ODS_BUSINESS", pattern="ODS_", priority=60),
    LayerRule(layer="FEATURE", pattern=None, priority=90),
)


def matchTableNameLayer(
    table_name: str, rules: tuple[LayerRule, ...] = DEFAULT_LAYER_RULES
) -> str | None:
    """按 priority 升序匹配表名前缀；未命中返回 None（调用方计 UNKNOWN）。

    纯函数，不做 strip/upper —— 调用方决定归一化口径（chat_recall 用大写源表，
    探测 API 用 information_schema 原名 + upper）。
    """
    for rule in sorted(rules, key=lambda r: r.priority):
        if rule.pattern is None:
            continue
        if table_name.startswith(rule.pattern):
            return rule.layer
    return None
