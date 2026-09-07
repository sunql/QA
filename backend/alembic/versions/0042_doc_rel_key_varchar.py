"""document_entity_relation.entity_key BIGINT → VARCHAR(100) 迁移.

策略：
  1. ADD COLUMN entity_key_new VARCHAR(100) IF NOT EXISTS（幂等）
  2. UPDATE 经 entity_mapping JOIN 回填 enterprise_code
  3. DELETE 孤儿行（无匹配 entity_mapping 或 enterprise_code 为 NULL）
  4. DROP 旧列 + RENAME + SET NOT NULL
  5. 加 CHECK 约束 length(entity_key) > 0
  6. 重建 ix_doc_rel_entity 索引（原索引随列删除而移除）

幂等：重复运行结果一致（entity_key 已是 VARCHAR 时 ADD IF NOT EXISTS 安全）。
Downgrade 数据不可逆（VARCHAR → BIGINT 丢失enterprise_code 语义）。
"""
from __future__ import annotations

from alembic import op

revision = "0042_doc_rel_key_varchar"
down_revision = "0041_entity_type_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 新增临时列（幂等：IF NOT EXISTS）
    op.execute("""
        ALTER TABLE document_entity_relation
        ADD COLUMN IF NOT EXISTS entity_key_new VARCHAR(100)
    """)

    # 2. 回填：经 entity_mapping JOIN enterprise_key → enterprise_code
    op.execute("""
        UPDATE document_entity_relation d
        SET entity_key_new = m.enterprise_code
        FROM entity_mapping m
        WHERE m.entity_type = d.entity_type
          AND m.enterprise_key = d.entity_key
          AND m.enterprise_code IS NOT NULL
    """)

    # 3. 删除孤儿行（无匹配 entity_mapping 或 enterprise_code 为 NULL）
    op.execute("""
        DELETE FROM document_entity_relation
        WHERE entity_key_new IS NULL
    """)

    # 4. 替换列
    op.execute("ALTER TABLE document_entity_relation DROP COLUMN entity_key")
    op.execute(
        "ALTER TABLE document_entity_relation "
        "RENAME COLUMN entity_key_new TO entity_key"
    )
    op.execute(
        "ALTER TABLE document_entity_relation "
        "ALTER COLUMN entity_key SET NOT NULL"
    )

    # 5. CHECK 约束（幂等：先 DROP IF EXISTS 再 CREATE）
    op.execute(
        "ALTER TABLE document_entity_relation "
        "DROP CONSTRAINT IF EXISTS ck_doc_rel_entity_key_nonempty"
    )
    op.create_check_constraint(
        "ck_doc_rel_entity_key_nonempty",
        "document_entity_relation",
        "length(entity_key) > 0",
    )

    # 6. 重建索引（原 ix_doc_rel_entity 随 entity_key 列删除而移除）
    op.create_index(
        "ix_doc_rel_entity",
        "document_entity_relation",
        ["entity_type", "entity_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_doc_rel_entity", "document_entity_relation")
    op.drop_constraint(
        "ck_doc_rel_entity_key_nonempty",
        "document_entity_relation",
        type_="check",
    )
    op.execute("ALTER TABLE document_entity_relation DROP COLUMN entity_key")
    op.execute("ALTER TABLE document_entity_relation ADD COLUMN entity_key BIGINT")
    # 注：downgrade 数据已丢失（enterprise_code 语义无法还原为 enterprise_key）
