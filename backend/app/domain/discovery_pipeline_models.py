"""本体发现流水线的 ORM 映射（WP-ONT-AI A2 / G3，迁移 0166）。

设计依据 `docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md` §4
与 `docs/update/1118/M7/ontology-evolution-program.md` §二（统一数据模型）。

六张表，围绕「一次运行产出证据 → 候选 → 提案 → 审核」：

| 表 | 职责 |
|---|---|
| `discovery_run` | 运行记录：触发方式 / 快照 / prompt 版本 / **LLM 计量**（token + cost） |
| `discovery_profile` | 确定性剖析结果（行数 / 空值 / 值域 / 基数）+ **可复现 SQL 摘要** |
| `discovery_candidate` | 候选（对象/属性/关系/分类/指标）+ **证据引用**（复用 `KnowledgeEvidence` 契约） |
| `discovery_finding` | 主数据问题（重复/冲突/缺失/映射） |
| `discovery_proposal` | 变更单：`base_release_id` 钉住基线；`diff_json` **必须可编译成 manifest v2** |
| `discovery_review` | 审核留痕（四眼）；`approval_task_id` 可选（二期接 P5 审批） |

## 不许碰的红线

- **表名前缀只能是 `discovery_*`**（+ 既有 `metadata_snapshot`）：`ontology_*` 是正式本体表，
  候选混进去 = 「AI 写本体」。
- **本域不写治理表**：发布必须走既有 `semantic_release_service.activate`（唯一写口），
  提案只持有 `release_id` 指针。
- **LLM 调用必须计量**：`discovery_run` 的 `prompt_tokens` / `completion_tokens` / `cost`
  是 V0.3 的强制口径（发现域一旦引入 LLM，守卫测试会同时要求这三个字段出现）。

守卫：`app/tests/unit/test_wp_ont_ai_fusion_contract.py`（命名空间 / 写路径 / 计量）、
`app/tests/unit/test_discovery_pipeline_models.py`（ORM ↔ 迁移逐字对照）。

## 四眼审核为什么不在库层强制

`discovery_review.reviewer != discovery_proposal.proposer` 是**跨表**约束，
PostgreSQL 的 CHECK 表达不了 ⇒ 由服务层在写入时拒绝（同人自审 409），
守卫测试覆盖。`approval_task_id` 非空时还要求 `approval_task.subject_type='ontology_proposal'`，
同样只能服务层判。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models import Base, BigIntFk, BigIntPk

# 与 `app/alembic/versions/0166_discovery_pipeline.py` 的列注释**逐字一致**
# （alembic autogenerate 会比对列注释；单测双向钉住）。
RUN_TRIGGER_COMMENT = "0166，WP-ONT-AI A2：运行触发方式（MANUAL / SCHEDULED / DIFF）"
RUN_STATUS_COMMENT = "0166，WP-ONT-AI A2：运行状态（RUNNING / SUCCEEDED / FAILED）"
RUN_MODEL_VERSION_COMMENT = "0166，WP-ONT-AI A2：本次运行使用的模型版本"
RUN_PROMPT_VERSION_COMMENT = "0166，WP-ONT-AI A2：本次运行使用的 prompt 版本"
RUN_TOKENS_COMMENT = "0166，WP-ONT-AI A2：LLM 用量（prompt_tokens / completion_tokens / cost）"
PROFILE_TABLE_COMMENT = "0166，WP-ONT-AI A2：被剖析的表名"
PROFILE_COLUMN_COMMENT = "0166，WP-ONT-AI A2：被剖析的列名（NULL = 表级指标）"
PROFILE_DIGEST_COMMENT = "0166，WP-ONT-AI A2：剖析 SQL 的 SHA-256 摘要（可复现）"
CANDIDATE_TYPE_COMMENT = "0166，WP-ONT-AI A2：候选类型（OBJECT / PROPERTY / RELATION / CLASSIFICATION / METRIC）"
CANDIDATE_PAYLOAD_COMMENT = "0166，WP-ONT-AI A2：候选内容（JSON 对象，结构按 candidate_type）"
CANDIDATE_EVIDENCE_COMMENT = "0166，WP-ONT-AI A2：证据引用（JSON 数组，复用 KnowledgeEvidence 契约；不得为空）"
CANDIDATE_STATUS_COMMENT = "0166，WP-ONT-AI A2：候选状态（CANDIDATE / NEEDS_EVIDENCE / ACCEPTED / REJECTED / MERGED）"
FINDING_TYPE_COMMENT = "0166，WP-ONT-AI A2：问题类型（DUPLICATE / CONFLICT / MISSING / MAPPING）"
FINDING_SEVERITY_COMMENT = "0166，WP-ONT-AI A2：问题严重级别（ERROR / WARNING / INFO）"
FINDING_STATUS_COMMENT = "0166，WP-ONT-AI A2：问题状态（OPEN / ACKNOWLEDGED / RESOLVED / IGNORED）"
PROPOSAL_BASE_RELEASE_COMMENT = "0166，WP-ONT-AI A2：提案基线发布（diff 相对它计算）"
PROPOSAL_RELEASE_COMMENT = "0166，WP-ONT-AI A2：审批后生成的语义发布（走既有 activate）"
PROPOSAL_DIFF_COMMENT = "0166，WP-ONT-AI A2：变更内容（JSON 对象；入库前必须通过 compileManifest）"
PROPOSAL_IMPACT_COMMENT = "0166，WP-ONT-AI A2：影响面摘要（受影响类 / 指标 / 绑定）"
PROPOSAL_STATUS_COMMENT = "0166，WP-ONT-AI A2：提案状态（DRAFT / IN_REVIEW / APPROVED / REJECTED / PUBLISHED）"
REVIEW_DECISION_COMMENT = "0166，WP-ONT-AI A2：审核结论（APPROVE / REJECT / REQUEST_CHANGES）"
REVIEW_APPROVAL_TASK_COMMENT = "0166，WP-ONT-AI A2：可选：P5 审批任务（二期回填；非空时 subject_type 必须是 ontology_proposal）"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DiscoveryRun(Base):
    """一次剖析/候选运行（含 LLM 计量 —— V0.3 强制口径）。"""

    __tablename__ = "discovery_run"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_source.id", ondelete="RESTRICT", name="fk_discovery_run_source"
        ),
        nullable=False,
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "metadata_snapshot.id", ondelete="RESTRICT", name="fk_discovery_run_snapshot"
        ),
        nullable=True,
    )
    trigger: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="MANUAL",
        server_default=sa_text("'MANUAL'"),
        comment=RUN_TRIGGER_COMMENT,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="RUNNING",
        server_default=sa_text("'RUNNING'"),
        comment=RUN_STATUS_COMMENT,
    )
    model_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=RUN_MODEL_VERSION_COMMENT
    )
    prompt_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment=RUN_PROMPT_VERSION_COMMENT
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6),
        nullable=False,
        default=Decimal("0"),
        server_default=sa_text("0"),
        comment=RUN_TOKENS_COMMENT,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "trigger IN ('MANUAL','SCHEDULED','DIFF')", name="ck_discovery_run_trigger"
        ),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED')", name="ck_discovery_run_status"
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost >= 0",
            name="ck_discovery_run_usage_nonneg",
        ),
        Index("ix_discovery_run_source", "source_id"),
        Index("ix_discovery_run_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryRun id={self.id} source={self.source_id} status={self.status}>"
        )


class DiscoveryProfile(Base):
    """确定性剖析结果（每个 run × 表 × 列一行；列名为 NULL 表示表级指标）。"""

    __tablename__ = "discovery_profile"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_run.id", ondelete="CASCADE", name="fk_discovery_profile_run"
        ),
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_source.id", ondelete="RESTRICT", name="fk_discovery_profile_source"
        ),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "metadata_snapshot.id",
            ondelete="RESTRICT",
            name="fk_discovery_profile_snapshot",
        ),
        nullable=False,
    )
    table_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment=PROFILE_TABLE_COMMENT
    )
    column_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment=PROFILE_COLUMN_COMMENT
    )
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    null_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    distinct_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    min_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    max_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # ⚠️ `none_as_null=True` 不可省：JSONB 默认把 Python `None` 存成 **JSON `null`**，
    # 而 CHECK 写的是 `sample_values IS NULL`（SQL NULL）⇒ 直接违反约束。
    sample_values: Mapped[list[Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    sql_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, comment=PROFILE_DIGEST_COMMENT
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(row_count IS NULL OR row_count >= 0) "
            "AND (null_count IS NULL OR null_count >= 0) "
            "AND (distinct_count IS NULL OR distinct_count >= 0)",
            name="ck_discovery_profile_counts_nonneg",
        ),
        CheckConstraint(
            "sql_digest ~ '^[0-9a-f]{64}$'", name="ck_discovery_profile_digest_sha256"
        ),
        CheckConstraint(
            "sample_values IS NULL OR jsonb_typeof(sample_values) = 'array'",
            name="ck_discovery_profile_samples_array",
        ),
        Index("ix_discovery_profile_run", "run_id"),
        # 幂等：同一 run 上同一 (表, 列) 只允许一行。`COALESCE` 不可省 ——
        # 列名为 NULL 的表级指标在 PostgreSQL 里 NULL 互不相等，直建唯一索引等于没建
        # （与 `semantic_release_check` 同一个坑）。
        Index(
            "uq_discovery_profile_scope",
            "run_id",
            "table_name",
            sa_text("COALESCE(column_name, '')"),
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryProfile id={self.id} run={self.run_id} "
            f"{self.table_name}.{self.column_name or '*'}>"
        )


class DiscoveryCandidate(Base):
    """候选（对象/属性/关系/分类/指标）：**证据非空**才允许落库。"""

    __tablename__ = "discovery_candidate"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_run.id", ondelete="CASCADE", name="fk_discovery_candidate_run"
        ),
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_source.id",
            ondelete="RESTRICT",
            name="fk_discovery_candidate_source",
        ),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "metadata_snapshot.id",
            ondelete="RESTRICT",
            name="fk_discovery_candidate_snapshot",
        ),
        nullable=False,
    )
    candidate_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment=CANDIDATE_TYPE_COMMENT
    )
    table_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    column_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment=CANDIDATE_PAYLOAD_COMMENT
    )
    evidence_refs: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa_text("'[]'::jsonb"),
        comment=CANDIDATE_EVIDENCE_COMMENT,
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="CANDIDATE",
        server_default=sa_text("'CANDIDATE'"),
        comment=CANDIDATE_STATUS_COMMENT,
    )
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "candidate_type IN ('OBJECT','PROPERTY','RELATION','CLASSIFICATION','METRIC')",
            name="ck_discovery_candidate_type",
        ),
        CheckConstraint(
            "status IN ('CANDIDATE','NEEDS_EVIDENCE','ACCEPTED','REJECTED','MERGED')",
            name="ck_discovery_candidate_status",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_discovery_candidate_payload_object"
        ),
        # 证据完整率 100% 是硬性红线（评测指标）⇒ 库层也不接受空证据。
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0",
            name="ck_discovery_candidate_evidence_nonempty",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_discovery_candidate_confidence_range",
        ),
        Index("ix_discovery_candidate_run", "run_id"),
        Index("ix_discovery_candidate_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryCandidate id={self.id} run={self.run_id} "
            f"type={self.candidate_type} status={self.status}>"
        )


class DiscoveryFinding(Base):
    """主数据问题（重复/冲突/缺失/映射）。"""

    __tablename__ = "discovery_finding"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_run.id", ondelete="CASCADE", name="fk_discovery_finding_run"
        ),
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_source.id",
            ondelete="RESTRICT",
            name="fk_discovery_finding_source",
        ),
        nullable=False,
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "metadata_snapshot.id",
            ondelete="RESTRICT",
            name="fk_discovery_finding_snapshot",
        ),
        nullable=True,
    )
    finding_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment=FINDING_TYPE_COMMENT
    )
    table_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    column_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    severity: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="WARNING",
        server_default=sa_text("'WARNING'"),
        comment=FINDING_SEVERITY_COMMENT,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="OPEN",
        server_default=sa_text("'OPEN'"),
        comment=FINDING_STATUS_COMMENT,
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "finding_type IN ('DUPLICATE','CONFLICT','MISSING','MAPPING')",
            name="ck_discovery_finding_type",
        ),
        CheckConstraint(
            "severity IN ('ERROR','WARNING','INFO')", name="ck_discovery_finding_severity"
        ),
        CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED','IGNORED')",
            name="ck_discovery_finding_status",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_discovery_finding_evidence_array",
        ),
        CheckConstraint("length(btrim(detail)) > 0", name="ck_discovery_finding_detail"),
        Index("ix_discovery_finding_run", "run_id"),
        Index("ix_discovery_finding_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryFinding id={self.id} run={self.run_id} "
            f"type={self.finding_type} status={self.status}>"
        )


class DiscoveryProposal(Base):
    """变更单：`diff_json` 必须可编译成 manifest v2；发布只走既有 `activate`。"""

    __tablename__ = "discovery_proposal"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_source.id",
            ondelete="RESTRICT",
            name="fk_discovery_proposal_source",
        ),
        nullable=False,
    )
    base_release_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "semantic_release.id",
            ondelete="RESTRICT",
            name="fk_discovery_proposal_base_release",
        ),
        nullable=True,
        comment=PROPOSAL_BASE_RELEASE_COMMENT,
    )
    release_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "semantic_release.id",
            ondelete="RESTRICT",
            name="fk_discovery_proposal_release",
        ),
        nullable=True,
        comment=PROPOSAL_RELEASE_COMMENT,
    )
    diff_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment=PROPOSAL_DIFF_COMMENT
    )
    # 同 `sample_values`：CHECK 用 SQL NULL 判空 ⇒ 必须 `none_as_null=True`。
    impact_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, comment=PROPOSAL_IMPACT_COMMENT
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="DRAFT",
        server_default=sa_text("'DRAFT'"),
        comment=PROPOSAL_STATUS_COMMENT,
    )
    proposer: Mapped[str] = mapped_column(String(64), nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','IN_REVIEW','APPROVED','REJECTED','PUBLISHED')",
            name="ck_discovery_proposal_status",
        ),
        CheckConstraint(
            "jsonb_typeof(diff_json) = 'object'", name="ck_discovery_proposal_diff_object"
        ),
        CheckConstraint(
            "impact_json IS NULL OR jsonb_typeof(impact_json) = 'object'",
            name="ck_discovery_proposal_impact_object",
        ),
        CheckConstraint("length(btrim(title)) > 0", name="ck_discovery_proposal_title"),
        Index("ix_discovery_proposal_status", "status"),
        Index("ix_discovery_proposal_source", "source_id"),
        Index("ix_discovery_proposal_release", "release_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryProposal id={self.id} status={self.status} "
            f"release={self.release_id}>"
        )


class DiscoveryReview(Base):
    """审核留痕（四眼；`reviewer != proposer` 由服务层拒绝同人自审）。"""

    __tablename__ = "discovery_review"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_proposal.id",
            ondelete="CASCADE",
            name="fk_discovery_review_proposal",
        ),
        nullable=False,
    )
    reviewer: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(
        String(20), nullable=False, comment=REVIEW_DECISION_COMMENT
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_task_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "approval_task.id",
            ondelete="SET NULL",
            name="fk_discovery_review_approval_task",
        ),
        nullable=True,
        comment=REVIEW_APPROVAL_TASK_COMMENT,
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVE','REJECT','REQUEST_CHANGES')",
            name="ck_discovery_review_decision",
        ),
        Index("ix_discovery_review_proposal", "proposal_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryReview id={self.id} proposal={self.proposal_id} "
            f"decision={self.decision}>"
        )


EVALUATION_DATASET_VERSION_COMMENT = "0167，WP-ONT-AI A3：评测数据集版本（业务签署的基线）"
EVALUATION_METRICS_COMMENT = "0167，WP-ONT-AI A3：评测指标（JSON 对象：候选数/证据完整率/分类分布等）"
EVALUATION_VERDICT_COMMENT = "0167，WP-ONT-AI A3：评测结论（PASS / FAIL / INCONCLUSIVE）"


class DiscoveryEvaluation(Base):
    """一次发现评测的结论（`discovery_evaluation`，0167）。

    只记录**结论与指标**：数据集版本由业务签署（`datasetVersion`），指标是
    可复算的摘要（不是原始明细）；明细仍由 `discovery_run` / `discovery_candidate` 承载。
    `verdict=INCONCLUSIVE` 用于「没有候选可评」这类**没结论**的情况
    —— 与 `FAIL` 区分，避免把「没跑出东西」当成「质量差」。
    """

    __tablename__ = "discovery_evaluation"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int | None] = mapped_column(
        BigIntFk,
        ForeignKey(
            "discovery_proposal.id",
            ondelete="RESTRICT",
            name="fk_discovery_evaluation_proposal",
        ),
        nullable=True,
    )
    dataset_version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment=EVALUATION_DATASET_VERSION_COMMENT
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment=EVALUATION_METRICS_COMMENT
    )
    verdict: Mapped[str] = mapped_column(
        String(20), nullable=False, comment=EVALUATION_VERDICT_COMMENT
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('PASS','FAIL','INCONCLUSIVE')",
            name="ck_discovery_evaluation_verdict",
        ),
        CheckConstraint(
            "jsonb_typeof(metrics_json) = 'object'",
            name="ck_discovery_evaluation_metrics_object",
        ),
        CheckConstraint(
            "length(btrim(dataset_version)) > 0",
            name="ck_discovery_evaluation_dataset_version",
        ),
        Index("ix_discovery_evaluation_proposal", "proposal_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DiscoveryEvaluation id={self.id} dataset={self.dataset_version} "
            f"verdict={self.verdict}>"
        )
