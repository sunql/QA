"""wiki_import_task.retry_of_task_id - 失败任务重试链关联（Phase 4）。

幂等/增量缓存（基于 ``wiki_page.content_hash`` + page_id 派生）在 0061 已落地，
本迁移只为失败任务补一个重试链记录：

- 原始任务失败（FAILED / PARTIAL）→ 用户重跑：新 task 写入 retry_of_task_id
  指向旧 task_id。
- ``ondelete=SET NULL``：原始任务被清理时不影响重试链历史；前端能看到「这是
  一次重试」即可，原任务是否还存在并不关键。
- self-referencing FK 在 PG 完全合法，但刻意**不加**自引用检查（应用层
  不会链式重试）。

Revision ID: 0068
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0068_wiki_import_task_retry"
down_revision: str | None = "0067_wiki_graph_insight"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE wiki_import_task
        ADD COLUMN IF NOT EXISTS retry_of_task_id BIGINT
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_wiki_import_task_retry_of'
                  AND table_name = 'wiki_import_task'
            ) THEN
                ALTER TABLE wiki_import_task
                ADD CONSTRAINT fk_wiki_import_task_retry_of
                FOREIGN KEY (retry_of_task_id)
                REFERENCES wiki_import_task (id) ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE wiki_import_task DROP CONSTRAINT IF EXISTS fk_wiki_import_task_retry_of"
    )
    op.execute(
        "ALTER TABLE wiki_import_task DROP COLUMN IF EXISTS retry_of_task_id"
    )
