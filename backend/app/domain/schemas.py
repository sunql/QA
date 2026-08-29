"""Pydantic 请求/响应 Schema（DTO）。

约定：内部字段使用 snake_case（与 ORM 对齐），通过 alias_generator 输出 camelCase JSON，
匹配设计稿 API 契约（sessionId、modelName、tokenUsage 等）。
遵循不可变原则：所有 DTO 默认 frozen 风格，调用方不应修改响应 DTO。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.domain.enums import ChartType, DataSourceType
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
    MSG_SCHEMA_USAGE_BY_MODEL,
    MSG_SCHEMA_USAGE_LAST_QUESTION,
    MSG_SCHEMA_USAGE_TOTAL_COST,
    MSG_SCHEMA_USAGE_TOTAL_REQUESTS,
    MSG_SCHEMA_USAGE_TOTAL_SESSIONS,
    MSG_SCHEMA_USAGE_TOTAL_TOKENS,
)


class CamelModel(BaseModel):
    """所有 DTO 基类：snake_case 字段名 + camelCase JSON 别名。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        protected_namespaces=(),
    )


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
    parent_class_id: int | None = None
    created_by: str | None = None


class OntologyClassUpdate(CamelModel):
    class_name: str | None = Field(default=None, max_length=100)
    class_alias: str | None = Field(default=None, max_length=100)
    description: str | None = None
    source_table: str | None = Field(default=None, max_length=100)
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

    _check_aliases = field_validator("business_aliases")(_validateBusinessAliases)


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


class OntologyJoinCreate(CamelModel):
    source_class_id: int
    source_columns: list[str] = Field(..., min_length=1)
    target_class_id: int
    target_columns: list[str] = Field(..., min_length=1)
    join_type: str = Field(default="INNER", max_length=10)
    relation_type: str = Field(default="business", max_length=20)
    description: str | None = None


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
    # ReDoS 警告：patterns 以 Python re 执行（re.search、无超时）。
    # 当前仅供服务端内置默认值使用；若未来将此字段开放给用户配置，
    # 必须先加防护（如限制 pattern 长度/复杂度、编译白名单校验，
    # 或改用 fnmatch 等无回溯的匹配），否则恶意 pattern 可导致
    # 灾难性回溯、阻塞导入流程。
    name_blacklist_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TABLE_NAME_BLACKLIST_PATTERNS)
    )


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


class ImportRuleConfig(CamelModel):
    """本地导入规则配置：表过滤 + 类型映射 + LLM 增强。"""

    table_filter: TableFilterRules = Field(default_factory=TableFilterRules)
    type_mapping: TypeMappingRules = Field(default_factory=TypeMappingRules)
    llm_enhance_options: LlmEnhanceOptions = Field(default_factory=LlmEnhanceOptions)


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


class ImportPreviewRequest(CamelModel):
    """本地导入预览请求。"""

    rules: ImportRuleConfig = Field(default_factory=ImportRuleConfig)


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
