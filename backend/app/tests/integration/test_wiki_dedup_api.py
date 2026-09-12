"""P1 去重集成测试（feat-wiki-dedup）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）：
从 HTTP 入口发起，经路由校验 → service → 真实 PG，数据准备与断言都落在真实行上。

覆盖：
- 迁移 0061 的三处 DDL 确实生效（两列的列类型/可空 + 两个索引的名字/唯一性方向）
- POST /wiki/pages 落 content_hash；PATCH content 后哈希跟着变、page_id 不变
- 同一份文件导入两次 → wiki_page 恰 N 行，第二次全部计入 skippedPages
- 同标题不同内容 → 两条（不再撞号）
- 同内容不同 sourceRef → 两条（spec 公式的显式边界）
- 台账被删光后重放仍然跳过（钉死「跳过不依赖 page_ids」）

断言一律走 **raw SQL**：本项目的集成测试踩过 SQLAlchemy 2.x identity map
返回「还在」假象的坑（见 seed upsert 的教训），台账计数尤其要用裸 SQL 读。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
        .venv/bin/pytest app/tests/integration/test_wiki_dedup_api.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PAGES = "/api/v1/wiki/pages"
_IMPORT = "/api/v1/wiki/import"


async def _countPages(dbSession: AsyncSession) -> int:
    return (
        await dbSession.execute(text("SELECT count(*) FROM wiki_page"))
    ).scalar_one()


async def _taskRow(dbSession: AsyncSession, taskId: int) -> dict[str, Any]:
    """裸 SQL 读台账行（绕开 identity map）。"""
    row = (
        await dbSession.execute(
            text(
                "SELECT status, total_pages, success_pages, skipped_pages, "
                "failed_pages, page_ids, error_message "
                "FROM wiki_import_task WHERE id = :id"
            ),
            {"id": taskId},
        )
    ).mappings().one()
    return dict(row)


# ---------------------------------------------------------------------------
# 迁移 0061
# ---------------------------------------------------------------------------


async def test_wiki_page_content_hash_column_exists(
    dbSession: AsyncSession,
) -> None:
    """0061 之后 wiki_page.content_hash 存在：VARCHAR(64) 且可空。"""
    rows = (
        await dbSession.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = 'wiki_page' AND column_name = 'content_hash'"
            )
        )
    ).mappings().all()
    assert len(rows) == 1, "wiki_page.content_hash 缺失 —— 0061_wiki_dedup 未应用"
    assert rows[0]["data_type"] == "character varying"
    assert rows[0]["character_maximum_length"] == 64
    assert rows[0]["is_nullable"] == "YES"


async def test_wiki_page_content_hash_index_is_not_unique(
    dbSession: AsyncSession,
) -> None:
    """索引存在且**非唯一**（同一段正文出现在两条知识里是合法的）。"""
    indexDef = (
        await dbSession.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'wiki_page' "
                "AND indexname = 'ix_wiki_page_content_hash'"
            )
        )
    ).scalar_one_or_none()
    assert indexDef is not None, "ix_wiki_page_content_hash 缺失"
    assert "UNIQUE" not in indexDef.upper(), (
        "content_hash 索引变成了唯一索引 —— 共享模板的正常写入会 500，"
        "且「同 ID 同内容 → 跳过」分支永远走不到"
    )


async def test_import_task_skipped_pages_column_exists(
    dbSession: AsyncSession,
) -> None:
    """0061 之后 wiki_import_task.skipped_pages 存在：NOT NULL DEFAULT 0。"""
    row = (
        await dbSession.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'wiki_import_task' "
                "AND column_name = 'skipped_pages'"
            )
        )
    ).mappings().one_or_none()
    assert row is not None, (
        "wiki_import_task.skipped_pages 缺失 —— 0061_wiki_dedup 未应用"
    )
    assert row["is_nullable"] == "NO"
    assert row["column_default"] is not None and "0" in row["column_default"]


async def test_document_catalog_content_hash_index_is_unique(
    dbSession: AsyncSession,
) -> None:
    """0061 补上 spec §4.7 推迟到 P1 的 ``content_hash`` 唯一约束。

    与 ``wiki_page`` 那两个索引方向相反：``document_catalog`` 的行 = 一份源文档，
    同一份文件重复上传必须映射到同一份文档，所以这里**要**唯一。
    （``wiki_page.content_hash`` 反而必须非唯一 —— 共享模板是合法的。两处
    同名不同约束不是笔误，见 0061 模块 docstring。）

    另外确认它**允许 NULL 重复**：PostgreSQL 的唯一索引把 NULL 视为互不相等，
    所以尚未算出摘要的行不会被挡。这条断言把该依赖钉住 —— 换数据库或改成
    ``NULLS NOT DISTINCT`` 时立刻打红。
    """
    indexDef = (
        await dbSession.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'document_catalog' "
                "AND indexname = 'uq_document_catalog_content_hash'"
            )
        )
    ).scalar_one_or_none()
    assert indexDef is not None, (
        "uq_document_catalog_content_hash 缺失 —— spec §4.7 的唯一约束未落地"
    )
    assert "UNIQUE" in indexDef.upper()
    assert "NULLS NOT DISTINCT" not in indexDef.upper(), (
        "索引变成了 NULLS NOT DISTINCT —— content_hash 为 NULL 的行会互相冲突，"
        "而 P1 不回填历史行，写入会立刻炸"
    )
    assert indexDef.upper().count("(CONTENT_HASH)") == 1
