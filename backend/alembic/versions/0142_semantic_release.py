"""semantic_release：语义发布自包含快照 + 状态机（feat-semantic-release-a / P1a D4）。

设计依据 `docs/update/1109/p0/裁决记录.md`：

- 裁决一：本体治理表是原地 UPDATE（Phase 6 已移除版本管理），`version/valid_*`
  只是兼容残留 ⇒ 发布必须整份快照落库（`manifest_json` + `manifest_hash`）。
- 裁决四：首期全局快照，`domain_code` 可空；非空时必须存在于 `data_domain.code`
  且域不可被删除（RESTRICT）。

三条索引各有明确目的，不是装饰：
- `uq_semantic_release_global_version`：全局快照（domain_code IS NULL）版本号唯一；
  PostgreSQL 唯一约束对 NULL 互不相等，所以必须用部分索引。
- `uq_semantic_release_one_active`：同域最多一条 ACTIVE，并发激活的库级兜底；
  `COALESCE(domain_code, '')` 把 NULL 归一，否则全局快照会漏防。
- `ix_semantic_release_status`：按状态列举（发布列表页 / 巡检）。

纯加法 DDL：只建新表，不触碰任何既有表。downgrade 仅删本表。

**修订策略**：本迁移随 PR 一起落地、尚未在任何持久环境执行（生产仍在 0140），
因此评审补的两条 CHECK 是**原地加在本 revision 里**的，不是新开迁移。
一旦某个库已经 stamp 到 0142，`alembic upgrade head` 不再重放 ⇒ 那个库必须先
DROP / 重建（测试库 `qa_metadata_test_semantic` 每次按此重建）。本 PR 合并后
0142 冻结，再改约束必须新开迁移。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None

_TABLE = "semantic_release"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("domain_code", sa.String(length=20), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column(
            "schema_version",
            sa.String(length=20),
            nullable=False,
            server_default="v1",
        ),
        sa.Column(
            "manifest_json", postgresql.JSONB(), nullable=False
        ),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("activated_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
            ["domain_code"],
            ["data_domain.code"],
            name="fk_semantic_release_domain",
            ondelete="RESTRICT",
        ),
        # `uq_semantic_release_one_active` 的谓词是字面量 status = 'ACTIVE'；
        # 大小写/空格变体都会绕过它 ⇒ 用 CHECK 把状态字面量钉死在库侧。
        sa.CheckConstraint(
            "status IN ('DRAFT','VALIDATED','ACTIVE','ARCHIVED')",
            name="ck_semantic_release_status",
        ),
        # 快照的形状/哈希格式在库侧钉死：绕过 ORM 的脚本写进 'null' / '[1,2]' /
        # 任意 64 字符，会让"看起来已发布、回放时才炸"的坏快照落库。
        sa.CheckConstraint(
            "jsonb_typeof(manifest_json) = 'object'",
            name="ck_semantic_release_manifest_is_object",
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="ck_semantic_release_manifest_hash_sha256",
        ),
    )

    op.create_index(
        "uq_semantic_release_global_version",
        _TABLE,
        ["version"],
        unique=True,
        postgresql_where=sa.text("domain_code IS NULL"),
    )
    op.create_index(
        "uq_semantic_release_one_active",
        _TABLE,
        [sa.text("COALESCE(domain_code, '')")],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index("ix_semantic_release_status", _TABLE, ["status"])


def downgrade() -> None:
    op.drop_index("ix_semantic_release_status", table_name=_TABLE)
    op.drop_index("uq_semantic_release_one_active", table_name=_TABLE)
    op.drop_index("uq_semantic_release_global_version", table_name=_TABLE)
    op.drop_table(_TABLE)
