"""知识条目批量删除端点集成测试（feat-wiki-batch-delete）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）：
从 HTTP 入口 ``POST /api/v1/wiki/pages/batch-delete`` 发起，经路由校验 →
service → 真实 PG，数据准备与断言都落在真实行上。

覆盖：
- 正常批量删 / 空数组 422 / 超上限 422 / 重复 id 去重
- 含不存在 id 的**部分成功** / 全部不存在
- 未认证 403（router 级认证覆盖新端点，防「新路由漏挂依赖」回归）
- 级联：claim / evidence / relation / 建议 / 规则 / 流程确实被 DB 清掉
- 入站关系（downstream 指向被删条目）**刻意保留** —— 与单条删除同一语义

断言一律走 **raw SQL**：批量删除用的是 ORM-enabled ``DELETE`` 语句，
SQLAlchemy 的 identity map 仍持有先前的 WikiPage 对象，用 ``select(Model)``
会命中缓存返回「还在」的假象（本项目踩过，见 seed upsert 的教训）。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pw_dev_2026@localhost:5433/qa_metadata_test \\
        .venv/bin/pytest app/tests/integration/test_wiki_batch_delete_api.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_learning_models import (
    ProcessWorkflow,
    StructureSuggestion,
    WikiRuleExecutable,
)
from app.domain.wiki_models import KnowledgeClaim, KnowledgeRelation
from app.domain.wiki_schemas import MAX_BATCH_DELETE_PAGES

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/pages"
_BATCH = f"{_BASE}/batch-delete"


async def _createPage(
    client: AsyncClient, *, pageId: str | None = None, title: str = "供应商准入规则"
) -> str:
    """建一条知识条目，返回 page_id。"""
    payload: dict[str, Any] = {"title": title, "content": "注册资本 >= 1000 万"}
    if pageId is not None:
        payload["pageId"] = pageId
    resp = await client.post(_BASE, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["pageId"]


async def _exists(dbSession: AsyncSession, pageId: str) -> bool:
    """该 page_id 是否还在库里（raw SQL，绕开 identity map 缓存）。"""
    row = (
        await dbSession.execute(
            text("SELECT 1 FROM wiki_page WHERE page_id = :pid"), {"pid": pageId}
        )
    ).first()
    return row is not None


async def _countRows(
    dbSession: AsyncSession, sql: str, params: dict[str, Any]
) -> int:
    return (await dbSession.execute(text(sql), params)).scalar_one()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_batch_delete_removes_all_requested_pages(client: AsyncClient) -> None:
    """一次删三条：全部命中，逐条落库消失。"""
    # Arrange
    pageIds = [await _createPage(client, title=f"知识-{i}") for i in range(3)]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": pageIds})

    # Assert
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested"] == 3
    assert sorted(body["deletedPageIds"]) == sorted(pageIds)
    assert body["notFound"] == []
    for pageId in pageIds:
        assert (await client.get(f"{_BASE}/{pageId}")).status_code == 404


async def test_batch_delete_leaves_other_pages_untouched(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """没被点名的条目不受影响 —— 批量删除只按 page_ids 精确匹配。"""
    # Arrange
    keepId = await _createPage(client, title="保留的条目")
    goneId = await _createPage(client, title="要删的条目")

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [goneId]})

    # Assert
    assert resp.status_code == 200
    assert await _exists(dbSession, keepId) is True
    assert await _exists(dbSession, goneId) is False


# ---------------------------------------------------------------------------
# 入参边界
# ---------------------------------------------------------------------------


async def test_empty_page_ids_returns_422(client: AsyncClient) -> None:
    """空数组是调用方 bug：静默成功会让「删了 0 条」看起来像成功。"""
    resp = await client.post(_BATCH, json={"pageIds": []})
    assert resp.status_code == 422


async def test_missing_page_ids_key_returns_422(client: AsyncClient) -> None:
    resp = await client.post(_BATCH, json={})
    assert resp.status_code == 422


async def test_over_limit_batch_returns_422(client: AsyncClient) -> None:
    """超过单批上限 → 422（不设上限就是单请求放大入口）。"""
    # Arrange
    tooMany = [f"PAGE-{i}" for i in range(MAX_BATCH_DELETE_PAGES + 1)]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": tooMany})

    # Assert
    assert resp.status_code == 422


async def test_exactly_at_limit_is_accepted(client: AsyncClient) -> None:
    """恰好等于上限应当放行 —— 边界是「大于」(>)，不是「大于等于」(>=)。"""
    # Arrange：全是库里没有的 id，故不会有任何删除，只验入参校验。
    atLimit = [f"PAGE-{i}" for i in range(MAX_BATCH_DELETE_PAGES)]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": atLimit})

    # Assert
    assert resp.status_code == 200
    assert resp.json()["requested"] == MAX_BATCH_DELETE_PAGES
    assert resp.json()["deletedPageIds"] == []
    assert len(resp.json()["notFound"]) == MAX_BATCH_DELETE_PAGES


async def test_duplicate_ids_are_deduped_without_error(client: AsyncClient) -> None:
    """重复 id 去重保序，不报错也不重复计数。"""
    # Arrange
    first = await _createPage(client, title="第一条")
    second = await _createPage(client, title="第二条")

    # Act
    resp = await client.post(
        _BATCH, json={"pageIds": [first, second, first, first]}
    )

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert body["requested"] == 2
    assert body["deletedPageIds"] == [first, second]
    assert body["notFound"] == []


# ---------------------------------------------------------------------------
# 部分成功语义
# ---------------------------------------------------------------------------


async def test_missing_ids_are_reported_but_rest_still_deleted(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """含不存在的 id → 不整体失败，已存在的照常删掉，缺失项如实回报。"""
    # Arrange
    a = await _createPage(client, title="A")
    b = await _createPage(client, title="B")

    # Act
    resp = await client.post(
        _BATCH, json={"pageIds": [a, "NO-SUCH-PAGE", b]}
    )

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert body["requested"] == 3
    assert body["deletedPageIds"] == [a, b]
    assert body["notFound"] == ["NO-SUCH-PAGE"]
    assert await _exists(dbSession, a) is False
    assert await _exists(dbSession, b) is False


async def test_all_ids_missing_returns_200_with_empty_deletions(
    client: AsyncClient,
) -> None:
    """全部不存在 → 200 + deletedPageIds 空（并发下同一条被删两次是正常的）。"""
    resp = await client.post(
        _BATCH, json={"pageIds": ["GHOST-1", "GHOST-2"]}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deletedPageIds"] == []
    assert body["notFound"] == ["GHOST-1", "GHOST-2"]


async def test_repeat_batch_delete_is_idempotent(client: AsyncClient) -> None:
    """同一批删两次：第二次全部进 notFound，不 5xx。"""
    # Arrange
    pageId = await _createPage(client, title="只删一次")

    # Act
    first = await client.post(_BATCH, json={"pageIds": [pageId]})
    second = await client.post(_BATCH, json={"pageIds": [pageId]})

    # Assert
    assert first.status_code == 200
    assert first.json()["deletedPageIds"] == [pageId]
    assert second.status_code == 200
    assert second.json()["deletedPageIds"] == []
    assert second.json()["notFound"] == [pageId]


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


async def test_batch_delete_requires_authentication(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉 stub auth（生产形态）→ 未认证请求 403。

    这条守的是「新端点漏挂 router 级 ``Depends(getCurrentUser)``」这个回归：
    挂到别的 router 上、或改成独立 router，就会在这里红。
    """
    # Arrange
    pageId = await _createPage(client, title="不该被删掉")
    monkeypatch.setenv("AUTH_STUB_ENABLED", "0")

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [pageId]})

    # Assert
    assert resp.status_code == 403
    # 鉴权发生在删除之前：拒绝的请求不能留下任何副作用
    monkeypatch.delenv("AUTH_STUB_ENABLED")
    assert (await client.get(f"{_BASE}/{pageId}")).status_code == 200


# ---------------------------------------------------------------------------
# 级联
# ---------------------------------------------------------------------------


async def test_batch_delete_cascades_relations_and_claims(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """claim / evidence / 传出 relation 随条目一起被 DB 级联清掉。"""
    # Arrange
    a = await _createPage(client, title="条目 A")
    b = await _createPage(client, title="条目 B")
    dbSession.add_all(
        [
            KnowledgeClaim(page_id=a, claim_text="注册资本 >= 1000 万", claim_type="RULE"),
            KnowledgeClaim(page_id=b, claim_text="成立 >= 3 年", claim_type="RULE"),
            KnowledgeRelation(
                upstream_page_id=a,
                downstream_type="ONTOLOGY_CLASS",
                downstream_id="SUPPLIER",
                relation_type="DESCRIBES",
            ),
        ]
    )
    await dbSession.commit()
    claimId = (
        await dbSession.execute(
            text("SELECT id FROM knowledge_claim WHERE page_id = :pid"), {"pid": a}
        )
    ).scalar_one()
    await dbSession.execute(
        text(
            "INSERT INTO evidence (claim_id, source_type, content) "
            "VALUES (:cid, 'DOCUMENT', '原文摘录')"
        ),
        {"cid": claimId},
    )
    await dbSession.commit()

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [a, b]})

    # Assert
    assert resp.status_code == 200
    body = resp.json()
    assert body["deletedPageIds"] == [a, b]
    # 报告的行数必须与真实落库结果一致，否则前端展示的「连带清理 N 条」是假的
    assert body["cascade"]["claims"] == 2
    assert body["cascade"]["relations"] == 1
    assert (
        await _countRows(
            dbSession,
            "SELECT count(*) FROM knowledge_claim WHERE page_id = ANY(CAST(:pids AS varchar[]))",
            {"pids": [a, b]},
        )
        == 0
    )
    assert (
        await _countRows(
            dbSession,
            "SELECT count(*) FROM knowledge_relation "
            "WHERE upstream_page_id = ANY(CAST(:pids AS varchar[]))",
            {"pids": [a, b]},
        )
        == 0
    )
    # evidence 是二级级联（挂在 claim 之下），随 claim 一起走
    assert (
        await _countRows(
            dbSession,
            "SELECT count(*) FROM evidence WHERE claim_id = :cid",
            {"cid": claimId},
        )
        == 0
    )


async def test_batch_delete_cascades_structure_artifacts(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """结构化产物（建议 / 可执行规则 / 流程）同样被级联清掉。"""
    # Arrange
    pageId = await _createPage(client, title="有结构化产物的条目")
    dbSession.add_all(
        [
            StructureSuggestion(
                page_id=pageId, suggested_dimension="RULE", status="PENDING"
            ),
            WikiRuleExecutable(
                page_id=pageId,
                rule_kind="THRESHOLD",
                rule_expression={
                    "conditions": [
                        {"field": "amount", "operator": ">=", "value": 1000}
                    ],
                    "action": {"kind": "REJECT"},
                },
            ),
            ProcessWorkflow(
                page_id=pageId,
                steps=[{"seq": 1, "name": "提交", "actor_role": "采购员"}],
            ),
        ]
    )
    await dbSession.commit()

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [pageId]})

    # Assert
    assert resp.status_code == 200
    cascade = resp.json()["cascade"]
    assert (cascade["suggestions"], cascade["rules"], cascade["workflows"]) == (1, 1, 1)
    for table in ("structure_suggestion", "wiki_rule_executable", "process_workflow"):
        assert (
            await _countRows(
                dbSession,
                f"SELECT count(*) FROM {table} WHERE page_id = :pid",
                {"pid": pageId},
            )
            == 0
        ), table


async def test_inbound_relations_are_kept_on_purpose(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """**入站关系刻意保留**：别的条目指向被删条目的关系行不跟着消失。

    ``knowledge_relation.downstream_id`` 是多态业务键（无 DB 级 FK），指向本条
    的那些行**属于别的条目**。删掉等于替别人静默丢掉一条已确认关系；保留后
    由机制 3 的 GAP 检测标成「悬空引用」，处置权仍在业务专家手里。
    单条删除（DELETE /pages/{pageId}）是同一语义，这里守住批量版本不跑偏。
    """
    # Arrange
    target = await _createPage(client, title="被引用的条目")
    referrer = await _createPage(client, title="引用方条目")
    dbSession.add(
        KnowledgeRelation(
            upstream_page_id=referrer,
            downstream_type="PAGE",
            downstream_id=target,
            relation_type="REFERENCES",
            confirmed=True,
        )
    )
    await dbSession.commit()

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [target]})

    # Assert
    assert resp.status_code == 200
    assert resp.json()["deletedPageIds"] == [target]
    # 引用方（upstream）没被删 → 它的关系行还在，且 cascade.relations 只算传出关系
    assert await _exists(dbSession, referrer) is True
    assert resp.json()["cascade"]["relations"] == 0
    assert (
        await _countRows(
            dbSession,
            "SELECT count(*) FROM knowledge_relation WHERE upstream_page_id = :pid",
            {"pid": referrer},
        )
        == 1
    )


async def test_batch_delete_does_not_touch_import_task_page_ids(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """导入任务的 ``page_ids`` 台账保留（它是历史记录，不是引用完整性）。

    ``wiki_import_task.page_ids`` 是 ARRAY 业务键、无 FK，按设计**只写不读**：
    删条目不回改台账，否则「这次导入产出了什么」这段历史就被抹掉了。
    """
    # Arrange
    pageId = await _createPage(client, title="导入来的条目")
    await dbSession.execute(
        text(
            "INSERT INTO wiki_import_task (task_type, status, page_ids, total_pages, "
            "success_pages, failed_pages, total_cost_usd) "
            "VALUES ('BULK_IMPORT', 'SUCCEEDED', CAST(:pids AS varchar[]), 1, 1, 0, 0)"
        ),
        {"pids": [pageId]},
    )
    await dbSession.commit()

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [pageId]})

    # Assert
    assert resp.status_code == 200
    assert (
        await _countRows(
            dbSession,
            "SELECT count(*) FROM wiki_import_task WHERE :pid = ANY(page_ids)",
            {"pid": pageId},
        )
        == 1
    )
