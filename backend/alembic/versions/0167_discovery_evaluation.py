"""发现评测结论：`discovery_evaluation`（WP-ONT-AI A3，0167）。

设计依据 `docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md` §4 与
`docs/update/1118/M7/ontology-evolution-program.md` G4-2。

一张表：一次评测 = 「数据集版本 + 可复算指标 + 结论」。

- `datasetVersion`：业务签署的评测基线版本（空串拒绝）；
- `metricsJson`：**JSON 对象**（候选数 / 证据完整率 / 分类分布等），不是原始明细；
- `verdict`：`PASS` / `FAIL` / `INCONCLUSIVE` —— **三态**：没有候选可评时是
  `INCONCLUSIVE`（没结论），不能混成 `FAIL`（质量差）。这是两件不同的事。

外键 `proposal_id` 可空（评测可以只针对一次运行，不对应提案），
有引用时 RESTRICT（评测证据不悬空）。

**手写**（禁 autogenerate）：纯加法 DDL，只建一张新表。
列注释 / CHECK 表达式与 `app/domain/discovery_pipeline_models.py` 逐字一致
（`alembic check` 只比名不比式 ⇒ 由单测钉住）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0167"
down_revision = "0166"
branch_labels = None
depends_on = None

_TABLE = "discovery_evaluation"

COLUMN_COMMENTS = {
    "dataset_version": "0167，WP-ONT-AI A3：评测数据集版本（业务签署的基线）",
    "metrics_json": "0167，WP-ONT-AI A3：评测指标（JSON 对象：候选数/证据完整率/分类分布等）",
    "verdict": "0167，WP-ONT-AI A3：评测结论（PASS / FAIL / INCONCLUSIVE）",
}

CHECK_EXPRESSIONS = {
    "verdict": "verdict IN ('PASS','FAIL','INCONCLUSIVE')",
    "metrics": "jsonb_typeof(metrics_json) = 'object'",
    "dataset_version": "length(btrim(dataset_version)) > 0",
}


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "dataset_version",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["dataset_version"],
        ),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(),
            nullable=False,
            comment=COLUMN_COMMENTS["metrics_json"],
        ),
        sa.Column(
            "verdict",
            sa.String(length=20),
            nullable=False,
            comment=COLUMN_COMMENTS["verdict"],
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["discovery_proposal.id"],
            name="fk_discovery_evaluation_proposal",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["verdict"], name="ck_discovery_evaluation_verdict"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["metrics"], name="ck_discovery_evaluation_metrics_object"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["dataset_version"],
            name="ck_discovery_evaluation_dataset_version",
        ),
    )
    op.create_index("ix_discovery_evaluation_proposal", _TABLE, ["proposal_id"])


def downgrade() -> None:
    op.drop_index("ix_discovery_evaluation_proposal", table_name=_TABLE)
    op.drop_table(_TABLE)
