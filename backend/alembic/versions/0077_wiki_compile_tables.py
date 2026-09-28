"""补建 wiki_compile_task / wiki_compile_item 两表（僵尸迁移修复）。

**背景**：``0063_wiki_compile_tables`` 是一个空壳（``pass``，只为 revision-map
完整性而存在），P3 批量知识编译器的两张表从未被任何迁移真正建出。ORM
（``app/domain/wiki_compile_models.py``）与 schema、service、API 均已就位，
于是 ``schema_drift`` 对账以 **blocking** 级别报「ORM 声明但 DB 缺失的表」——
测试库在 head 上仍缺表，prod 同样缺。这是本项目第三例「僵尸特性」：
迁移/ORM/代码三方缺一即静默漂移（前两例：eval-report 的 ORM 未提交、
OntologyProperty 的 4 个约束列未进 ORM）。

列/索引/约束与 ORM 逐项对齐（drift 校验已到列+索引粒度，缺一即红）：

- 主键 BIGSERIAL（``BigIntPk`` 在 PG 方言即 BigInteger 自增）；
- ``status`` 的 ``server_default="PENDING"`` 是**唯一**的 DB 级默认值
  （其余 ``default=0`` 等都是 Python 侧默认，不落 DDL）；
- ``total_cost_usd`` NUMERIC(12,6)；
- ``mechanism_counts`` JSONB；
- task 两个单列索引（status / created_time）；
- item 复合索引 (task_id, status) + 唯一约束 (task_id, page_id)——
  drift 对约束支撑索引按**列集合**认亲，命名约束与 ORM 同名即认；
- ``task_id`` FK 带 ``ON DELETE CASCADE``（ORM ondelete 同步）；
- task 的两个 model FK 不带 ondelete（RESTRICT），与 ORM 一致。

本迁移只建新表，不触碰任何既有对象；``upgrade``/``downgrade`` 严格对称。
prod 与测试库均需执行（两库结构必须同步收敛，见 0060 的教训）。

Revision ID: 0077
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0077_wiki_compile_tables"
down_revision: str | None = "0076_data_quality_rule_params"
branch_labels = None
depends_on = None

# revision id 长度 24 ≤ 32（alembic_version.version_num 上限，见 0062 教训）


def upgrade() -> None:
    op.create_table(
        "wiki_compile_task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(length=30), nullable=True),
        sa.Column("selected_model_id", sa.BigInteger(), nullable=True),
        sa.Column("fallback_model_id", sa.BigInteger(), nullable=True),
        sa.Column("total_items", sa.Integer(), nullable=False),
        sa.Column("success_items", sa.Integer(), nullable=False),
        sa.Column("skipped_items", sa.Integer(), nullable=False),
        sa.Column("failed_items", sa.Integer(), nullable=False),
        sa.Column("total_cost_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("started_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_time", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["selected_model_id"], ["llm_config.id"]
        ),
        sa.ForeignKeyConstraint(
            ["fallback_model_id"], ["llm_config.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_wiki_compile_task_status",
        "wiki_compile_task",
        ["status"],
    )
    op.create_index(
        "ix_wiki_compile_task_created",
        "wiki_compile_task",
        ["created_time"],
    )

    op.create_table(
        "wiki_compile_item",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("mechanism_counts", JSONB(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_time", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["wiki_compile_task.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "page_id", name="uq_wiki_compile_item_task_page"
        ),
    )
    op.create_index(
        "ix_wiki_compile_item_task_status",
        "wiki_compile_item",
        ["task_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wiki_compile_item_task_status", table_name="wiki_compile_item"
    )
    op.drop_table("wiki_compile_item")
    op.drop_index(
        "ix_wiki_compile_task_created", table_name="wiki_compile_task"
    )
    op.drop_index(
        "ix_wiki_compile_task_status", table_name="wiki_compile_task"
    )
    op.drop_table("wiki_compile_task")
