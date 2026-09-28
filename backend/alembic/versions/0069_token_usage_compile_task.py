"""wiki_token_usage.compile_task_id - 编译任务 token 用量归属（schema 漂移补救）。

与 0065/0068 同模式：模型层早已落地但从未写进 Alembic 迁移。
dev 库靠旁路补齐了，test 库没有 → Phase 4 集成测试一调 token 计量就崩。

``compile_task_id`` 是 wiki_compile_task.id 的外键（可选）；插入时哪条路径
被调（import vs compile）就用哪条 id。``purpose`` 字段在模型层为 nullable，
不在本迁移范围。

Revision ID: 0069
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0069_token_usage_compile_task"
down_revision: str | None = "0068_wiki_import_task_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'wiki_token_usage'
                  AND column_name = 'compile_task_id'
                  AND table_schema = 'public'
            ) THEN
                ALTER TABLE wiki_token_usage
                ADD COLUMN compile_task_id BIGINT;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'ix_wiki_token_usage_compile_task'
            ) THEN
                CREATE INDEX ix_wiki_token_usage_compile_task
                ON wiki_token_usage (compile_task_id);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE constraint_name = 'fk_wiki_token_usage_compile_task'
                  AND table_name = 'wiki_token_usage'
            ) AND EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'wiki_compile_task'
            ) THEN
                ALTER TABLE wiki_token_usage
                ADD CONSTRAINT fk_wiki_token_usage_compile_task
                FOREIGN KEY (compile_task_id) REFERENCES wiki_compile_task (id)
                ON DELETE SET NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE wiki_token_usage DROP CONSTRAINT IF EXISTS fk_wiki_token_usage_compile_task"
    )
    op.execute("DROP INDEX IF EXISTS ix_wiki_token_usage_compile_task")
    op.execute(
        "ALTER TABLE wiki_token_usage DROP COLUMN IF EXISTS compile_task_id"
    )
