"""Pydantic 请求/响应 Schema（DTO）。

约定：内部字段使用 snake_case（与 ORM 对齐），通过 alias_generator 输出 camelCase JSON，
匹配设计稿 API 契约（sessionId、modelName、tokenUsage 等）。
遵循不可变原则：所有 DTO 默认 frozen 风格，调用方不应修改响应 DTO。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from app.domain.agent_vocabulary import (
    AGENT_DATA_DOMAINS,
    AGENT_DATA_LAYERS,
    normalizeAgentDomain,
)
from app.domain.enums import (
    AgentPermission,
    AgentToolHandlerKind,
    AgentResponseLatency,
    AgentStatus,
    AgentTriggerType,
    BusinessObjectCode,
    ChartType,
    DataSourceType,
    DerivationType,
    DocumentSecurityLevel,
    DocumentStatus,
    DocumentType,
    DocEntityRelationType,
    FeatureRefreshFrequency,
    FeatureStatus,
    KpiStatus,
    LineageLayer,
    MatchRule,
    ObjectType,
    RefreshFrequency,
    RiskLevel,
    RuleType,
    RuleOperator,
    ScoreType,
    Severity,
    SourceSystem,
)
from app.domain.exceptions import ConfigError
from typing import Annotated, Literal
from pydantic import BeforeValidator

from app.services.business_object_registry import businessObjectRegistry


def _validateBusinessObjectCode(code: str) -> str:
    """Runtime validation against business_object table via registry."""
    if not businessObjectRegistry.isValid(code):
        raise ValueError(f"Invalid business object code: {code!r}")
    return code


BusinessObjectCodeType = Annotated[str, BeforeValidator(_validateBusinessObjectCode)]
"""Pydantic annotated type for dynamic BusinessObjectCode validation.

Replaces the hardcoded Literal. Validation is runtime against the business_object
table — adding new business objects requires no code change, only an INSERT.
"""

# format-only validator (no registry check) — used when creating NEW codes
def _validateBusinessObjectCodeFormat(code: str) -> str:
    """Format validation for new business object codes (registry check is done by DB constraint)."""
    if not code or len(code) > 20 or not code.isupper() or not code.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid business object code format: {code!r}. "
            f"Must be uppercase alphanumeric (underscore allowed), max 20 chars."
        )
    return code


BusinessObjectCodeNew = Annotated[str, BeforeValidator(_validateBusinessObjectCodeFormat)]
"""Format-only validation for new business object codes in create payloads.

Unlike BusinessObjectCodeType, this does NOT check the registry — a code being
CREATED is by definition not yet in the registry. DB CHECK constraint (uppercase
VARCHAR(20)) enforces the rest.
"""

from app.domain.error_messages import (
    MSG_SCHEMA_CHAT_AFFINITY,
    MSG_SCHEMA_CHAT_CHART_TYPE_EXPLICIT,
    MSG_SCHEMA_CHAT_CHART_TYPE_EXTRACTED,
    MSG_SCHEMA_CHAT_DATASOURCE_ID,
    MSG_SCHEMA_CHAT_DIMENSION,
    MSG_SCHEMA_CHAT_HISTORY,
    MSG_SCHEMA_CHAT_HISTORY_FIRST_TIME,
    MSG_SCHEMA_CHAT_HISTORY_LAST_ANSWER_PREVIEW,
    MSG_SCHEMA_CHAT_HISTORY_LAST_QUESTION,
    MSG_SCHEMA_CHAT_HISTORY_LAST_TIME,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_CONTENT,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_COUNT,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_CREATED_TIME,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_ID,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_QUESTION,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_ROLE,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGE_SQL,
    MSG_SCHEMA_CHAT_HISTORY_MESSAGES,
    MSG_SCHEMA_CHAT_HISTORY_SESSION_ID,
    MSG_SCHEMA_CHAT_LOCKED_MODEL,
    MSG_SCHEMA_CHAT_METRIC,
    MSG_SCHEMA_CHAT_MODEL_ID,
    MSG_SCHEMA_CHAT_QUERY_ENTITIES,
    MSG_SCHEMA_CHAT_QUERY_PLAN,
    MSG_SCHEMA_CHAT_QUESTION,
    MSG_SCHEMA_CHAT_REMAINING_TURNS,
    MSG_SCHEMA_CHAT_SERVED_MODEL,
    MSG_SCHEMA_DATASOURCE_DATABASE,
    MSG_SCHEMA_DATASOURCE_NAME,
    MSG_SCHEMA_DATASOURCE_ORACLE_VERSION,
    MSG_SCHEMA_DQ_RULE_NAME,
    MSG_SCHEMA_DQ_RULE_CODE,
    MSG_SCHEMA_DQ_DATASOURCE_ID,
    MSG_SCHEMA_DQ_TARGET_TABLE,
    MSG_SCHEMA_DQ_TARGET_COLUMN,
    MSG_SCHEMA_DQ_RULE_TYPE,
    MSG_SCHEMA_DQ_RULE_EXPRESSION,
    MSG_SCHEMA_DQ_THRESHOLD,
    MSG_SCHEMA_DQ_SEVERITY,
    MSG_SCHEMA_DQ_IS_ENABLED,
    MSG_SCHEMA_DQ_VERSION,
    MSG_SCHEMA_DQ_OWNER,
    MSG_SCHEMA_DQ_DESCRIPTION,
    MSG_SCHEMA_DQ_CREATED_TIME,
    MSG_SCHEMA_DQ_UPDATED_TIME,
    MSG_SCHEMA_DQ_EVAL_RULE_ID,
    MSG_SCHEMA_DQ_EVAL_RULE_CODE,
    MSG_SCHEMA_DQ_EVAL_RULE_TYPE,
    MSG_SCHEMA_DQ_EVAL_DATASOURCE_ID,
    MSG_SCHEMA_DQ_EVAL_TOTAL_COUNT,
    MSG_SCHEMA_DQ_EVAL_PASSED_COUNT,
    MSG_SCHEMA_DQ_EVAL_PASS_RATE,
    MSG_SCHEMA_DQ_EVAL_STATUS,
    MSG_SCHEMA_DQ_EVAL_EVALUATED_AT,
    MSG_SCHEMA_DQ_EVAL_DURATION_MS,
    MSG_SCHEMA_DQ_EVAL_MESSAGE,
    MSG_SCHEMA_DQ_EVAL_RULE_IDS,
    MSG_SCHEMA_DQ_EVAL_RESULTS,
    MSG_SCHEMA_DQ_EVAL_SUMMARY_TOTAL,
    MSG_SCHEMA_DQ_EVAL_SUMMARY_PASSED,
    MSG_SCHEMA_DQ_SCORE_ID,
    MSG_SCHEMA_DQ_SCORE_TARGET_TABLE,
    MSG_SCHEMA_DQ_SCORE_TYPE,
    MSG_SCHEMA_DQ_SCORE_COMPLETENESS,
    MSG_SCHEMA_DQ_SCORE_VALIDITY,
    MSG_SCHEMA_DQ_SCORE_UNIQUENESS,
    MSG_SCHEMA_DQ_SCORE_CONSISTENCY,
    MSG_SCHEMA_DQ_SCORE_TIMELINESS,
    MSG_SCHEMA_DQ_SCORE_REFERENTIAL,
    MSG_SCHEMA_DQ_SCORE_OVERALL,
    MSG_SCHEMA_DQ_SCORE_EVALUATED_AT,
    MSG_SCHEMA_DQ_SCORE_DURATION_MS,
    MSG_SCHEMA_DQ_SCORE_RULES_COUNT,
    MSG_SCHEMA_DQ_SCORE_CREATED_TIME,
    MSG_SCHEMA_DQ_SCORE_UPDATED_TIME,
    MSG_SCHEMA_CHAT_DQ_BADGE_TARGET_TABLE,
    MSG_SCHEMA_CHAT_DQ_BADGE_OVERALL,
    MSG_SCHEMA_CHAT_DQ_BADGE_EVALUATED_AT,
    MSG_SCHEMA_CHAT_DQ_BADGE_RULES_COUNT,
    MSG_SCHEMA_CHAT_DQ_BADGE_EVALUATED,
    MSG_SCHEMA_CHAT_DQ_BADGES,
    MSG_SCHEMA_CHAT_SUPPLIER_360,
    MSG_SCHEMA_CHAT_AGENT_RUN,
    MSG_SCHEMA_CHAT_SUPPLIER_RISK,
    MSG_SCHEMA_GRAPH_HOP,
    MSG_SCHEMA_GRAPH_TRAVERSAL,
    MSG_SCHEMA_SCHEDULE_CRON,
    MSG_SCHEMA_SCHEDULE_INPUT,
    MSG_SCHEMA_DQ_COMPUTE_EVALUATED_RULES,
    MSG_SCHEMA_DQ_COMPUTE_SAVED_SCORES,
    MSG_SCHEMA_DQ_COMPUTE_DURATION_MS,
    MSG_SCHEMA_DQ_COMPUTE_SCORES,
    MSG_SCHEMA_AGENT_CODE,
    MSG_SCHEMA_AGENT_DATA_DOMAINS,
    MSG_SCHEMA_AGENT_DATA_LAYERS,
    MSG_SCHEMA_AGENT_DESCRIPTION,
    MSG_SCHEMA_AGENT_NAME,
    MSG_SCHEMA_AGENT_OWNER,
    MSG_SCHEMA_AGENT_POLICY_DATA_LAYER,
    MSG_SCHEMA_AGENT_POLICY_DATA_OBJECT,
    MSG_SCHEMA_AGENT_POLICY_NOTES,
    MSG_SCHEMA_AGENT_POLICY_PERMISSION,
    MSG_SCHEMA_AGENT_RESPONSE_LATENCY,
    MSG_SCHEMA_AGENT_RUN_INPUT,
    MSG_SCHEMA_AGENT_STATUS,
    MSG_SCHEMA_AGENT_TRIGGER_TYPE,
    MSG_SCHEMA_AGENT_VERSION,
    MSG_SCHEMA_ENTITY_MAPPING_CREATED_TIME,
    MSG_SCHEMA_ENTITY_MAPPING_EFFECTIVE_DATE,
    MSG_SCHEMA_ENTITY_MAPPING_ENTERPRISE_CODE,
    MSG_SCHEMA_ENTITY_MAPPING_ENTERPRISE_KEY,
    MSG_SCHEMA_ENTITY_MAPPING_ENTITY_TYPE,
    MSG_SCHEMA_ENTITY_MAPPING_EXPIRY_DATE,
    MSG_SCHEMA_ENTITY_MAPPING_MATCH_RULE,
    MSG_SCHEMA_ENTITY_MAPPING_SOURCE_CODE,
    MSG_SCHEMA_ENTITY_MAPPING_SOURCE_KEY,
    MSG_SCHEMA_ENTITY_MAPPING_SOURCE_SYSTEM,
    MSG_SCHEMA_ENTITY_MAPPING_UPDATED_TIME,
    MSG_SCHEMA_DATASOURCE_ORACLE_VERSION_FULL,
    MSG_SCHEMA_DATASOURCE_PASSWORD_KEEP,
    MSG_SCHEMA_DATASOURCE_PASSWORD_PLAIN,
    MSG_SCHEMA_DATASOURCE_SCHEMA_CACHED_AT,
    MSG_SCHEMA_DATASOURCE_SCHEMA_TABLES,
    MSG_SCHEMA_EMBEDDING_PROVIDER_API_KEY,
    MSG_SCHEMA_EMBEDDING_PROVIDER_BASE_URL,
    MSG_SCHEMA_EMBEDDING_PROVIDER_DIMENSION,
    MSG_SCHEMA_EMBEDDING_PROVIDER_IS_ACTIVE,
    MSG_SCHEMA_EMBEDDING_PROVIDER_MODEL_NAME,
    MSG_SCHEMA_EMBEDDING_PROVIDER_NAME,
    MSG_SCHEMA_EMBEDDING_PROVIDER_TYPE,
    MSG_SCHEMA_MODEL_API_ENDPOINT,
    MSG_SCHEMA_MODEL_API_KEY,
    MSG_SCHEMA_MODEL_COST_INPUT,
    MSG_SCHEMA_MODEL_COST_OUTPUT,
    MSG_SCHEMA_MODEL_COST_THRESHOLD,
    MSG_SCHEMA_MODEL_IS_ACTIVE,
    MSG_SCHEMA_MODEL_MAX_INPUT_TOKENS,
    MSG_SCHEMA_MODEL_NAME,
    MSG_SCHEMA_MODEL_PROVIDER,
    MSG_SCHEMA_MODEL_WEIGHT,
    MSG_SCHEMA_QUERY_PLAN_FORMULA,
    MSG_SCHEMA_SIMILAR_QUESTION,
    MSG_SCHEMA_SIMILAR_SIMILARITY,
    MSG_SCHEMA_SIMILAR_SQL,
    MSG_SCHEMA_SIMILAR_SUGGESTIONS,
    MSG_SCHEMA_LINEAGE_CREATED_TIME,
    MSG_SCHEMA_LINEAGE_DESCRIPTION,
    MSG_SCHEMA_LINEAGE_IS_ACTIVE,
    MSG_SCHEMA_LINEAGE_OWNER,
    MSG_SCHEMA_LINEAGE_REFRESH_FREQ,
    MSG_SCHEMA_LINEAGE_SOURCE_FIELD,
    MSG_SCHEMA_LINEAGE_SOURCE_LAYER,
    MSG_SCHEMA_LINEAGE_SOURCE_OBJECT,
    MSG_SCHEMA_LINEAGE_SOURCE_SYSTEM,
    MSG_SCHEMA_LINEAGE_TARGET_FIELD,
    MSG_SCHEMA_LINEAGE_TARGET_LAYER,
    MSG_SCHEMA_LINEAGE_TARGET_OBJECT,
    MSG_SCHEMA_LINEAGE_TARGET_SYSTEM,
    MSG_SCHEMA_LINEAGE_TRANSFORMATION,
    MSG_SCHEMA_LINEAGE_UPDATED_TIME,
    MSG_SCHEMA_USAGE_BY_MODEL,
    MSG_SCHEMA_USAGE_LAST_QUESTION,
    MSG_SCHEMA_USAGE_TOTAL_COST,
    MSG_SCHEMA_USAGE_TOTAL_REQUESTS,
    MSG_SCHEMA_USAGE_TOTAL_SESSIONS,
    MSG_SCHEMA_USAGE_TOTAL_TOKENS,
    MSG_AGENT_DOMAIN_NOT_IN_VOCAB,
    MSG_AGENT_LAYER_NOT_IN_VOCAB,
    MSG_AGENT_TOOL_UNKNOWN,
    MSG_AGENT_TOOL_LAYER_MISMATCH,
)


class CamelModel(BaseModel):
    """所有 DTO 基类：snake_case 字段名 + camelCase JSON 别名。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        protected_namespaces=(),
    )


# ---------------------------------------------------------------------------
# Phase 7 feat-agent-tool-binding：tool_name 跨字段一致性写时校验（Task 4）
# ---------------------------------------------------------------------------
#
# 设计原则：deny-by-default 从运行时移到配置面 —— Pydantic 在 DTO 边界拒绝
# 非法组合，service 层只接管合法写入。
# 共享逻辑同时被 AgentDefinitionCreate / AgentDefinitionUpdate 复用。
#
# 为什么不在模块顶层 import agent_tool_registry：agent_tools → services →
# schemas 形成循环依赖。此处用 TYPE_CHECKING 标注静态类型，运行时再延迟
# 导入避免循环；validator 仅在 Pydantic 实例化时触发，循环时机已解开。

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # 运行时无需类型标注，agent_tool_registry 仅在 validator 内引用


def _validateToolNameShared(v: str | None, info) -> str | None:
    """Create/Update 共用逻辑：tool_name 必须在 registry 中，
    且 agent.data_layers 必须完全覆盖 tool.data_layers。

    非法值抛 ValueError（Pydantic 422 拒绝）：
    - tool 未注册 → MSG_AGENT_TOOL_UNKNOWN
    - data_layers 未覆盖 → MSG_AGENT_TOOL_LAYER_MISMATCH
    """
    if v is None:
        return v
    # 延迟导入打破 schemas ↔ agent_tools ↔ services 循环依赖
    from app.services.agent_tool_config_registry import agent_tool_config_registry
    tool = agent_tool_config_registry.get(v)
    if tool is None:
        registered = ",".join(t.name for t in agent_tool_config_registry.all())
        raise ValueError(MSG_AGENT_TOOL_UNKNOWN.format(
            name=v, registered=registered,
        ))
    data_layers = info.data.get("data_layers") or []
    missing = [layer for layer in tool.data_layers if layer not in data_layers]
    if missing:
        raise ValueError(MSG_AGENT_TOOL_LAYER_MISMATCH.format(
            tool=v, missing=",".join(missing),
        ))
    return v


# ===== 模型配置 =====


class LlmConfigCreate(CamelModel):
    model_name: str = Field(..., min_length=1, max_length=50, description=MSG_SCHEMA_MODEL_NAME)
    provider: str = Field(..., description=MSG_SCHEMA_MODEL_PROVIDER)
    api_endpoint: str | None = Field(default=None, description=MSG_SCHEMA_MODEL_API_ENDPOINT)
    api_key: str | None = Field(default=None, description=MSG_SCHEMA_MODEL_API_KEY)
    cost_per_1k_input: Decimal = Field(default=Decimal("0"), ge=0, description=MSG_SCHEMA_MODEL_COST_INPUT)
    cost_per_1k_output: Decimal = Field(default=Decimal("0"), ge=0, description=MSG_SCHEMA_MODEL_COST_OUTPUT)
    max_input_tokens: int = Field(default=8000, ge=1, description=MSG_SCHEMA_MODEL_MAX_INPUT_TOKENS)
    weight: int = Field(default=10, ge=0, le=100, description=MSG_SCHEMA_MODEL_WEIGHT)
    cost_threshold: Decimal = Field(default=Decimal("0.05"), ge=0, description=MSG_SCHEMA_MODEL_COST_THRESHOLD)
    is_active: bool = Field(default=True, description=MSG_SCHEMA_MODEL_IS_ACTIVE)


class LlmConfigUpdate(CamelModel):
    api_endpoint: str | None = None
    api_key: str | None = None
    cost_per_1k_input: Decimal | None = Field(default=None, ge=0)
    cost_per_1k_output: Decimal | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=1)
    weight: int | None = Field(default=None, ge=0, le=100)
    cost_threshold: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None


class LlmConfigRead(CamelModel):
    id: int
    model_name: str
    provider: str
    api_endpoint: str | None = None
    cost_per_1k_input: Decimal
    cost_per_1k_output: Decimal
    max_input_tokens: int
    weight: int
    cost_threshold: Decimal
    is_active: bool
    created_time: datetime | None = None
    updated_time: datetime | None = None


# ===== Embedding 服务注册表 =====


class EmbeddingProviderCreate(CamelModel):
    name: str = Field(..., min_length=1, max_length=100, description=MSG_SCHEMA_EMBEDDING_PROVIDER_NAME)
    provider_type: str = Field(
        ..., min_length=1, max_length=30, description=MSG_SCHEMA_EMBEDDING_PROVIDER_TYPE
    )
    base_url: str = Field(..., min_length=1, max_length=255, description=MSG_SCHEMA_EMBEDDING_PROVIDER_BASE_URL)
    model_name: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_EMBEDDING_PROVIDER_MODEL_NAME
    )
    api_key: str | None = Field(default=None, description=MSG_SCHEMA_EMBEDDING_PROVIDER_API_KEY)
    dimension: int = Field(default=1024, ge=1, description=MSG_SCHEMA_EMBEDDING_PROVIDER_DIMENSION)
    is_active: bool = Field(default=False, description=MSG_SCHEMA_EMBEDDING_PROVIDER_IS_ACTIVE)


class EmbeddingProviderUpdate(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    provider_type: str | None = Field(default=None, min_length=1, max_length=30)
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    model_name: str | None = Field(default=None, min_length=1, max_length=100)
    api_key: str | None = None
    dimension: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class EmbeddingProviderRead(CamelModel):
    id: int
    name: str
    provider_type: str
    base_url: str
    model_name: str
    dimension: int
    is_active: bool
    created_time: datetime | None = None
    updated_time: datetime | None = None


# ===== Token 消耗 =====


class SessionTokenUsageRead(CamelModel):
    id: int
    session_id: str
    model_name: str | None = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Decimal
    request_time: datetime
    purpose: str | None = None


class ModelUsageStat(CamelModel):
    model_name: str
    requests: int
    total_tokens: int
    total_cost: Decimal


class TokenUsageSummary(CamelModel):
    session_id: str
    total_requests: int
    total_tokens: int
    total_cost: Decimal
    by_model: list[ModelUsageStat] = Field(default_factory=list, description=MSG_SCHEMA_USAGE_BY_MODEL)


class SessionListItem(CamelModel):
    """会话列表项（聚合统计 + 最近活动），用于用量看板。"""

    session_id: str
    total_requests: int
    total_tokens: int
    total_cost: Decimal
    first_request_time: datetime
    last_request_time: datetime
    last_question: str | None = Field(default=None, description=MSG_SCHEMA_USAGE_LAST_QUESTION)


class GlobalUsageSummary(CamelModel):
    """全局用量摘要（跨全部会话），用量看板顶部卡片数据源。"""

    total_sessions: int = Field(description=MSG_SCHEMA_USAGE_TOTAL_SESSIONS)
    total_requests: int = Field(description=MSG_SCHEMA_USAGE_TOTAL_REQUESTS)
    total_tokens: int = Field(description=MSG_SCHEMA_USAGE_TOTAL_TOKENS)
    total_cost: Decimal = Field(description=MSG_SCHEMA_USAGE_TOTAL_COST)


class DailyUsageTrend(CamelModel):
    """单日全局用量趋势点，按日期升序。"""

    date: date
    requests: int
    tokens: int
    cost: Decimal


class ModelUsageAggregate(CamelModel):
    """按模型汇总的全局用量（model_name 为空时归入 unknown）。"""

    model_name: str
    requests: int
    tokens: int
    cost: Decimal


# ===== 本体（Phase 2）=====


class OntologyClassCreate(CamelModel):
    class_name: str = Field(..., min_length=1, max_length=100)
    class_alias: str | None = Field(default=None, max_length=100)
    description: str | None = None
    source_table: str | None = Field(default=None, max_length=100)
    # 治理字段（Phase 3.4，采购域 Sheet 03 业务对象目录）
    object_type: ObjectType | None = Field(
        default=None, description="Master/Transaction/Reference/Event"
    )
    parent_class_id: int | None = None
    created_by: str | None = None


class OntologyClassUpdate(CamelModel):
    class_name: str | None = Field(default=None, max_length=100)
    class_alias: str | None = Field(default=None, max_length=100)
    description: str | None = None
    source_table: str | None = Field(default=None, max_length=100)
    object_type: ObjectType | None = Field(
        default=None, description="Master/Transaction/Reference/Event"
    )
    parent_class_id: int | None = None


_PROPERTY_DESCRIPTION_MAX = 500
_PROPERTY_ALIAS_MAX_ITEMS = 20
_PROPERTY_ALIAS_ITEM_MAX = 100


def _validateBusinessAliases(value: list[str] | None) -> list[str] | None:
    """业务别名数量/单项长度封顶：本体配置进 schema 文本，失控会撑爆 prompt。"""
    if value is None:
        return None
    if len(value) > _PROPERTY_ALIAS_MAX_ITEMS:
        raise ValueError(f"business_aliases 最多 {_PROPERTY_ALIAS_MAX_ITEMS} 项")
    for alias in value:
        if len(alias) > _PROPERTY_ALIAS_ITEM_MAX:
            raise ValueError(f"business_aliases 单项最长 {_PROPERTY_ALIAS_ITEM_MAX} 字符")
    return value


class OntologyPropertyCreate(CamelModel):
    class_id: int
    property_name: str = Field(..., min_length=1, max_length=100)
    property_alias: str | None = Field(default=None, max_length=100)
    business_aliases: list[str] | None = Field(
        default=None, description="业务别名/同义词列表，如 [\"营业额\", \"收入\"]（2-2）"
    )
    description: str | None = Field(
        default=None, max_length=_PROPERTY_DESCRIPTION_MAX, description="列含义描述（2-2）"
    )
    data_type: str = Field(..., description="STRING|INT|DECIMAL|DATETIME|BOOLEAN")
    is_primary_key: bool = False
    is_foreign_key: bool = False
    ref_class_id: int | None = None
    source_column: str | None = Field(default=None, max_length=100)

    _check_aliases = field_validator("business_aliases")(_validateBusinessAliases)


class OntologyPropertyUpdate(CamelModel):
    property_name: str | None = Field(default=None, max_length=100)
    property_alias: str | None = Field(default=None, max_length=100)
    business_aliases: list[str] | None = None
    description: str | None = Field(default=None, max_length=_PROPERTY_DESCRIPTION_MAX)
    data_type: str | None = None
    is_primary_key: bool | None = None
    is_foreign_key: bool | None = None
    ref_class_id: int | None = None
    source_column: str | None = None
    # 值域：本体属性管理页可手动调整 LLM 采纳的值；
    # None 表示不修改；显式空数组 视作清空值域。
    allowed_values: list[str] | None = Field(default=None, max_length=50)

    _check_aliases = field_validator("business_aliases")(_validateBusinessAliases)

    @field_validator("allowed_values")
    @classmethod
    def _validateAllowedValuesNoQuotes(cls, v: list[str] | None) -> list[str] | None:
        """与 apply-suggestion 一致：禁止单引号（SQL 注入防护）。"""
        if v is None:
            return v
        for s in v:
            if "'" in s:
                raise ValueError("allowed_values must not contain single quote")
        return v


class OntologyMetricCreate(CamelModel):
    metric_name: str = Field(..., min_length=1, max_length=100)
    metric_alias: str | None = Field(default=None, max_length=100)
    formula: str = Field(..., description=MSG_SCHEMA_QUERY_PLAN_FORMULA)
    agg_function: str = Field(default="SUM", description="SUM|AVG|COUNT|MAX|MIN")
    target_class_id: int | None = None
    dimension_defaults: dict | None = None
    created_by: str | None = None


class OntologyMetricUpdate(CamelModel):
    metric_name: str | None = Field(default=None, max_length=100)
    metric_alias: str | None = None
    formula: str | None = None
    agg_function: str | None = None
    target_class_id: int | None = None
    dimension_defaults: dict | None = None


class OntologyClassRead(CamelModel):
    id: int
    class_name: str
    class_alias: str | None = None
    description: str | None = None
    source_table: str | None = None
    object_type: ObjectType | None = None
    object_owner: str | None = None
    parent_class_id: int | None = None
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None
    # 版本字段保留兼容（版本管理已移除，updateClass 不再递增）
    version: int = 1
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class OntologyPropertyRead(CamelModel):
    id: int
    class_id: int
    property_name: str
    property_alias: str | None = None
    business_aliases: list[str] | None = None
    description: str | None = None
    data_type: str
    is_primary_key: bool
    is_foreign_key: bool
    ref_class_id: int | None = None
    source_column: str | None = None
    # 值域（LLM 采纳或人工填入）；null 表示未约束。
    # 暴露给本体属性管理页（让用户能看到「已沉淀」的值并手动修正）。
    allowed_values: list[str] | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class OntologyMetricRead(CamelModel):
    id: int
    metric_name: str
    metric_alias: str | None = None
    formula: str
    agg_function: str
    target_class_id: int | None = None
    dimension_defaults: dict | None = None
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class KpiCatalogCreate(CamelModel):
    """KPI 业务目录创建请求（Phase 4.1）。

    kpi_code 唯一（DB 层 unique 约束兜底）。status 默认 DRAFT；version 默认 v1.0；
    revision_count 默认 0，PUT 时由 service 自增。
    """

    kpi_code: str = Field(..., min_length=1, max_length=50)
    kpi_name: str = Field(..., min_length=1, max_length=200)
    business_definition: str | None = Field(default=None, max_length=4000)
    formula: str | None = Field(default=None, max_length=4000)
    numerator: str | None = Field(default=None, max_length=1000)
    denominator: str | None = Field(default=None, max_length=1000)
    grain: str | None = Field(default=None, max_length=100)
    unit: str | None = Field(default=None, max_length=50)
    data_source: str | None = Field(default=None, max_length=200)
    owner: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=20)
    status: KpiStatus = KpiStatus.DRAFT
    metric_id: int | None = None
    created_by: str | None = Field(default=None, max_length=50)
    semantic_keywords: list[str] | None = Field(
        default=None,
        description="L1 语义匹配关键词（自动晋升时由 question 提取）",
    )
    match_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="匹配阈值，默认 0.75",
    )


class KpiCatalogUpdate(CamelModel):
    """KPI 业务目录更新请求。

    全部字段可选；exclude_unset 模式下未传字段不动。显式传 null 清空（前端
    清空表单依赖此契约，与 Phase 3.4 object_type 同模式）。
    """

    kpi_code: str | None = Field(default=None, max_length=50)
    kpi_name: str | None = Field(default=None, max_length=200)
    business_definition: str | None = Field(default=None, max_length=4000)
    formula: str | None = Field(default=None, max_length=4000)
    numerator: str | None = Field(default=None, max_length=1000)
    denominator: str | None = Field(default=None, max_length=1000)
    grain: str | None = Field(default=None, max_length=100)
    unit: str | None = Field(default=None, max_length=50)
    data_source: str | None = Field(default=None, max_length=200)
    owner: str | None = Field(default=None, max_length=100)
    version: str | None = Field(default=None, max_length=20)
    status: KpiStatus | None = Field(default=None, description="KPI 状态；显式传 null 会被 422 拒绝（NOT NULL 约束）")
    metric_id: int | None = None


class KpiCatalogRead(CamelModel):
    id: int
    kpi_code: str
    kpi_name: str
    business_definition: str | None = None
    formula: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    grain: str | None = None
    unit: str | None = None
    data_source: str | None = None
    owner: str | None = None
    version: str
    revision_count: int
    status: KpiStatus
    metric_id: int | None = None
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class FeatureDefinitionCreate(CamelModel):
    """AI 特征定义创建请求（Phase 4.3）。

    owner 不在 DTO 中：由 actor.departments[0] 派生（entity_mapping 同模式），
    防止 client 任意声明 owner 越权。calculation_logic 必须是读业务库的单条
    只读 SELECT（service 层 _assert_read_only 校验）。
    """

    feature_name: str = Field(..., min_length=1, max_length=100)
    feature_alias: str | None = Field(default=None, max_length=200)
    feature_definition: str | None = Field(default=None, max_length=8000)
    entity_type: BusinessObjectCodeType
    calculation_logic: str = Field(..., min_length=1, max_length=8000)
    window_size: str | None = Field(default=None, max_length=20)
    refresh_frequency: FeatureRefreshFrequency = FeatureRefreshFrequency.DAILY
    unit: str | None = Field(default=None, max_length=50)
    version: str | None = Field(default=None, max_length=20)
    status: FeatureStatus = FeatureStatus.DRAFT
    is_enabled: bool = True
    datasource_id: int = Field(..., gt=0)


class FeatureDefinitionUpdate(CamelModel):
    """AI 特征定义更新请求。

    全部字段可选；exclude_unset 模式下未传字段不动。非空列传 null 视为不动
    （entity_mapping 的 _NON_NULL_UPDATE_FIELDS 同模式）。owner 不可改（不在 DTO）。
    """

    feature_name: str | None = Field(default=None, min_length=1, max_length=100)
    feature_alias: str | None = Field(default=None, max_length=200)
    feature_definition: str | None = Field(default=None, max_length=8000)
    entity_type: BusinessObjectCodeType | None = None
    calculation_logic: str | None = Field(default=None, min_length=1, max_length=8000)
    window_size: str | None = Field(default=None, max_length=20)
    refresh_frequency: FeatureRefreshFrequency | None = None
    unit: str | None = Field(default=None, max_length=50)
    version: str | None = Field(default=None, max_length=20)
    status: FeatureStatus | None = None
    is_enabled: bool | None = None
    datasource_id: int | None = Field(default=None, gt=0)


class FeatureDefinitionRead(CamelModel):
    id: int
    feature_name: str
    feature_alias: str | None = None
    feature_definition: str | None = None
    entity_type: BusinessObjectCodeType
    calculation_logic: str
    window_size: str | None = None
    refresh_frequency: FeatureRefreshFrequency
    unit: str | None = None
    owner: str | None = None
    version: str
    status: FeatureStatus
    is_enabled: bool
    datasource_id: int
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class FeatureValueRead(CamelModel):
    """特征值读 DTO（Phase 4.3）。value / value_text 至少一者非空。"""

    id: int
    feature_id: int
    entity_key: str
    value: Decimal | None = None
    value_text: str | None = None
    valid_at: date
    computed_at: datetime


class FeatureQueryResponse(CamelModel):
    """按 feature_name 在线查询响应（Phase 4.4，外部消费契约）。

    valid_at 为实际返回值的有效期（请求缺省时为该特征最新窗口；
    无任何值时为占位今日 + 空 values，前端渲染空态）。
    """

    feature_name: str
    entity_type: BusinessObjectCodeType
    unit: str | None = None
    valid_at: date
    values: list[FeatureValueRead] = Field(default_factory=list)


# ===========================================================================
# Phase 5.3: Supplier 360° ADS 视图 DTO
# ===========================================================================


class Supplier360Profile(CamelModel):
    """供应商 360° 主数据块（Phase 5.3）。

    来源：entity_mapping.enterprise_code (e.g. SUP000001) + 各源系统 source_code。
    DIM_SUPPLIER 主数据表（dw/04_dim.sql）目前未在 metadata DB 中镜像，本块先
    用 enterprise_code 作为展示名锚点；后续 ADS view 接入后可补充 supplier_name。
    """

    enterprise_key: int = Field(..., description="企业统一代理键（MDM 主数据）")
    enterprise_code: str = Field(..., description="企业统一编码（如 SUP000001）")
    entity_type: BusinessObjectCodeType


class Supplier360Kpi(CamelModel):
    """单个特征 KPI 卡片（Phase 5.3）。

    对应 SUPPLIER_OTD_3M / SUPPLIER_DEFECT_RATE_3M / SUPPLIER_PRICE_VARIANCE_3M /
    SUPPLIER_RISK_SCORE 四类。value/value_text 至少一者非空（与 FeatureValueRead
    一致）；latest 为 false 表示特征已定义但尚无最新值（前端应展示「暂无数据」态）。
    """

    feature_name: str = Field(..., description="特征名（来自 feature_definition）")
    feature_alias: str | None = None
    value: Decimal | None = None
    value_text: str | None = None
    unit: str | None = None
    valid_at: date | None = None
    computed_at: datetime | None = None
    latest: bool = Field(default=False, description="True=已取到最新值，False=特征无值")


class Supplier360EntityCode(CamelModel):
    """供应商跨系统编码（Phase 5.3）。"""

    source_system: str
    source_code: str
    source_key: str
    match_rule: str


class Supplier360Read(CamelModel):
    """供应商 360° 视图响应（Phase 5.3，外部消费契约）。

    设计原则（plan §5.3）：
    - 不建新表，**实时聚合** entity_mapping + feature_value + （未来）DW ADS view
    - 任意子模块失败不影响整体响应（log warn + 该字段空值返回，前端按字段渲染）
    - kpis 默认拉 4 个 SUPPLIER 特征最新值；空值即「暂无数据」，不报错
    - entity_codes 来自 entity_mapping；为空表示该 supplier 尚未建立跨系统映射
    """

    profile: Supplier360Profile
    entity_codes: list[Supplier360EntityCode] = Field(default_factory=list)
    kpis: list[Supplier360Kpi] = Field(default_factory=list)
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="后端聚合时间；前端可用作缓存命中判断",
    )


# ===========================================================================
# Phase 5.4: Supplier Risk Agent DTO
# ===========================================================================


class SupplierRiskKpiContribution(CamelModel):
    """单个 feature 在风险评估中的贡献度（Phase 5.4）。

    一一映射 supplier 360° 的 DEFAULT_SUPPLIER_FEATURES。passed=False 表示
    该 feature 触发违规（如 OTD < 阈值、DEFECT_RATE > 阈值）；note 给出
    「85% 低于阈值 90%」类的人类可读描述。
    """

    feature_name: str
    feature_alias: str | None = None
    value: str | None = None  # Decimal → string
    unit: str | None = None
    threshold: str | None = None  # 该 feature 在风险规则中的阈值
    passed: bool = Field(default=True, description="True=未触发违规，False=触发")
    note: str | None = None


class SupplierRiskRead(CamelModel):
    """供应商风险评估响应（Phase 5.4，外部消费契约）。

    设计原则（plan §5.4）：
    - 复用 Supplier360Profile（profile 来源不变；风险 Agent 不另查主数据）
    - 主路径：RISK_SCORE（0-1，越高越优）→ 直接映射 High/Medium/Low
    - Fallback：其他 3 个 feature 违规计数（≥2 → Medium，3 → High）
    - risk_points：默认 LLM 生成（自然语言 1-2 句）；LLM 不可用降级到模板
    - recommended_actions：按等级静态生成（不调 LLM，保持响应稳定）
    """

    profile: Supplier360Profile
    level: RiskLevel
    level_source: str = Field(
        ..., description="risk_score / fallback_composite / unknown"
    )
    contributions: list[SupplierRiskKpiContribution] = Field(default_factory=list)
    risk_points: str | None = None
    risk_points_source: str = Field(default="llm", description="llm / fallback_template")
    recommended_actions: list[str] = Field(default_factory=list)
    tokens_used: int = Field(default=0, ge=0, description="LLM 调用 token 数；fallback=0")
    prompt_tokens: int = Field(default=0, ge=0, description="LLM 调用 prompt token 数；fallback=0")
    completion_tokens: int = Field(default=0, ge=0, description="LLM 调用 completion token 数；fallback=0")
    cost: float = Field(default=0.0, ge=0.0, description="LLM 调用成本（CNY）；fallback=0")
    llm_model_name: str | None = None
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="后端聚合时间",
    )


class GraphTraversalHop(CamelModel):
    """一跳遍历记录（Phase 6.3，外部消费契约）。

    对应 ``traverseBusinessGraph`` 返回行：depth = 1..maxHops，
    起点固定为遍历起始实体（variable-length path 的每条行按末边展开）。
    """

    depth: int = Field(..., ge=1, description=MSG_SCHEMA_GRAPH_HOP)
    from_key: str
    from_code: str
    from_name: str | None = None
    from_type: str
    rel_type: str
    to_key: str
    to_code: str
    to_name: str | None = None
    to_type: str


class GraphTraversalRead(CamelModel):
    """多跳推理结果（Phase 6.3，外部消费契约）。

    - start：遍历起点（entityType + key，来自用户问句或 API 参数）
    - maxHops：实际遍历深度上限（1..5）
    - hops：逐跳展开的可达链（已按深度 + 终点 key 排序，不含起始节点自身）
    - reachableTypes：去重后的可达实体类型集合（快速摘要，前端徽标用）
    """

    start_key: str
    start_type: str
    max_hops: int = Field(..., ge=1)
    hops: list[GraphTraversalHop] = Field(default_factory=list)
    reachable_types: list[str] = Field(default_factory=list)
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="遍历执行时间",
    )


class FeatureComputeResult(CamelModel):
    """单特征计算结果（Phase 4.3）。rows = 落库/覆盖的特征值行数。"""

    feature_id: int
    rows: int


class FeatureComputeBatchResult(CamelModel):
    """批量计算汇总（Phase 4.3）。"""

    results: list[FeatureComputeResult]
    total_rows: int


class OntologyJoinCreate(CamelModel):
    model_config = ConfigDict(extra="forbid")

    source_class_id: int
    source_columns: list[str] = Field(..., min_length=1)
    target_class_id: int
    target_columns: list[str] = Field(..., min_length=1)
    join_type: str = Field(default="INNER", max_length=10)
    relation_type: str = Field(default="business", max_length=20)
    description: str | None = Field(default=None, max_length=1000)


class OntologyJoinRead(CamelModel):
    id: int
    source_class_id: int
    source_columns: list[str]
    target_class_id: int
    target_columns: list[str]
    join_type: str
    relation_type: str
    description: str | None = None
    join_key: str
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class OntologyJoinUpdate(CamelModel):
    join_type: str | None = None
    relation_type: str | None = None
    description: str | None = None


class OntologyRelationCreate(CamelModel):
    """创建本体「类 × 类」语义关系。relation_type 取值由 service 校验 ClassRelationType。"""

    model_config = ConfigDict(extra="forbid")

    source_class_id: int
    target_class_id: int
    relation_type: str = Field(..., max_length=30)
    description: str | None = Field(default=None, max_length=1000)


class OntologyRelationRead(CamelModel):
    id: int
    source_class_id: int
    target_class_id: int
    relation_type: str
    description: str | None = None
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class RelationBackfillResult(CamelModel):
    """一键补关系结果：join 全量入图数 + X3 外键 ref_class_id 补全数。"""

    synced_joins: int
    backfilled_references: int


# ===== Ontology Batch Relation Engine（通用批量关系引擎） =====

# 冲突策略：skip=已存在则跳过；overwrite=已存在则覆盖可更新字段（源/目标/列是身份）
OnConflictPolicy = Literal["skip", "overwrite"]


class InferredJoin(CamelModel):
    """共享列推断出的 join 候选（落库前只读描述；来源区分命名约定/共享列）。"""

    source_class_id: int
    source_class_name: str
    source_columns: list[str]
    target_class_id: int
    target_class_name: str
    target_columns: list[str]
    relation_type: str = "foreign_key"
    inferred_by: str = "name_convention"  # name_convention | shared_column


class RelationManifest(CamelModel):
    """批量关系清单：joins + relations（与单条创建 schema 一一对应）。"""

    model_config = ConfigDict(extra="forbid")

    joins: list[OntologyJoinCreate] = Field(default_factory=list)
    relations: list[OntologyRelationCreate] = Field(default_factory=list)


class BatchRowError(CamelModel):
    """清单单行的错误（index 为该清单内 0-based 行序；message 为原因）。"""

    index: int
    message: str


class BatchCounts(CamelModel):
    """join/relations 批量应用计数（含行级错误，不 fail-fast）。"""

    created: int = 0
    skipped: int = 0
    overwritten: int = 0
    errors: list[BatchRowError] = Field(default_factory=list)


class GraphSyncResult(CamelModel):
    """syncGraph 本体入图统计（Neo4j 节点/边数）。"""

    classes: int = 0
    properties: int = 0
    has_property_edges: int = 0
    reference_edges: int = 0


class BatchRelationRequest(CamelModel):
    """批量关系引擎请求：可任选其一或多个动作；对已存在关系可选覆盖/跳过。"""

    model_config = ConfigDict(extra="forbid")

    sync_graph: bool = False
    infer_joins: bool = False
    apply_manifest: bool = False
    on_conflict: OnConflictPolicy = "skip"
    manifest: RelationManifest | None = None

    @model_validator(mode="after")
    def _validateAtLeastOneAction(self) -> BatchRelationRequest:
        if not (self.sync_graph or self.infer_joins or self.apply_manifest):
            raise ValueError(
                "batch requires at least one action: syncGraph | inferJoins | applyManifest"
            )
        if self.apply_manifest and self.manifest is None:
            raise ValueError("applyManifest requires a non-null manifest")
        return self


class BatchRelationResult(CamelModel):
    """批量关系引擎结果（执行或只读预览共用；不可变计数，Neo4j 失败不阻断 PG）。"""

    sync_graph: GraphSyncResult | None = None
    inferred_joins: list[InferredJoin] = Field(default_factory=list)
    joins: BatchCounts = Field(default_factory=BatchCounts)
    relations: BatchCounts = Field(default_factory=BatchCounts)


class OntologyCsvParseResult(CamelModel):
    """CSV 清单解析结果：manifest（按类名反解 id）+ 行级错误（Excel 友好）。"""

    manifest: RelationManifest = Field(default_factory=RelationManifest)
    errors: list[BatchRowError] = Field(default_factory=list)


class OntologySearchResult(CamelModel):
    id: int
    type: str = Field(..., description="class|property|metric")
    name: str
    alias: str | None = None
    description: str | None = None
    score: float


# ===== DataSource (Phase 3) =====


class DataSourceCreate(CamelModel):
    name: str = Field(..., min_length=1, max_length=100, description=MSG_SCHEMA_DATASOURCE_NAME)
    type: DataSourceType = Field(..., description="mysql | postgresql | oracle")
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database_name: str = Field(..., min_length=1, max_length=100, description=MSG_SCHEMA_DATASOURCE_DATABASE)
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, description=MSG_SCHEMA_DATASOURCE_PASSWORD_PLAIN)
    description: str | None = None
    is_active: bool = True
    is_default: bool = False
    # Oracle 版本，用于确定 SQL 分页语法；None 表示由系统自动判断（默认 12c 及以上用 FETCH FIRST）
    oracle_version: str | None = Field(default=None, description=MSG_SCHEMA_DATASOURCE_ORACLE_VERSION_FULL)


class DataSourceUpdate(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    type: DataSourceType | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    password: str | None = Field(default=None, description=MSG_SCHEMA_DATASOURCE_PASSWORD_KEEP)
    description: str | None = None
    is_active: bool | None = None
    is_default: bool | None = None
    oracle_version: str | None = Field(default=None, description=MSG_SCHEMA_DATASOURCE_ORACLE_VERSION)


class DataSourceRead(CamelModel):
    id: int
    name: str
    type: DataSourceType
    host: str
    port: int
    database_name: str
    username: str
    description: str | None = None
    is_active: bool
    is_default: bool
    oracle_version: str | None = Field(default=None, description=MSG_SCHEMA_DATASOURCE_ORACLE_VERSION)
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


class DataSourceTestRequest(CamelModel):
    type: DataSourceType
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    database_name: str = Field(..., min_length=1, max_length=100)
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1)


class DataSourceTestResponse(CamelModel):
    success: bool
    message: str


# ===== Schema 自动发现（Phase 5.7）=====


class ColumnSchemaRead(CamelModel):
    column_name: str
    data_type: str
    # 必填：nullable 缺失说明数据字典返回形状异常，fail-fast 而非静默默认
    nullable: bool


class ForeignKeySchemaRead(CamelModel):
    column_name: str
    ref_table: str
    ref_column: str


class TableSchemaRead(CamelModel):
    table_name: str
    owner: str = ""
    columns: list[ColumnSchemaRead] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeySchemaRead] = Field(default_factory=list)


class SchemaIntrospectResponse(CamelModel):
    tables: list[TableSchemaRead] = Field(default_factory=list, description=MSG_SCHEMA_DATASOURCE_SCHEMA_TABLES)
    cached_at: datetime = Field(..., description=MSG_SCHEMA_DATASOURCE_SCHEMA_CACHED_AT)


# ===== 本地导入规则（local import）=====

# 默认表名黑名单：排除日志表与临时表（匹配 tmp_/temp_ 前缀另行由 include_temp_tables 控制）。
DEFAULT_TABLE_NAME_BLACKLIST_PATTERNS: list[str] = [r"^log$", r"^log_", r"_log$"]

# 表名黑名单正则防护上限：封顶数量与单项长度，避免恶意 pattern 撑爆正则求值（ReDoS）。
_NAME_BLACKLIST_MAX_PATTERNS = 20
_NAME_BLACKLIST_PATTERN_MAX_LEN = 100


def _validateNameBlacklistPatterns(value: list[str]) -> list[str]:
    """校验表名黑名单正则：数量/长度封顶 + re.compile 合法性（防 ReDoS 与 500）。

    该字段由客户端经 ImportPreviewRequest.rules.tableFilter 直接提供，
    非法 pattern 在 DTO 边界即抛 ConfigError（→ HTTP 400），不流入规则引擎。
    """
    if len(value) > _NAME_BLACKLIST_MAX_PATTERNS:
        raise ConfigError(f"name_blacklist_patterns 最多 {_NAME_BLACKLIST_MAX_PATTERNS} 项")
    for pattern in value:
        if len(pattern) > _NAME_BLACKLIST_PATTERN_MAX_LEN:
            raise ConfigError(
                f"name_blacklist_patterns 单项最长 {_NAME_BLACKLIST_PATTERN_MAX_LEN} 字符"
            )
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(f"表名黑名单正则非法: {pattern!r}（{exc}）") from exc
    return value

# 默认类型映射：key 为规范化后的 DB 类型（大写、无参数），value 为 DataType 枚举值。
DEFAULT_TYPE_MAPPINGS: dict[str, str] = {
    "NUMBER(p=0,s=0)": "INT",
    "NUMBER": "DECIMAL",
    "INT": "INT",
    "INTEGER": "INT",
    "BIGINT": "INT",
    "SMALLINT": "INT",
    "TINYINT": "INT",
    "DECIMAL": "DECIMAL",
    "NUMERIC": "DECIMAL",
    "FLOAT": "DECIMAL",
    "DOUBLE": "DECIMAL",
    "REAL": "DECIMAL",
    "VARCHAR": "STRING",
    "VARCHAR2": "STRING",
    "NVARCHAR": "STRING",
    "NCHAR": "STRING",
    "CHAR": "STRING",
    "TEXT": "STRING",
    "CLOB": "STRING",
    "DATE": "DATETIME",
    "TIMESTAMP": "DATETIME",
    "DATETIME": "DATETIME",
    "BOOLEAN": "BOOLEAN",
    "BOOL": "BOOLEAN",
    "BIT": "BOOLEAN",
}


class TableFilterRules(CamelModel):
    """导入时的表过滤规则。"""

    # 是否包含临时表（tmp_/temp_/# 前缀）；默认 False 即排除。
    include_temp_tables: bool = False
    # 表名黑名单正则（大小写不敏感，re.search 语义）。
    #
    # 由客户端经 ImportPreviewRequest.rules.tableFilter 提供；缺省时回落到
    # DEFAULT_TABLE_NAME_BLACKLIST_PATTERNS。ReDoS 防护：字段校验器
    # _validateNameBlacklistPatterns 在 DTO 边界封顶数量/长度并做 re.compile 校验，
    # 非法 pattern 直接 400 拒绝，避免灾难性回溯与未捕获 re.error 导致的 500。
    name_blacklist_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TABLE_NAME_BLACKLIST_PATTERNS)
    )

    _check_blacklist = field_validator("name_blacklist_patterns")(_validateNameBlacklistPatterns)


class TypeMappingRules(CamelModel):
    """导入时的 DB 类型 -> DataType 映射规则。"""

    # 规范化后的 DB 类型（大写、无参数）-> DataType 枚举值。
    mappings: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TYPE_MAPPINGS))


class LlmEnhanceOptions(CamelModel):
    """本地导入时 LLM 语义增强选项。"""

    generate_aliases: bool = True
    generate_descriptions: bool = True
    detect_enums: bool = True
    suggest_filters: bool = True


class JoinInferenceRules(CamelModel):
    """关联关系推断开关。

    - infer_declared_fk：按数据字典声明的外键生成 join（PG/MySQL/Oracle 声明 FK）。
    - infer_name_convention：按列名约定推断 Sage X3（THBI）引用边。THBI 不声明
      任何 FK/PK 约束，schema 缓存 primary_keys/foreign_keys 为空，只有列名约定
      能还原主数据/单据头引用（ITMREF_0 → ITMMASTER 等）。对无约定列的小写库
      无匹配，保持空。注册表见 services/join_inference.py。
    """

    infer_declared_fk: bool = True
    infer_name_convention: bool = True


class ImportRuleConfig(CamelModel):
    """本地导入规则配置：表过滤 + 类型映射 + LLM 增强 + 关联推断。"""

    table_filter: TableFilterRules = Field(default_factory=TableFilterRules)
    type_mapping: TypeMappingRules = Field(default_factory=TypeMappingRules)
    llm_enhance_options: LlmEnhanceOptions = Field(default_factory=LlmEnhanceOptions)
    join_inference: JoinInferenceRules = Field(default_factory=JoinInferenceRules)


class ConflictType(StrEnum):
    """导入冲突类型：类（表级）或属性（列级）。"""

    CLASS = "class"
    PROPERTY = "property"


class ImportConflict(CamelModel):
    """本地导入时，建议的类/属性与既有本体的冲突。"""

    type: ConflictType = Field(..., description="冲突类型：class | property")
    source_table: str | None = Field(default=None, description="冲突涉及的源表名")
    source_column: str | None = Field(default=None, description="冲突涉及的源列名（仅属性冲突）")
    existing_id: int = Field(..., description="既有本体类/属性的 id")
    existing_name: str | None = Field(default=None, description="既有本体类/属性名称")
    proposed_name: str | None = Field(default=None, description="建议的类/属性名称")
    # 处置动作：skip（默认，保留既有）| overwrite（覆盖）| rename（改名新建）
    action: str = "skip"


class ProposedProperty(CamelModel):
    """本地导入建议的属性。"""

    source_column: str
    property_name: str
    property_alias: str | None = None
    description: str | None = None
    data_type: str
    is_primary_key: bool = False
    is_foreign_key: bool = False
    enum_values: list[str] | None = None


class ProposedClass(CamelModel):
    """本地导入建议的类。"""

    source_table: str
    class_name: str
    class_alias: str | None = None
    description: str | None = None
    properties: list[ProposedProperty] = Field(default_factory=list)
    is_selected: bool = True


class ProposedJoin(CamelModel):
    """本地导入建议的关联关系。"""

    source_table: str
    source_columns: list[str]
    target_table: str
    target_columns: list[str]
    join_type: str = "INNER"
    relation_type: str = "foreign_key"
    is_selected: bool = True
    # 推断来源，供前端预览标注：declared_fk | name_convention；旧响应为 None。
    inferred_by: str | None = Field(
        default=None,
        description="关联推断来源：declared_fk（声明外键）| name_convention（列名约定）",
    )


class ImportPreviewRequest(CamelModel):
    """本地导入预览请求。"""

    rules: ImportRuleConfig = Field(default_factory=ImportRuleConfig)
    # 表名白名单：非空时预览只返回命中该列表的表（用于超大 schema 分批导入）。
    # 与 rules.table_filter 是 AND 关系：先按规则过滤，再仅保留白名单命中的表。
    selected_tables: list[str] | None = None
    # 可选：按表限制参与导入的属性列子集 {表名: [列名]}；省略表示全列。
    # 单表「只导入部分属性」场景用（列名匹配大小写不敏感，未知列忽略）。
    selected_columns: dict[str, list[str]] | None = Field(
        default=None,
        description="按表限制预览的属性列子集，如 {\"PORDERQ\": [\"POHNUM_0\", \"QTYUOM_0\"]}",
    )
    # 可选：内省目标 Oracle owner 命名空间（如 THBI）；缺省取连接用户默认 owner。
    # 仅影响表来源（schema 缓存键）；本体 source_table 仍存裸表名。
    # 字段名取 schema_name 避免与 BaseModel/CamelModel 的 schema 属性冲突；
    # JSON 契约仍为 schema（显式 alias 覆盖 camelCase 生成器）。
    schema_name: str | None = Field(
        default=None,
        alias="schema",
        description="要预览的 Oracle owner 命名空间（如 THBI）；缺省连接用户默认 owner",
    )


class FilterSuggestions(CamelModel):
    """本地导入的表过滤建议。"""

    recommended_blacklist_patterns: list[str] = Field(default_factory=list)
    excluded_tables: list[str] = Field(default_factory=list)


class LlmUsageInfo(CamelModel):
    """本地导入 LLM 用量信息。"""

    model_name: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ImportPreviewResponse(CamelModel):
    """本地导入预览响应。"""

    datasource_id: int
    proposed_classes: list[ProposedClass] = Field(default_factory=list)
    proposed_joins: list[ProposedJoin] = Field(default_factory=list)
    conflicts: list[ImportConflict] = Field(default_factory=list)
    filter_suggestions: FilterSuggestions = Field(default_factory=FilterSuggestions)
    llm_usage: LlmUsageInfo = Field(default_factory=LlmUsageInfo)


class ConflictResolution(CamelModel):
    """本地导入冲突处置。"""

    type: str
    existing_id: int
    action: str  # skip | overwrite | rename
    new_name: str | None = None


class ImportExecuteRequest(CamelModel):
    """本地导入执行请求。"""

    confirmed_classes: list[ProposedClass]
    confirmed_joins: list[ProposedJoin]
    conflict_resolutions: list[ConflictResolution] = Field(default_factory=list)
    sync_embeddings: bool = True


class ImportErrorInfo(CamelModel):
    """本地导入执行错误项。"""

    type: str
    name: str
    message: str


class ImportExecuteResponse(CamelModel):
    """本地导入执行响应。"""

    success: bool = True
    created_classes: int = 0
    created_properties: int = 0
    created_joins: int = 0
    skipped_conflicts: int = 0
    overwritten_conflicts: int = 0
    errors: list[ImportErrorInfo] = Field(default_factory=list)


class MissingColumnRead(CamelModel):
    """本体系引用的、在当前数据源实际 schema 中已不存在的字段。"""

    table: str = Field(..., description="本体系引用的表（source_table）")
    column: str = Field(..., description="本体系引用的列（source_column）")


class OntologyDriftReport(CamelModel):
    """本体表/列 vs 数据源 schema 缓存的漂移报告（2-4）。

    表本身缺失时只计入 missing_tables，不重复计入 missing_columns；
    missing_columns 仅针对"表存在但列缺失"。schema_cached=false 表示尚未 introspect，
    报告为空不代表干净（无实际 schema 可对照）。
    """

    datasource_id: int = Field(..., description="数据源 id")
    has_drift: bool = Field(..., description="是否存在表/列漂移")
    schema_cached: bool = Field(..., description="该数据源是否已有 schema 缓存可对照")
    checked_tables: int = Field(..., description="被校验的本体系引用表数量（有 source_table 的类）")
    missing_tables: list[str] = Field(default_factory=list, description="已不存在的表")
    missing_columns: list[MissingColumnRead] = Field(default_factory=list, description="已不存在的字段")


# ===== NL2SQL 术语字典 =====


class TermDictionaryCreate(CamelModel):
    term: str = Field(..., min_length=1, max_length=100)
    definition: str = Field(..., min_length=1)
    mapped_class_name: str | None = Field(default=None, max_length=100)
    mapped_property_name: str | None = Field(default=None, max_length=100)
    formula_hint: str | None = None


class TermDictionaryRead(CamelModel):
    id: int
    term: str
    definition: str
    mapped_class_name: str | None = None
    mapped_property_name: str | None = None
    formula_hint: str | None = None
    created_by: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


# ===== Chat / NL2SQL (Phase 4) =====


class HistoryMessage(CamelModel):
    """历史对话消息（客户端回传最近若干轮）。"""

    role: str = Field(..., description="user | assistant")
    content: str


class ChatRequest(CamelModel):
    sessionId: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1, max_length=2000)
    datasourceId: int
    history: list[HistoryMessage] = Field(default_factory=list, description=MSG_SCHEMA_CHAT_HISTORY)
    # 指定使用的模型 ID；None 表示自动（由 router 根据问题内容路由）
    modelId: int | None = Field(default=None, description=MSG_SCHEMA_CHAT_MODEL_ID)
    # 用户显式指定的图表类型；None 表示由系统按数据形状自动推荐
    chartType: ChartType | None = Field(default=None, description=MSG_SCHEMA_CHAT_CHART_TYPE_EXPLICIT)


class DocQaRequest(CamelModel):
    """文档问答请求（与 ChatRequest 解耦）。"""

    session_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1)
    top_k: int = Field(default=8, ge=1, le=20)
    security_level: str | None = None
    document_type: str | None = None
    model_id: int | None = None


class ExtractedEntities(CamelModel):
    """从用户问题中抽取的结构化查询实体（best-effort，任一字段可为空）。"""

    dimension: str | None = Field(default=None, description=MSG_SCHEMA_CHAT_DIMENSION)
    metric: str | None = Field(default=None, description=MSG_SCHEMA_CHAT_METRIC)
    chartType: ChartType | None = Field(default=None, description=MSG_SCHEMA_CHAT_CHART_TYPE_EXTRACTED)


class StepResultRead(CamelModel):
    """多步查询中单个子步骤的执行结果（响应 + 流式事件共享）。"""

    step_index: int
    description: str
    sub_question: str
    sql: str | None = None
    data: list[dict] | None = None
    summary: str = ""
    error: str | None = None


class AgentSuggestion(CamelModel):
    """Chat 未指名 Agent 建议卡片（Phase 7 G4）。

    中置信（0.4 ≤ confidence < 0.7）语义路由命中时，随 QUERY/NEW_QUERY 响应
    附带，前端渲染「建议使用 X Agent」卡片；高置信（≥ 0.7）直接 AGENT_RUN，
    不附带此字段。
    """

    recommended_agent_code: str
    confidence: float
    reason: str


class ChatResponse(CamelModel):
    answer: str
    intent: str = Field(
        ...,
        description="query | chitchat | refine | follow_up | new_query | clarify | define | map | metric | multi_step",
    )
    sql: str | None = None
    queryPlan: dict | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_QUERY_PLAN
    )
    chartType: ChartType | None = None
    chartOption: dict | None = None
    data: list[dict] | None = None
    tokensUsed: int = 0
    cost: float = 0.0
    modelName: str | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_SERVED_MODEL
    )
    extractedEntities: ExtractedEntities | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_QUERY_ENTITIES
    )
    # 会话亲和性（Phase 7）：仅在 turnCount < N 且 priorModelId 有值时返回；解锁/闲聊/领域命令为 None
    affinityStatus: AffinityStatus | None = Field(
        default=None,
        description=MSG_SCHEMA_CHAT_AFFINITY,
    )
    # 多步查询：各子步骤执行结果（仅 intent=multi_step 时填充；单步查询为 None）
    steps: list[StepResultRead] | None = Field(
        default=None,
        description="多步查询各子步骤执行结果",
    )
    # Phase 1.4 数据可信度 badge：每张 selectedClass 对应一条；无 selectedClasses 或
    # DQ 服务降级时为 None；存在 selectedClasses 但部分表未评估时，相应 badge 的
    # evaluated=False、其余分数字段全 None。
    data_quality: list[DataQualityBadge] | None = Field(
        default=None,
        description=MSG_SCHEMA_CHAT_DQ_BADGES,
    )
    # Phase 5.3：供应商 360° 视图（仅 intent=supplier_360 时填充；前端按字段存在性路由）
    supplier360: Supplier360Read | None = Field(
        default=None,
        description=MSG_SCHEMA_CHAT_SUPPLIER_360,
    )
    # Phase 5.4：供应商风险 Agent（仅 intent=supplier_risk 时填充；前端按字段存在性路由）
    supplier_risk: SupplierRiskRead | None = Field(
        default=None,
        description=MSG_SCHEMA_CHAT_SUPPLIER_RISK,
    )
    # Phase 6.3：知识图谱多跳推理（仅 intent=graph_reasoning 时填充；前端按字段存在性路由）
    graph_traversal: GraphTraversalRead | None = Field(
        default=None,
        description=MSG_SCHEMA_GRAPH_TRAVERSAL,
    )
    # Phase 6.4：Agent 运行时执行结果（仅 intent=agent_run 时填充；前端按字段存在性路由）
    agent_run: AgentRunRead | None = Field(
        default=None,
        description=MSG_SCHEMA_CHAT_AGENT_RUN,
    )
    # Phase 7 G4：未指名 Agent 语义路由建议卡片（仅中置信命中时随 QUERY/NEW_QUERY
    # 附带；高置信直接 intent=agent_run，低置信无此字段；前端按字段存在性渲染）
    suggested_agent: AgentSuggestion | None = Field(
        default=None,
        description="中置信语义路由建议卡片：推荐执行某个 Agent（推荐编码 + 置信度 + 理由）",
    )
    # Phase 1.4：L1 KPI 语义匹配命中结果（仅 intent=l1_match 时填充；前端按字段存在性渲染）
    kpi_code: str | None = Field(
        default=None,
        description="L1 命中的 KPI code",
    )
    kpi_name: str | None = Field(
        default=None,
        description="L1 命中的 KPI 名称",
    )
    confidence: float | None = Field(
        default=None,
        description="L1 匹配置信度（精确 alias=1.0，关键词 Jaccard∈(0,1]）",
    )


class DataQualityBadge(CamelModel):
    """Chat UI 数据可信度 badge（精简版，不暴露 6 维明细）。

    与 DataQualityScoreRead 的关系：
    - DataQualityScoreRead 是后端 /scores 接口的完整历史记录（含 id、6 维、duration）。
    - DataQualityBadge 是 chat 流式响应中嵌入的精简版（仅 overall_score + 时间戳），
      避免 chat payload 膨胀；明细按需前端拉 /scores?table=X。
    """

    target_table: str = Field(..., description=MSG_SCHEMA_CHAT_DQ_BADGE_TARGET_TABLE)
    overall_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_DQ_BADGE_OVERALL
    )
    evaluated_at: datetime | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_DQ_BADGE_EVALUATED_AT
    )
    rules_count: int | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_DQ_BADGE_RULES_COUNT
    )
    evaluated: bool = Field(
        ..., description=MSG_SCHEMA_CHAT_DQ_BADGE_EVALUATED
    )


class AffinityStatus(CamelModel):
    """会话亲和性：前 N 轮锁定的模型名 + 剩余轮数。

    前端根据此状态渲染 badge（🔒 锁定 X · 剩 N 轮）。
    """

    lockedModel: str = Field(..., description=MSG_SCHEMA_CHAT_LOCKED_MODEL)
    remainingTurns: int = Field(..., ge=0, description=MSG_SCHEMA_CHAT_REMAINING_TURNS)


# ===== 相似问答建议（Phase 5）=====


class QuerySuggestRequest(CamelModel):
    question: str = Field(..., min_length=1, max_length=2000, description=MSG_SCHEMA_CHAT_QUESTION)
    datasourceId: int | None = Field(default=None, description=MSG_SCHEMA_CHAT_DATASOURCE_ID)


class SimilarQuery(CamelModel):
    question: str = Field(..., description=MSG_SCHEMA_SIMILAR_QUESTION)
    sql: str | None = Field(default=None, description=MSG_SCHEMA_SIMILAR_SQL)
    similarity: float = Field(default=0.0, description=MSG_SCHEMA_SIMILAR_SIMILARITY)


class QuerySuggestResponse(CamelModel):
    suggestions: list[SimilarQuery] = Field(default_factory=list, description=MSG_SCHEMA_SIMILAR_SUGGESTIONS)


class HealthResponse(CamelModel):
    status: str = "ok"
    version: str
    app_env: str


# ===== 服务状态监控 =====


class ServiceStatus(CamelModel):
    """单个依赖服务的健康状态（服务状态看板的数据项）。

    status 取值：up | down | not_configured（仅 embedding 可能出现未配置）。
    endpoint 为已脱敏端点（数据库隐藏密码、Neo4j 去除内嵌凭据）。
    """

    name: str = Field(..., description="postgresql | neo4j | milvus | embedding")
    status: str = Field(..., description="up | down | not_configured")
    latency_ms: int | None = Field(default=None, description="探测往返耗时（毫秒）")
    endpoint: str | None = Field(default=None, description="已脱敏的端点")
    detail: str | None = Field(default=None, description="down 时的错误信息")


class ServiceStatusResponse(CamelModel):
    """全部依赖服务的状态汇总。注意顶层不使用 success 字段，避免前端信封误解析。"""

    services: list[ServiceStatus] = Field(default_factory=list)
    checked_at: datetime = Field(..., description="探测完成时间")


class ErrorResponse(CamelModel):
    success: bool = False
    error: str
    detail: str | None = None
    # Phase 6.5：结构化错误数据（如供应商名歧义候选列表）；仅部分 ValidationError 携带
    details: dict | None = None


# ===== 聊天会话历史（右侧历史面板）=====


class ChatSessionListItem(CamelModel):
    """聊天语义会话列表项（按 session_message 聚合）。

    与 SessionListItem（用量看板）解耦：前者按业务消息维度聚合，后者按 Token 流水。
    """

    session_id: str = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_SESSION_ID)
    first_time: datetime = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_FIRST_TIME)
    last_time: datetime = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_LAST_TIME)
    message_count: int = Field(..., ge=0, description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_COUNT)
    last_question: str | None = Field(default=None, description=MSG_SCHEMA_CHAT_HISTORY_LAST_QUESTION)
    last_answer_preview: str | None = Field(
        default=None, description=MSG_SCHEMA_CHAT_HISTORY_LAST_ANSWER_PREVIEW
    )


class ChatMessageRead(CamelModel):
    """单条历史消息（user + assistant 共享），按时间正序返回。

    不复用 StepResultRead：后者粒度为子步骤（含 step_index/description/sub_question），
    本 DTO 是完整对话流中的单条消息。
    """

    id: int = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_ID)
    role: str = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_ROLE)
    content: str = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_CONTENT)
    question: str | None = Field(default=None, description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_QUESTION)
    sql: str | None = Field(default=None, description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_SQL)
    created_time: datetime = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_MESSAGE_CREATED_TIME)


class SessionMessagesResponse(CamelModel):
    """会话消息流响应包装：包含 session_id 便于前端切换时校验。"""

    session_id: str = Field(..., description=MSG_SCHEMA_CHAT_HISTORY_SESSION_ID)
    messages: list[ChatMessageRead] = Field(
        default_factory=list, description=MSG_SCHEMA_CHAT_HISTORY_MESSAGES
    )


# ===== 数据质量规则（Phase 1.1） =====


class DataQualityRuleCreate(CamelModel):
    """创建数据质量规则的请求体。"""

    rule_name: str = Field(..., min_length=1, max_length=100, description=MSG_SCHEMA_DQ_RULE_NAME)
    rule_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description=MSG_SCHEMA_DQ_RULE_CODE,
    )
    datasource_id: int = Field(..., gt=0, description=MSG_SCHEMA_DQ_DATASOURCE_ID)
    target_table: str = Field(..., min_length=1, max_length=100, description=MSG_SCHEMA_DQ_TARGET_TABLE)
    target_column: str | None = Field(default=None, max_length=100, description=MSG_SCHEMA_DQ_TARGET_COLUMN)
    rule_type: RuleType = Field(..., description=MSG_SCHEMA_DQ_RULE_TYPE)
    rule_expression: str | None = Field(default=None, description=MSG_SCHEMA_DQ_RULE_EXPRESSION)
    threshold: Decimal = Field(default=Decimal("95.00"), ge=0, le=100, description=MSG_SCHEMA_DQ_THRESHOLD)
    severity: Severity = Field(default=Severity.MEDIUM, description=MSG_SCHEMA_DQ_SEVERITY)
    is_enabled: bool = Field(default=True, description=MSG_SCHEMA_DQ_IS_ENABLED)
    version: str = Field(default="v1.0", max_length=20, description=MSG_SCHEMA_DQ_VERSION)
    description: str | None = Field(default=None, description=MSG_SCHEMA_DQ_DESCRIPTION)


class DataQualityRuleUpdate(CamelModel):
    """更新数据质量规则的请求体。rule_code 不允许修改。"""

    rule_name: str | None = Field(default=None, min_length=1, max_length=100)
    datasource_id: int | None = Field(default=None, gt=0)
    target_table: str | None = Field(default=None, min_length=1, max_length=100)
    target_column: str | None = Field(default=None, max_length=100)
    rule_type: RuleType | None = None
    rule_expression: str | None = None
    threshold: Decimal | None = Field(default=None, ge=0, le=100)
    severity: Severity | None = None
    is_enabled: bool | None = None
    version: str | None = Field(default=None, max_length=20)
    description: str | None = None


class DataQualityRuleRead(CamelModel):
    """数据质量规则响应。"""

    id: int
    rule_name: str
    rule_code: str
    datasource_id: int
    target_table: str
    target_column: str | None = None
    rule_type: RuleType
    rule_expression: str | None = None
    threshold: Decimal
    severity: Severity
    is_enabled: bool
    version: str
    owner: str | None = None
    description: str | None = None
    source_class_id: int | None = None
    source_property_id: int | None = None
    derivation_type: DerivationType
    created_time: datetime | None = None
    updated_time: datetime | None = None


# ===== Phase 1.2 数据质量评估执行 =====


class EvaluationResult(CamelModel):
    """单条规则的评估结果。

    status=PASS 时表示 pass_rate ≥ rule.threshold；FAIL 反之。
    message 包含错误信息（如 SQL 校验失败、目标表不存在等）。
    """

    rule_id: int = Field(..., description=MSG_SCHEMA_DQ_EVAL_RULE_ID)
    rule_code: str = Field(..., description=MSG_SCHEMA_DQ_EVAL_RULE_CODE)
    rule_type: RuleType = Field(..., description=MSG_SCHEMA_DQ_EVAL_RULE_TYPE)
    datasource_id: int | None = Field(default=None, description=MSG_SCHEMA_DQ_EVAL_DATASOURCE_ID)
    total_count: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_EVAL_TOTAL_COUNT)
    passed_count: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_EVAL_PASSED_COUNT)
    pass_rate: float = Field(default=0.0, ge=0.0, description=MSG_SCHEMA_DQ_EVAL_PASS_RATE)
    status: str = Field(default="FAIL", description=MSG_SCHEMA_DQ_EVAL_STATUS)
    evaluated_at: datetime = Field(..., description=MSG_SCHEMA_DQ_EVAL_EVALUATED_AT)
    duration_ms: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_EVAL_DURATION_MS)
    message: str | None = Field(default=None, description=MSG_SCHEMA_DQ_EVAL_MESSAGE)


class EvaluateBatchRequest(CamelModel):
    """批量评估请求体。"""

    rule_ids: list[int] = Field(default_factory=list, description=MSG_SCHEMA_DQ_EVAL_RULE_IDS)


class EvaluateBatchResponse(CamelModel):
    """批量评估响应：results + summary。"""

    results: list[EvaluationResult] = Field(
        default_factory=list, description=MSG_SCHEMA_DQ_EVAL_RESULTS
    )
    summary_total: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_EVAL_SUMMARY_TOTAL)
    summary_passed: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_EVAL_SUMMARY_PASSED)


# ===== Phase 1.3 数据质量评分 =====


class DataQualityScoreRead(CamelModel):
    """数据质量评分响应（一条历史记录）。"""

    id: int = Field(..., description=MSG_SCHEMA_DQ_SCORE_ID)
    target_table: str = Field(..., description=MSG_SCHEMA_DQ_SCORE_TARGET_TABLE)
    score_type: ScoreType = Field(..., description=MSG_SCHEMA_DQ_SCORE_TYPE)
    completeness_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_COMPLETENESS
    )
    validity_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_VALIDITY
    )
    uniqueness_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_UNIQUENESS
    )
    consistency_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_CONSISTENCY
    )
    timeliness_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_TIMELINESS
    )
    referential_score: Decimal | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_REFERENTIAL
    )
    overall_score: Decimal = Field(..., description=MSG_SCHEMA_DQ_SCORE_OVERALL)
    evaluated_at: datetime = Field(..., description=MSG_SCHEMA_DQ_SCORE_EVALUATED_AT)
    evaluation_duration_ms: int = Field(
        default=0, ge=0, description=MSG_SCHEMA_DQ_SCORE_DURATION_MS
    )
    rules_count: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_SCORE_RULES_COUNT)
    created_time: datetime | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_CREATED_TIME
    )
    updated_time: datetime | None = Field(
        default=None, description=MSG_SCHEMA_DQ_SCORE_UPDATED_TIME
    )


class ComputeScoresResponse(CamelModel):
    """触发 compute 后的响应：聚合结果 + 落库条数 + 总耗时。"""

    evaluated_rules: int = Field(
        default=0, ge=0, description=MSG_SCHEMA_DQ_COMPUTE_EVALUATED_RULES
    )
    saved_scores: int = Field(
        default=0, ge=0, description=MSG_SCHEMA_DQ_COMPUTE_SAVED_SCORES
    )
    duration_ms: int = Field(default=0, ge=0, description=MSG_SCHEMA_DQ_COMPUTE_DURATION_MS)
    scores: list[DataQualityScoreRead] = Field(
        default_factory=list, description=MSG_SCHEMA_DQ_COMPUTE_SCORES
    )


# ===== 数据血缘（Phase 2.1）=====


class LineageEdgeCreate(CamelModel):
    """创建数据血缘边的请求体。

    source_field / target_field 可空（表级血缘）；不为空时表示字段级血缘。
    """

    source_layer: LineageLayer = Field(..., description=MSG_SCHEMA_LINEAGE_SOURCE_LAYER)
    source_system: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_LINEAGE_SOURCE_SYSTEM
    )
    source_object: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_LINEAGE_SOURCE_OBJECT
    )
    source_field: str | None = Field(
        default=None, max_length=100, description=MSG_SCHEMA_LINEAGE_SOURCE_FIELD
    )
    target_layer: LineageLayer = Field(..., description=MSG_SCHEMA_LINEAGE_TARGET_LAYER)
    target_system: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_LINEAGE_TARGET_SYSTEM
    )
    target_object: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_LINEAGE_TARGET_OBJECT
    )
    target_field: str | None = Field(
        default=None, max_length=100, description=MSG_SCHEMA_LINEAGE_TARGET_FIELD
    )
    transformation_rule: str | None = Field(
        default=None, description=MSG_SCHEMA_LINEAGE_TRANSFORMATION
    )
    refresh_frequency: RefreshFrequency = Field(
        default=RefreshFrequency.DAILY, description=MSG_SCHEMA_LINEAGE_REFRESH_FREQ
    )
    owner: str | None = Field(default=None, max_length=100, description=MSG_SCHEMA_LINEAGE_OWNER)
    description: str | None = Field(default=None, description=MSG_SCHEMA_LINEAGE_DESCRIPTION)


class LineageEdgeUpdate(CamelModel):
    """更新血缘边的请求体。所有字段可选，None 视为不动。"""

    transformation_rule: str | None = Field(default=None)
    refresh_frequency: RefreshFrequency | None = Field(default=None)
    owner: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None)
    is_active: bool | None = Field(default=None)


class LineageEdgeRead(CamelModel):
    """血缘边响应。"""

    id: int
    source_layer: LineageLayer
    source_system: str
    source_object: str
    source_field: str | None = None
    target_layer: LineageLayer
    target_system: str
    target_object: str
    target_field: str | None = None
    transformation_rule: str | None = None
    refresh_frequency: RefreshFrequency
    owner: str | None = None
    description: str | None = None
    is_active: bool
    created_time: datetime | None = Field(default=None, description=MSG_SCHEMA_LINEAGE_CREATED_TIME)
    updated_time: datetime | None = Field(default=None, description=MSG_SCHEMA_LINEAGE_UPDATED_TIME)


class LineageExtractResult(CamelModel):
    """自动抽取血缘的结果。

    来源与 scripts/lineage_auto_extract.py 一致（OntologyJoin + OntologyMetric.formula
    + schema introspection）；created = 本次实际写入 data_lineage 的新增边数
    （幂等：重复调用返回 0）。
    """

    created: int = Field(..., description="本次实际写入 data_lineage 的新增血缘边数")


# ===== 跨系统编码映射（Phase 3.1）=====


class EntityMappingCreate(CamelModel):
    """创建跨系统编码映射的请求体。

    enterprise_key / enterprise_code 为企业侧统一标识；source_key / source_code
    为源系统侧原始标识。同一 (entity_type, enterprise_key, source_system) 不允许重复。
    """

    entity_type: BusinessObjectCodeType = Field(..., description=MSG_SCHEMA_ENTITY_MAPPING_ENTITY_TYPE)
    enterprise_key: int = Field(
        ...,
        gt=0,
        le=2**63 - 1,
        description=MSG_SCHEMA_ENTITY_MAPPING_ENTERPRISE_KEY,
    )
    enterprise_code: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_ENTITY_MAPPING_ENTERPRISE_CODE
    )
    source_system: SourceSystem = Field(
        ..., description=MSG_SCHEMA_ENTITY_MAPPING_SOURCE_SYSTEM
    )
    source_key: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_ENTITY_MAPPING_SOURCE_KEY
    )
    source_code: str = Field(
        ..., min_length=1, max_length=100, description=MSG_SCHEMA_ENTITY_MAPPING_SOURCE_CODE
    )
    match_rule: MatchRule = Field(
        default=MatchRule.MAPPING, description=MSG_SCHEMA_ENTITY_MAPPING_MATCH_RULE
    )
    effective_date: date | None = Field(
        default=None, description=MSG_SCHEMA_ENTITY_MAPPING_EFFECTIVE_DATE
    )
    expiry_date: date | None = Field(
        default=None, description=MSG_SCHEMA_ENTITY_MAPPING_EXPIRY_DATE
    )


class EntityMappingUpdate(CamelModel):
    """更新编码映射的请求体。所有字段可选，None 视为不动。

    非空列（enterprise_code / source_key / source_code / match_rule）不接受
    空串（min_length=1）且不可置 None；日期列允许显式 null 以清除。
    """

    enterprise_code: str | None = Field(default=None, min_length=1, max_length=100)
    source_key: str | None = Field(default=None, min_length=1, max_length=100)
    source_code: str | None = Field(default=None, min_length=1, max_length=100)
    match_rule: MatchRule | None = Field(default=None)
    effective_date: date | None = Field(default=None)
    expiry_date: date | None = Field(default=None)


class EntityMappingRead(CamelModel):
    """编码映射响应。"""

    id: int
    entity_type: BusinessObjectCodeType
    enterprise_key: int
    enterprise_code: str
    source_system: SourceSystem
    source_key: str
    source_code: str
    match_rule: MatchRule
    effective_date: date | None = None
    expiry_date: date | None = None
    owner: str | None = None
    created_time: datetime | None = Field(
        default=None, description=MSG_SCHEMA_ENTITY_MAPPING_CREATED_TIME
    )
    updated_time: datetime | None = Field(
        default=None, description=MSG_SCHEMA_ENTITY_MAPPING_UPDATED_TIME
    )


class EntityMappingSearchHit(CamelModel):
    """编码映射搜索结果（Phase 6.x AutoComplete 用）。

    比 EntityMappingRead 轻量：仅含 AutoComplete 下拉需要展示 + 解析的字段，
    不暴露 source_key/match_rule/owner/有效期 等治理字段（防止 PII / 越权侧信道）。
    """

    id: int
    entity_type: BusinessObjectCodeType
    enterprise_key: int
    enterprise_code: str
    source_system: SourceSystem
    source_code: str
    # Phase 6.x 扩展：业务名（供应商 supplier_name / 物料 description_1），下拉直接展示
    name: str | None = None


# ---------------------------------------------------------------------------
# Phase 4.5: audit_log 查询 API
# ---------------------------------------------------------------------------


class AuditLogRead(CamelModel):
    """audit_log 单条记录响应（Phase 4.5 治理 API）。"""
    id: int
    entity_type: str
    entity_id: int
    action: str
    actor: str
    actor_departments: str | None = None
    before_json: dict | None = None
    after_json: dict | None = None
    created_at: datetime


class AuditLogPage(CamelModel):
    """审计日志分页响应（feat-audit-history-api Task 1）。

    rows 为当前页 AuditLogRead 列表，total 为满足过滤条件的总条数
    （独立于 limit/offset，前端用于分页渲染）。

    替代 Phase 4.5 的「裸 list[AuditLogRead]」响应，避免前端拉全表
    后再客户端分页（N+1 风险 + 大结果集 JSON 序列化成本）。
    """
    rows: list["AuditLogRead"]
    total: int


# ---------------------------------------------------------------------------
# Phase 4.5: history 回放 API
# ---------------------------------------------------------------------------


class KpiCatalogHistoryRead(CamelModel):
    """KPI 历史快照记录（Phase 4.5 回放 API）。"""
    id: int
    kpi_id: int | None = None  # FK ON DELETE SET NULL
    revision: int
    snapshot_json: dict
    changed_by: str | None = None
    changed_at: datetime


# ---------------------------------------------------------------------------
# Phase 1 Task 1.6: KPI Catalog Search
# ---------------------------------------------------------------------------


class KpiSearchHit(CamelModel):
    """KPI 搜索命中结果（Phase 1.6）。

    仅含前端 AutoComplete 下拉需要展示的字段，不暴露 formula / owner 等治理字段。
    """

    kpi_code: str
    kpi_name: str
    confidence: float


class KpiSearchResponse(CamelModel):
    """KPI 搜索响应 envelope。"""

    query: str
    results: list[KpiSearchHit]


class FeatureDefinitionHistoryRead(CamelModel):
    """FeatureDefinition 历史快照记录（Phase 4.5 回放 API）。"""
    id: int
    feature_id: int | None = None  # FK ON DELETE SET NULL
    snapshot_json: dict
    changed_by: str | None = None
    changed_at: datetime


# ---------------------------------------------------------------------------
# Phase 5.1: Document Catalog
# ---------------------------------------------------------------------------


class DocumentCreate(CamelModel):
    """创建文档的请求体（Phase 5.1）。"""
    document_id: str = Field(
        ..., min_length=1, max_length=50,
        description="业务唯一文档编号（如 DOC-2026-001）",
    )
    document_name: str = Field(..., min_length=1, max_length=255)
    document_type: DocumentType = Field(default=DocumentType.CONTRACT)
    version: str = Field(default="v1.0", max_length=20)
    owner: str | None = Field(default=None, max_length=100)
    effective_date: date | None = Field(default=None)
    security_level: DocumentSecurityLevel = Field(default=DocumentSecurityLevel.L1)
    storage_url: str | None = Field(default=None, max_length=512)
    content_hash: str | None = Field(default=None, max_length=64)


class DocumentUpdate(CamelModel):
    """更新文档的请求体（Phase 5.1）。所有字段可选。"""
    document_name: str | None = Field(default=None, max_length=255)
    document_type: DocumentType | None = Field(default=None)
    version: str | None = Field(default=None, max_length=20)
    status: DocumentStatus | None = Field(default=None)
    owner: str | None = Field(default=None, max_length=100)
    effective_date: date | None = Field(default=None)
    security_level: DocumentSecurityLevel | None = Field(default=None)
    storage_url: str | None = Field(default=None, max_length=512)
    content_hash: str | None = Field(default=None, max_length=64)


class DocumentRead(CamelModel):
    """文档响应（Phase 5.1）。"""
    id: int
    document_id: str
    document_name: str
    document_type: DocumentType
    version: str
    status: DocumentStatus
    owner: str | None = None
    effective_date: date | None = None
    security_level: DocumentSecurityLevel
    storage_url: str | None = None
    content_hash: str | None = None
    created_time: datetime
    updated_time: datetime


class DocEntityRelationCreate(CamelModel):
    """创建文档-实体关联的请求体（Phase 5.1 + entity_key VARCHAR）。"""
    document_id: str = Field(..., min_length=1, max_length=50)
    entity_type: BusinessObjectCodeType = Field(...)
    entity_key: str = Field(..., min_length=1, max_length=100)
    relation_type: DocEntityRelationType = Field(default=DocEntityRelationType.CONTRACT)


class DocEntityRelationRead(CamelModel):
    """文档-实体关联响应（Phase 5.1 + entity_key VARCHAR）。"""
    id: int
    document_id: str
    entity_type: BusinessObjectCodeType
    entity_key: str
    relation_type: DocEntityRelationType


def _normalizeDataLayer(value: str | None) -> str | None:
    """data_layer 边界归一化：strip + upper，空串 → None 通配。

    工具声明规范大写层（DIM/DWD/FEATURE…）。大小写/空白不一致会静默 fail-closed
    （看似授权实则分层匹配失败 → 403，难排查），故在写入边界统一后再比较。
    """
    if value is None:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _normalizeDataObject(value: str | None) -> str:
    """data_object 边界归一化：strip + upper；空串/None → ValueError（Pydantic 422）。

    与 _normalizeDataLayer 区别：data_object 不允许 None（ACL 主体必填），
    空串/纯空白视同未提供。
    """
    if value is None:
        raise ValueError("data_object 不能为空")
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("data_object 不能为空字符串")
    return normalized


class AgentAccessPolicyCreate(CamelModel):
    """创建 Agent 访问策略请求（Phase 6.1）。"""

    data_object: str = Field(..., min_length=1, max_length=128)
    permission: AgentPermission = Field(default=AgentPermission.READ)
    data_layer: str | None = Field(
        default=None, max_length=32, description="数据层（DIM/DWD/FEATURE…；None=跨层通配）"
    )
    notes: str | None = Field(default=None, max_length=2000)

    # data_object 必须与 data_layer 一样在边界归一化：运行时按
    # ``p.data_object == tool.data_object`` **精确比对**（工具侧的值已在
    # AgentToolAssembly.assemble 归一为 strip+upper）。少这一步，管理员填
    # "supplier " 会存成原样、策略永不命中，而写入本身毫无报错 —— 授权静默
    # 失效，最终表现成 403。AgentToolConfigCreate 早就有这个校验器，此处补齐。
    _check_data_object = field_validator("data_object")(_normalizeDataObject)
    _check_data_layer = field_validator("data_layer")(_normalizeDataLayer)


class AgentAccessPolicyUpdate(CamelModel):
    """更新 Agent 访问策略请求（Phase 6.1）。"""

    permission: AgentPermission | None = None
    data_layer: str | None = Field(
        default=None, max_length=32, description="数据层（DIM/DWD/FEATURE…；None=跨层通配）"
    )
    notes: str | None = Field(default=None, max_length=2000)

    _check_data_layer = field_validator("data_layer")(_normalizeDataLayer)


class AgentAccessPolicyRead(CamelModel):
    """Agent 访问策略响应（Phase 6.1）。"""

    id: int
    data_object: str
    permission: AgentPermission
    data_layer: str | None = None
    notes: str | None = None
    created_time: datetime | None = None


def _vocabCheckDomains(values: list[str]) -> list[str]:
    """域归一化 + 词表校验 + 去重保序；非法值抛 ValueError（Pydantic 422）。"""
    normalized = [normalizeAgentDomain(x) for x in values]
    rejected = [x for x in normalized if x not in AGENT_DATA_DOMAINS]
    if rejected:
        raise ValueError(MSG_AGENT_DOMAIN_NOT_IN_VOCAB.format(
            value=",".join(rejected), allowed=",".join(AGENT_DATA_DOMAINS)
        ))
    seen: set[str] = set()
    deduped: list[str] = []
    for x in normalized:
        if x not in seen:
            seen.add(x); deduped.append(x)
    return deduped


def _vocabCheckLayers(values: list[str]) -> list[str]:
    """层归一化 + 词表校验 + 去重保序；复用既有 `_normalizeDataLayer`（None → None 会被过滤）。"""
    normalized = [n for n in (_normalizeDataLayer(x) for x in values if x is not None) if n]
    rejected = [x for x in normalized if x not in AGENT_DATA_LAYERS]
    if rejected:
        raise ValueError(MSG_AGENT_LAYER_NOT_IN_VOCAB.format(
            value=",".join(rejected), allowed=",".join(AGENT_DATA_LAYERS)
        ))
    seen: set[str] = set()
    deduped: list[str] = []
    for x in normalized:
        if x not in seen:
            seen.add(x); deduped.append(x)
    return deduped


class AgentDefinitionCreate(CamelModel):
    """创建 Agent 注册请求（Phase 6.1）。

    owner 不在 DTO 中：由 actor.departments[0] 派生（entity_mapping 同模式），
    防止 client 任意声明 owner 越权；actor.departments 为空 → owner=None →
    仅 admin 可改。
    """

    agent_code: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description=MSG_SCHEMA_AGENT_CODE,
    )
    agent_name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=8000)
    trigger_type: AgentTriggerType = Field(default=AgentTriggerType.USER_QUESTION)
    response_latency: AgentResponseLatency = Field(
        default=AgentResponseLatency.REALTIME
    )
    data_domains: list[str] = Field(default_factory=list)
    data_layers: list[str] = Field(default_factory=list)
    status: AgentStatus = Field(default=AgentStatus.DRAFT)
    version: str | None = Field(default=None, max_length=32)
    tool_name: str | None = Field(default=None, max_length=64)
    tool_name_updated_at: datetime | None = Field(default=None)
    policies: list[AgentAccessPolicyCreate] = Field(default_factory=list)

    _check_domains = field_validator("data_domains")(_vocabCheckDomains)
    _check_layers = field_validator("data_layers")(_vocabCheckLayers)

    @field_validator("tool_name")
    @classmethod
    def _validateToolName(cls, v, info):
        return _validateToolNameShared(v, info)


class AgentDefinitionUpdate(CamelModel):
    """更新 Agent 注册请求（Phase 6.1）。

    全部字段可选；exclude_unset 模式下未传字段不动。policies 不在此处更新
    （走独立 /policies 子端点，便于事务粒度更细）。
    """

    agent_name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=8000)
    trigger_type: AgentTriggerType | None = None
    response_latency: AgentResponseLatency | None = None
    data_domains: list[str] | None = None
    data_layers: list[str] | None = None
    status: AgentStatus | None = None
    version: str | None = Field(default=None, max_length=32)
    tool_name: str | None = Field(default=None, max_length=64)
    tool_name_updated_at: datetime | None = Field(default=None)

    @field_validator("data_domains")
    @classmethod
    def _check_domains_update(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _vocabCheckDomains(v)

    @field_validator("data_layers")
    @classmethod
    def _check_layers_update(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _vocabCheckLayers(v)

    @field_validator("tool_name")
    @classmethod
    def _validateToolName(cls, v, info):
        return _validateToolNameShared(v, info)


class AgentDefinitionRead(CamelModel):
    """Agent 注册响应（Phase 6.1）。

    Phase 6.4 扩展：新增 ``runnable`` 派生字段（SSOT），表示该 Agent
    是否可通过 ``POST /agents/{code}/run`` 调用。判定：
    ``status == ACTIVE and cache has tool_name binding``。
    仅注册元数据、未绑定工具的 Agent（如 SUPPLIER_OTD_REPORT /
    PROCUREMENT_COPILOT）即使 status=active，runnable=False。
    """

    id: int
    agent_code: str
    agent_name: str
    description: str | None = None
    trigger_type: AgentTriggerType
    response_latency: AgentResponseLatency
    data_domains: list[str]
    data_layers: list[str]
    status: AgentStatus
    owner: str | None = None
    version: str
    policies: list[AgentAccessPolicyRead] = Field(default_factory=list)
    created_time: datetime | None = None
    updated_time: datetime | None = None
    created_time: datetime
    tool_name: str | None = None
    tool_name_updated_at: datetime | None = None
    runnable: bool = False


class AgentToolOption(CamelModel):
    """Agent 可绑定工具的展示选项（Phase 7 feat-agent-tool-binding Task 5）。

    来自 ``agent_tool_registry.all()``；前端 tool 下拉数据源。
    ``data_object`` / ``data_layers`` 用于前端预校验（tool 选择后必须覆盖 tool 层）。
    """

    name: str
    description: str
    data_object: str
    data_layers: list[str]


class AgentOptionsRead(CamelModel):
    """Agent 编辑选项下拉数据（Phase 7 feat-agent-vocabulary + Task 5 tool binding）。

    返回 ``AGENT_DATA_DOMAINS`` / ``AGENT_DATA_LAYERS`` 词表常量 +
    ``agent_tool_registry.all()`` 工具列表（按 name 排序），由
    ``app.domain.agent_vocabulary`` / ``app.services.agent_tools`` 模块分别提供。
    字段名 ``domains`` / ``layers`` 是顶层 options，不带 ``data_`` 前缀
    （避免与嵌套在 AgentDefinition 内的 data_domains/data_layers 混淆）。
    前端 useAgentOptions() 拉一次缓存；同时下发 tool 选项，避免前端再请求
    /agent-tools 单独接口。
    """

    domains: list[str]
    layers: list[str]
    tools: list[AgentToolOption] = Field(default_factory=list)


class AgentRunRequest(CamelModel):
    """Agent 运行请求（POST /agents/{code}/run，Phase 6.4）。"""

    input: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description=MSG_SCHEMA_AGENT_RUN_INPUT,
    )


class AgentRunRead(CamelModel):
    """Agent 运行时执行结果（Phase 6.4 feat-agent-runtime-mvp）。

    run() 成功时返回；失败（Agent 不存在 / 不可运行 / 策略拦截 / 参数缺失）
    抛领域异常（404 / 409 / 403 / 422），由全局 DomainError handler 映射。
    result 为 Tool 原始输出（JSON-serializable）；answer 为人类可读回答。
    """

    agent_code: str
    agent_name: str
    agent_owner: str | None = None
    tool: str
    result: dict
    answer: str
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    llm_model_name: str | None = None
    executed_at: datetime


class AgentScheduleCreate(CamelModel):
    """创建 Agent 定时调度（POST /agents/{code}/schedules，Phase 7 G5）。"""

    cron_expression: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=MSG_SCHEMA_SCHEDULE_CRON,
    )
    params: dict = Field(
        default_factory=dict,
        description=MSG_SCHEMA_SCHEDULE_INPUT,
    )


class AgentScheduleRead(CamelModel):
    """Agent 定时调度（Phase 7 G5）。"""

    id: int
    agent_code: str
    cron_expression: str
    params: dict
    is_active: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_time: datetime
    updated_time: datetime


class AgentRunLogRead(CamelModel):
    """Agent 定时调度执行记录（Phase 7 G5）。"""

    id: int
    schedule_id: int | None = None
    agent_code: str
    status: str
    answer: str | None = None
    error: str | None = None
    tokens_used: int = 0
    cost: float = 0.0
    actor: str
    started_at: datetime
    finished_at: datetime


# ---------------------------------------------------------------------------
# AgentToolConfig DTOs (feat-agent-tool-config-db, 2026-09-03)
# ---------------------------------------------------------------------------


class _UnsetType:
    """「未提供」哨兵值（区分 PATCH 语义中的「字段未传」与「显式置空/默认值」）。

    模式参考 entity_mapping_service / kpi_catalog_service 的 PATCH DTO：
    - 默认值 = UNSET（未提供 → 跳过）
    - 显式赋 None = 清除（仅 nullable 字段允许）
    - 显式赋非 None = 更新
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema

        return core_schema.no_info_plain_validator_function(
            lambda v: v, serialization=core_schema.plain_serializer_function_ser_schema(lambda _: None)
        )


UNSET: _UnsetType = _UnsetType()


class AgentToolConfigBase(CamelModel):
    """Agent 工具配置基础字段（Create/Read 共享；Update 由 UnsetType 区分「未提供」）。"""

    description: str | None = Field(default=None, max_length=2000)
    data_object: str = Field(min_length=1, max_length=128)
    data_layers: list[str] = Field(default_factory=list)
    input_schema: dict = Field(default_factory=dict)
    handler_kind: AgentToolHandlerKind
    handler_ref: str = Field(min_length=1, max_length=64)
    arg_extractor_kind: str = Field(default="supplier_key", max_length=64)


class AgentToolConfigCreate(AgentToolConfigBase):
    """创建 Agent 工具配置请求。"""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")

    _check_data_object = field_validator("data_object")(_normalizeDataObject)


class AgentToolConfigUpdate(CamelModel):
    """更新 Agent 工具配置请求（PATCH 语义；name 不可改，version 必传用于乐观锁）。"""

    description: _UnsetType | str | None = UNSET
    data_object: _UnsetType | str = UNSET
    data_layers: _UnsetType | list[str] = UNSET
    input_schema: _UnsetType | dict = UNSET
    handler_kind: _UnsetType | AgentToolHandlerKind = UNSET
    handler_ref: _UnsetType | str = UNSET
    arg_extractor_kind: _UnsetType | str = UNSET
    enabled: _UnsetType | bool = UNSET
    version: int  # required for optimistic lock

    @field_validator("data_object")
    @classmethod
    def _v_data_object(cls, v):
        if isinstance(v, _UnsetType):
            return v
        return _normalizeDataObject(v)


class AgentToolConfigRead(AgentToolConfigBase):
    """Agent 工具配置响应。"""

    id: int
    name: str
    version: int
    enabled: bool
    created_time: datetime
    updated_time: datetime | None


# ===== 业务对象注册表（Phase 4.4） =====


class BusinessObjectCreate(CamelModel):
    """创建业务对象 (Phase 4.4)."""

    code: BusinessObjectCodeNew = Field(...)
    name: str = Field(..., min_length=1, max_length=100)
    header_class_id: int | None = None
    graph_label: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4000)


class BusinessObjectUpdate(CamelModel):
    """更新业务对象 (code 不可改)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    header_class_id: int | None = None
    graph_label: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=4000)


class BusinessObjectRead(CamelModel):
    """业务对象响应。

    created_time / updated_time 允许缺省（与 LlmConfigRead 同模式）：DTO 也用于
    未落库的构造场景，时间戳由 TimestampMixin 在持久化时补齐。
    """

    code: BusinessObjectCodeType
    name: str
    header_class_id: int | None = None
    graph_label: str | None = None
    description: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None


# ---------------------------------------------------------------------------
# feat-feature-rule-config (Phase 9): Feature Rule DTOs (spec §9.1)
# ---------------------------------------------------------------------------


class FeatureRuleThresholdRead(CamelModel):
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None
    threshold_order: int


class FeatureRuleThresholdCreate(CamelModel):
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None = None
    threshold_order: int = 1


class FeatureRuleThresholdSuggestion(CamelModel):
    """parse-description LLM 输出（spec §7.1 + §9.1）。"""

    feature_name: str
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class FeatureRuleRead(CamelModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool
    priority: int
    policy_description: str | None
    version: int
    thresholds: list[FeatureRuleThresholdRead]
    created_time: datetime
    updated_time: datetime | None


class FeatureRuleCreate(CamelModel):
    code: str = Field(min_length=1, max_length=64)
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool = True
    priority: int = 100
    policy_description: str | None = None
    thresholds: list[FeatureRuleThresholdCreate] = Field(min_length=1)

    @field_validator("thresholds")
    @classmethod
    def _uniqueSeverities(cls, v: list[FeatureRuleThresholdCreate]) -> list[FeatureRuleThresholdCreate]:
        sevs = [t.severity.value for t in v]
        if len(sevs) != len(set(sevs)):
            raise ValueError("thresholds 内 severity 必须唯一")
        return v


class FeatureRuleUpdate(CamelModel):
    """code / data_object / data_layer / target_level / feature_name 不可变。"""

    enabled: bool | _UnsetType = UNSET
    priority: int | _UnsetType = UNSET
    policy_description: str | _UnsetType | None = UNSET
    thresholds: list[FeatureRuleThresholdCreate] | _UnsetType = UNSET
    version: int  # 必填，乐观锁


class FeatureRuleParseDescriptionRequest(CamelModel):
    data_object: str
    data_layer: str
    target_level: str
    natural_language: str = Field(min_length=10, max_length=4000)


class FeatureRuleParseDescriptionResponse(CamelModel):
    suggested_thresholds: list[FeatureRuleThresholdSuggestion]
    reasoning: str
    overall_confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str]


# ===========================================================================
# 数据质量规则自动生成（dq-rule-auto-generation Task 4）
# ===========================================================================


class GeneratePreviewRequest(CamelModel):
    class_id: int = Field(..., gt=0)
    datasource_id: int = Field(..., gt=0)


class RuleSuggestionRead(CamelModel):
    rule_code: str
    rule_name: str
    rule_type: RuleType
    target_table: str
    target_column: str | None = None
    rule_expression: str | None = None
    threshold: Decimal
    severity: Severity
    derivation_type: DerivationType
    source_property_id: int | None = None
    source_class_id: int | None = None
    confidence: str
    status: str  # NEW / EXISTS
    reason: str


class BlockedPropertyRead(CamelModel):
    property_name: str
    reason: str


class GeneratePreviewResponse(CamelModel):
    class_id: int
    class_name: str
    source_table: str | None = None
    datasource_id: int
    suggestions: list[RuleSuggestionRead] = Field(default_factory=list)
    blocked: list[BlockedPropertyRead] = Field(default_factory=list)


# ===========================================================================
# 数据质量规则自动生成 confirm（dq-rule-auto-generation Task 5）
# ===========================================================================


class GenerateRuleItem(CamelModel):
    """confirm 请求中单条规则条目。"""

    rule_code: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    rule_name: str = Field(..., min_length=1, max_length=100)
    target_table: str = Field(..., min_length=1, max_length=100)
    target_column: str | None = Field(default=None, max_length=100)
    rule_type: RuleType
    rule_expression: str | None = None
    threshold: Decimal = Field(default=Decimal("95.00"), ge=0, le=100)
    severity: Severity = Severity.MEDIUM
    source_class_id: int | None = None
    source_property_id: int | None = None
    derivation_type: DerivationType = DerivationType.MANUAL
    description: str | None = None


class GenerateConfirmRequest(CamelModel):
    """confirm 批量写入请求。"""

    datasource_id: int = Field(..., gt=0)
    rules: list[GenerateRuleItem] = Field(..., min_length=1, max_length=500)


class GenerateConfirmResponse(CamelModel):
    """confirm 批量写入响应。"""

    created: list[DataQualityRuleRead] = Field(default_factory=list)
    skipped_codes: list[str] = Field(default_factory=list)


# ===========================================================================
# 数据质量规则自动生成 LLM advisory（dq-rule-auto-generation Task 6）
# ===========================================================================


class ParseDescriptionsRequest(CamelModel):
    """parse-descriptions 请求：给定本体类，让 LLM 从属性描述中提取候选约束。"""

    class_id: int = Field(..., gt=0)
    # 可选 LLM 模型配置 id（前端 modelId）；None 走默认 OPENAI_API_KEY env 路径。
    # 序列化时通过 to_camel alias 输出 modelId（前端约定）。
    model_id: int | None = Field(default=None, gt=0, alias="modelId")


class PropertyConstraintSuggestionRead(CamelModel):
    """LLM 返回的单条候选约束建议。"""

    property_id: int
    property_name: str
    kind: str  # allowed_values | not_null
    values: list[str] | None = None
    confidence: float
    rationale: str


class ParseDescriptionsResponse(CamelModel):
    """parse-descriptions 响应。"""

    suggestions: list[PropertyConstraintSuggestionRead] = Field(default_factory=list)
    # 当前类下，已在 ontology_property.allowed_values 写入值的 propertyId 列表；
    # 前端用它初始化 LlmPanel.adoptedIds，让刷新页面也保持已采纳状态。
    persisted_property_ids: list[int] = Field(default_factory=list)


class ApplySuggestionRequest(CamelModel):
    """apply-suggestion 请求：采纳 LLM 推荐的 allowed_values，写入 ontology_property。"""

    property_id: int = Field(..., gt=0)
    allowed_values: list[str] = Field(..., min_length=1, max_length=50)


class ApplySuggestionResponse(CamelModel):
    """apply-suggestion 响应。"""

    property_id: int
    allowed_values: list[str]


class DatasourceOption(CamelModel):
    """数据源下拉选项（仅 id + name，轻量）。"""

    id: int
    name: str


class RuleOptionsRead(CamelModel):
    """GET /data-quality/rules/options 响应：规则列表筛选下拉的所有可选值。

    - ruleNames：data_quality_rule 中已用过的 rule_name DISTINCT
    - datasourceIds：active 数据源全量
    - targetTables：data_quality_rule 中已用过的 target_table DISTINCT
    - severities：静态全集（HIGH/MEDIUM/LOW/INFO）
    """

    rule_names: list[str] = Field(default_factory=list)
    datasource_ids: list[DatasourceOption] = Field(default_factory=list)
    target_tables: list[str] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)


class SystemConfigRead(CamelModel):
    """system_config 单行读视图（feat-system-config-admin）。

    key 是不可变主键；value 是可编辑字段；description 是元数据（创建时写入，UI 只读）。
    updated_time 由 DB DEFAULT NOW() 自动维护（每次 UPDATE 也由 ORM 自动刷新）。
    """

    key: str
    value: str | None
    description: str | None
    updated_time: datetime


class SystemConfigUpdate(CamelModel):
    """system_config 更新 payload：仅 value 字段可改。

    - key：URL path param 单独传；payload 不重（防 mass-assignment 改主键）。
    - description：元数据，本计划不允许通过 admin API 改（需 DDL 同步才能让种子和
      UI 一致；见后续 SSOT 治理）。
    - value：必填字段（即便清空也要显式传 None 而非省略；用 min_length=0 允许空串）。
    """

    value: str | None = Field(default=None, max_length=4096)
