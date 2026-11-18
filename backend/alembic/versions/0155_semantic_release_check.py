"""发布校验结论表：`semantic_release_check`（0155）。

计划 §1.2：「发布校验落表：`semantic_release_check`（校验项、severity、result、detail）」；
准则 H + D1 裁决：V0.3 草案里的 `procurement_ontology_release_check` **建**，改名为
`semantic_release_check`（不新建 `procurement/*` 语义重复表）。

设计依据 `Harness/changes/2026-11-18-m1-semantic-release-check/summary.md` §3。

约束：

- FK → `semantic_release.id` **CASCADE**：发布被删 ⇒ 校验证据一并消失。
- `uq_semantic_release_check_unique`：`(release_id, check_code, COALESCE(target_key, ''))`
  唯一。`COALESCE` 不是可选的 —— PostgreSQL 的 NULL 互不相等，
  直接建唯一索引会让 `target_key IS NULL` 的发布级校验项
  （快照完整性 / Manifest 契约）无限重复落库。
- `ck_*`：severity / result 字面量 + 校验项名非空。绕过 ORM 的写入只能落进
  「看起来已校验、其实没人知道结论」的坏行。

**手写**（禁 autogenerate）：纯加法 DDL，只建一张新表，不触碰任何既有表。
列与 CHECK 表达式与 `app/domain/semantic_release_check_models.py` 的 ORM 声明一致。

迁移号说明：head 0154（采购源绑定两表）→ 本批 0155。
A4-0（审批面放开 `instance_id`）也需要一个新号，按「串行占号、不许插队」规则
由本批先占 0155，A4-0 落地时取当时的下一个空号。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0155"
down_revision = "0154"
branch_labels = None
depends_on = None

_TABLE = "semantic_release_check"

# 与 ORM 逐字一致的 CHECK 表达式。
CHECK_EXPRESSIONS = {
    "severity": "severity IN ('ERROR','WARNING')",
    "result": "result IN ('PASS','FAIL')",
    "item_not_empty": "length(check_code) > 0 AND length(check_item) > 0",
}

# 与 ORM 逐字一致的列注释。
COLUMN_COMMENTS = {
    "release_id": "0155，M1-1.2：所属发布（semantic_release.id；发布删除 ⇒ 证据 CASCADE）",
    "check_code": "0155，M1-1.2：校验项编码（机器可读，如 METRIC_SOURCE_PRESENT）",
    "check_item": "0155，M1-1.2：校验项名称（中文，展示用）",
    "severity": "0155，M1-1.2：失败时的严重级别（ERROR = 阻断发布；WARNING = 治理债）",
    "result": "0155，M1-1.2：本次结论（PASS / FAIL）",
    "target_type": "0155，M1-1.2：被校验对象类型（RELEASE / METRIC / …）",
    "target_key": "0155，M1-1.2：被校验对象标识（指标名等；NULL = 发布级校验项）",
    "detail": "0155，M1-1.2：结论详情（人类可读）",
    "checked_time": "0155，M1-1.2：校验发生时刻（覆盖写 ⇒ 每次评估刷新）",
    "checked_by": "0155，M1-1.2：触发本次校验的操作人（治理可追溯）",
}


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "release_id",
            sa.BigInteger(),
            nullable=False,
            comment=COLUMN_COMMENTS["release_id"],
        ),
        sa.Column(
            "check_code",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["check_code"],
        ),
        sa.Column(
            "check_item",
            sa.String(length=200),
            nullable=False,
            comment=COLUMN_COMMENTS["check_item"],
        ),
        sa.Column(
            "severity",
            sa.String(length=10),
            nullable=False,
            comment=COLUMN_COMMENTS["severity"],
        ),
        sa.Column(
            "result",
            sa.String(length=10),
            nullable=False,
            comment=COLUMN_COMMENTS["result"],
        ),
        sa.Column(
            "target_type",
            sa.String(length=32),
            nullable=True,
            comment=COLUMN_COMMENTS["target_type"],
        ),
        sa.Column(
            "target_key",
            sa.String(length=200),
            nullable=True,
            comment=COLUMN_COMMENTS["target_key"],
        ),
        sa.Column("detail", sa.Text(), nullable=False, comment=COLUMN_COMMENTS["detail"]),
        sa.Column(
            "checked_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment=COLUMN_COMMENTS["checked_time"],
        ),
        sa.Column(
            "checked_by",
            sa.String(length=64),
            nullable=True,
            comment=COLUMN_COMMENTS["checked_by"],
        ),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["semantic_release.id"],
            name="fk_semantic_release_check_release",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["severity"],
            name="ck_semantic_release_check_severity",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["result"],
            name="ck_semantic_release_check_result",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["item_not_empty"],
            name="ck_semantic_release_check_item_not_empty",
        ),
    )
    op.create_index(
        "ix_semantic_release_check_release",
        _TABLE,
        ["release_id"],
    )
    op.create_index(
        "uq_semantic_release_check_unique",
        _TABLE,
        ["release_id", "check_code", sa.text("COALESCE(target_key, '')")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_semantic_release_check_unique", table_name=_TABLE)
    op.drop_index("ix_semantic_release_check_release", table_name=_TABLE)
    op.drop_table(_TABLE)
