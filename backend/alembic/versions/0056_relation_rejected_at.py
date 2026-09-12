"""knowledge_relation.rejected_at - 关系候选的打回状态（Phase 8 M4）。

为什么需要这一列：
机制 2 发现的候选关系要能被业务专家**打回**。若打回 = 删行，下一次发现
会重新算出同一条候选，用户得反复打回同一条建议——一个会自己复活的「拒绝」
不算拒绝。故打回是把行留下并盖章，发现逻辑据此跳过。

三态（服务层维护的不变量）：

- 待审核：``confirmed = false`` 且 ``rejected_at IS NULL``
- 已确认：``confirmed = true``  且 ``rejected_at IS NULL``
- 已打回：``confirmed = false`` 且 ``rejected_at IS NOT NULL``

刻意**不**再加一个 ``rejected`` 布尔：两个布尔能表达出 4 种组合，其中
「既确认又打回」是非法态，需要一个本来不存在的约束去挡。用「时间戳有值
= 打回过」把非法态从类型上消掉，同时保留「什么时候打回的」这个信息。

不改 ``confirmed`` 的既有语义（M1 的 ``confirmedOnly`` 过滤照旧只认 true）。

Revision ID: 0056
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0056_relation_rejected_at"
down_revision: str | None = "0055_learning_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_relation "
        "ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_relation DROP COLUMN IF EXISTS rejected_at")
