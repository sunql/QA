"""document_catalog.content_hash 唯一索引（spec §4.7 D2-2 的推迟项，P1 补上）。

**为什么单独成一个迁移**：这条索引原先写在 0061 里，与 ``wiki_page.content_hash``
列、``wiki_import_task.skipped_pages`` 列同批交付。但运维上需要一类操作 ——
**只撤掉唯一索引、保留另外两项**（唯一索引是三项里唯一有阻断能力的：它会让某串
已入库的字节永久占据该哈希，后续同内容的合法上传/导入一律 409）。这诉求在 0061
里表达不出来：``downgrade()`` 必须对称反转 ``upgrade()``，三项会被一起撤掉；
若为了「只撤索引」把 0061 的 downgrade 改成不对称，库就会停在**没有任何 revision
描述**的状态（``alembic_version`` = 0060 而两列仍在），而「版本号相同 ≠ 结构相同」
正是本项目历史「两库结构漂移」的病根（见 ``0060_schema_reconcile``）。
拆成独立迁移后，**撤索引 = ``alembic downgrade 0062``**，一条命令、只撤这一项、
版本号如实反映结构，且 ``upgrade``/``downgrade`` 仍严格对称。

语义：一条 catalog 行 = 一份源文档，同一份文件重复上传应映射到同一份文档。
与 ``wiki_page.content_hash`` 那两个索引**方向刻意相反**（那里必须非唯一：
共享模板是合法的）。两处同名不同约束不是笔误。

**PostgreSQL 的唯一索引允许多个 NULL**（NULL 互不相等），所以现存/未来的
``content_hash IS NULL`` 行不会被挡 —— 约束只作用于「已经算出了摘要」的行。
这条依赖由集成测试钉住：索引若被改成 ``NULLS NOT DISTINCT``，测试立刻打红。

**建索引前先查重**（``GROUP BY content_hash HAVING count(*) > 1``）：安全建索引
的依据是「**无重复分组**」，而不是「表是空的」。拆分当时实测两库均为 **0 个重复
分组** ⇒ 不会因重复摘要失败。建索引失败是**安全失败**（DDL 原子，要么建成要么
报错回滚），不会静默丢数据。

**拆分当时的两库状态**（2026-09-13）：两库 ``alembic_version`` 都已是
``0061_wiki_dedup`` 且**索引早已存在**（它最初由 0061 建出），故在这些库上本迁移
的 ``upgrade`` 是 **no-op**（``IF NOT EXISTS``），只是把版本号推进到 0062；
``upgrade`` 真正建索引的场景是「从 0060 一路升上来」的新库。
另：拆分时 prod ``document_catalog`` 为 1 行（``content_hash`` 非空、无重复），
测试库为 1 行（`规则.txt`，**测试残渣**）—— 0061 旧 docstring 里
「测试库为 0 行」的说法在迁移执行时成立、但已过时，此处按实测更正。

**防撞车**：P0 落地时不得再建同名或同义索引。

**revision id 为什么是缩写**：``alembic_version.version_num`` 是
``character varying(32)``。全拼 ``0062_document_catalog_content_hash_unique``
有 **41** 字符，写版本号时会
``StringDataRightTruncationError: value too long for type character varying(32)``
—— 而且**发生在 DDL 执行之后、版本号落库之前**，DB 会被留在「对象已建、版本没动」
的半截状态。本 ID 长 28 字符，留出余量。新增迁移时请先数一遍长度。

Revision ID: 0062
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0062_doc_catalog_hash_unique"
down_revision: str | None = "0061_wiki_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 幂等：存量库上索引已由拆分前的 0061 建出，此处必须是 no-op。
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_catalog_content_hash "
        "ON document_catalog (content_hash)"
    )


def downgrade() -> None:
    # 只撤这一条 —— 这正是本迁移独立成档的目的。另两项
    # （wiki_page.content_hash、wiki_import_task.skipped_pages）不在此处，故不受影响。
    op.execute("DROP INDEX IF EXISTS uq_document_catalog_content_hash")
