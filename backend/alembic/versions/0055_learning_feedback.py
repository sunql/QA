"""learning_feedback - 学习闭环反馈表（Phase 8 M3）。

新增：

- ``learning_feedback``：机制 1~4 的**用户反馈事件流**。每条事件记录
  「系统当时给了什么建议（system_output）+ 用户怎么处理的（user_action）+
  改成了什么（user_modification）」。

为什么是事件流而不是「建议表加个状态列」：
机制 1/2/4 的建议本身各有归宿（分类建议落 ``wiki_page.auto_classification``、
关系建议落 ``knowledge_relation``、结构化建议 M5 落 ``structure_suggestion``），
它们的状态列只表达**当前**状态；而训练分类器需要的是「历史上有多少次被改、
从什么改成了什么」——这是 append-only 的语义，塞进建议表会把当前态与历史态
混在一起。故本表只 append，从不 update。

``input_snapshot`` / ``system_output`` / ``user_modification`` 用 JSONB 而非
结构化列：三类机制的快照结构互不相同（分类是 dimension+confidence，关系是
triple+confidence），拆成宽表会得到一堆永远为 NULL 的列。

编号说明：方案原文把 ``learning_feedback`` 排在 0054，但 M2 导入任务先落地，
M3 顺延为 0055（down_revision=0054）。后续里程碑按顺序接 0056+。

建表用 ``IF NOT EXISTS`` 幂等守卫，与 0053/0054 同模式。

Revision ID: 0055
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0055_learning_feedback"
down_revision: str | None = "0054_wiki_import_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_feedback (
            id                BIGSERIAL PRIMARY KEY,
            mechanism         VARCHAR(50)  NOT NULL,
            entity_type       VARCHAR(50)  NOT NULL,
            entity_id         VARCHAR(128) NOT NULL,
            input_snapshot    JSONB,
            system_output     JSONB,
            user_action       VARCHAR(50)  NOT NULL,
            user_modification JSONB,
            feedback_user_id  BIGINT,
            feedback_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
        """
    )
    # 主查询形态：按「机制 + 目标实体」拉某条知识/关系的全部反馈历史。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_learning_feedback_entity "
        "ON learning_feedback (mechanism, entity_type, entity_id)"
    )
    # 次查询形态：按时间倒序做「最近谁改了什么」的运营视图。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_learning_feedback_time "
        "ON learning_feedback (feedback_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS learning_feedback")
