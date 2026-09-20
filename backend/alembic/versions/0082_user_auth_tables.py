"""新增 ``user_sessions`` 表 + ``users.must_change_password`` 部分索引（feat-user-auth）。

**触发**：feat-user-auth 引入真实登录。``user_sessions`` 表写入每条 Bearer token 的
``jti`` + ``revoked_at``；``getCurrentUser`` 每次同步查该表验证 session 未被吊销。
``must_change_password=true`` 部分索引方便 admin 查找「需强制改密用户」。

**表结构**：
- ``id``：BigIntPk
- ``jti``：UUID4，唯一，对应 JWT 的 jti 字段
- ``user_id``：FK→users.id ON DELETE CASCADE
- ``issued_at`` / ``expires_at``：时间窗
- ``revoked_at`` / ``revoked_reason``：logout / password_changed / admin_reset 三种 reason
- ``ip`` / ``user_agent``：审计
- ``created_time``：写入时刻

**索引**：
- ``ix_user_sessions_jti`` UNIQUE（jti 唯一）
- ``ix_user_sessions_user_id``（按 user 查 session 列表）
- ``ix_user_sessions_active`` 复合 (user_id, revoked_at)（查活跃 session）

**与现有 schema 的兼容性**：``users`` 表的 4 列（``password_hash`` / ``must_change_password``
/ ``last_login_at`` / ``last_login_ip``）在 0060 schema-drift 收敛时已声明 + 存在 prod，
本迁移不重复加列（避免 42P07 重复列报错）。仅加 ``must_change_password`` 部分索引
（前提：DB 未建该索引——如有，try/except 跳过）。

**两库同步**：升级到 head；prod 与 qa_metadata_test 都需要。

Revision ID: 0082
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision: str = "0082_user_auth_tables"
down_revision: str | None = "0081_db_pool_size_tune"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users.must_change_password 部分索引（仅真值行，admin 强制改密用户查询加速）
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_users_must_change_password "
            "ON users (must_change_password) WHERE must_change_password = true"
        )
    )

    # user_sessions 表 + 索引
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(64), nullable=True),
        sa.Column("ip", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column(
            "created_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_user_sessions_jti", "user_sessions", ["jti"], unique=True
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index(
        "ix_user_sessions_active", "user_sessions", ["user_id", "revoked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_sessions_active", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_index("ix_user_sessions_jti", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.execute(text("DROP INDEX IF EXISTS ix_users_must_change_password"))