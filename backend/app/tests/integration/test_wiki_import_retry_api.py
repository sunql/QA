"""Wiki 导入任务 retry + 单 task 实时状态 集成测试（Phase 4）。

真实 PG + 完整 API 链路。
覆盖：
- retry_of_task_id 字段写入并出现在 GET 响应里
- 单 task 端点：返回当前 counts（执行中任务用）
- 横向隔离：非 admin 不能查别人的 task（404 而非 403，避免泄露存在性）
- retry 自然走幂等：重跑原失败任务，content_hash 命中的草稿计入 skipped_pages
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import LlmConfig
from app.domain.wiki_learning_models import WikiImportTask

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/wiki/import"
_PAGES = "/api/v1/wiki/pages"
_ADMIN_HEADERS = {"X-User-Id": "wiki-test-admin", "X-User-Roles": "admin"}


class _StubLlm:
    """空响应客户端 —— 模型预检通过、不返回任何有意义的分类结果。

    签名对齐 ``BaseLlmClient.complete(self, messages, **kwargs)``。
    """

    async def complete(self, _messages, **_kwargs):  # noqa: ANN003
        from app.infrastructure.llm.base_client import LlmResponse

        return LlmResponse(
            content="{}",
            modelName="stub",
            promptTokens=10,
            completionTokens=10,
            totalTokens=20,
        )


async def _seedModel(dbSession: AsyncSession) -> int:
    from decimal import Decimal

    config = LlmConfig(
        model_name="retry-test",
        provider="openai",
        cost_per_1k_input=Decimal("0"),
        cost_per_1k_output=Decimal("0"),
        max_input_tokens=8000,
        weight=10,
        cost_threshold=Decimal("0.05"),
        is_active=True,
    )
    dbSession.add(config)
    await dbSession.commit()
    return config.id


# ---------------------------------------------------------------------------
# 单 task 实时状态端点
# ---------------------------------------------------------------------------


async def test_get_task_returns_current_counts(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """GET /tasks/{id} 返回当前 counts（用于执行中任务轮询）。"""
    modelId = await _seedModel(dbSession)
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [
                    {"title": "甲", "content": "甲正文"},
                    {"title": "乙", "content": "乙正文"},
                ],
                "modelId": modelId,
            },
            headers=_ADMIN_HEADERS,
        )
    assert resp.status_code == 201
    taskId = resp.json()["id"]

    # 单 task GET
    single = await client.get(
        f"{_BASE}/tasks/{taskId}", headers=_ADMIN_HEADERS
    )
    assert single.status_code == 200
    body = single.json()
    assert body["id"] == taskId
    assert body["totalPages"] == 2
    assert body["status"] in {"SUCCEEDED", "PARTIAL"}
    assert body["successPages"] + body["failedPages"] + body["skippedPages"] == 2


async def test_get_task_404_for_unknown_id(client: AsyncClient) -> None:
    """不存在的 taskId → 404。"""
    resp = await client.get(
        f"{_BASE}/tasks/99999999", headers=_ADMIN_HEADERS
    )
    assert resp.status_code == 404


async def test_get_task_404_for_other_users_job(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """横向隔离：非 admin 查别人的 task → 404（不暴露存在性）。"""
    from scripts.seed_rbac import seedRbacBaseline

    await seedRbacBaseline(dbSession)
    for username in ("alice", "bob"):
        resp = await client.post(
            "/api/v1/users",
            headers=_ADMIN_HEADERS,
            json={"username": username, "display_name": username, "email": None},
        )
        assert resp.status_code == 201

    modelId = await _seedModel(dbSession)
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"title": "私密任务", "content": "私密内容"}],
                "modelId": modelId,
            },
            headers={"X-User-Id": "alice", "X-User-Roles": ""},
        )
    assert resp.status_code == 201
    taskId = resp.json()["id"]

    # bob 来查 → 404
    bobResp = await client.get(
        f"{_BASE}/tasks/{taskId}",
        headers={"X-User-Id": "bob", "X-User-Roles": ""},
    )
    assert bobResp.status_code == 404

    # alice 自己查 200
    aliceResp = await client.get(
        f"{_BASE}/tasks/{taskId}",
        headers={"X-User-Id": "alice", "X-User-Roles": ""},
    )
    assert aliceResp.status_code == 200


# ---------------------------------------------------------------------------
# retry_of_task_id 字段与重试幂等
# ---------------------------------------------------------------------------


async def test_execute_with_retry_of_task_id_records_link(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST execute 携带 retryOfTaskId → 新 task 的 retry_of_task_id 字段被持久化。"""
    modelId = await _seedModel(dbSession)
    # 原始任务（不需要真跑完 —— 直接 DB 落一条 FAILED 占位即可）
    original = WikiImportTask(
        task_type="BULK_IMPORT",
        source_type="MARKDOWN",
        status="FAILED",
        total_pages=1,
        success_pages=0,
        failed_pages=1,
        total_cost_usd=0,
        error_message="simulated failure",
    )
    dbSession.add(original)
    await dbSession.flush()
    originalId = original.id
    await dbSession.commit()

    # retry
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        resp = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"title": "重试的条目", "content": "新内容"}],
                "modelId": modelId,
                "retryOfTaskId": originalId,
            },
            headers=_ADMIN_HEADERS,
        )
    assert resp.status_code == 201
    assert resp.json()["retryOfTaskId"] == originalId

    # 库内确认
    dbSession.expire_all()
    retryTask = (
        await dbSession.execute(
            select(WikiImportTask).where(WikiImportTask.id == resp.json()["id"])
        )
    ).scalar_one()
    assert retryTask.retry_of_task_id == originalId


async def test_retry_naturally_dedupes_via_content_hash(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """重跑同一份 drafts：content_hash 命中跳过，skipped_pages=2（不重写）。

    验证 qa-system 的 content_hash 幂等机制在 retry 链路里也成立 —— 这就是
    任务重跑「免费」的根因。
    """
    modelId = await _seedModel(dbSession)
    drafts = [
        {"title": "规则一", "content": "正文 A"},
        {"title": "规则二", "content": "正文 B"},
    ]

    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        first = await client.post(
            f"{_BASE}/execute",
            json={"drafts": drafts, "modelId": modelId},
            headers=_ADMIN_HEADERS,
        )
        second = await client.post(
            f"{_BASE}/execute",
            json={"drafts": drafts, "modelId": modelId, "retryOfTaskId": first.json()["id"]},
            headers=_ADMIN_HEADERS,
        )

    assert first.json()["successPages"] == 2
    # 第二次重跑：draft 内容与第一次入库完全一致 → 全部命中 content_hash 跳过
    assert second.json()["skippedPages"] == 2
    assert second.json()["successPages"] == 0
    assert second.json()["failedPages"] == 0


async def test_retry_with_modified_content_creates_new_page(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """重跑改过内容 → 走正常路径写入；content_hash 不命中 → ConflictError 失败。

    这是 content_hash 幂等机制的另一面：内容确实变了就别想白嫖，必须显式
    决策。原 pageId 是按 (title, source_ref, content) 派生的，改内容后 pageId
    也变了 —— 落入新建路径而非重跑。
    """
    modelId = await _seedModel(dbSession)
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        first = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"title": "标题", "content": "原始内容"}],
                "modelId": modelId,
            },
            headers=_ADMIN_HEADERS,
        )
        # 改内容 → pageId 派生不同 → 全新写入
        second = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"title": "标题", "content": "改过的内容"}],
                "modelId": modelId,
                "retryOfTaskId": first.json()["id"],
            },
            headers=_ADMIN_HEADERS,
        )

    assert first.json()["successPages"] == 1
    assert second.json()["successPages"] == 1
    assert second.json()["skippedPages"] == 0


# ---------------------------------------------------------------------------
# POST /tasks/{id}/retry —— 服务端反查 Page 拼 drafts（Phase 4）
# ---------------------------------------------------------------------------


async def test_retry_endpoint_rebuilds_drafts_from_page_ids(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST retry：服务端从原 task.page_ids 反查 Page → 拼回 drafts → 重跑。

    前端不需要持有原 drafts —— 这正是 retry 走服务端的关键。
    """
    from app.domain.wiki_models import WikiPage

    modelId = await _seedModel(dbSession)
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        first = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [
                    {"title": "规则 A", "content": "A 正文"},
                    {"title": "规则 B", "content": "B 正文"},
                ],
                "modelId": modelId,
            },
            headers=_ADMIN_HEADERS,
        )
    assert first.status_code == 201
    originalId = first.json()["id"]
    originalPageIds = first.json()["pageIds"]
    assert len(originalPageIds) == 2

    # 校验 Page 真在库里（retry 路径依赖它们）
    pages = (
        await dbSession.execute(
            select(WikiPage).where(WikiPage.page_id.in_(originalPageIds))
        )
    ).scalars().all()
    assert {p.title for p in pages} == {"规则 A", "规则 B"}

    # 点 retry
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        resp = await client.post(
            f"{_BASE}/tasks/{originalId}/retry", headers=_ADMIN_HEADERS
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["retryOfTaskId"] == originalId
    # content 与第一次完全一致 → content_hash 命中 → 全部计入 skipped
    assert body["skippedPages"] == 2
    assert body["successPages"] == 0


async def test_retry_endpoint_404_for_unknown_task(client: AsyncClient) -> None:
    """未知 taskId → 404。"""
    resp = await client.post(
        f"{_BASE}/tasks/99999999/retry", headers=_ADMIN_HEADERS
    )
    assert resp.status_code == 404


async def test_retry_endpoint_404_for_other_users_job(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """横向隔离：非 admin 不可 retry 别人的 task → 404（不暴露存在性）。"""
    from scripts.seed_rbac import seedRbacBaseline

    await seedRbacBaseline(dbSession)
    for username in ("alice", "bob"):
        resp = await client.post(
            "/api/v1/users",
            headers=_ADMIN_HEADERS,
            json={"username": username, "display_name": username, "email": None},
        )
        assert resp.status_code == 201

    modelId = await _seedModel(dbSession)
    with patch(
        "app.services.learning.llm_invoker.createClient",
        return_value=_StubLlm(),
    ), patch(
        "app.services.wiki_import_service.createClient",
        return_value=_StubLlm(),
    ):
        first = await client.post(
            f"{_BASE}/execute",
            json={
                "drafts": [{"title": "私密", "content": "私密正文"}],
                "modelId": modelId,
            },
            headers={"X-User-Id": "alice", "X-User-Roles": ""},
        )
    assert first.status_code == 201
    taskId = first.json()["id"]

    bobResp = await client.post(
        f"{_BASE}/tasks/{taskId}/retry",
        headers={"X-User-Id": "bob", "X-User-Roles": ""},
    )
    assert bobResp.status_code == 404

    aliceResp = await client.post(
        f"{_BASE}/tasks/{taskId}/retry",
        headers={"X-User-Id": "alice", "X-User-Roles": ""},
    )
    assert aliceResp.status_code == 201


async def test_retry_endpoint_fails_when_task_has_no_pages(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """原任务 page_ids 为空 → 422（没法反查 drafts）。"""
    task = WikiImportTask(
        task_type="BULK_IMPORT",
        source_type="MARKDOWN",
        status="FAILED",
        page_ids=[],
        total_pages=2,
        success_pages=0,
        failed_pages=2,
        total_cost_usd=0,
        error_message="全失败",
    )
    dbSession.add(task)
    await dbSession.flush()
    taskId = task.id
    await dbSession.commit()

    resp = await client.post(
        f"{_BASE}/tasks/{taskId}/retry", headers=_ADMIN_HEADERS
    )
    assert resp.status_code == 422
