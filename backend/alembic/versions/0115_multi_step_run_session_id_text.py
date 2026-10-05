"""multi_step_run.session_id 改 String(64) 并去掉 research_session 外键

触发：Task 6 阻塞裁决（方案 A，2026-10-05）
背景：0114 把 session_id 建成 `UUID NOT NULL + FK -> research_session.id`，
但多步持久化的唯一消费方是 **chat 链路**（`ChatService` 继承
`MultiStepPersistMixin`，见 app/services/chat_service.py:296），而 chat 会话 id
是自由字符串 —— 前端是 `chat-<uuid>` / `docqa-<uuid>`
（frontend/src/stores/chatStore.ts:79），测试里是 `"s1"`。chat 链路从不创建
`ResearchSession`（全仓唯一的非测试创建点是
app/services/research_session_service.py:78，属研究功能），故该 FK 指向的行在 chat
侧**永远不存在**；写入 `"s1"` 直接抛 `DataError: invalid UUID`。

全仓既有约定是 `String(64)`：`session_message.session_id`、
`session_query_state.session_id`（后者还带唯一约束）。本次对齐该约定。
研究链路有自己的一套（`ResearchAgentService` + `ResearchSqlRunner`），不写这两张表；
将来若也落库，`str(uuid)` 36 字符同样放得下。

幂等性：本迁移只跑一次（drop_constraint 无 IF EXISTS，重复执行会报错）
两库同步：prod 与 test 需分别应用（升级按 DATABASE_URL 定向，勿裸跑）
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0115"
down_revision: str | None = "0114"
branch_labels = None
depends_on = None

_FK_NAME = "multi_step_run_session_id_fkey"


def upgrade() -> None:
    op.drop_constraint(_FK_NAME, "multi_step_run", type_="foreignkey")
    # uuid -> varchar 是显式转换，必须给 USING，否则 PG 报「cannot be cast automatically」。
    op.alter_column(
        "multi_step_run",
        "session_id",
        existing_type=postgresql.UUID(as_uuid=True),
        type_=sa.String(64),
        existing_nullable=False,
        postgresql_using="session_id::text",
    )


def downgrade() -> None:
    # 回滚会丢掉非 UUID 形态的 session_id（chat-* / s1），先显式失败而不是静默截断。
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM multi_step_run "
        "WHERE session_id !~ '^[0-9a-fA-F-]{36}$') THEN "
        "RAISE EXCEPTION 'multi_step_run.session_id 含非 UUID 值，无法回滚到 UUID 列'; "
        "END IF; END $$;"
    )
    op.alter_column(
        "multi_step_run",
        "session_id",
        existing_type=sa.String(64),
        type_=postgresql.UUID(as_uuid=True),
        existing_nullable=False,
        postgresql_using="session_id::uuid",
    )
    op.create_foreign_key(
        _FK_NAME,
        "multi_step_run",
        "research_session",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )
