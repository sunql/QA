"""schema_cache schema 作用域化：加 schema_name 列 + 复合唯一键（Alembic 0048）。

一个 Oracle 数据源可按 owner 命名空间拆成多份 schema 缓存（ImportWizard「选择
Schema」：THBI 连接用户 ZJTH 下既能看 ZJTH 的 X3 表，也能看 THBI 的数仓表）。

- 新增 schema_name VARCHAR(100) NOT NULL DEFAULT '' —— '' 表示「连接默认 /
  未指定 owner」（PG/MySQL 单份语义不变，保持向后兼容）。
- 存量 Oracle 行回填：schema_name = UPPER(data_source.username)。先前后端恒以
  _oracleOwner(username)（即 UPPER(username)）为 owner 内省，schema_data 每行
  owner 均等于 UPPER(username)，故该回填精确等价，无需解析 JSONB。
  非 Oracle 行保持 ''（连接默认）。
- 唯一约束 uq_schema_cache_datasource 升级为 (datasource_id, schema_name) 复合键，
  使同数据源多份 owner 缓存互不覆盖。

向后兼容：存量行回填后复合键内每个 datasource 至多一行（原单列唯一保证），
复合唯一键建立无冲突。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_schema_cache_schema_scope"
down_revision = "0047_doc_qa_message"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "schema_cache",
        sa.Column("schema_name", sa.String(100), nullable=False, server_default=""),
    )
    # 存量 Oracle 行按 UPPER(username) 回填 owner；非 Oracle 行维持 ''（连接默认）
    op.execute(
        """
        UPDATE schema_cache sc
        SET schema_name = UPPER(ds.username)
        FROM data_source ds
        WHERE sc.datasource_id = ds.id
          AND ds.type = 'oracle'
        """
    )
    op.drop_constraint("uq_schema_cache_datasource", "schema_cache", type_="unique")
    op.create_unique_constraint(
        "uq_schema_cache_datasource_schema",
        "schema_cache",
        ["datasource_id", "schema_name"],
    )
    # 移除 server_default：运行时始终显式传 schema_name，不留隐式默认
    op.alter_column("schema_cache", "schema_name", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_schema_cache_datasource_schema", "schema_cache", type_="unique")
    op.create_unique_constraint("uq_schema_cache_datasource", "schema_cache", ["datasource_id"])
    op.drop_column("schema_cache", "schema_name")
