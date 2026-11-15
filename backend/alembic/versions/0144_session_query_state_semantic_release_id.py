"""会话查询状态钉住语义发布：`session_query_state.semantic_release_id`。

P2 D16（spec: Harness/changes/feat-semantic-registry-nl2sql/plan.md §5 D16）：
把「本会话当前钉住的那一版语义发布（下一轮要用的）」记进运行记录，
下一轮据此**钉住**那一版 ⇒ 同一请求重放 ⇒ 同一发布 ⇒ 同一口径。

- session_query_state.semantic_release_id：INTEGER，nullable

纯加法 DDL，不触碰既有列；可空 ⇒ 存量旧行（NULL）**语义不变**
（NULL = 没钉住 = 照旧取当前 ACTIVE，与今天一致）；downgrade 直接删列。

列语义（写侧三态）：给了 id ⇒ 覆盖；`None` 且本轮没失效 ⇒ 保留；
`None` 且本轮**钉住失效**（那一版被删 / id 非法 / 完整性失败 / 版本不符 /
快照陈旧）⇒ 清空 ⇒ 下一轮回到当前 ACTIVE（死钉自愈）。

**手写**（禁 autogenerate）：autogenerate 会把 ORM 里新增的
`comment` / 类型细节一并生成，且可能顺带改出别的列 —— 本项目要求
「只加法 DDL、零副作用」，手写是唯一能保证这一点的做法。

⚠️ 本文件的列 `comment` 与 `app/domain/models.py` 里 ORM 的 `comment`
**必须逐字一致**：alembic 的 autogenerate 会比对列注释（PG 支持），
两边不一致 ⇒ `alembic check` 报漂移。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None

# 与 `app/domain/models.py` 的 `SessionQueryState.semantic_release_id` 逐字一致。
_COLUMN_COMMENT = "0144，P2 D16：本会话当前钉住的语义发布 id（NULL = 未钉住；下一轮要用的那版）"


def upgrade() -> None:
    op.add_column(
        "session_query_state",
        sa.Column(
            "semantic_release_id",
            sa.Integer(),
            nullable=True,
            comment=_COLUMN_COMMENT,
        ),
    )


def downgrade() -> None:
    op.drop_column("session_query_state", "semantic_release_id")
