"""wiki 去重地基：content_hash 落列与加约束 + 台账 skipped_pages（P1）。

三处改动同属 P1，故同一次迁移交付（spec §4.7 D2-2 的「只付一次迁移成本」）：

1. ``wiki_page.content_hash VARCHAR(64)`` —— 正文的 sha256 十六进制（小写 64 位），
   与 ``document_catalog.content_hash`` **同算法同格式同列型**（都 sha256 小写
   64 位 hex、``VARCHAR(64)``），但**输入不同**：``document_catalog`` 哈希的是
   **上传文件字节**，``wiki_page`` 哈希的是**草稿文本**，两处从不相等、也不互相
   派生。用途是让「同 ID 不同内容」与「同文件重跑」在数据层可判定（导入路径据此
   把后者记成跳过、前者记成冲突）。
   **刻意不加唯一约束**：同一段正文出现在两条知识里是合法的（共享模板、多部门
   引用同一条款），唯一约束会把正常写入变成 500；且唯一约束一旦存在，
   「同 ID 同内容 → 跳过」的分支在 INSERT 前就炸了，永远走不到。
2. ``document_catalog.content_hash`` 加**唯一索引** —— spec §4.7 的 D2-2 明确把
   「`content_hash` 唯一约束」推迟到 P1「一并做，只付一次迁移成本」，P1 就是
   现在，故本次补上。实测 prod：该列已存在（`VARCHAR(64) NULL`）、
   `document_catalog` **1 行**（`content_hash` 非空，P0 的上传路径已插入）、
   且**还没有**这个索引。建索引前先查重：`GROUP BY content_hash HAVING
   count(*) > 1` 返回 **0 个重复分组** —— 安全建索引的依据是「无重复分组」，
   而不是「表是空的」。
   **两个库都已实测**：prod `qa_metadata` 的 `document_catalog` 为 **1 行**
   （`content_hash` 非空、无重复），测试库 `qa_metadata_test` 为 **0 行**
   （后者版本号 = `0060_schema_reconcile`），故 `upgrade head` 在两边都不会
   因重复摘要而失败。
   语义：一条 catalog 行 = 一份源文档，同一份文件重复上传应映射到同一份文档。
   **PostgreSQL 的唯一索引允许多个 NULL**（NULL 互不相等），所以现存/未来的
   `content_hash IS NULL` 行不会被挡 —— 约束只作用于「已经算出了摘要」的行。
   建索引失败是**安全失败**（DDL 是原子的，要么建成要么报错），不会静默丢数据。
   **防撞车**：P0 落地时不得再建同名/同义索引（见 summary §3 与 §9 的交接项）。

3. ``wiki_import_task.skipped_pages INTEGER NOT NULL DEFAULT 0`` —— P1 之后重复项
   不再计失败（spec §5.4）。不新增这一列的话，「本次 8 条全是重复」会退化成
   ``success_pages=0, failed_pages=0`` 的第三种状态，运维无从区分「任务没跑」
   与「跑了但都已入库」。

**幂等**：``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``，
与 0053-0060 同模式 —— prod 是从 dump 恢复出来的，重复执行必须是 no-op。

**downgrade 是对称的**（删索引 + 删列）：三处 DDL 都由本迁移新建，不存在 0060
那种「列早于迁移且带真实数据」的情形。删列会丢掉已回填的 content_hash、删索引
会放开重复上传，但那正是 downgrade 的语义 —— 0060 的系统里没有任何代码读这些
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
    # spec §4.7 D2-2 把「content_hash 唯一约束」推迟到 P1 一并做，本次补上。
    # 作用在 document_catalog（P0 的表）而非 wiki_page：一条 catalog 行 = 一份
    # 源文档，同一份文件重复上传应映射到同一份文档。P1 只建索引，不碰该表其它列，
    # 也不依赖 P0 的任何代码。
    # PostgreSQL 唯一索引允许多个 NULL，故 content_hash IS NULL 的行不受影响。
    # 建索引前查重（GROUP BY content_hash HAVING count(*) > 1）返回 0 个重复分组
    # → 无重复可清理，直接建即可；建失败会原子回滚（安全失败）。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_catalog_content_hash "
        "ON document_catalog (content_hash)"
    )
    op.execute(
        "ALTER TABLE wiki_import_task ADD COLUMN IF NOT EXISTS skipped_pages "
        "INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE wiki_import_task DROP COLUMN IF EXISTS skipped_pages")
    op.execute("DROP INDEX IF EXISTS uq_document_catalog_content_hash")
    op.execute("DROP INDEX IF EXISTS ix_wiki_page_content_hash")
    op.execute("ALTER TABLE wiki_page DROP COLUMN IF EXISTS content_hash")
