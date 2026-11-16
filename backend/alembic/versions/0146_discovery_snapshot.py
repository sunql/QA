"""本体发现域：`discovery_source` / `metadata_snapshot`（WP-ONT-AI A1-1，0146）。

设计依据 `docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md` 与
`Harness/changes/2026-11-16-ontology-discovery-ai/plan.md` D-1 / D-2 / D-9。

两张表：

- `discovery_source`：**授权目录**。只引用既有 `data_source.id`（FK RESTRICT），
  **不新建连接** ⇒ 不出现第二套数据源真相源；凭据、只读护栏、主机白名单、审计
  全部沿用 `business_db_pool` / `datasource_service`。
- `metadata_snapshot`：**不可变**的 schema 结构快照。内容寻址
  （`schema_hash` = 规范化 JSON 的 SHA-256）：同源同内容复用同一行 ⇒ 重复采集幂等；
  内容变化 ⇒ **新增**一行。行只增不改（无 `updated_time`，服务层不提供更新路径）。

约束各有明确目的：

- `uq_discovery_source_name`：发现源名称唯一，避免同一份授权被登记成两条。
- `uq_metadata_snapshot_source_hash`：内容寻址的唯一性（幂等采集的库级兜底）。
- `ck_discovery_source_status`：状态字面量钉死在库侧（防 `Actice` 之类变体）。
- `ck_metadata_snapshot_hash_sha256`：哈希格式钉死（防非 SHA-256 混入）。
- `ck_metadata_snapshot_is_object`：快照必须是 JSON 对象（防塞成数组/字符串）。
- `ck_metadata_snapshot_counts_nonneg`：计数非负。
- 两个 FK 全部 `RESTRICT`：有快照的发现源不可删 ⇒ 快照证据不悬空。

**手写**（禁 autogenerate）：纯加法 DDL，只建两张新表，不触碰任何既有表。
列 `comment` 与 CHECK 表达式与 `app/domain/discovery_models.py` 的 ORM 声明
**逐字一致**（alembic autogenerate 会比对列注释；`alembic check` 只比约束名不比式，
表达式这一层由 `app/tests/unit/test_discovery_models.py` 的单测双向钉住）。

**修订策略**：本迁移随 PR 落地、尚未在任何持久环境执行 ⇒ 评审期补的约束
原地加在本 revision 里，不新开迁移。一旦某个库已 stamp 到 0146，
再改约束必须新开迁移。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None

_TABLE_SOURCE = "discovery_source"
_TABLE_SNAPSHOT = "metadata_snapshot"

# 与 `app/domain/discovery_models.py` 的 ORM 列注释逐字一致
# （单测 test_discovery_models.py 双向钉住）。
COLUMN_COMMENTS = {
    "source_name": "0146，WP-ONT-AI A1：发现源名称（同一实例内唯一）",
    "source_access_policy": "0146，WP-ONT-AI A1：访问策略（JSON 对象；键仅 mode=READ_ONLY 与 schema_owner=库模式属主，未知键拒绝）",
    "source_status": "0146，WP-ONT-AI A1：发现源状态（ACTIVE / DISABLED）",
    "snapshot_hash": "0146，WP-ONT-AI A1：schema 结构快照的内容哈希（规范化 JSON 的 SHA-256 十六进制）",
    "snapshot_json": "0146，WP-ONT-AI A1：schema 结构快照内容（表 / 列 / 类型 / 主外键 / 注释）",
    "snapshot_captured_at": "0146，WP-ONT-AI A1：快照采集时刻",
}

# 与 ORM 逐字一致的 CHECK 表达式。
CHECK_EXPRESSIONS = {
    "source_status": "status IN ('ACTIVE','DISABLED')",
    "snapshot_hash": "schema_hash ~ '^[0-9a-f]{64}$'",
    "snapshot_is_object": "jsonb_typeof(snapshot_json) = 'object'",
    "snapshot_counts": "table_count >= 0 AND column_count >= 0",
}


def upgrade() -> None:
    op.create_table(
        _TABLE_SOURCE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            comment=COLUMN_COMMENTS["source_name"],
        ),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column(
            "access_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment=COLUMN_COMMENTS["source_access_policy"],
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'ACTIVE'"),
            comment=COLUMN_COMMENTS["source_status"],
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
            ["datasource_id"],
            ["data_source.id"],
            name="fk_discovery_source_datasource",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("name", name="uq_discovery_source_name"),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["source_status"],
            name="ck_discovery_source_status",
        ),
    )
    op.create_index(
        "ix_discovery_source_datasource",
        _TABLE_SOURCE,
        ["datasource_id"],
    )

    op.create_table(
        _TABLE_SNAPSHOT,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "schema_hash",
            sa.String(length=64),
            nullable=False,
            comment=COLUMN_COMMENTS["snapshot_hash"],
        ),
        sa.Column(
            "snapshot_json",
            postgresql.JSONB(),
            nullable=False,
            comment=COLUMN_COMMENTS["snapshot_json"],
        ),
        sa.Column(
            "table_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "column_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment=COLUMN_COMMENTS["snapshot_captured_at"],
        ),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["discovery_source.id"],
            name="fk_metadata_snapshot_source",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "source_id", "schema_hash", name="uq_metadata_snapshot_source_hash"
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["snapshot_hash"],
            name="ck_metadata_snapshot_hash_sha256",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["snapshot_is_object"],
            name="ck_metadata_snapshot_is_object",
        ),
        sa.CheckConstraint(
            CHECK_EXPRESSIONS["snapshot_counts"],
            name="ck_metadata_snapshot_counts_nonneg",
        ),
    )
    op.create_index(
        "ix_metadata_snapshot_source",
        _TABLE_SNAPSHOT,
        ["source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_snapshot_source", table_name=_TABLE_SNAPSHOT)
    op.drop_table(_TABLE_SNAPSHOT)
    op.drop_index("ix_discovery_source_datasource", table_name=_TABLE_SOURCE)
    op.drop_table(_TABLE_SOURCE)
