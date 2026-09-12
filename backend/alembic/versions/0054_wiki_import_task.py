"""wiki 导入任务 + LLM 计量 - Phase 8 M2。

新增：

- ``wiki_import_task``：导入任务（含用户选择的模型 primary/fallback）
- ``wiki_token_usage``：学习机制的 LLM 计量（**独立于** ``session_token_usage``
  —— 导入/分类没有 chat session，硬塞 sessionId 会污染会话维度报表）
- ``wiki_page`` 追加 2 列：``imported_via_task_id`` / ``processing_model_id``

编号说明：方案原写 0057，但 Alembic 是线性链，M2 早于 M5-M7 落地，
故顺延为 0054（down_revision=0053），后续里程碑按顺序接 0055+。

建表用 ``IF NOT EXISTS`` 幂等守卫；FK/列的新增用 ``DO $$`` 块先查
``information_schema`` / ``pg_constraint``——PG 不支持
``ALTER TABLE ... ADD CONSTRAINT IF NOT EXISTS``，裸写会在重跑时爆。

Revision ID: 0054
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0054_wiki_import_task"
down_revision: str | None = "0053_wiki_core_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- wiki_import_task：导入任务（含模型选择）----
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_import_task (
            id                 BIGSERIAL PRIMARY KEY,
            task_type          VARCHAR(30) NOT NULL,
            source_type        VARCHAR(30),
            source_ref         TEXT,
            selected_model_id  BIGINT,
            fallback_model_id  BIGINT,
            status             VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            page_ids           VARCHAR(64)[],
            total_pages        INTEGER     NOT NULL DEFAULT 0,
            success_pages      INTEGER     NOT NULL DEFAULT 0,
            failed_pages       INTEGER     NOT NULL DEFAULT 0,
            total_cost_usd     NUMERIC(12, 6) NOT NULL DEFAULT 0,
            error_message      TEXT,
            created_by_user_id BIGINT,
            created_time       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            finished_time      TIMESTAMP WITH TIME ZONE,
            CONSTRAINT fk_wiki_import_task_selected_model
                FOREIGN KEY (selected_model_id) REFERENCES llm_config (id),
            CONSTRAINT fk_wiki_import_task_fallback_model
                FOREIGN KEY (fallback_model_id) REFERENCES llm_config (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_import_task_status "
        "ON wiki_import_task (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_import_task_created "
        "ON wiki_import_task (created_time DESC)"
    )

    # ---- wiki_token_usage：学习机制的 LLM 计量 ----
    # import_task_id 用 ON DELETE SET NULL：任务清理不应抹掉成本账。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_token_usage (
            id                BIGSERIAL PRIMARY KEY,
            import_task_id    BIGINT,
            mechanism         VARCHAR(50) NOT NULL,
            model_config_id   BIGINT,
            model_name        VARCHAR(100),
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost              NUMERIC(12, 6) NOT NULL DEFAULT 0,
            purpose           VARCHAR(50),
            request_time      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT fk_wiki_token_usage_task
                FOREIGN KEY (import_task_id) REFERENCES wiki_import_task (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_wiki_token_usage_model
                FOREIGN KEY (model_config_id) REFERENCES llm_config (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_token_usage_mechanism_time "
        "ON wiki_token_usage (mechanism, request_time DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_token_usage_task "
        "ON wiki_token_usage (import_task_id)"
    )

    # ---- wiki_page 追加溯源列 ----
    op.execute(
        "ALTER TABLE wiki_page ADD COLUMN IF NOT EXISTS imported_via_task_id BIGINT"
    )
    op.execute(
        "ALTER TABLE wiki_page ADD COLUMN IF NOT EXISTS processing_model_id BIGINT"
    )
    # FK 单独加（PG 无 ADD CONSTRAINT IF NOT EXISTS，用 pg_constraint 守卫幂等）
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_wiki_page_import_task'
            ) THEN
                ALTER TABLE wiki_page
                    ADD CONSTRAINT fk_wiki_page_import_task
                    FOREIGN KEY (imported_via_task_id)
                    REFERENCES wiki_import_task (id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_wiki_page_processing_model'
            ) THEN
                ALTER TABLE wiki_page
                    ADD CONSTRAINT fk_wiki_page_processing_model
                    FOREIGN KEY (processing_model_id) REFERENCES llm_config (id);
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE wiki_page DROP CONSTRAINT IF EXISTS fk_wiki_page_processing_model"
    )
    op.execute(
        "ALTER TABLE wiki_page DROP CONSTRAINT IF EXISTS fk_wiki_page_import_task"
    )
    op.execute("ALTER TABLE wiki_page DROP COLUMN IF EXISTS processing_model_id")
    op.execute("ALTER TABLE wiki_page DROP COLUMN IF EXISTS imported_via_task_id")
    op.execute("DROP TABLE IF EXISTS wiki_token_usage")
    op.execute("DROP TABLE IF EXISTS wiki_import_task")
