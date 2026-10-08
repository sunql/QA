"""research entry: research_session.datasource_id (feat-research-entry Task 13e)

**触发**：研究型入口真机验收暴露的根因 —— 研究侧执行业务 SQL 时**没有业务库连接**，
把 SQL 打在应用自己的元数据库（Postgres）会话上，而业务表在 Oracle（``THBI Oracle``，
``data_source`` id=1，``is_default=t``）⇒ 一律 ``UndefinedTableError``。前两次验收
（13b 6/6、13d「.sql 事件出现」）都从未触达业务数据，因此全绿。

**变更**：给 ``research_session`` 加 ``datasource_id``（``sa.Integer()``，**nullable=True**）。

- **不加外键**：与 ``created_by`` 同款裁定 —— 跨域 FK 只带来锁表 / 删除顺序依赖，
  无收益；数据源被删时会话保留原 id（解析时显式报错，见 ``depsForSession``）。
- **nullable=True**：迁移前已存在的会话（本特性尚未合并，仅验收产生的临时行）无源，
  由读取侧显式报错，**绝不静默回落到元数据库会话**。
- **为什么落库而不像 chat 那样纯按请求带**：研究是**多轮 + 可恢复**的，``state`` 不持久化
  （0111 无 state 列），turn 只把 ``(sessionId, turnId, question, userId, mode)`` 交给后台
  任务。若只按请求带，用户在新请求里答 checkpoint 时就会丢源。存到会话 ⇒ 一次选定全程沿用。

**幂等性**：``downgrade()`` 删列（与 0111 风格一致）。

**两库同步**：prod + test（容器启动 / 部署脚本自动迁移）。

Revision ID: 0112
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0112"
down_revision: str | None = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_session",
        sa.Column("datasource_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_session", "datasource_id")
