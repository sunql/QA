"""wiki_ontology_link + nl2sql_wiki_trace（feat-wiki-ontology-link，Task 1）。

新增 2 张表承接「wiki ↔ ontology 链接设计」(spec 0d41413) 的写入路径：

- ``wiki_ontology_link``：page/chunk ↔ ontology(class/property) 多对多关系。
  - 可撤销（``revoked_time IS NULL`` 视为活动关系），同 (page_id, chunk_id,
    ontology_type, ontology_id) 撤销后唯一索引允许重新插入，实现"软删+复活"。
  - chunk_id 可空：NULL = page 级语义；非空 = chunk 级定位。
  - 三个 partial unique/普通索引都 ``WHERE revoked_time IS NULL``，
    撤销记录不占主键空间、不参与去重。
  - ``chk_link_granularity`` 改用 raw DDL（match brief）：含 ``length()`` 表达式
    的 CHECK 不适合 ``sa.CheckConstraint(...)`` 字面量写法。

- ``nl2sql_wiki_trace``：每次 NL2SQL 调用注入业务规则的埋点行（用于量本分析 + 效果
  归因：哪些 ontology 规则被注入、占 prompt 多少字符、向量命中率评分）。
  - 无 FK 指向 wiki_page/ontology_*：trace 是 snapshot，源被删后仍要留账。
  - ``(session_id, created_at)`` 索引供会话回放与窗口聚合。

**幂等**：与本项目其他迁移同口径 —— 重跑本 upgrade 会因表/索引已存在而报错。
无 ``IF NOT EXISTS`` 是惯例：alembic 链每步只走一次，幂等由 alembic_version 表守，
不要为重复跑而 ``IF NOT EXISTS``（已 0034 后未再新增此类）。

Revision ID: 0091
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0091_wiki_ontology_link"
down_revision: str | None = "0090_llm_cache_hit_multiplier"
branch_labels = None
depends_on = None

TABLE_LINK = "wiki_ontology_link"
TABLE_TRACE = "nl2sql_wiki_trace"


def upgrade() -> None:
    op.create_table(
        TABLE_LINK,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("page_id", sa.String(64), nullable=False),
        sa.Column("chunk_id", sa.String(64), nullable=True),
        sa.Column("ontology_type", sa.String(16), nullable=False),
        sa.Column("ontology_id", sa.BigInteger, nullable=False),
        sa.Column(
            "weight",
            sa.Numeric(3, 2),
            nullable=False,
            server_default=sa.text("1.00"),
        ),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_by", sa.BigInteger, nullable=False),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_time", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["page_id"], ["wiki_page.page_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "ontology_type IN ('class','property')",
            name="chk_link_type",
        ),
        sa.CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="chk_link_weight",
        ),
    )
    # 含 length() 的 CHECK 用 raw DDL（brief §1.3 指引）
    op.execute(
        f"ALTER TABLE {TABLE_LINK} ADD CONSTRAINT chk_link_granularity "
        f"CHECK (chunk_id IS NULL OR length(chunk_id) <= 64)"
    )
    op.create_index(
        "ix_wol_ontology",
        TABLE_LINK,
        ["ontology_type", "ontology_id"],
        postgresql_where=text("revoked_time IS NULL"),
    )
    op.create_index(
        "ix_wol_page",
        TABLE_LINK,
        ["page_id"],
        postgresql_where=text("revoked_time IS NULL"),
    )
    op.create_index(
        "uq_wol_active",
        TABLE_LINK,
        ["page_id", "chunk_id", "ontology_type", "ontology_id"],
        unique=True,
        postgresql_where=text("revoked_time IS NULL"),
    )

    op.create_table(
        TABLE_TRACE,
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("ontology_type", sa.String(16), nullable=False),
        sa.Column("ontology_id", sa.BigInteger, nullable=False),
        sa.Column("page_id", sa.String(64), nullable=False),
        sa.Column("chunk_id", sa.String(64), nullable=True),
        sa.Column("prompt_position", sa.String(32), nullable=False),
        sa.Column("injected_chars", sa.Integer, nullable=False),
        sa.Column("score", sa.Numeric(5, 3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_nlwt_session",
        TABLE_TRACE,
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_nlwt_session", table_name=TABLE_TRACE)
    op.drop_table(TABLE_TRACE)
    op.drop_index("uq_wol_active", table_name=TABLE_LINK)
    op.drop_index("ix_wol_page", table_name=TABLE_LINK)
    op.drop_index("ix_wol_ontology", table_name=TABLE_LINK)
    op.drop_table(TABLE_LINK)
