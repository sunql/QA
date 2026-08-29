"""session_token_usage.request_time 改 timestamptz - 修复模型/迁移 tz 漂移。

背景：模型声明 DateTime(timezone=True)，但 0001 迁移用 sa.DateTime() 建成
`timestamp without time zone`，两者不一致。后果：
- aware UTC 绑定被 SQLAlchemy 剥 tz 按 UTC 墙钟存储（当前 app 用 _utcnow()，无漂移）；
- naive datetime 绑定被当作服务器本地时区（UTC+8）再转 UTC，漂移 -8h；
- 裸 SQL 传 aware UTC 到 naive 列直接 DataError。

修复：列改为 TIMESTAMP WITH TIME ZONE 对齐模型语义。存量数据按 `AT TIME ZONE 'UTC'`
重解释为 UTC 时刻（既有值即 UTC 墙钟，时刻不变），无需 backfill。
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_request_time_tz"
down_revision = "0010_recent_rounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 存量 naive 值按 UTC 墙钟重解释为 timestamptz（时刻不变），避免被服务器时区误解
    op.alter_column(
        "session_token_usage",
        "request_time",
        existing_type=sa.DateTime(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        postgresql_using="request_time AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    # timestamptz -> naive：取 UTC 墙钟，恢复为 0001 的原始形态
    op.alter_column(
        "session_token_usage",
        "request_time",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(),
        existing_nullable=False,
        postgresql_using="request_time AT TIME ZONE 'UTC'",
    )
