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


# ---------------------------------------------------------------------------
# 创建 / 更新落 content_hash
# ---------------------------------------------------------------------------

_CONTENT = "注册资本 >= 1000 万"
_CONTENT_HASH = "5dd995a8688226c1fc01cc593b6bce29b2b1b96fcde0feca24ddaab3a13d4041"


async def test_create_page_writes_content_hash(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST /wiki/pages 落 content_hash = sha256(content)（可直接与 document_catalog 比对）。"""
    resp = await client.post(
        _PAGES, json={"title": "供应商准入规则", "content": _CONTENT}
    )
    assert resp.status_code == 201, resp.text
    pageId = resp.json()["pageId"]

    stored = (
        await dbSession.execute(
            text(
                "SELECT content_hash, page_id FROM wiki_page WHERE page_id = :pid"
            ),
            {"pid": pageId},
        )
    ).mappings().one()
    assert stored["content_hash"] == _CONTENT_HASH
    # API 生成的 ID 是确定性的内容派生（不再是随机后缀）
    assert stored["page_id"] == "PAGE-UNTITLED-012CA6C8"


async def test_patch_content_updates_hash_but_keeps_page_id(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """PATCH content 后 content_hash 必须重算，page_id 必须**不变**。

    两件事都是刻意的：
    - 哈希不重算 → 「同 ID 同内容 → 跳过」基于过期值判定，会把真冲突误判成重跑
      而静默丢知识（或反之）。
    - page_id 变了 → knowledge_claim / knowledge_relation 的 FK 指向的旧 ID 变成
      悬空，条目在关系网里直接断链。
    """
    created = await client.post(
        _PAGES, json={"title": "供应商准入规则", "content": _CONTENT}
    )
    pageId = created.json()["pageId"]

    resp = await client.patch(
        f"{_PAGES}/{pageId}", json={"content": "注册资本 >= 2000 万"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pageId"] == pageId

    stored = (
        await dbSession.execute(
            text("SELECT content_hash FROM wiki_page WHERE page_id = :pid"),
            {"pid": pageId},
        )
    ).scalar_one()
    assert stored == (
        "87fa50ec95c812008c1482260d4ab13894e579bdeeee3a675d663766169a8914"
    )


# ---------------------------------------------------------------------------
# 导入：重复 → 跳过
# ---------------------------------------------------------------------------


async def _execute(
    client: AsyncClient, drafts: list[dict[str, Any]], *, sourceRef: str
) -> dict[str, Any]:
    """跑一次导入（关闭自动分类 → 不调 LLM，把断言集中在去重本身）。"""
    resp = await client.post(
        f"{_IMPORT}/execute",
        json={
            "drafts": drafts,
            "sourceRef": sourceRef,
            "autoClassify": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_replay_same_file_yields_one_row_and_all_skipped(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """验收信号（spec §十 P1）：同一份文件导入两次 → wiki_page 恰 N 行。

    第二次不是「失败」而是「跳过」：运维看到的失败数不该因为重跑而虚高
    （spec §5.4）。
    """
    drafts = [
        {"title": "甲", "content": "内容甲"},
        {"title": "乙", "content": "内容乙"},
    ]

    first = await _execute(client, drafts, sourceRef="policy-v1.md")
    assert first["successPages"] == 2
    assert first["skippedPages"] == 0
    assert first["failedPages"] == 0
    assert first["status"] == "SUCCEEDED"
    assert await _countPages(dbSession) == 2

    second = await _execute(client, drafts, sourceRef="policy-v1.md")

    assert second["successPages"] == 0
    assert second["skippedPages"] == 2
    assert second["failedPages"] == 0
    assert second["status"] == "SUCCEEDED"
    assert second["pageIds"] == []  # 跳过路径不伪造台账条目
    assert await _countPages(dbSession) == 2, "重跑产生了副本"

    row = await _taskRow(dbSession, second["id"])
    assert row["skipped_pages"] == 2
    assert row["success_pages"] == 0
    assert row["failed_pages"] == 0
    assert row["error_message"] is None


async def test_replay_id_is_content_derived_not_random(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """page_id 是 (sourceRef, title, content) 的函数 —— 重跑算出同一个 ID。

    这条把「为什么重复项落不了库」的机制本身写进断言：不是靠读台账跳过，
    而是靠 uq_wiki_page_page_id 撞车。
    """
    await _execute(client, [{"title": "甲", "content": "内容甲"}], sourceRef="policy-v1.md")
    ids = list(
        (await dbSession.execute(text("SELECT page_id FROM wiki_page"))).scalars().all()
    )
    assert ids == ["PAGE-UNTITLED-840CCA89"]


async def test_same_title_different_content_creates_second_entry(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同标题不同内容**不是**重复：各自一条（旧实现下两者都表现为 ID 冲突，无法区分）。"""
    await _execute(client, [{"title": "条款", "content": "甲"}], sourceRef="v.md")
    second = await _execute(client, [{"title": "条款", "content": "乙"}], sourceRef="v.md")

    assert second["successPages"] == 1, "新内容被当成了重复而跳过"
    assert second["skippedPages"] == 0
    assert await _countPages(dbSession) == 2

    hashes = list(
        (await dbSession.execute(text("SELECT content_hash FROM wiki_page"))).scalars().all()
    )
    assert len(set(hashes)) == 2, "两条的 content_hash 必须不同"


async def test_explicit_page_id_with_different_content_still_fails(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式 pageId 撞车但内容不同 → 仍是冲突（计失败），不得静默跳过。

    这是 D1-1 的守门测试：`[:8]` 只有 32 bit，把「ID 撞车」当作「重复」会
    静默丢掉另一份文档。内容不可判定时宁可报错。
    """
    await _execute(
        client, [{"pageId": "MANUAL-001", "title": "甲", "content": "x"}], sourceRef="m.md"
    )
    second = await _execute(
        client, [{"pageId": "MANUAL-001", "title": "甲", "content": "y"}], sourceRef="m.md"
    )

    assert second["skippedPages"] == 0
    assert second["failedPages"] == 1
    assert second["status"] == "FAILED"
    assert second["errorMessage"]


async def test_explicit_page_id_with_same_content_is_skipped(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """显式 pageId 撞车且内容一致 → 确证重跑 → 跳过。"""
    await _execute(
        client, [{"pageId": "MANUAL-002", "title": "甲", "content": "x"}], sourceRef="m.md"
    )
    second = await _execute(
        client, [{"pageId": "MANUAL-002", "title": "甲", "content": "x"}], sourceRef="m.md"
    )

    assert second["skippedPages"] == 1
    assert second["failedPages"] == 0
    assert second["status"] == "SUCCEEDED"
    assert await _countPages(dbSession) == 1
