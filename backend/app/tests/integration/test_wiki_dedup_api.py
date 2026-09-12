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
    """POST /wiki/pages 落 content_hash = sha256(content)。

    与 ``document_catalog.content_hash`` **同算法、同格式、同列型**（都是 sha256
    小写 64 位 hex、``VARCHAR(64)``），但**输入不同**：那里哈希上传文件字节，
    这里哈希草稿文本，两处从不相等、也不互相派生。
    """
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
    assert stored["page_id"] == "PAGE-UNTITLED-7048C5E6"


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
    # 台账不变量：total == success + skipped + failed。结构上恒成立，但**只在
    # 跑完的路径上成立**——异常中止（abort）时还有未处理的草稿，等式有意不成立，
    # 所以这条断言只能放在一条已完成的导入任务上，不能拿去套 _markFailedBestEffort。
    assert row["total_pages"] == (
        row["success_pages"] + row["skipped_pages"] + row["failed_pages"]
    )


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
    assert ids == ["PAGE-UNTITLED-6B6EF282"]


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


async def test_null_content_hash_collision_is_never_a_skip(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """历史行 ``content_hash IS NULL`` 撞上再导入 → 必须计失败，不得静默跳过。

    0061 刻意**不回填** ``content_hash``，因此任何在此之前的行都处于「行存在但哈希
    为 NULL」这一状态，且重导这些内容时必然撞上它 —— 这是升级后**首先**会遇到、
    也最难自证的一种碰撞形状（不是「历史上最常见」：本表上线以来各库均为 0 行，
    见 summary §1）。跳过分支的守卫是 ``_importOne`` 里的 ``existingHash is not
    None``——一旦有人把它当成「自然清理」删掉，NULL 行撞上新导入就会退化成静默
    skip，悄悄丢弃一份内容从未被比对过的页面，且**没有任何测试打红**。本测试把
    这条分支钉死。
    """
    # Arrange：用 raw SQL 造一条 content_hash 为 NULL 的历史行（显式 page_id）。
    await dbSession.execute(
        text(
            "INSERT INTO wiki_page (page_id, title, content, content_hash) "
            "VALUES (:pid, :title, :content, NULL)"
        ),
        {"pid": "NULLHASH-001", "title": "甲", "content": "旧内容"},
    )
    await dbSession.commit()

    # Act/Assert：内容**不同** → 冲突（失败），不是跳过。
    result = await _execute(
        client,
        [{"pageId": "NULLHASH-001", "title": "甲", "content": "新内容"}],
        sourceRef="n.md",
    )
    assert result["skippedPages"] == 0
    assert result["failedPages"] == 1
    assert result["status"] == "FAILED"

    # 对称情形：即便内容**完全相同**也必须失败——NULL 不可判定为「重跑」，
    # 与「同内容 = 跳过」的那条路径不是一回事。
    again = await _execute(
        client,
        [{"pageId": "NULLHASH-001", "title": "甲", "content": "旧内容"}],
        sourceRef="n.md",
    )
    assert again["skippedPages"] == 0
    assert again["failedPages"] == 1


# ---------------------------------------------------------------------------
# 钉死 docstring 的声称 / 台账无关性
# ---------------------------------------------------------------------------


async def test_replay_skip_survives_deleted_task_ledger(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """跳过**不依赖** wiki_import_task.page_ids —— 把台账删光后重放仍然跳过。

    spec §5.5 要求「钉死 wiki_learning_models.py:172-173 今天只声称、并不存在
    的幂等重放」。做法是把台账整行删掉（``page_ids`` 随行消失），再导入同一份
    文件：
    - 若跳过靠读 page_ids → 台账没了就跳过不了 → 必落副本 → 断言打红；
    - 实际实现靠内容派生的 page_id 撞车 → 台账在不在都一样跳过。

    诚实边界：本测试证明的是「跳过的**可观测结果**不依赖台账」，不是某条 SQL
    的静态不变量（后者用语句级断言更脆）。对「docstring 的声称是否属实」而言，
    这个强度足够。
    """
    drafts = [
        {"title": "甲", "content": "内容甲"},
        {"title": "乙", "content": "内容乙"},
    ]
    await _execute(client, drafts, sourceRef="ledger-pin.md")
    assert await _countPages(dbSession) == 2

    # 抹掉台账（page_ids 随之消失）。wiki_page.imported_via_task_id 的 FK 是
    # ON DELETE SET NULL，行不会被连带删除。
    await dbSession.execute(text("DELETE FROM wiki_import_task"))
    await dbSession.commit()

    replay = await _execute(client, drafts, sourceRef="ledger-pin.md")

    assert replay["successPages"] == 0
    assert replay["skippedPages"] == 2
    assert await _countPages(dbSession) == 2, (
        "台账被删后就跳过不了了 —— 说明跳过依赖 page_ids，而它按设计只写不读"
    )


async def test_same_content_different_source_ref_is_two_entries(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """已知边界：sourceRef 参与身份派生，故同内容不同来源 = 两条。

    sourceRef 参与身份派生（spec §5.2 的设计；实现按长度前缀拼接 source_ref/
    title/content 后 sha256），这里把该设计的**代价**钉成显式行为而不是让它在
    生产里被偶然发现：用户第一次导入留空 sourceRef、第二次填了文件名，同一份
    文件会得到两个 ID、两条知识。
    缓解方式记录在 summary 风险段（向导层统一要求填 sourceRef，或后续把来源
    归一化为上传文件的内容哈希）。
    """
    await _execute(client, [{"title": "模板条款", "content": "同样的话"}], sourceRef="")
    second = await _execute(
        client, [{"title": "模板条款", "content": "同样的话"}], sourceRef="dept-b.md"
    )

    assert second["successPages"] == 1
    assert second["skippedPages"] == 0
    assert await _countPages(dbSession) == 2
