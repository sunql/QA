"""本体发现流水线：run / profile / candidate / finding / proposal / review（WP-ONT-AI A2，0166）。

设计依据 `docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md` §4 与
`docs/update/1118/M7/ontology-evolution-program.md` §二。

六张表串起「运行 → 证据 → 候选 → 提案 → 审核」：

- `discovery_run`：运行记录 + **LLM 计量**（`prompt_tokens` / `completion_tokens` / `cost`）。
- `discovery_profile`：确定性剖析结果（行数 / 空值 / 基数 / 值域）+ 可复现 SQL 摘要。
- `discovery_candidate`：候选；`evidence_refs` **不得为空**（证据完整率 100% 是硬红线）。
- `discovery_finding`：主数据问题（重复 / 冲突 / 缺失 / 映射）。
- `discovery_proposal`：变更单；`diff_json` 入库前必须通过 `compileManifest`，
  发布只走既有 `activate`（本迁移不建任何发布写路径）。
- `discovery_review`：审核留痕；`approval_task_id` 允许为空（二期接 P5 审批时回填）。

## 约束（各有明确目的）

- **运行/候选/问题/剖析 FK → `discovery_run` CASCADE**：run 被删 ⇒ 它产出的证据一并消失
  （孤儿证据会让人以为那次运行还存在）。
- **`source_id` / `snapshot_id` → RESTRICT**：证据链不悬空。
- **`release_id` / `base_release_id` → `semantic_release` RESTRICT**：发布是治理对象，
  有提案引用时不允许删。
- **`approval_task_id` → SET NULL**：审批任务属 P5 域，二期接入前为空；被删时保留审核留痕。
- `ck_discovery_candidate_evidence_nonempty`：空证据的候选 = 不可复核的结论 ⇒ 库层拒绝。
- 字面量 CHECK（trigger / status / type / severity / decision）：绕过 ORM 的写入只能落进
  「看起来有状态、其实没人知道状态」的坏行。
- `uq_discovery_profile_scope`：`(run_id, table_name, COALESCE(column_name, ''))` 唯一 ——
  `COALESCE` 不可省（PG 里 NULL 互不相等，表级指标行会无限重复；同 `semantic_release_check` 的坑）。

## 四眼审核

`reviewer != proposer` 是**跨表**约束，CHECK 表达不了 ⇒ 服务层拒绝（同人自审 409），
守卫测试覆盖。`approval_task_id` 非空时要求 `subject_type='ontology_proposal'`，同样服务层判。

**手写**（禁 autogenerate）：纯加法 DDL，只建六张新表，不触碰任何既有表。
列 `comment` 与 CHECK 表达式与 `app/domain/discovery_pipeline_models.py` **逐字一致**
（`alembic check` 只比约束名不比式 ⇒ 表达式由单测双向钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text as sa_text
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0166"
down_revision = "0165"
branch_labels = None
depends_on = None

_TABLE_RUN = "discovery_run"
_TABLE_PROFILE = "discovery_profile"
_TABLE_CANDIDATE = "discovery_candidate"
_TABLE_FINDING = "discovery_finding"
_TABLE_PROPOSAL = "discovery_proposal"
_TABLE_REVIEW = "discovery_review"

# 与 `app/domain/discovery_pipeline_models.py` 的 ORM 列注释逐字一致
# （单测 test_discovery_pipeline_models.py 双向钉住）。
COLUMN_COMMENTS = {
    "run_trigger": "0166，WP-ONT-AI A2：运行触发方式（MANUAL / SCHEDULED / DIFF）",
    "run_status": "0166，WP-ONT-AI A2：运行状态（RUNNING / SUCCEEDED / FAILED）",
    "run_model_version": "0166，WP-ONT-AI A2：本次运行使用的模型版本",
    "run_prompt_version": "0166，WP-ONT-AI A2：本次运行使用的 prompt 版本",
    "run_usage": "0166，WP-ONT-AI A2：LLM 用量（prompt_tokens / completion_tokens / cost）",
    "profile_table": "0166，WP-ONT-AI A2：被剖析的表名",
    "profile_column": "0166，WP-ONT-AI A2：被剖析的列名（NULL = 表级指标）",
    "profile_digest": "0166，WP-ONT-AI A2：剖析 SQL 的 SHA-256 摘要（可复现）",
    "candidate_type": "0166，WP-ONT-AI A2：候选类型（OBJECT / PROPERTY / RELATION / CLASSIFICATION / METRIC）",
    "candidate_payload": "0166，WP-ONT-AI A2：候选内容（JSON 对象，结构按 candidate_type）",
    "candidate_evidence": "0166，WP-ONT-AI A2：证据引用（JSON 数组，复用 KnowledgeEvidence 契约；不得为空）",
    "candidate_status": "0166，WP-ONT-AI A2：候选状态（CANDIDATE / NEEDS_EVIDENCE / ACCEPTED / REJECTED / MERGED）",
    "finding_type": "0166，WP-ONT-AI A2：问题类型（DUPLICATE / CONFLICT / MISSING / MAPPING）",
    "finding_severity": "0166，WP-ONT-AI A2：问题严重级别（ERROR / WARNING / INFO）",
    "finding_status": "0166，WP-ONT-AI A2：问题状态（OPEN / ACKNOWLEDGED / RESOLVED / IGNORED）",
    "proposal_base_release": "0166，WP-ONT-AI A2：提案基线发布（diff 相对它计算）",
    "proposal_release": "0166，WP-ONT-AI A2：审批后生成的语义发布（走既有 activate）",
    "proposal_diff": "0166，WP-ONT-AI A2：变更内容（JSON 对象；入库前必须通过 compileManifest）",
    "proposal_impact": "0166，WP-ONT-AI A2：影响面摘要（受影响类 / 指标 / 绑定）",
    "proposal_status": "0166，WP-ONT-AI A2：提案状态（DRAFT / IN_REVIEW / APPROVED / REJECTED / PUBLISHED）",
    "review_decision": "0166，WP-ONT-AI A2：审核结论（APPROVE / REJECT / REQUEST_CHANGES）",
    "review_approval_task": "0166，WP-ONT-AI A2：可选：P5 审批任务（二期回填；非空时 subject_type 必须是 ontology_proposal）",
}

# 与 ORM 逐字一致的 CHECK 表达式（`alembic check` 不比式 ⇒ 单测钉住）。
CHECK_EXPRESSIONS = {
    "run_trigger": "trigger IN ('MANUAL','SCHEDULED','DIFF')",
    "run_status": "status IN ('RUNNING','SUCCEEDED','FAILED')",
    "run_usage": "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost >= 0",
    "profile_counts": (
        "(row_count IS NULL OR row_count >= 0) "
        "AND (null_count IS NULL OR null_count >= 0) "
        "AND (distinct_count IS NULL OR distinct_count >= 0)"
    ),
    "profile_digest": "sql_digest ~ '^[0-9a-f]{64}$'",
    "profile_samples": "sample_values IS NULL OR jsonb_typeof(sample_values) = 'array'",
    "candidate_type": "candidate_type IN ('OBJECT','PROPERTY','RELATION','CLASSIFICATION','METRIC')",
    "candidate_status": "status IN ('CANDIDATE','NEEDS_EVIDENCE','ACCEPTED','REJECTED','MERGED')",
    "candidate_payload": "jsonb_typeof(payload) = 'object'",
    "candidate_evidence": (
        "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0"
    ),
    "candidate_confidence": "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    "finding_type": "finding_type IN ('DUPLICATE','CONFLICT','MISSING','MAPPING')",
    "finding_severity": "severity IN ('ERROR','WARNING','INFO')",
    "finding_status": "status IN ('OPEN','ACKNOWLEDGED','RESOLVED','IGNORED')",
    "finding_evidence": "jsonb_typeof(evidence_refs) = 'array'",
    "finding_detail": "length(btrim(detail)) > 0",
    "proposal_status": "status IN ('DRAFT','IN_REVIEW','APPROVED','REJECTED','PUBLISHED')",
    "proposal_diff": "jsonb_typeof(diff_json) = 'object'",
    "proposal_impact": "impact_json IS NULL OR jsonb_typeof(impact_json) = 'object'",
    "proposal_title": "length(btrim(title)) > 0",
    "review_decision": "decision IN ('APPROVE','REJECT','REQUEST_CHANGES')",
}


def _timestamps(*, updated: bool) -> list[sa.Column]:
    columns = [
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        )
    ]
    if updated:
        columns.append(
            sa.Column(
                "updated_time",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            )
        )
    return columns


def _createRun() -> None:
    op.create_table(
        _TABLE_RUN,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "trigger",
            sa.String(length=20),
            nullable=False,
            server_default=sa_text("'MANUAL'"),
            comment=COLUMN_COMMENTS["run_trigger"],
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa_text("'RUNNING'"),
            comment=COLUMN_COMMENTS["run_status"],
        ),
        sa.Column(
            "model_version",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["run_model_version"],
        ),
        sa.Column(
            "prompt_version",
            sa.String(length=32),
            nullable=True,
            comment=COLUMN_COMMENTS["run_prompt_version"],
        ),
        sa.Column(
            "prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cost",
            sa.Numeric(12, 6),
            nullable=False,
            server_default=sa.text("0"),
            comment=COLUMN_COMMENTS["run_usage"],
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        *_timestamps(updated=False),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_source.id"],
            name="fk_discovery_run_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["metadata_snapshot.id"],
            name="fk_discovery_run_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["run_trigger"], name="ck_discovery_run_trigger"),
        sa.CheckConstraint(CHECK_EXPRESSIONS["run_status"], name="ck_discovery_run_status"),
        sa.CheckConstraint(CHECK_EXPRESSIONS["run_usage"], name="ck_discovery_run_usage_nonneg"),
    )
    op.create_index("ix_discovery_run_source", _TABLE_RUN, ["source_id"])
    op.create_index("ix_discovery_run_status", _TABLE_RUN, ["status"])


def _createProfile() -> None:
    op.create_table(
        _TABLE_PROFILE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "table_name",
            sa.String(length=128),
            nullable=False,
            comment=COLUMN_COMMENTS["profile_table"],
        ),
        sa.Column(
            "column_name",
            sa.String(length=128),
            nullable=True,
            comment=COLUMN_COMMENTS["profile_column"],
        ),
        sa.Column("row_count", sa.BigInteger(), nullable=True),
        sa.Column("null_count", sa.BigInteger(), nullable=True),
        sa.Column("distinct_count", sa.BigInteger(), nullable=True),
        sa.Column("min_value", sa.String(length=200), nullable=True),
        sa.Column("max_value", sa.String(length=200), nullable=True),
        sa.Column("sample_values", postgresql.JSONB(), nullable=True),
        sa.Column(
            "sql_digest",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["profile_digest"],
        ),
        *_timestamps(updated=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_run.id"],
            name="fk_discovery_profile_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_source.id"],
            name="fk_discovery_profile_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["metadata_snapshot.id"],
            name="fk_discovery_profile_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["profile_counts"], name="ck_discovery_profile_counts_nonneg"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["profile_digest"], name="ck_discovery_profile_digest_sha256"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["profile_samples"], name="ck_discovery_profile_samples_array"
        ),
    )
    op.create_index("ix_discovery_profile_run", _TABLE_PROFILE, ["run_id"])
    op.create_index(
        "uq_discovery_profile_scope",
        _TABLE_PROFILE,
        ["run_id", "table_name", sa_text("COALESCE(column_name, '')")],
        unique=True,
    )


def _createCandidate() -> None:
    op.create_table(
        _TABLE_CANDIDATE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "candidate_type",
            sa.String(length=20),
            nullable=False,
            comment=COLUMN_COMMENTS["candidate_type"],
        ),
        sa.Column("table_name", sa.String(length=128), nullable=True),
        sa.Column("column_name", sa.String(length=128), nullable=True),
        sa.Column("target_code", sa.String(length=200), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            comment=COLUMN_COMMENTS["candidate_payload"],
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa_text("'[]'::jsonb"),
            comment=COLUMN_COMMENTS["candidate_evidence"],
        ),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa_text("'CANDIDATE'"),
            comment=COLUMN_COMMENTS["candidate_status"],
        ),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_time", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(updated=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_run.id"],
            name="fk_discovery_candidate_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_source.id"],
            name="fk_discovery_candidate_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["metadata_snapshot.id"],
            name="fk_discovery_candidate_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["candidate_type"], name="ck_discovery_candidate_type"),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["candidate_status"], name="ck_discovery_candidate_status"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["candidate_payload"], name="ck_discovery_candidate_payload_object"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["candidate_evidence"],
            name="ck_discovery_candidate_evidence_nonempty",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["candidate_confidence"],
            name="ck_discovery_candidate_confidence_range",
        ),
    )
    op.create_index("ix_discovery_candidate_run", _TABLE_CANDIDATE, ["run_id"])
    op.create_index("ix_discovery_candidate_status", _TABLE_CANDIDATE, ["status"])


def _createFinding() -> None:
    op.create_table(
        _TABLE_FINDING,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "finding_type",
            sa.String(length=20),
            nullable=False,
            comment=COLUMN_COMMENTS["finding_type"],
        ),
        sa.Column("table_name", sa.String(length=128), nullable=True),
        sa.Column("column_name", sa.String(length=128), nullable=True),
        sa.Column(
            "severity",
            sa.String(length=10),
            nullable=False,
            server_default=sa_text("'WARNING'"),
            comment=COLUMN_COMMENTS["finding_severity"],
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa_text("'OPEN'"),
            comment=COLUMN_COMMENTS["finding_status"],
        ),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa_text("'[]'::jsonb"),
        ),
        *_timestamps(updated=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_run.id"],
            name="fk_discovery_finding_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_source.id"],
            name="fk_discovery_finding_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["metadata_snapshot.id"],
            name="fk_discovery_finding_snapshot",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["finding_type"], name="ck_discovery_finding_type"),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["finding_severity"], name="ck_discovery_finding_severity"
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["finding_status"], name="ck_discovery_finding_status"),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["finding_evidence"], name="ck_discovery_finding_evidence_array"
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["finding_detail"], name="ck_discovery_finding_detail"),
    )
    op.create_index("ix_discovery_finding_run", _TABLE_FINDING, ["run_id"])
    op.create_index("ix_discovery_finding_status", _TABLE_FINDING, ["status"])


def _createProposal() -> None:
    op.create_table(
        _TABLE_PROPOSAL,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "base_release_id",
            sa.BigInteger(),
            nullable=True,
            comment=COLUMN_COMMENTS["proposal_base_release"],
        ),
        sa.Column(
            "release_id",
            sa.BigInteger(),
            nullable=True,
            comment=COLUMN_COMMENTS["proposal_release"],
        ),
        sa.Column(
            "diff_json",
            postgresql.JSONB(),
            nullable=False,
            comment=COLUMN_COMMENTS["proposal_diff"],
        ),
        sa.Column(
            "impact_json",
            postgresql.JSONB(),
            nullable=True,
            comment=COLUMN_COMMENTS["proposal_impact"],
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa_text("'DRAFT'"),
            comment=COLUMN_COMMENTS["proposal_status"],
        ),
        sa.Column("proposer", sa.String(length=64), nullable=False),
        *_timestamps(updated=True),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_source.id"],
            name="fk_discovery_proposal_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["base_release_id"],
            ["semantic_release.id"],
            name="fk_discovery_proposal_base_release",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["semantic_release.id"],
            name="fk_discovery_proposal_release",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["proposal_status"], name="ck_discovery_proposal_status"),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["proposal_diff"], name="ck_discovery_proposal_diff_object"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["proposal_impact"], name="ck_discovery_proposal_impact_object"
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["proposal_title"], name="ck_discovery_proposal_title"),
    )
    op.create_index("ix_discovery_proposal_status", _TABLE_PROPOSAL, ["status"])
    op.create_index("ix_discovery_proposal_source", _TABLE_PROPOSAL, ["source_id"])
    op.create_index("ix_discovery_proposal_release", _TABLE_PROPOSAL, ["release_id"])


def _createReview() -> None:
    op.create_table(
        _TABLE_REVIEW,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewer", sa.String(length=64), nullable=False),
        sa.Column(
            "decision",
            sa.String(length=20),
            nullable=False,
            comment=COLUMN_COMMENTS["review_decision"],
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "approval_task_id",
            sa.BigInteger(),
            nullable=True,
            comment=COLUMN_COMMENTS["review_approval_task"],
        ),
        *_timestamps(updated=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["discovery_proposal.id"],
            name="fk_discovery_review_proposal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approval_task_id"],
            ["approval_task.id"],
            name="fk_discovery_review_approval_task",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(CHECK_EXPRESSIONS["review_decision"], name="ck_discovery_review_decision"),
    )
    op.create_index("ix_discovery_review_proposal", _TABLE_REVIEW, ["proposal_id"])


def upgrade() -> None:
    _createRun()
    _createProfile()
    _createCandidate()
    _createFinding()
    _createProposal()
    _createReview()


def downgrade() -> None:
    op.drop_index("ix_discovery_review_proposal", table_name=_TABLE_REVIEW)
    op.drop_table(_TABLE_REVIEW)
    op.drop_index("ix_discovery_proposal_release", table_name=_TABLE_PROPOSAL)
    op.drop_index("ix_discovery_proposal_source", table_name=_TABLE_PROPOSAL)
    op.drop_index("ix_discovery_proposal_status", table_name=_TABLE_PROPOSAL)
    op.drop_table(_TABLE_PROPOSAL)
    op.drop_index("ix_discovery_finding_status", table_name=_TABLE_FINDING)
    op.drop_index("ix_discovery_finding_run", table_name=_TABLE_FINDING)
    op.drop_table(_TABLE_FINDING)
    op.drop_index("ix_discovery_candidate_status", table_name=_TABLE_CANDIDATE)
    op.drop_index("ix_discovery_candidate_run", table_name=_TABLE_CANDIDATE)
    op.drop_table(_TABLE_CANDIDATE)
    op.drop_index("uq_discovery_profile_scope", table_name=_TABLE_PROFILE)
    op.drop_index("ix_discovery_profile_run", table_name=_TABLE_PROFILE)
    op.drop_table(_TABLE_PROFILE)
    op.drop_index("ix_discovery_run_status", table_name=_TABLE_RUN)
    op.drop_index("ix_discovery_run_source", table_name=_TABLE_RUN)
    op.drop_table(_TABLE_RUN)
