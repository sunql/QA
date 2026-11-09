"""语义发布只读 API（P1b / D9）的真实 PG 全链路。

D9 的两条硬约束在这里钉住：
1. **开关关闭时路由不注册**（`ENABLE_SEMANTIC_RELEASE=false` ⇒ 404），
   与通用 Agent 编排路由同一模式 —— 未启用的子系统不该有可探测的端点。
2. **admin 门禁**：草稿 / 归档版本、`created_by` / `note` 属于治理元数据，
   非 admin 一律 403。这不是「只读所以随意」——只读的是数据，不是治理信息。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.semantic_manifest_schema import manifestHash
from app.domain.semantic_release_models import SemanticRelease
from app.infrastructure.llm.embedding_provider_factory import resetEmbeddingClientCache
from app.infrastructure.security import crypto
from app.services.messages_zh import MSG_SEMANTIC_RELEASE_SCOPE_CONFLICT
from app.services.semantic_registry_service import SemanticReleaseIntegrityError
from app.tests import _pg_support

SEMANTIC_RELEASES_PATH = "/api/v1/semantic/releases"
_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
_USER_HEADERS = {"X-User-Id": "test-user", "X-User-Roles": "user"}

_MINIMAL_MANIFEST: dict[str, Any] = {
    "schemaVersion": "v1",
    "domains": [],
    "classes": [],
    "joins": [],
    "metrics": [],
    "kpis": [],
    "businessObjects": [],
}


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """开关打开的完整 API 链路客户端（覆盖 conftest 的默认 client）。"""
    monkeypatch.setenv("ENABLE_SEMANTIC_RELEASE", "true")
    getSettings.cache_clear()
    crypto.resetFernet()
    resetEmbeddingClientCache()
    try:
        async for apiClient in _pg_support.pgApiClient():
            yield apiClient
    finally:
        getSettings.cache_clear()


async def _insertRelease(
    session: AsyncSession,
    *,
    version: str,
    status: str = "DRAFT",
    domainCode: str | None = None,
    manifest: dict[str, Any] | None = None,
) -> SemanticRelease:
    body = manifest if manifest is not None else dict(_MINIMAL_MANIFEST)
    row = SemanticRelease(
        version=version,
        status=status,
        domain_code=domainCode,
        manifest_json=body,
        manifest_hash=manifestHash(body),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        SEMANTIC_RELEASES_PATH,
        f"{SEMANTIC_RELEASES_PATH}/active",
        f"{SEMANTIC_RELEASES_PATH}/1",
    ],
)
async def test_routes_are_not_registered_when_flag_is_off(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.delenv("ENABLE_SEMANTIC_RELEASE", raising=False)
    getSettings.cache_clear()
    try:
        async for apiClient in _pg_support.pgApiClient():
            response = await apiClient.get(path, headers=_ADMIN_HEADERS)
    finally:
        getSettings.cache_clear()

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# admin 门禁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        SEMANTIC_RELEASES_PATH,
        f"{SEMANTIC_RELEASES_PATH}/active",
        f"{SEMANTIC_RELEASES_PATH}/1",
    ],
)
async def test_non_admin_is_forbidden(client: AsyncClient, path: str) -> None:
    response = await client.get(path, headers=_USER_HEADERS)

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


async def test_list_returns_summaries_and_total(client: AsyncClient, dbSession: AsyncSession) -> None:
    first = await _insertRelease(dbSession, version="2026.11.10-1", status="DRAFT")
    second = await _insertRelease(
        dbSession, version="2026.11.10-2", status="ACTIVE", domainCode="PROCUREMENT"
    )

    response = await client.get(SEMANTIC_RELEASES_PATH, headers=_ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [row["id"] for row in payload["rows"]] == [second.id, first.id]
    # 列表不带 ~1MB 快照正文；字段走 camelCase。
    assert "manifest" not in payload["rows"][0]
    assert payload["rows"][0]["manifestHash"] == second.manifest_hash
    assert payload["rows"][0]["schemaVersion"] == "v1"


async def test_list_filters_by_status_and_domain(client: AsyncClient, dbSession: AsyncSession) -> None:
    await _insertRelease(dbSession, version="2026.11.10-1", status="DRAFT")
    await _insertRelease(dbSession, version="2026.11.10-2", status="DRAFT")
    await _insertRelease(dbSession, version="2026.11.10-3", status="ACTIVE", domainCode="PROCUREMENT")

    draftOnly = await client.get(SEMANTIC_RELEASES_PATH, params={"status": "DRAFT"}, headers=_ADMIN_HEADERS)
    assert draftOnly.status_code == 200
    assert draftOnly.json()["total"] == 2

    domainOnly = await client.get(
        SEMANTIC_RELEASES_PATH,
        params={"domainCode": "PROCUREMENT"},
        headers=_ADMIN_HEADERS,
    )
    assert domainOnly.status_code == 200
    assert [row["version"] for row in domainOnly.json()["rows"]] == ["2026.11.10-3"]

    globalOnly = await client.get(
        SEMANTIC_RELEASES_PATH,
        params={"globalOnly": "true"},
        headers=_ADMIN_HEADERS,
    )
    assert globalOnly.status_code == 200
    assert globalOnly.json()["total"] == 2


async def test_list_rejects_ambiguous_domain_scope(
    client: AsyncClient,
) -> None:
    response = await client.get(
        SEMANTIC_RELEASES_PATH,
        params={"globalOnly": "true", "domainCode": "PROCUREMENT"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 422
    # 是领域异常（`{"error": ...}`），不是 FastAPI 的参数校验 422（`{"detail": [...]}`）。
    assert response.json()["error"] == MSG_SEMANTIC_RELEASE_SCOPE_CONFLICT


async def test_list_rejects_out_of_range_limit(client: AsyncClient) -> None:
    response = await client.get(SEMANTIC_RELEASES_PATH, params={"limit": 0}, headers=_ADMIN_HEADERS)

    assert response.status_code == 422


@pytest.mark.parametrize("domainCode", ["", "X" * 21])
async def test_list_rejects_malformed_domain_code(client: AsyncClient, domainCode: str) -> None:
    """列表与 `/active` 的域编码契约必须一致（空串/超长都是 422，不是静默空集）。"""
    response = await client.get(
        SEMANTIC_RELEASES_PATH, params={"domainCode": domainCode}, headers=_ADMIN_HEADERS
    )

    assert response.status_code == 422


async def test_list_rejects_unknown_status(client: AsyncClient) -> None:
    response = await client.get(SEMANTIC_RELEASES_PATH, params={"status": "DELETED"}, headers=_ADMIN_HEADERS)

    assert response.status_code == 422


async def test_pagination_total_is_not_limited_by_page_size(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """分页头：rows 受 limit 约束，total 是全量（D9 的硬约束）。"""
    await _insertRelease(dbSession, version="2026.11.10-1")
    await _insertRelease(dbSession, version="2026.11.10-2")

    response = await client.get(SEMANTIC_RELEASES_PATH, params={"limit": 1}, headers=_ADMIN_HEADERS)

    assert response.status_code == 200
    assert len(response.json()["rows"]) == 1
    assert response.json()["total"] == 2


async def test_responses_are_not_cacheable(client: AsyncClient) -> None:
    """治理元数据（created_by / note / 快照）不得进浏览器或代理缓存。"""
    response = await client.get(SEMANTIC_RELEASES_PATH, headers=_ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# 激活快照
# ---------------------------------------------------------------------------


async def test_active_returns_null_when_nothing_published(client: AsyncClient) -> None:
    """未发布是正常状态：200 + null，不是 404 / 500。"""
    response = await client.get(f"{SEMANTIC_RELEASES_PATH}/active", headers=_ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json() is None


async def test_active_returns_the_published_manifest(client: AsyncClient, dbSession: AsyncSession) -> None:
    manifest = dict(_MINIMAL_MANIFEST)
    row = await _insertRelease(dbSession, version="2026.11.10-1", status="ACTIVE", manifest=manifest)

    response = await client.get(f"{SEMANTIC_RELEASES_PATH}/active", headers=_ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == row.id
    assert payload["manifest"] == manifest
    assert payload["manifestHash"] == manifestHash(manifest)


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


async def test_detail_returns_snapshot_and_missing_is_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    row = await _insertRelease(dbSession, version="2026.11.10-1")

    found = await client.get(f"{SEMANTIC_RELEASES_PATH}/{row.id}", headers=_ADMIN_HEADERS)
    assert found.status_code == 200
    assert found.json()["status"] == "DRAFT"
    assert found.json()["manifest"] == _MINIMAL_MANIFEST

    missing = await client.get(f"{SEMANTIC_RELEASES_PATH}/999999", headers=_ADMIN_HEADERS)
    assert missing.status_code == 404


@pytest.mark.parametrize("rawId", ["0", "-3", "abc", str(2**63)])
async def test_detail_rejects_invalid_ids(client: AsyncClient, rawId: str) -> None:
    """非正数 / 非整数 / 超 bigint：都是 422，不能退化到 500（asyncpg 整型越界）。"""
    response = await client.get(f"{SEMANTIC_RELEASES_PATH}/{rawId}", headers=_ADMIN_HEADERS)

    assert response.status_code == 422


async def test_tampered_snapshot_fails_closed(client: AsyncClient, dbSession: AsyncSession) -> None:
    """绕过发布路径改坏哈希的行：整条请求失败（异常上抛），不返回任何数据。

    ASGI 测试传输默认把未处理异常抛给调用方（与线上 500 同一条路径）；
    这里断言的是「fail-closed」本身——坏数据不会被当成语义下发。
    """
    row = await _insertRelease(dbSession, version="2026.11.10-1", status="ACTIVE")
    row.manifest_hash = "b" * 64
    await dbSession.commit()

    with pytest.raises(SemanticReleaseIntegrityError):
        await client.get(f"{SEMANTIC_RELEASES_PATH}/active", headers=_ADMIN_HEADERS)


# ---------------------------------------------------------------------------
# 写路径（D10）：HTTP 层的状态码 / 响应包 / 错误映射 / 门禁
# ---------------------------------------------------------------------------


async def test_create_returns_201_operation_envelope(client: AsyncClient) -> None:
    response = await client.post(
        SEMANTIC_RELEASES_PATH,
        headers=_ADMIN_HEADERS,
        json={"version": "2026.11.10-http-1", "note": "评审预览"},
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"release", "blockers", "warnings"}
    assert body["release"]["status"] == "DRAFT"
    assert body["release"]["version"] == "2026.11.10-http-1"
    assert body["release"]["note"] == "评审预览"
    # 响应是 CamelModel：JSON 必须是 camelCase 且带哈希契约
    assert len(body["release"]["manifestHash"]) == 64
    assert response.headers["cache-control"] == "no-store"


async def test_write_lifecycle_over_http(client: AsyncClient) -> None:
    """四个 POST 端点走完整生命周期；激活后读路径立刻可见。"""
    created = await client.post(
        SEMANTIC_RELEASES_PATH, headers=_ADMIN_HEADERS, json={"version": "2026.11.10-http-life"}
    )
    assert created.status_code == 201
    releaseId = created.json()["release"]["id"]

    validated = await client.post(
        f"{SEMANTIC_RELEASES_PATH}/{releaseId}/validate", headers=_ADMIN_HEADERS
    )
    assert validated.status_code == 200
    assert validated.json()["release"]["status"] == "VALIDATED"
    assert validated.json()["blockers"] == []

    activated = await client.post(
        f"{SEMANTIC_RELEASES_PATH}/{releaseId}/activate", headers=_ADMIN_HEADERS
    )
    assert activated.status_code == 200
    assert activated.json()["release"]["status"] == "ACTIVE"

    active = await client.get(f"{SEMANTIC_RELEASES_PATH}/active", headers=_ADMIN_HEADERS)
    assert active.status_code == 200
    assert active.json()["id"] == releaseId

    archived = await client.post(
        f"{SEMANTIC_RELEASES_PATH}/{releaseId}/archive", headers=_ADMIN_HEADERS
    )
    assert archived.status_code == 200
    assert archived.json()["release"]["status"] == "ARCHIVED"

    noActive = await client.get(f"{SEMANTIC_RELEASES_PATH}/active", headers=_ADMIN_HEADERS)
    assert noActive.status_code == 200
    assert noActive.json() is None


async def test_write_errors_map_to_domain_status_codes(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """409 / 404 / 422 必须是领域错误包（`error`），不是 FastAPI 的 `detail`。"""
    await _insertRelease(dbSession, version="2026.11.10-http-dup")

    duplicate = await client.post(
        SEMANTIC_RELEASES_PATH, headers=_ADMIN_HEADERS, json={"version": "2026.11.10-http-dup"}
    )
    assert duplicate.status_code == 409
    assert "error" in duplicate.json()

    missing = await client.post(
        f"{SEMANTIC_RELEASES_PATH}/999999/validate", headers=_ADMIN_HEADERS
    )
    assert missing.status_code == 404
    assert "error" in missing.json()

    badDomain = await client.post(
        SEMANTIC_RELEASES_PATH,
        headers=_ADMIN_HEADERS,
        json={"version": "2026.11.10-http-dom", "domainCode": "NO_SUCH_DOMAIN"},
    )
    assert badDomain.status_code == 422
    assert "error" in badDomain.json()

    invalidId = await client.post(f"{SEMANTIC_RELEASES_PATH}/0/validate", headers=_ADMIN_HEADERS)
    assert invalidId.status_code == 422


async def test_http_illegal_transition_is_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    row = await _insertRelease(dbSession, version="2026.11.10-http-act", status="ACTIVE")

    response = await client.post(
        f"{SEMANTIC_RELEASES_PATH}/{row.id}/validate", headers=_ADMIN_HEADERS
    )

    assert response.status_code == 409
    assert "error" in response.json()


@pytest.mark.parametrize(
    "path",
    [
        SEMANTIC_RELEASES_PATH,
        f"{SEMANTIC_RELEASES_PATH}/1/validate",
        f"{SEMANTIC_RELEASES_PATH}/1/activate",
        f"{SEMANTIC_RELEASES_PATH}/1/archive",
    ],
)
async def test_write_routes_are_forbidden_for_non_admin(client: AsyncClient, path: str) -> None:
    response = await client.post(path, headers=_USER_HEADERS, json={"version": "x"})

    assert response.status_code == 403
