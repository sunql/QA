"""SQLAlchemy 领域模型。

Phase 1 包含 llm_config 与 session_token_usage。
后续 Phase 会追加 ontology_*、data_source、session_context 等表。

约定：ORM 列属性使用 snake_case，与设计稿 SQL 列名（model_name、session_id 等）对齐。
JSON 契约的 camelCase 由 Pydantic schema 的 alias_generator 负责。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.enums import (
    DataSourceType,
    EntityType,
    LineageLayer,
    MatchRule,
    RefreshFrequency,
    RuleType,
    ScoreType,
    Severity,
    SourceSystem,
)


def _utcnow() -> datetime:
    """时区感知的当前 UTC 时间（替代已弃用的 datetime.utcnow）。"""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    pass


# 跨库主键类型：生产用 BigInteger，SQLite 测试用 Integer 以支持自增
BigIntPk = BigInteger().with_variant(Integer, "sqlite")
BigIntFk = BigInteger().with_variant(Integer, "sqlite")


class TimestampMixin:
    """公共时间戳字段。"""

    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class LlmConfig(Base, TimestampMixin):
    """模型配置表 - 对应设计稿 llm_config。"""

    __tablename__ = "llm_config"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    api_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    cost_per_1k_input: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    cost_per_1k_output: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    cost_threshold: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0.05")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    usages: Mapped[list[SessionTokenUsage]] = relationship(
        back_populates="model", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<LlmConfig id={self.id} name={self.model_name} provider={self.provider}>"


class SessionTokenUsage(Base):
    """会话 Token 消耗流水表 - 对应设计稿 session_token_usage。"""

    __tablename__ = "session_token_usage"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_config_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("llm_config.id"), nullable=True
    )
    model_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    request_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    purpose: Mapped[str | None] = mapped_column(String(50), nullable=True)

    model: Mapped[LlmConfig | None] = relationship(back_populates="usages", lazy="selectin")

    __table_args__ = (
        Index("idx_session_time", "session_id", "request_time"),
        Index("idx_model_time", "model_config_id", "request_time"),
    )

    def __repr__(self) -> str:
        return (
            f"<SessionTokenUsage id={self.id} session={self.session_id} "
            f"model={self.model_name} total={self.total_tokens} cost={self.cost}>"
        )


class EmbeddingProvider(Base, TimestampMixin):
    """Embedding 模型服务注册表 - 记录可用的 embedding 服务与当前激活项。

    单活互斥：is_active 同一时刻至多一行 True（由 service 层维护）。
    """

    __tablename__ = "embedding_provider"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(30), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return (
            f"<EmbeddingProvider id={self.id} name={self.name} "
            f"type={self.provider_type} model={self.model_name} active={self.is_active}>"
        )


__all__ = [
    "Base",
    "TimestampMixin",
    "LlmConfig",
    "SessionTokenUsage",
    "EmbeddingProvider",
    "OntologyClass",
    "OntologyProperty",
    "OntologyMetric",
    "OntologyJoin",
    "DataSource",
    "SessionMessage",
    "SchemaCache",
    "SessionQueryState",
    "DataQualityRule",
    "DataQualityScore",
]


# =============================================================================
# Phase 2: Ontology
# =============================================================================


class OntologyClass(Base, TimestampMixin):
    """本体类定义表。

    版本管理（Phase 6，已移除）：updateClass 现为原地 UPDATE，主键 id 稳定，
    不再产生新版本行。version/validFrom/validTo 列保留兼容：
    - createClass 起始 version=1, validFrom=now(), validTo=None。
    - deleteClass 软删除：valid_to 设为 now()（墓碑），listClasses 默认过滤。
    - 历史版本行由一次性脚本折叠清理，本表正常情况下每 class_name 仅一行。
    """

    __tablename__ = "ontology_class"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # class_name 不再 unique：同名可有多版本；唯一性由业务层在 (class_name, version) 上保证
    class_name: Mapped[str] = mapped_column(String(100), nullable=False)
    class_alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 治理字段（Phase 3.4，采购域 Sheet 03 业务对象目录）
    object_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="Master/Transaction/Reference/Event"
    )
    object_owner: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="责任部门/人"
    )
    parent_class_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("ontology_class.id"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 版本与有效时间窗
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    parent: Mapped[OntologyClass | None] = relationship(
        back_populates="children", remote_side=[id], lazy="selectin"
    )
    children: Mapped[list[OntologyClass]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_class_id],
        lazy="selectin",
    )
    properties: Mapped[list[OntologyProperty]] = relationship(
        back_populates="ontology_class",
        primaryjoin="OntologyClass.id == OntologyProperty.class_id",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    metrics: Mapped[list[OntologyMetric]] = relationship(
        back_populates="target_class", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("class_name", "version", name="uq_ontology_class_name_version"),
        Index("ix_ontology_class_valid_to", "valid_to"),
    )

    def __repr__(self) -> str:
        return (
            f"<OntologyClass id={self.id} name={self.class_name} version={self.version}>"
        )


class OntologyProperty(Base, TimestampMixin):
    """本体属性定义表。"""

    __tablename__ = "ontology_property"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    class_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("ontology_class.id"), nullable=False
    )
    property_name: Mapped[str] = mapped_column(String(100), nullable=False)
    property_alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 2-2：业务别名（同义词列表，如 ["营业额", "收入"]）与列描述，供 schema 文本消歧缩写列名
    business_aliases: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    data_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_foreign_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ref_class_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("ontology_class.id"), nullable=True
    )
    source_column: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Relationships
    ontology_class: Mapped[OntologyClass] = relationship(
        back_populates="properties",
        foreign_keys=[class_id],
        lazy="selectin",
    )
    ref_class: Mapped[OntologyClass | None] = relationship(
        primaryjoin="OntologyProperty.ref_class_id == OntologyClass.id",
        lazy="selectin",
    )

    __table_args__ = (UniqueConstraint("class_id", "property_name", name="uq_class_property"),)

    def __repr__(self) -> str:
        return f"<OntologyProperty id={self.id} name={self.property_name}>"


class OntologyMetric(Base, TimestampMixin):
    """本体指标定义表。"""

    __tablename__ = "ontology_metric"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    metric_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    metric_alias: Mapped[str | None] = mapped_column(String(100), nullable=True)
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    agg_function: Mapped[str] = mapped_column(String(20), nullable=False, default="SUM")
    target_class_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("ontology_class.id"), nullable=True
    )
    dimension_defaults: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Relationships
    target_class: Mapped[OntologyClass | None] = relationship(
        back_populates="metrics", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<OntologyMetric id={self.id} name={self.metric_name}>"


class OntologyJoin(Base, TimestampMixin):
    """本体关联关系目录表：运行时 JOIN 生成的唯一真源。

    每条记录是一条 join 边，由 (source_class_id, source_columns) →
    (target_class_id, target_columns) 描述，多列按顺序一一配对（source_columns[i]
    对应 target_columns[i]）。相比外键标志，它显式给出目标列，修复了"源列名 ≠
    目标主键名"导致 JOIN 接错的根因，也能表达无外键标志的跨单据业务流转关系。

    relation_type 标注来源：foreign_key（种子物化自外键标志）| business（curated
    业务流转）。join_key 为幂等去重键（列按配对顺序拼接），跨 PG/SQLite 均可比较。
    本表仅由 NL2SQL 消费，不写 Neo4j/Milvus。
    """

    __tablename__ = "ontology_join"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source_class_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("ontology_class.id"), nullable=False
    )
    source_columns: Mapped[list[str]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
    )
    target_class_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("ontology_class.id"), nullable=False
    )
    target_columns: Mapped[list[str]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
    )
    join_type: Mapped[str] = mapped_column(String(10), nullable=False, default="INNER")
    relation_type: Mapped[str] = mapped_column(String(20), nullable=False, default="business")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_key: Mapped[str] = mapped_column(String(300), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("join_key", name="uq_ontology_join_key"),
        Index("idx_join_source_class", "source_class_id"),
        Index("idx_join_target_class", "target_class_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<OntologyJoin id={self.id} {self.source_class_id}->{self.target_class_id} "
            f"type={self.join_type}>"
        )


# =============================================================================
# Phase 3: DataSource
# =============================================================================


class DataSource(Base, TimestampMixin):
    """业务数据源配置表 - 对应设计稿 data_source。

    密码以 Fernet 加密存储于 password_encrypted，读 DTO 永不返回明文/密文。
    """

    __tablename__ = "data_source"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[DataSourceType] = mapped_column(String(20), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(String(100), nullable=False)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Oracle 版本用于 NL2SQL 分页语法判断；11g 用 ROWNUM，12c+ 用 FETCH FIRST
    oracle_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (UniqueConstraint("name", name="uq_datasource_name"),)

    def __repr__(self) -> str:
        return f"<DataSource id={self.id} name={self.name} type={self.type}>"


# =============================================================================
# Phase 5: Session Context
# =============================================================================


class SessionMessage(Base, TimestampMixin):
    """会话消息持久化表 - 服务端上下文，供后续轮次 Prompt 注入。

    role 取值：user / assistant。question 与 sql_generated 仅在 assistant 侧回填。
    读取按 (session_id, created_time) 索引，保留最近 N 轮作为上下文。
    """

    __tablename__ = "session_message"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql_generated: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_session_msg_time", "session_id", "created_time"),)

    def __repr__(self) -> str:
        return f"<SessionMessage id={self.id} session={self.session_id} role={self.role}>"


class SchemaCache(Base, TimestampMixin):
    """业务数据源 schema 缓存表（5.7）。

    datasource_id 唯一；schema_data 为结构化表清单（JSON，生产 PG 落 JSONB），
    schema_version 为内容 MD5，用于判断 schema 是否变化、是否需要刷新。
    """

    __tablename__ = "schema_cache"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    datasource_id: Mapped[int] = mapped_column(
        BigIntFk, ForeignKey("data_source.id"), nullable=False
    )
    schema_data: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (UniqueConstraint("datasource_id", name="uq_schema_cache_datasource"),)

    def __repr__(self) -> str:
        return f"<SchemaCache id={self.id} datasource_id={self.datasource_id} version={self.schema_version}>"


class SessionQueryState(Base, TimestampMixin):
    """会话级查询状态表（ReAct 多轮）。

    每次成功查询后 UPSERT（session_id 唯一）：保存上一轮的用户问题、
    查询计划（JSON）、生成 SQL、结果列名，供下一轮 REFINE/FOLLOW_UP
    意图注入 prompt 作为结构化上下文（而非仅原始对话文本）。

    3-4：`recent_rounds` 保留更早的 N 轮快照（question + sql），支持跨多轮
    REFINE/FOLLOW_UP 回溯（如"回到刚才第一个查询改一下""和上一次比"）。
    `last_*` 仍是"最近一轮"，`recent_rounds` 是其前的历史，按新到旧排列。
    """

    __tablename__ = "session_query_state"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_plan: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    last_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_result_columns: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recent_rounds: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )

    __table_args__ = (UniqueConstraint("session_id", name="uq_session_query_state_session"),)

    def __repr__(self) -> str:
        return f"<SessionQueryState session={self.session_id} turn={self.turn_count}>"


class TermDictionary(Base, TimestampMixin):
    """NL2SQL 术语字典表：用户习惯用语到本体概念的可编辑映射。

    用户在对答中发现 LLM 误解某术语（如"实际到货""订的数量""占比"）时，可将该
    术语的真实含义、映射的本体类/属性、以及结构性提示（如占比公式）录入此表；
    计划阶段将其渲染进 prompt，帮助 LLM 下次正确理解。

    mapped_class_name / mapped_property_name 用普通字符串引用 class_name /
    property_name（class_name 因版本化不唯一，故不做 FK），与 QueryPlan.selectedClasses 一致。
    """

    __tablename__ = "term_dictionary"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(100), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    mapped_class_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mapped_property_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    formula_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (UniqueConstraint("term", name="uq_term_dictionary_term"),)

    def __repr__(self) -> str:
        return f"<TermDictionary id={self.id} term={self.term}>"


# =============================================================================
# Phase 1.1: Data Quality Rule
# =============================================================================


class DataQualityRule(Base, TimestampMixin):
    """数据质量规则定义表。

    一行 = 一条可执行的规则，覆盖 5 种 rule_type 维度（COMPLETENESS / VALIDITY /
    UNIQUENESS / CONSISTENCY / REFERENTIAL），TIMELINESS 留 Phase 2 血缘模块。
    rule_code 业务唯一；rule_expression 为文本表达式（如 "ORDER_QTY > 0"），
    threshold DECIMAL(5,2) 表示通过率阈值（0-100）；is_enabled 软启用开关；
    severity 分级（HIGH/MEDIUM/LOW/INFO）。version 为治理版本（不参与 NL2SQL）。

    datasource_id 关联 data_source，决定评估执行时的业务库连接。Phase 1.2 引入
    （迁移 0018 追加），由评估 dispatcher 通过 business_db_pool.get_adapter 复用。

    评估执行与评分由 Phase 1.2 / 1.3 通过外部 evaluator 调用实现；本表仅承载
    规则定义本身的 CRUD。
    """

    __tablename__ = "data_quality_rule"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    datasource_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_table: Mapped[str] = mapped_column(String(100), nullable=False)
    target_column: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rule_type: Mapped[RuleType] = mapped_column(String(20), nullable=False)
    rule_expression: Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("95.00")
    )
    severity: Mapped[Severity] = mapped_column(
        String(10), nullable=False, default=Severity.MEDIUM
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False, default="v1.0")
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("rule_code", name="uq_data_quality_rule_code"),
        Index("ix_data_quality_rule_table", "target_table"),
        Index("ix_data_quality_rule_type", "rule_type"),
        Index("ix_data_quality_rule_enabled", "is_enabled"),
        Index("ix_data_quality_rule_datasource", "datasource_id"),
        ForeignKeyConstraint(
            ["datasource_id"],
            ["data_source.id"],
            name="fk_data_quality_rule_datasource",
            ondelete="RESTRICT",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DataQualityRule id={self.id} code={self.rule_code} "
            f"type={self.rule_type} enabled={self.is_enabled}>"
        )


# =============================================================================
# Phase 1.3: Data Quality Score
# =============================================================================


class DataQualityScore(Base, TimestampMixin):
    """数据质量评分历史表。

    一次 compute 写 N 条 score：每个 target_table 一条 TABLE 记录 + 一条 GLOBAL
    记录（target_table='*'）。6 维评分中 completeness/validity/uniqueness/
    consistency/referential 由 Phase 1.2 evaluator 实际填充；timeliness 暂为
    NULL（Phase 2 血缘模块补 ETL 时间字段后再算）。

    overall_score = 6 维非 NULL 分量的算术平均（分子维度数 = sum(dim != NULL)）。
    维度缺失时不拉低整体分，避免单维度未实现就把整体分打到 0。
    """

    __tablename__ = "data_quality_score"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    target_table: Mapped[str] = mapped_column(String(100), nullable=False)
    score_type: Mapped[ScoreType] = mapped_column(
        String(10), nullable=False, default=ScoreType.TABLE
    )
    completeness_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    validity_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    uniqueness_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    consistency_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    timeliness_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    referential_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    overall_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evaluation_duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    rules_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint(
            "score_type IN ('TABLE', 'GLOBAL')",
            name="ck_data_quality_score_type",
        ),
        Index(
            "ix_data_quality_score_lookup",
            "target_table",
            "score_type",
            "evaluated_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DataQualityScore id={self.id} target_table={self.target_table} "
            f"score_type={self.score_type} overall={self.overall_score}>"
        )


# =============================================================================
# Phase 2.1: Data Lineage
# =============================================================================


class DataLineage(Base, TimestampMixin):
    """数据血缘边表。

    一行 = 一条「上游对象/字段 → 下游对象/字段」血缘边，覆盖 7 层模型：
    SOURCE_SYSTEM / ODS / DWD / DWS / ADS / KPI / AI。

    表级血缘：source_field / target_field 留空，仅描述 table → table 流向。
    字段级血缘：source_field / target_field 必填，描述字段 → 字段映射。
    同一上下游 (source_layer, source_system, source_object, source_field,
    target_layer, target_system, target_object, target_field) 不允许重复：
    由唯一约束 uq_data_lineage_edge 保护，service 层抛 ValidationError。

    transformation_rule 是自然语言描述（如「标准化 + 代理键」「CDC 原样接入」），
    Phase 2.2 自动提取脚本会从 ontology formula 推断；本期为人工录入字段。
    refresh_frequency 描述 ETL 刷新节奏（REALTIME/HOURLY/DAILY/WEEKLY），
    与 Phase 1.3 TIMELINESS 评分计算互为输入。
    is_active=false 表示软删除（保留审计与历史可视化追溯）。
    """

    __tablename__ = "data_lineage"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source_layer: Mapped[LineageLayer] = mapped_column(String(20), nullable=False)
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    source_object: Mapped[str] = mapped_column(String(100), nullable=False)
    source_field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_layer: Mapped[LineageLayer] = mapped_column(String(20), nullable=False)
    target_system: Mapped[str] = mapped_column(String(100), nullable=False)
    target_object: Mapped[str] = mapped_column(String(100), nullable=False)
    target_field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transformation_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_frequency: Mapped[RefreshFrequency] = mapped_column(
        String(20), nullable=False, default=RefreshFrequency.DAILY
    )
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        # 同上下游 + 字段组合唯一；表级血缘 source/target_field 同时为 NULL 时
        # 唯一约束在 PG 视为「(..., NULL) 与 (..., NULL) 不冲突」（SQL 标准语义）。
        # 因此 service 层在创建时主动查重；DB 约束作为兜底，防止 race condition。
        UniqueConstraint(
            "source_layer",
            "source_system",
            "source_object",
            "source_field",
            "target_layer",
            "target_system",
            "target_object",
            "target_field",
            name="uq_data_lineage_edge",
        ),
        Index("ix_data_lineage_source", "source_layer", "source_system", "source_object"),
        Index("ix_data_lineage_target", "target_layer", "target_system", "target_object"),
        Index("ix_data_lineage_active", "is_active"),
    )

    def __repr__(self) -> str:
        src = f"{self.source_layer.value}.{self.source_object}"
        if self.source_field:
            src += f".{self.source_field}"
        tgt = f"{self.target_layer.value}.{self.target_object}"
        if self.target_field:
            tgt += f".{self.target_field}"
        return f"<DataLineage id={self.id} {src} -> {tgt}>"


# =============================================================================
# Phase 3.1: Entity Mapping（跨系统编码映射）
# =============================================================================


class EntityMapping(Base, TimestampMixin):
    """跨系统编码映射表。

    一行 = 一个企业实体（SUPPLIER/MATERIAL/PO/GR/IQC/NCR）在某个源系统
    （ERP/SRM/QMS/MDM/PLM）中的原始编码到企业统一代理键 / 统一编码的映射。

    企业侧标识：
      enterprise_key   — 企业统一代理键（BIGINT，MDM 主数据）
      enterprise_code  — 企业统一编码（可读，如 SUP000001）
    源系统侧标识：
      source_key       — 源系统原始 key（如 ERP 的 V000001）
      source_code      — 源系统原始编码
      match_rule       — 匹配规则（MDM_MASTER/BUSINESS_KEY/MAPPING）

    effective_date / expiry_date 描述映射有效期；expiry_date 为空表示长期有效。
    同一 (entity_type, enterprise_key, source_system) 只允许一条映射：
    由唯一约束 uq_entity_mapping_entity_source 保护，service 层抛 ValidationError。
    """

    __tablename__ = "entity_mapping"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    entity_type: Mapped[EntityType] = mapped_column(String(20), nullable=False)
    enterprise_key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enterprise_code: Mapped[str] = mapped_column(String(100), nullable=False)
    source_system: Mapped[SourceSystem] = mapped_column(String(20), nullable=False)
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    source_code: Mapped[str] = mapped_column(String(100), nullable=False)
    match_rule: Mapped[MatchRule] = mapped_column(
        String(20), nullable=False, default=MatchRule.MAPPING
    )
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        # 同一实体 + 同一源系统只允许一条映射；三列均非空，DB 约束即完整兜底。
        UniqueConstraint(
            "entity_type",
            "enterprise_key",
            "source_system",
            name="uq_entity_mapping_entity_source",
        ),
        Index("ix_entity_mapping_enterprise_key", "enterprise_key"),
        Index("ix_entity_mapping_source", "entity_type", "source_system", "source_key"),
    )

    def __repr__(self) -> str:
        return (
            f"<EntityMapping id={self.id} "
            f"{self.entity_type.value} key={self.enterprise_key} "
            f"via {self.source_system.value}/{self.source_key}>"
        )
