"""knowledge_claim 三元组列 + evidence 定位列修正 - 补齐模型与库的漂移。

背景（schema drift 事故）：
``KnowledgeClaim`` 的三元组字段（subject_id / predicate / object_value /
object_type / confidence / authority_level / status / valid_from / valid_to /
triple_stale）与 ``Evidence`` 的 content_hash / confidence、以及
page_number / paragraph_no 的 INTEGER 类型，在模型层早已落地，但从未写入
Alembic 迁移——0053 的 ``CREATE TABLE IF NOT EXISTS`` 只建了初版列，
已存在的库（含测试库 qa_metadata_test）不会回补。dev 库靠旁路（手工/
create_all）补齐了这些列，测试库没有，于是一跑真实 PG 集成测试就
``UndefinedColumnError``。

本迁移以模型定义为 SSOT 把两张表补齐，全部幂等（IF NOT EXISTS / 类型
转换对已是 INTEGER 的列是无害 no-op），dev / test / 生产同路径生效。

page_number / paragraph_no 由 VARCHAR(30) 改 INTEGER：历史数据若存在，
应为 locateExcerpt 写出的纯数字文本；空串按 NULL 处理，非数字文本
（不应存在）会被 PostgreSQL 拒绝——那正是需要人工介入的脏数据信号，
不应静默吞掉。

Revision ID: 0065
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0065_claim_evidence_column_fix"
down_revision: str | None = "0064_add_llm_config_temperature"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIM_NEW_COLUMNS = (
    "subject_id VARCHAR(128)",
    "predicate VARCHAR(100)",
    "object_value TEXT",
    "object_type VARCHAR(30)",
    "confidence NUMERIC(5, 4)",
    "authority_level VARCHAR(10)",
    "status VARCHAR(20)",
    "valid_from TIMESTAMP WITH TIME ZONE",
    "valid_to TIMESTAMP WITH TIME ZONE",
)

_EVIDENCE_NEW_COLUMNS = (
    "content_hash VARCHAR(64)",
    "confidence NUMERIC(5, 4)",
)


def upgrade() -> None:
    for columnDef in _CLAIM_NEW_COLUMNS:
        op.execute(f"ALTER TABLE knowledge_claim ADD COLUMN IF NOT EXISTS {columnDef}")
    op.execute(
        "ALTER TABLE knowledge_claim "
        "ADD COLUMN IF NOT EXISTS triple_stale BOOLEAN NOT NULL DEFAULT FALSE"
    )
    for columnDef in _EVIDENCE_NEW_COLUMNS:
        op.execute(f"ALTER TABLE evidence ADD COLUMN IF NOT EXISTS {columnDef}")
    # 类型修正仅当列当前不是 integer 时执行：已是 integer 的库（dev）上
    # NULLIF(col, '') 会因 '' 无法转 integer 而报错。
    op.execute(
        """
        DO $$
        BEGIN
            IF (SELECT data_type FROM information_schema.columns
                WHERE table_name = 'evidence' AND column_name = 'page_number'
                  AND table_schema = 'public') <> 'integer' THEN
                ALTER TABLE evidence ALTER COLUMN page_number TYPE INTEGER
                    USING NULLIF(page_number, '')::integer;
            END IF;
            IF (SELECT data_type FROM information_schema.columns
                WHERE table_name = 'evidence' AND column_name = 'paragraph_no'
                  AND table_schema = 'public') <> 'integer' THEN
                ALTER TABLE evidence ALTER COLUMN paragraph_no TYPE INTEGER
                    USING NULLIF(paragraph_no, '')::integer;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE evidence ALTER COLUMN page_number TYPE VARCHAR(30) "
        "USING page_number::varchar"
    )
    op.execute(
        "ALTER TABLE evidence ALTER COLUMN paragraph_no TYPE VARCHAR(30) "
        "USING paragraph_no::varchar"
    )
    for columnDef in reversed(_EVIDENCE_NEW_COLUMNS):
        columnName = columnDef.split()[0]
        op.execute(f"ALTER TABLE evidence DROP COLUMN IF EXISTS {columnName}")
    op.execute("ALTER TABLE knowledge_claim DROP COLUMN IF EXISTS triple_stale")
    for columnDef in reversed(_CLAIM_NEW_COLUMNS):
        columnName = columnDef.split()[0]
        op.execute(f"ALTER TABLE knowledge_claim DROP COLUMN IF EXISTS {columnName}")
