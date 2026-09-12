"""wiki 去重地基：content_hash 落列与索引 + 台账 skipped_pages（P1）。

两处改动同属 P1，故同一次迁移交付（spec §4.7 D2-2 的「只付一次迁移成本」）。

1. ``wiki_page.content_hash VARCHAR(64)`` —— 正文的 sha256 十六进制（小写 64 位），
   与 ``document_catalog.content_hash`` **同算法同格式同列型**（都 sha256 小写
   64 位 hex、``VARCHAR(64)``），但**输入不同**：``document_catalog`` 哈希的是
   **上传文件字节**，``wiki_page`` 哈希的是**草稿文本**，两处从不相等、也不互相
   派生。用途是让「同 ID 不同内容」与「同文件重跑」在数据层可判定（导入路径据此
   把后者记成跳过、前者记成冲突）。
   配套的非唯一索引（``ix_wiki_page_content_hash``）服务「这份正文还出现在哪些
   条目里」这类排查/审计查询。**刻意不加唯一约束**：同一段正文出现在两条知识里
   是合法的（共享模板、多部门引用同一条款），唯一约束会把正常写入变成 500；
   且唯一约束一旦存在，「同 ID 同内容 → 跳过」的分支在 INSERT 前就炸了，
   永远走不到。
2. ``wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0`` —— P1 之后重复项
   不再计失败（spec §5.4）。不新增这一列的话，「本次 8 条全是重复」会退化成
   ``success_pages=0, failed_pages=0`` 的第三种状态，运维无从区分「任务没跑」
   与「跑了但都已入库」。

**为什么 ``document_catalog`` 的唯一索引不在本迁移（已拆到 0062）**：
原先把 ``CREATE UNIQUE INDEX uq_document_catalog_content_hash`` 放在这里，于是
「只想撤掉唯一索引、保留另外两项」在 Alembic 里**做不到** —— ``downgrade()``
必须对称反转 ``upgrade()``，三项会被一起撤掉；若硬改成只撤索引，库就会停在
一个**没有任何 revision 描述**的状态（``alembic_version`` 回到 0060 而两列仍在），
而「版本号相同 ≠ 结构相同」正是本项目历史「两库结构漂移」的病根。
拆开后 ``alembic downgrade 0061`` 天然只撤那一项，且每个 ``downgrade`` 仍严格
对称。撤唯一索引的正确方式因此是 ``alembic downgrade 0062`` ——
见 ``0062_doc_catalog_hash_unique.py``。

**幂等**：``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``，
与 0053-0060 同模式 —— prod 是从 dump 恢复出来的，重复执行必须是 no-op。

**downgrade 是对称的**（删索引 + 删两列）：本迁移的两处 DDL 都由本迁移新建，
不存在 0060 那种「列早于迁移且带真实数据」的情形。删列会丢掉已回填的
content_hash，但那正是 downgrade 的语义 —— 0060 的系统里没有任何代码读这些
对象，回退后不会留下半截状态。

Revision ID: 0061
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0061_wiki_dedup"
down_revision: str | None = "0060_schema_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE wiki_page ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)"
    )
    # 非唯一索引：去重判定走 page_id（已有 uq_wiki_page_page_id），本索引服务的是
    # 「这份正文还出现在哪些条目里」这类排查/审计查询。不加唯一约束的理由见
    # 模块 docstring。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wiki_page_content_hash "
        "ON wiki_page (content_hash)"
    )
    op.execute(
        "ALTER TABLE wiki_import_task ADD COLUMN IF NOT EXISTS skipped_pages "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE wiki_import_task DROP COLUMN IF EXISTS skipped_pages")
    op.execute("DROP INDEX IF EXISTS ix_wiki_page_content_hash")
    op.execute("ALTER TABLE wiki_page DROP COLUMN IF EXISTS content_hash")
