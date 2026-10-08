"""Chat 模块的字面常量 SSOT（避免散落字面量与跨文件重复）。

本模块不承载运行时行为；仅为下列字符串标识符提供唯一真相：

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