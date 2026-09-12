"""Wiki 知识管理领域模型（feat-wiki-knowledge，Phase 8 M1）。

本模块承载「通用知识管理」的核心 4 张表：

- ``wiki_page``：知识条目本体（Markdown 正文 + 维度/结构阶段/自动分类元数据）
- ``knowledge_claim``：从 Page 抽取的事实原子（弱结构，可独立检索）
- ``evidence``：Claim 的证据 5 元组（来源/页码/小节/段落/原文）
- ``knowledge_relation``：Page ↔ Page / Class / Metric / Entity 的关系

设计约束：
- 分类不做「静默决定」：``auto_classification`` 存 LLM 建议，``dimension``
  是**可被业务专家覆盖**的生效值（写入前由 service 决定取建议还是取人工值）。
- 自动发现的关系**默认不生效**：``confirmed=False`` 的关系仅供审核，
  用户确认后才同步 Neo4j（避免误连污染图）。
- JSON 列用 ``JSON().with_variant(JSONB, 'postgresql')``，与 models.py 一致，
  保证 SQLite 单元测试与 PG 集成测试都能建表。

约定：ORM 列属性 snake_case（与 DB 列一致）；JSON 契约的 camelCase 由
Pydantic schema 的 alias_generator 负责。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.models import Base, BigIntFk, BigIntPk, TimestampMixin, _utcnow

# JSON 列的跨库写法：生产 PG 落 JSONB，SQLite 测试落 JSON
# （wiki_learning_models.py 复用此别名，避免两处各写一份 with_variant）
JsonColumn = JSON().with_variant(postgresql.JSONB(), "postgresql")

# 知识维度词表（机制 1 自动分类的输出域；业务专家可覆盖为其中之一）。
# 刻意用字符串而非 DB enum：新增维度不需改 schema，与 feature_rule_threshold
# 的 severity 同思路。
KNOWLEDGE_DIMENSIONS: tuple[str, ...] = (
    "OBJECT",    # 业务对象（对应 ontology_class）
    "RULE",      # 业务规则（准入/定价/审批/质量）
    "PROCESS",   # 管理流程（审批流 / SOP）
    "CONCEPT",   # 业务定义（术语 / 口径）
    "METRIC",    # 指标与其算法
    "POLICY",    # 制度 / 政策
    "DOCUMENT",  # 文档
    "FAQ",       # 问答
)

# 知识条目生命周期状态。与 dimension 同理用白名单而非 DB enum：
# 状态轴是覆盖度/审核看板的聚合维度，脏值会污染统计。
WIKI_PAGE_STATUSES: tuple[str, ...] = (
    "DRAFT",      # 草稿
    "REVIEW",     # 待审
    "APPROVED",   # 已审
    "EFFECTIVE",  # 生效
    "EXPIRED",    # 失效
)

# 知识结构演进阶段（机制 5）：弱结构 → 半结构化 → 完全结构化
STRUCTURE_STAGES: tuple[str, ...] = (
    "MARKDOWN",           # Stage 1：纯自然语言
    "SEMI_STRUCTURED",    # Stage 2：补了部分结构化字段
    "FULLY_STRUCTURED",   # Stage 3：完整结构化，可执行/可同步
)

# 知识关系类型（Page → 目标）
RELATION_TYPES: tuple[str, ...] = (
    "REFERENCES",   # 引用
    "SUPERSEDES",   # 替代（新版本替换旧版本）
    "EXTENDS",      # 扩展
    "DESCRIBES",    # 描述（通常指向 ontology_class）
    "APPLIES_TO",   # 适用于
    "DEFINES",      # 定义（通常指向 ontology_metric）
    "AFFECTS",      # 影响（通常指向实体）
)

# 关系下游目标类型
RELATION_TARGET_TYPES: tuple[str, ...] = (
    "PAGE",
    "ONTOLOGY_CLASS",
    "ONTOLOGY_METRIC",
    "ENTITY_MAPPING",
)


class WikiPage(Base, TimestampMixin):
    """知识条目本体 —— Wiki 的核心表。

    业务专家只写 ``content``（Markdown），系统在摄取管线里自动补齐
    ``dimension`` / ``structure_stage`` / ``auto_classification``。
    其中 ``dimension`` 是**生效值**：默认取自动分类建议，业务专家可覆盖
    （覆盖动作写回 learning_feedback，见机制 1）。

    ``status`` 生命周期：DRAFT → REVIEW → APPROVED → EFFECTIVE → EXPIRED。
    ``authority_level`` 为 L0-L5 权威等级（方案 §Authority）。
    """

    __tablename__ = "wiki_page"
    __table_args__ = (
        UniqueConstraint("page_id", name="uq_wiki_page_page_id"),
        Index("ix_wiki_page_dimension_status", "dimension", "status"),
        Index("ix_wiki_page_structure_stage", "structure_stage"),
        Index("ix_wiki_page_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # 业务主键：由 service 生成的稳定可读 ID（如 RULE-SUPPLIER-QUALIFY-V3）
    page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 正文的 sha256（小写 64 位十六进制），与 document_catalog.content_hash 同口径。
    # 可空：0061 之前的历史行没有值，且 P1 不做回填 —— 代码必须在 NULL 时退化成
    # 「不可判定」而不是当成「内容相同」，见 wiki_import_service._importOne。
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 机制 1 产物：生效维度（默认取 auto_classification 建议，可人工覆盖）
    dimension: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # 机制 5 产物：结构演进阶段
    structure_stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default="MARKDOWN", server_default="MARKDOWN"
    )
    # 机制 1 原始输出：{"primary": "RULE", "confidence": 0.92, "alternatives": [...]}
    auto_classification: Mapped[dict[str, Any] | None] = mapped_column(
        JsonColumn, nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="DRAFT", server_default="DRAFT"
    )
    authority_level: Mapped[str | None] = mapped_column(String(10), nullable=True)
    version: Mapped[str] = mapped_column(
        String(30), nullable=False, default="v1.0", server_default="v1.0"
    )

    # 溯源（M2）：本次知识经由哪个导入任务、实际由哪个模型处理。
    # processing_model_id 记录**实际生效**模型——fallback 降级命中时会与
    # task.selected_model_id 不同，这个差异刻意保留以便审计降级。
    imported_via_task_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey("wiki_import_task.id", ondelete="SET NULL"),
        nullable=True,
    )
    processing_model_id: Mapped[int | None] = mapped_column(
        BigIntFk, ForeignKey("llm_config.id"), nullable=True
    )

    created_by_user_id: Mapped[int | None] = mapped_column(BigIntFk, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    claims: Mapped[list[KnowledgeClaim]] = relationship(
        back_populates="page",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<WikiPage id={self.id} page_id={self.page_id} "
            f"dim={self.dimension} stage={self.structure_stage} status={self.status}>"
        )


class KnowledgeClaim(Base):
    """从 Page 抽取的事实原子（弱结构）。

    一条 Claim = 一个可独立检索/独立引用的断言（如「注册资本 ≥ 1000 万」）。
    Claim 是 Page 与 Evidence 之间的桥梁：一个 Page 抽 N 条 Claim，
    一条 Claim 关联 M 条 Evidence（原文出处）。
    """

    __tablename__ = "knowledge_claim"
    __table_args__ = (
        Index("ix_knowledge_claim_page", "page_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    page_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("wiki_page.page_id", ondelete="CASCADE"),
        nullable=False,
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    # FACT / DEFINITION / RULE / STATISTIC
    claim_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Milvus 向量引用（知识条目嵌入的稳定 key）
    embedding_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    page: Mapped[WikiPage] = relationship(back_populates="claims")
    # lazy="selectin"：序列化 DTO 时同步预取证据，避免 async 上下文里
    # 触发惰性加载报 MissingGreenlet。
    evidences: Mapped[list[Evidence]] = relationship(
        back_populates="claim",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<KnowledgeClaim id={self.id} page={self.page_id} type={self.claim_type}>"


class Evidence(Base):
    """Claim 的证据出处（来源 5 元组 + 原文内容）。

    source_type/source_id 指向外部来源（文档目录 / 工单 / 邮件）；
    page_number/section_name/paragraph_no 为原文定位，便于回链与审计。
    """

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_claim", "claim_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey("knowledge_claim.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    page_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    section_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    paragraph_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    claim: Mapped[KnowledgeClaim] = relationship(back_populates="evidences")

    def __repr__(self) -> str:
        return f"<Evidence id={self.id} claim={self.claim_id} src={self.source_type}>"


class KnowledgeRelation(Base):
    """知识关系：Page → (Page | OntologyClass | OntologyMetric | EntityMapping)。

    **确认机制**：机制 2 自动发现的关系以 ``confirmed=False`` 落库，
    仅作为「待审核候选」展示；业务专家确认后 ``confirmed=True`` 才同步 Neo4j。
    这条规则防止低置信度误连污染知识图谱。

    ``downstream_id`` 是**多态外键**（按 downstream_type 解释），刻意不做
    DB 级 FK —— 目标可能是 Neo4j 节点或 PG 表的业务键，与
    term_dictionary.mapped_class_name 同思路。
    """

    __tablename__ = "knowledge_relation"
    __table_args__ = (
        UniqueConstraint(
            "upstream_page_id", "downstream_type", "downstream_id", "relation_type",
            name="uq_knowledge_relation_triple",
        ),
        Index("ix_knowledge_relation_upstream", "upstream_page_id"),
        Index("ix_knowledge_relation_downstream", "downstream_type", "downstream_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    upstream_page_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("wiki_page.page_id", ondelete="CASCADE"),
        nullable=False,
    )
    downstream_type: Mapped[str] = mapped_column(String(30), nullable=False)
    downstream_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    auto_detected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 打回时间戳（M4）。有值 = 业务专家否掉了这条候选。
    # 打回**不删行**：删了下次发现会重新算出同一条候选，用户得反复打回。
    # 三态 = (confirmed, rejected_at)：待审 (false, null) / 已确认 (true, null)
    # / 已打回 (false, 有值)。用时间戳而非布尔，非法态「既确认又打回」从
    # 类型上就不存在。
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<KnowledgeRelation id={self.id} {self.upstream_page_id} "
            f"-{self.relation_type}-> {self.downstream_type}:{self.downstream_id} "
            f"confirmed={self.confirmed}>"
        )


__all__ = [
    "JsonColumn",
    "KNOWLEDGE_DIMENSIONS",
    "WIKI_PAGE_STATUSES",
    "STRUCTURE_STAGES",
    "RELATION_TYPES",
    "RELATION_TARGET_TYPES",
    "WikiPage",
    "KnowledgeClaim",
    "Evidence",
    "KnowledgeRelation",
]
