"""alembic check 残余 36 条归零：必须 DDL 的 6 项。

背景与逐条运行时影响评估见
``Harness/changes/fix-schema-check-constraints/summary.md`` §11；
本迁移只做其中**必须 DDL** 的部分（零 DDL 的 16+6 条已由 ORM 侧对齐）：

1. ``data_quality_violation_sample.sample_pk_values`` JSONB → JSON
   ⚠️ 必须先 ``DROP DEFAULT``，否则 PG 报
   *default for column cannot be cast automatically to type json*；
   改类型后默认值写成 ``'[]'::json``（与 ORM ``server_default`` 一致）。
2. ``document_entity_relation.entity_type`` VARCHAR(30) → VARCHAR(20)
   （收窄；该列有 FK → ``business_object.code VARCHAR(20)``，值不可能超过 20；
   两库均空表，已实测）。
3. ``l3_acl.user_id`` / ``granted_by`` INTEGER → BIGINT（放宽；两库 0 行）。
4. ``wiki_token_usage.compile_task_id`` BIGINT → INTEGER（收窄；两库非空值 0 条）。
5. ``l3_acl.granted_at`` 加 NOT NULL（两库 0 行）。
6. ``wiki_compile_task.selected_model_id`` / ``fallback_model_id`` 补 FK →
   ``llm_config.id``。**幂等**：全新迁移库的 0077 已经建过（PG 自动命名），
   这里按「该列是否已有任何 FK」判断，已有则跳过；prod 缺这两条则补上。
   不能用 ``op.create_foreign_key`` 无脑执行——fresh 库会撞重复外键。

**禁止 autogenerate**：autogenerate 会对残余差异生成破坏性 DROP/ADD；
本文件逐条手写，upgrade / downgrade 严格对称。

**不碰** partial unique 索引（``uq_knowledge_conflict_open`` /
``uq_structure_suggestion_pending``）：它们的 ``ON CONFLICT`` 推断依赖谓词，
改成约束会直接报 *no unique or exclusion constraint matching*（§11.4 陷阱 2）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

_SAMPLE_TABLE = "data_quality_violation_sample"
_SAMPLE_COLUMN = "sample_pk_values"


def _execute(sql: str) -> None:
    """原始 SQL 走 ``exec_driver_sql`` —— 表达式里的 ``:`` 会被 bindparam 改写。

    ``::json`` / ``::jsonb`` 里的冒号正是 SQLAlchemy ``text()`` 的绑定参数语法；
    走 ``op.execute(str)`` 会被当成 ``:json`` 参数而报错或静默改写。
    """
    op.get_bind().exec_driver_sql(sql)


def _columnHasForeignKey(table: str, column: str) -> bool:
    """该列上是否已存在任何外键（不看约束名，兼容 0077 的 PG 自动命名）。"""
    query = sa.text(
        "SELECT EXISTS ("
        "  SELECT 1 FROM pg_constraint c"
        "  JOIN pg_class t ON t.oid = c.conrelid"
        "  JOIN pg_namespace n ON n.oid = t.relnamespace"
        "  JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = c.conkey[1]"
        "  WHERE n.nspname = 'public' AND t.relname = :table"
        "    AND c.contype = 'f' AND a.attname = :column"
        ") AS present"
    )
    return bool(
        op.get_bind().execute(query, {"table": table, "column": column}).scalar()
    )


def upgrade() -> None:
    # 1. sample_pk_values JSONB → JSON（先摘默认值，否则类型转换被默认值挡住）
    _execute(
        f"ALTER TABLE {_SAMPLE_TABLE} "
        f"ALTER COLUMN {_SAMPLE_COLUMN} DROP DEFAULT"
    )
    _execute(
        f"ALTER TABLE {_SAMPLE_TABLE} "
        f"ALTER COLUMN {_SAMPLE_COLUMN} TYPE json "
        f"USING {_SAMPLE_COLUMN}::json"
    )
    _execute(
        f"ALTER TABLE {_SAMPLE_TABLE} "
        f"ALTER COLUMN {_SAMPLE_COLUMN} SET DEFAULT '[]'::json"
    )

    # 2. entity_type VARCHAR(30) → VARCHAR(20)（收窄；值受 FK 目标列长度约束）
    _execute(
        "ALTER TABLE document_entity_relation "
        "ALTER COLUMN entity_type TYPE varchar(20)"
    )

    # 3. l3_acl.user_id / granted_by INTEGER → BIGINT（放宽）
    _execute("ALTER TABLE l3_acl ALTER COLUMN user_id TYPE bigint")
    _execute("ALTER TABLE l3_acl ALTER COLUMN granted_by TYPE bigint")

    # 4. wiki_token_usage.compile_task_id BIGINT → INTEGER（收窄；非空值已实测 0 条）
    _execute("ALTER TABLE wiki_token_usage ALTER COLUMN compile_task_id TYPE integer")

    # 5. l3_acl.granted_at 加 NOT NULL（0 行已实测）
    _execute("ALTER TABLE l3_acl ALTER COLUMN granted_at SET NOT NULL")

    # 6. wiki_compile_task 两条模型 FK（幂等：fresh 库 0077 已建，prod 缺）
    if not _columnHasForeignKey("wiki_compile_task", "selected_model_id"):
        _execute(
            "ALTER TABLE wiki_compile_task "
            "ADD CONSTRAINT fk_wiki_compile_task_selected_model "
            "FOREIGN KEY (selected_model_id) REFERENCES llm_config(id)"
        )
    if not _columnHasForeignKey("wiki_compile_task", "fallback_model_id"):
        _execute(
            "ALTER TABLE wiki_compile_task "
            "ADD CONSTRAINT fk_wiki_compile_task_fallback_model "
            "FOREIGN KEY (fallback_model_id) REFERENCES llm_config(id)"
        )


def downgrade() -> None:
    # 6. 只删除本迁移可能新增的命名 FK；fresh 库 0077 的自动命名 FK 保持不动
    _execute(
        "ALTER TABLE wiki_compile_task "
        "DROP CONSTRAINT IF EXISTS fk_wiki_compile_task_selected_model"
    )
    _execute(
        "ALTER TABLE wiki_compile_task "
        "DROP CONSTRAINT IF EXISTS fk_wiki_compile_task_fallback_model"
    )

    # 5. 撤销 NOT NULL
    _execute("ALTER TABLE l3_acl ALTER COLUMN granted_at DROP NOT NULL")

    # 4. compile_task_id INTEGER → BIGINT
    _execute("ALTER TABLE wiki_token_usage ALTER COLUMN compile_task_id TYPE bigint")

    # 3. user_id / granted_by BIGINT → INTEGER（仅在值域不溢出时才安全；
    #    0 行已实测。若回滚时已有超范围数据，此步会报错——这是有意的 fail-fast。）
    _execute("ALTER TABLE l3_acl ALTER COLUMN user_id TYPE integer")
    _execute("ALTER TABLE l3_acl ALTER COLUMN granted_by TYPE integer")

    # 2. entity_type VARCHAR(20) → VARCHAR(30)
    _execute(
        "ALTER TABLE document_entity_relation "
        "ALTER COLUMN entity_type TYPE varchar(30)"
    )

    # 1. sample_pk_values JSON → JSONB（同样要先摘默认值）
    _execute(
        f"ALTER TABLE {_SAMPLE_TABLE} "
        f"ALTER COLUMN {_SAMPLE_COLUMN} DROP DEFAULT"
    )
    _execute(
        f"ALTER TABLE {_SAMPLE_TABLE} "
        f"ALTER COLUMN {_SAMPLE_COLUMN} TYPE jsonb "
        f"USING {_SAMPLE_COLUMN}::jsonb"
    )
    _execute(
        f"ALTER TABLE {_SAMPLE_TABLE} "
        f"ALTER COLUMN {_SAMPLE_COLUMN} SET DEFAULT '[]'::jsonb"
    )
