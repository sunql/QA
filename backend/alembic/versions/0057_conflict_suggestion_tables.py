"""knowledge_conflict + structure_suggestion - 机制 3/4 的产出表（Phase 8 M5）。

新增两张表：

- ``knowledge_conflict``：机制 3 冲突检测的产出（矛盾/失效/缺口/重叠）
- ``structure_suggestion``：机制 4 结构化建议的产出（这条知识可以升成什么结构）

两张表都遵循 M1 定下的**「建议而非决定」**原则：机器只产出「待处理」的行，
人工处置（resolve / accept / reject）才让它进入终态。所以它们的终态用**时间戳
+ 动作**表达，而不是一个布尔开关——时间戳能回答「什么时候处理的」，布尔不能。

两处**部分唯一索引**是本迁移的核心设计，都是为了「重复跑同一机制不产生重复行」：

1. ``uq_knowledge_conflict_open``：同一组 page + 同一冲突类型，只允许存在
   **一条未解决**的冲突。前提是 ``page_ids`` **写入前必须排序**（service 层保证）
   ——不然 ``['A','B']`` 与 ``['B','A']`` 是数组不相等的两个值，唯一索引挡不住，
   同一对冲突每次检测都会新增一条。
2. ``uq_structure_suggestion_pending``：同一条知识的同一维度建议，只允许存在
   一条待处置的。用户接受/拒绝后（status 离开 PENDING）即可再次产出。

部分唯一索引（``WHERE`` 子句）而非普通唯一索引：已解决的历史冲突要能保留多条
（同一对知识可能反复冲突、反复解决），被唯一索引挡掉反而是错的。

建表用 ``IF NOT EXISTS`` 幂等守卫，与 0053-0056 同模式。

Revision ID: 0057
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0057_conflict_suggestion_tables"
down_revision: str | None = "0056_relation_rejected_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_conflict (
            id                 BIGSERIAL PRIMARY KEY,
            conflict_type      VARCHAR(30)  NOT NULL,
            page_ids           VARCHAR(64)[] NOT NULL,
            description        TEXT,
            severity           VARCHAR(20)  NOT NULL DEFAULT 'MEDIUM',
            detected_by        VARCHAR(20)  NOT NULL DEFAULT 'RULE',
            auto_detected_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            resolved_at        TIMESTAMP WITH TIME ZONE,
            resolution_action  VARCHAR(50),
            resolved_by_user_id BIGINT
        )
        """
    )
    # 同一组 page + 同一类型只能有一条「未解决」的冲突（见模块 docstring）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_conflict_open "
        "ON knowledge_conflict (conflict_type, page_ids) "
        "WHERE resolved_at IS NULL"
    )
    # 主查询形态一：看板按「未解决 + 严重度」排队。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_conflict_open "
        "ON knowledge_conflict (severity, auto_detected_at DESC) "
        "WHERE resolved_at IS NULL"
    )
    # 主查询形态二：`GET /pages/{id}/conflicts` 用数组包含做反查。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_conflict_page_ids "
        "ON knowledge_conflict USING GIN (page_ids)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS structure_suggestion (
            id                 BIGSERIAL PRIMARY KEY,
            page_id            VARCHAR(64) NOT NULL
                                 REFERENCES wiki_page(page_id) ON DELETE CASCADE,
            suggested_dimension VARCHAR(30) NOT NULL,
            extracted_structure JSONB,
            confidence         NUMERIC(4,3),
            status             VARCHAR(30) NOT NULL DEFAULT 'PENDING',
            suggested_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            resolved_at        TIMESTAMP WITH TIME ZONE,
            resolved_by_user_id BIGINT
        )
        """
    )
    # 同一条知识的同一维度建议只能有一条待处置的（见模块 docstring）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_structure_suggestion_pending "
        "ON structure_suggestion (page_id, suggested_dimension) "
        "WHERE status = 'PENDING'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_structure_suggestion_page "
        "ON structure_suggestion (page_id, suggested_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_structure_suggestion_pending "
        "ON structure_suggestion (status, suggested_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS structure_suggestion")
    op.execute("DROP TABLE IF EXISTS knowledge_conflict")
