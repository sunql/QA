"""Chat 模块的字面常量 SSOT（避免散落字面量与跨文件重复）。

本模块只承载常量与一个无副作用的纯映射函数（``metaIntentOf``）；不触库、不调 LLM。

- ``ROUTING_LAYER_*``：路由层标签（落 routing_metrics SQL 维度，前端展示）
- ``USAGE_PURPOSE_*``：token usage 用途标识（落 token_usage.purpose，SQL 聚合维度）
- ``AUDIT_*``：审计实体/动作/状态（落 audit_log.entity_type/action）

三族均与 DB 列及 SQL 聚合锁定，**禁止**改值不改下游：DB 列已存数据，重命名会让历史
行 group by 出空集。

新增规则：往这三族里加项前必须确认 DB 列长度（routing_layer VARCHAR / purpose VARCHAR
/ entity_type VARCHAR）。现有长度基于 audit_history_api 迁移定义；超长会被 DB 截断。
"""

from __future__ import annotations

# =============================================================================
# 路由层（落 routing_metrics.routing_layer；routing_metrics_service 按此 group by）
# =============================================================================

ROUTING_LAYER_L1 = "L1"  # 单轮 SQL 直答（简单查询，无追问、无拆步）
ROUTING_LAYER_L2 = "L2"  # 多轮对话 / 上下文召回 / 拆步计划（绝大多数生产流量）
ROUTING_LAYER_L3 = "L3"  # 多步持久化执行（chat_multistep 路径，独立 run 表）
ROUTING_LAYER_L4 = "L4"  # L4 Agent Loop（研究类问题，触发 _L4_EXPLORATORY_KEYWORDS）
# KPI 多命中歧义：不执行，返回澄清候选（设计 §5.2）。长度 3，safe for VARCHAR(10)。
ROUTING_LAYER_KPI_CLARIFY = "L1C"


# =============================================================================
# Meta frame intent values (SSE wire format; chat_stream._streamQuery L1/L4 preamble)
# =============================================================================
# Wire format only — NOT IntentType enum values (those are lowercase "query" /
# "agent_run" / etc.). Frontend chatApi / researchStore consume these exact strings.
# Changing requires coordinated PR across both repos. Locked by
# tests/unit/test_chat_stream_meta_constants.py + tests/integration/test_stream_meta_single_frame.py
# (the latter's reverse guards assert single["intent"] == "KPI").
META_INTENT_L1 = "KPI"  # L1 短路命中：KPI 目录直答，0 LLM cost
META_INTENT_L4 = "AGENT_RUN"  # L4 短路命中：Agent Loop 触发
META_INTENT_CLARIFY = "clarify"  # L1C 歧义澄清：复用 IntentType.CLARIFY 的值（前端已知意图）


def metaIntentOf(routingLayer: str) -> str:
    """路由层 → meta 帧 ``intent`` 的唯一映射（SSE wire 契约）。

    新增路由层**必须**在这里显式登记：未登记的值会落到 ``META_INTENT_L4``，
    前端会把它当 Agent 运行来渲染（图表卡/轨迹位全空）。这条兜底是刻意的
    fail-safe，不是「默认值随便挑」。
    """
    if routingLayer == ROUTING_LAYER_KPI_CLARIFY:
        return META_INTENT_CLARIFY
    if routingLayer == ROUTING_LAYER_L1:
        return META_INTENT_L1
    return META_INTENT_L4


# =============================================================================
# Token usage 用途（落 token_usage.purpose；按此维度做成本归因与告警）
# =============================================================================

USAGE_PURPOSE_CLARIFY = "clarify"
USAGE_PURPOSE_NL2SQL = "nl2sql"
USAGE_PURPOSE_ANSWER = "answer"
USAGE_PURPOSE_CHART = "chart"
USAGE_PURPOSE_STEP_PLAN = "step_plan"
USAGE_PURPOSE_FOLLOW_UP_REWRITE = "follow_up_rewrite"
USAGE_PURPOSE_MULTISTEP_GLOBAL_FILTER = "multistep_global_filter"
USAGE_PURPOSE_SUPPLIER_RISK = "supplier_risk"
USAGE_PURPOSE_AGENT_RUN = "agent_run"
USAGE_PURPOSE_L4_AGENT_LOOP = "l4_agent_loop"
# 降级路径下的"父 purpose"前缀（chat_service._callWithFallback 用 f"fallback_{purpose}"）
USAGE_PURPOSE_FALLBACK_PREFIX = "fallback_"
USAGE_PURPOSE_ANSWER_STREAM_FAILED = "answer_stream_failed"
USAGE_PURPOSE_FALLBACK_ANSWER = "fallback_answer"


# =============================================================================
# 审计字段（落 audit_log）
# =============================================================================

# 实体类型
AUDIT_ENTITY_AGENT_RUN_LOG = "agent_run_log"

# 动作
AUDIT_ACTION_CREATE = "CREATE"

# 运行状态
AUDIT_RUN_STATUS_SUCCESS = "SUCCESS"
AUDIT_RUN_STATUS_FAILED = "FAILED"

# 错误分类（chat_domain._emitAgentRunAuditAfter 5 处）
AGENT_RUN_ERROR_AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
AGENT_RUN_ERROR_PERMISSION_DENIED = "PERMISSION_DENIED"
AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE = "AGENT_NOT_RUNNABLE"
AGENT_RUN_ERROR_BAD_INPUT = "BAD_INPUT"
AGENT_RUN_ERROR_UNEXPECTED = "UNEXPECTED"