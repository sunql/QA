"""知识条目批量删除端点集成测试（feat-wiki-batch-delete）。

真实 PostgreSQL + 完整 API 链路（Harness/rules/测试规范.md）：
从 HTTP 入口 ``POST /api/v1/wiki/pages/batch-delete`` 发起，经路由校验 →
service → 真实 PG，数据准备与断言都落在真实行上。

覆盖：
- 正常批量删 / 空数组 422 / 重复 id 去重
- 条数：无**产品**上限（250 条放行）；受 asyncpg 绑定参数硬上限约束，
  恰好 32767 条放行、32768 条 422（换掉原本的 500）
- 含不存在 id 的**部分成功** / 全部不存在
- 未认证 403（router 级认证覆盖新端点，防「新路由漏挂依赖」回归）
- 级联：claim / evidence / relation / 建议 / 规则 / 流程确实被 DB 清掉
- 入站关系（downstream 指向被删条目）**刻意保留** —— 与单条删除同一语义
- **审计**：单条/批量各写 DELETE 行、actor 取 X-User-Id、notFound 不写
- 快照走列级 SELECT（防退化：不得触发 claims/evidence 的 selectin 预加载）

断言一律走 **raw SQL**：批量删除用的是 ORM-enabled ``DELETE`` 语句，
SQLAlchemy 的 identity map 仍持有先前的 WikiPage 对象，用 ``select(Model)``
会命中缓存返回「还在」的假象（本项目踩过，见 seed upsert 的教训）。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \\
        .venv/bin/pytest app/tests/integration/test_wiki_batch_delete_api.py -q
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_learning_models import (
    ProcessWorkflow,
    StructureSuggestion,
    WikiRuleExecutable,
)
from app.domain.wiki_models import KnowledgeClaim, KnowledgeRelation
from app.domain.wiki_schemas import MAX_BATCH_DELETE_PAGE_IDS
from app.infrastructure import database as dbModule

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


async def test_large_batch_within_driver_limit_is_accepted(
    client: AsyncClient,
) -> None:
    """单批**无产品意义上的条数上限**（2026-09-12 决策）。

    原先硬编码 100 条上限，但业务侧确实存在「整批清理一个来源的条目」这种
    远超 100 条的操作，上限只会逼用户分多次删、每次都要重新多选。破坏性操作
    的护栏改由**前端二次确认弹窗**承担（见 AdminWikiPagesPage），不在传输层设卡。

    这里用 250 条（>2x 旧上限）不存在于库里的 id 验证：远超旧上限的批量
    仍然放行，逐条进 notFound。
    """
    # Arrange
    bigBatch = [f"PAGE-NOCAP-{i}" for i in range(250)]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": bigBatch})

    # Assert
    assert resp.status_code == 200
    assert resp.json()["requested"] == 250
    assert resp.json()["deletedPageIds"] == []
    assert len(resp.json()["notFound"]) == 250


async def test_batch_at_driver_parameter_limit_is_accepted(
    client: AsyncClient,
) -> None:
    """恰好等于 asyncpg 绑定参数上限时仍然放行（上限别设过头）。

    这条是**反向**护栏：挡住「为了防 500 而把 max_length 设得过小」。DTO 的
    maxLength 取的是驱动硬上限 MAX_BATCH_DELETE_PAGE_IDS，若有人把它调小
    （比如又退回 1000），本用例立刻红。

    id 全不存在，故只走到快照 SELECT 就返回，不会真删东西。
    """
    # Arrange
    atLimit = [f"PAGE-ATLIMIT-{i}" for i in range(MAX_BATCH_DELETE_PAGE_IDS)]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": atLimit})

    # Assert
    assert resp.status_code == 200
    assert resp.json()["requested"] == MAX_BATCH_DELETE_PAGE_IDS
    assert resp.json()["deletedPageIds"] == []


async def test_batch_over_driver_parameter_limit_returns_422(
    client: AsyncClient,
) -> None:
    """超过驱动绑定参数上限 → 422，而**不是** 500。

    这道校验的全部意义就是把一个 500 换成可读的 422：asyncpg 单条语句的绑定
    参数不得超过 32767，而 SQLAlchemy 的 ``in_()`` 把 page_ids 展开成 N 个
    独立占位符，超限时抛
    ``InterfaceError: the number of query arguments cannot exceed 32767`` ——
    该异常没有领域映射，会一路冒到 500。DTO 在这里拦住，错误在进 DB 之前就
    变成调用方能读懂的 422。

    注意响应体里带上限值：只说「422」而不说「为什么」等于把问题留给调用方猜。
    """
    # Arrange
    overLimit = [
        f"PAGE-OVERLIMIT-{i}" for i in range(MAX_BATCH_DELETE_PAGE_IDS + 1)
    ]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": overLimit})

    # Assert
    assert resp.status_code == 422
    assert str(MAX_BATCH_DELETE_PAGE_IDS) in resp.text


async def test_medium_batch_with_all_ids_hitting_deletes_and_audits_every_row(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """N=200 **全部命中**时，整条删除路径（批量 DELETE + 级联统计 + N 条审计）都要对。

    为什么单开一条：上面两条规模用例（250 / 32767）用的都是**不存在**的 id，只走到
    快照 SELECT 就进 notFound 分支，真正被取消上限所放开的路径 —— 大 N 的
    ``DELETE ... IN``、5 个级联 COUNT、以及 N 条审计 INSERT 循环 —— 在 N=3 上验过
    就没了。这条把 N 拉到 200 且**全部命中**，补上这段覆盖。

    造数走 raw SQL（一条 generate_series）而非 API：200 次 HTTP 会撞 30/min 限流，
    但被测动作仍从 HTTP 入口进，符合测试规范对「完整 API 链路」的要求。

    审计行数按 ``before_json->>'page_id'`` 的 ``seed-`` 前缀圈定：同一用例内
    TRUNCATE 过的 audit_log 虽为空，但用前缀圈定能挡住「将来有人在造数之后、
    删之前又写审计」的串扰。
    """
    # Arrange
    seeded = 200
    await dbSession.execute(
        text(
            "INSERT INTO wiki_page (page_id, title, content) "
            "SELECT 'seed-' || g, '批量知识-' || g, '正文' "
            "FROM generate_series(1, :n) AS g"
        ),
        {"n": seeded},
    )
    await dbSession.commit()
    pageIds = [f"seed-{i}" for i in range(1, seeded + 1)]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": pageIds})

    # Assert —— 全部命中，无 notFound
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requested"] == seeded
    assert len(body["deletedPageIds"]) == seeded
    assert body["notFound"] == []

    # 行真的没了（raw SQL 绕开 identity map）
    remaining = await _countRows(
        dbSession,
        "SELECT count(*) FROM wiki_page WHERE page_id LIKE 'seed-%'",
        {},
    )
    assert remaining == 0

    # 审计一行不少、不多：逐条对应而非一条汇总
    auditRows = await _countRows(
        dbSession,
        "SELECT count(*) FROM audit_log WHERE entity_type = 'wiki_page' "
        "AND action = 'DELETE' AND before_json->>'page_id' LIKE 'seed-%'",
        {},
    )
    assert auditRows == seeded

    # 快照字段齐全（抽一条验，防「N 很大时快照被省掉」）
    oneSnapshot = (
        await dbSession.execute(
            text(
                "SELECT before_json FROM audit_log WHERE entity_type = 'wiki_page' "
                "AND before_json->>'page_id' = 'seed-1'"
            )
        )
    ).scalar_one()
    assert oneSnapshot["title"] == "批量知识-1"
    assert oneSnapshot["page_id"] == "seed-1"


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


# ---------------------------------------------------------------------------
# 审计（2026-09-12 补：单条 + 批量都落 audit_log）
# ---------------------------------------------------------------------------


async def _auditRowsFor(dbSession: AsyncSession, pageId: str) -> list[dict[str, Any]]:
    """该 page_id 的审计行（raw SQL，绕开 identity map）。

    按 ``before_json->>'page_id'`` 过滤而非 ``entity_id``：``entity_id`` 是
    ``wiki_page.id``（自增序列），序列不随 TRUNCATE 复位，测试间无法预测；
    ``page_id`` 是随机后缀的业务键，天然唯一，是更稳的定位锚点。
    """
    rows = (
        await dbSession.execute(
            text(
                "SELECT entity_type, entity_id, action, actor, actor_departments, "
                "before_json, after_json FROM audit_log "
                "WHERE entity_type = 'wiki_page' AND before_json->>'page_id' = :pid "
                "ORDER BY id"
            ),
            {"pid": pageId},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def test_single_delete_writes_audit_row(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """单条删除落一条 DELETE 审计，before 快照可定位被删对象。

    这是原先的缺口：``deletePage`` 直接 ``session.delete`` 走人，audit_log
    里查无此事 —— 条目没了、谁删的、删的是什么，全无痕迹。
    """
    # Arrange
    pageId = await _createPage(client, title="待审计的单条")

    # Act
    resp = await client.delete(f"{_BASE}/{pageId}")

    # Assert
    assert resp.status_code == 204
    rows = await _auditRowsFor(dbSession, pageId)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["action"] == "DELETE"
    assert row["actor"], "actor 必填（审计可追溯性底线）"
    assert row["actor_departments"] is None or isinstance(
        row["actor_departments"], str
    )
    # DELETE 语义：before 必有、after 必空
    assert row["after_json"] is None
    assert row["before_json"]["title"] == "待审计的单条"


async def test_single_delete_audit_before_snapshot_omits_content(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """``before`` 不落正文 —— 否则 audit_log 会被 Markdown 撑爆。

    DELETE 的 before 只需回答「删的是哪一条」，正文体积无上限且对溯源无用。
    """
    # Arrange
    pageId = await _createPage(client, title="正文不入审计")

    # Act
    await client.delete(f"{_BASE}/{pageId}")

    # Assert
    rows = await _auditRowsFor(dbSession, pageId)
    assert len(rows) == 1
    assert "content" not in rows[0]["before_json"]


async def test_batch_delete_writes_one_audit_row_per_deleted_page(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """批量删 N 条 → 恰好 N 条审计，每条独立可查。

    为什么不写「一条批审计」：``audit_log`` 是 ``entity_id`` 建索引的**按实体**
    记账表，``AuditService.record`` 的契约就是一条一实体。合成一条会让
    「这条条目是谁删的」这个问题在按实体查审计时无解。
    """
    # Arrange
    ids = [
        await _createPage(client, title=f"批量审计-{i}") for i in range(3)
    ]

    # Act
    resp = await client.post(_BATCH, json={"pageIds": ids})

    # Assert
    assert resp.status_code == 200
    assert len(resp.json()["deletedPageIds"]) == 3
    for pageId in ids:
        rows = await _auditRowsFor(dbSession, pageId)
        assert len(rows) == 1, f"{pageId} 应有 1 条审计，实际 {len(rows)}"
        assert rows[0]["action"] == "DELETE"
        assert rows[0]["before_json"]["page_id"] == pageId


async def test_batch_delete_does_not_audit_missing_pages(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """没删掉的条目**不留审计** —— notFound 不是「已删除」事件。

    伪造一条删除审计比不写更糟：审计的价值全在「所记即所发生」。
    """
    # Arrange
    real = await _createPage(client, title="真会被删")
    ghost = "PAGE-GHOST-NEVER-EXISTED"

    # Act
    resp = await client.post(_BATCH, json={"pageIds": [real, ghost]})

    # Assert
    assert resp.status_code == 200
    assert resp.json()["notFound"] == [ghost]
    assert len(await _auditRowsFor(dbSession, real)) == 1
    assert await _auditRowsFor(dbSession, ghost) == []


async def test_delete_audit_records_caller_identity(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """审计的 actor 取自 ``X-User-Id``，不是硬编码。

    写死 ``"system"`` 这类值的审计等于没有审计 —— 出事时定位不到人。
    """
    # Arrange
    pageId = await _createPage(client, title="谁删的")
    headers = {"X-User-Id": "wiki-auditor-42"}

    # Act
    resp = await client.delete(f"{_BASE}/{pageId}", headers=headers)

    # Assert
    assert resp.status_code == 204
    rows = await _auditRowsFor(dbSession, pageId)
    assert len(rows) == 1
    assert rows[0]["actor"] == "wiki-auditor-42"


async def test_batch_delete_audit_entity_id_matches_wiki_page_pk(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """``entity_id`` 落的是 ``wiki_page.id``（整型 PK），不是业务键 ``page_id``。

    ``audit_log.entity_id`` 是 BIGINT；业务键是 varchar，塞进去要么报错要么
    被强转成一串无声的错值。这里用删除**前**取到的 PK 反查校对。
    """
    # Arrange
    pageId = await _createPage(client, title="PK 对照")
    pk = (
        await dbSession.execute(
            text("SELECT id FROM wiki_page WHERE page_id = :pid"), {"pid": pageId}
        )
    ).scalar_one()

    # Act
    await client.post(_BATCH, json={"pageIds": [pageId]})

    # Assert
    rows = await _auditRowsFor(dbSession, pageId)
    assert len(rows) == 1
    assert rows[0]["entity_id"] == pk


async def test_batch_delete_does_not_eager_load_child_rows(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """批量删除**不得**把子行整行读进内存 —— 快照必须是列级 SELECT。

    这是一条防退化测试。给快照取数最自然的写法是 ``select(WikiPage)`` 再读属性，
    但 ``WikiPage.claims`` 是 ``lazy="selectin"``：查整实体会连带把
    ``claims`` → ``evidence`` 全部预加载（实测 1 条 SELECT 变 3 条）。那等于
    为了记「删了哪几条」，先把**即将被删掉的子行**全都读进内存 —— 而批量删除
    已经没有条数上限了，这条路径没有规模保护。

    判据选 ``evidence``：它只被这条预加载链碰到（级联统计只 COUNT
    claim/relation/建议/规则/流程，压根不查 evidence），所以「有没有查
    evidence」就是「有没有发生预加载」的干净信号。
    """
    # Arrange：造一条带 claim 的条目，让预加载一旦发生就必然可见
    pageId = await _createPage(client, title="不该被子行拖累")
    await dbSession.execute(
        text(
            "INSERT INTO knowledge_claim (page_id, claim_text) VALUES (:pid, '一条断言')"
        ),
        {"pid": pageId},
    )
    await dbSession.commit()

    engine = dbModule.getEngine()
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        # Act
        resp = await client.post(_BATCH, json={"pageIds": [pageId]})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    # Assert
    assert resp.status_code == 200
    eagerLoads = [s for s in seen if "evidence" in s]
    assert eagerLoads == [], (
        "批量删除对子行做了整实体预加载 —— 快照应走 _SNAPSHOT_FIELDS 列级 SELECT："
        f"{eagerLoads}"
    )
