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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.models import DataDomain
from app.domain.semantic_manifest_schema import manifestHash
from app.domain.semantic_release_models import SemanticRelease
from app.infrastructure.llm.embedding_provider_factory import resetEmbeddingClientCache
from app.infrastructure.security import crypto
from app.models.rbac import Organization, Role, User, UserOrganization, UserRole
from app.services.knowledge.messages import MSG_DOMAIN_NOT_VISIBLE, MSG_PINNED_RELEASE_UNAVAILABLE
from app.services.messages_zh import MSG_SEMANTIC_RELEASE_SCOPE_CONFLICT
from app.services.semantic_registry_service import SemanticReleaseIntegrityError
from app.tests import _pg_support
from app.tests._knowledge_manifest import sampleManifest

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


# ---------------------------------------------------------------------------
# 知识服务 Facade（P4 D32）：HTTP 层契约
#
# 为什么放在这个文件里（而不是新建一个 integration 文件）：
# CI 的 integration 分片是「按排序下标 mod 4」轮转的 ⇒ **新建 integration 文件
# 会移动分片归属**，让原本绿的用例落到别的机器上失败（净新增失败 > 0 ⇒ 闸门转红）。
# ⇒ 新集成用例一律并进**已存在的** integration 文件；新文件只允许在 unit/ 下。
# ---------------------------------------------------------------------------

KNOWLEDGE_PATH = "/api/v1/semantic/knowledge/query"
PROCUREMENT_KNOWLEDGE_PATH = "/api/v1/procurement/knowledge/query"
_QUESTION = "交付延迟最高的供应商？"


@pytest.fixture()
async def knowledgeClient(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """打开知识 Facade 开关的完整 API 链路客户端（真实 PG + 真实路由）。"""
    monkeypatch.setenv("ENABLE_KNOWLEDGE_FACADE", "true")
    getSettings.cache_clear()
    crypto.resetFernet()
    resetEmbeddingClientCache()
    try:
        async for apiClient in _pg_support.pgApiClient():
            yield apiClient
    finally:
        getSettings.cache_clear()


# --- 知识 Facade 的数据准备 ---------------------------------------------------


async def _seedKnowledgeDomain(
    session: AsyncSession,
    *,
    domainCode: str = "PROCUREMENT",
    orgCode: str = "PROCUREMENT_ORG",
) -> None:
    """落一个 enabled 的数据域（带主组织）⇒ 域对「该组织的成员」可见。"""
    org = Organization(code=orgCode, name=orgCode)
    session.add(org)
    await session.flush()
    session.add(
        DataDomain(
            code=domainCode,
            name=domainCode,
            primary_org_id=org.id,
            enabled=True,
        )
    )
    await session.commit()


async def _seedDomainWithoutOrg(
    session: AsyncSession,
    *,
    domainCode: str = "LOGISTICS",
) -> None:
    """落一个**没有主组织**的域（fail-closed 边界 L-P4-1 的数据形状）。"""
    session.add(
        DataDomain(code=domainCode, name=domainCode, primary_org_id=None, enabled=True)
    )
    await session.commit()


async def _seedOrgMember(
    session: AsyncSession,
    *,
    username: str,
    orgCode: str,
) -> None:
    """落一个**真实 DB 用户**（user 角色 + 组织归属）。

    只有 DB 身份才能让 stub 头解析出真实角色 / 组织 ⇒ 非 admin 的可见性用例
    才有意义（头里写 `X-User-Roles` 在查无此人时会被默认值顶掉）。
    """
    org = (
        await session.execute(
            select(Organization).where(Organization.code == orgCode)
        )
    ).scalar_one_or_none()
    if org is None:
        org = Organization(code=orgCode, name=orgCode)
        session.add(org)
        await session.flush()
    user = User(username=username, display_name=username)
    role = Role(code="user", name="普通用户")
    session.add_all([user, role])
    await session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.add(UserOrganization(user_id=user.id, organization_id=org.id))
    await session.commit()


async def _seedActiveRelease(
    session: AsyncSession,
    *,
    domainCode: str = "PROCUREMENT",
    version: str = "2026.11.16-1",
) -> SemanticRelease:
    """落一版该域的 ACTIVE 发布（快照 = 共用样本，含类 / JOIN / 指标 / KPI / BO）。"""
    return await _insertRelease(
        session,
        version=version,
        status="ACTIVE",
        domainCode=domainCode,
        manifest=sampleManifest(),
    )


@pytest.mark.parametrize("path", [KNOWLEDGE_PATH, PROCUREMENT_KNOWLEDGE_PATH])
async def test_knowledge_routes_are_not_registered_when_flag_is_off(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """开关关闭 ⇒ 端点不存在（404）：未启用的子系统不该有可探测的端点。"""
    monkeypatch.delenv("ENABLE_KNOWLEDGE_FACADE", raising=False)
    getSettings.cache_clear()
    try:
        async for apiClient in _pg_support.pgApiClient():
            response = await apiClient.post(
                path, headers=_USER_HEADERS, json={"domain": "PROCUREMENT", "question": _QUESTION}
            )
    finally:
        getSettings.cache_clear()

    assert response.status_code == 404

async def test_knowledge_query_returns_four_routes_in_fixed_order(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D34：四路编排、顺序固定、逐路可辨（状态 + 条数 + 降级原因）。

    四路**全部接线**（不再有 `PENDING` 占位）：

    - 图谱路 / 指标路 / SQL 路 ⇒ `OK`（按发布读 ⇒ 引用可回溯）；
    - RAG 路 ⇒ 能力未启用时**显式降级**（fail-closed：声明了作用域却没有收窄能力
      ⇒ 拒绝，**不许**回落成不过滤的召回）。
    """
    from app.services.knowledge import rag_route

    monkeypatch.setattr(rag_route, "registryRagEnabled", lambda: False)
    await _seedKnowledgeDomain(dbSession)
    await _seedActiveRelease(dbSession)

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH, headers=_ADMIN_HEADERS, json={"domain": "PROCUREMENT", "question": _QUESTION}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["domain"] == "PROCUREMENT"
    assert body["question"] == _QUESTION
    assert [row["channel"] for row in body["routeResults"]] == ["GRAPH", "METRIC", "RAG", "SQL"]
    statuses = {row["channel"]: row["status"] for row in body["routeResults"]}
    assert statuses["GRAPH"] == "OK"
    assert statuses["METRIC"] == "OK"
    assert statuses["SQL"] == "OK"
    assert statuses["RAG"] == "DEGRADED"
    ragRow = next(row for row in body["routeResults"] if row["channel"] == "RAG")
    assert ragRow["degradedReason"]
    assert ragRow["itemCount"] == 0
    pendingRows = [row for row in body["routeResults"] if row["status"] == "PENDING"]
    assert pendingRows == []


async def test_sql_route_returns_physical_schema_traceable_to_release(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """SQL 路：证据里能看到**物理表 / 列**，且带发布版本（G5 的「引用可回溯」）。"""
    await _seedKnowledgeDomain(dbSession)
    release = await _seedActiveRelease(dbSession)

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": "供应商", "channels": ["SQL"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["routeResults"][0]["status"] == "OK"
    assert body["evidence"]
    assert any("T_SUPPLIER" in (item["snippet"] or "") for item in body["evidence"])
    for item in body["evidence"]:
        assert item["channel"] == "SQL"
        assert item["source"] == "RELEASE"
        assert item["releaseId"] == release.id
        assert item["releaseVersion"] == "2026.11.16-1"


# --- D35：四路统一权限口径的验收矩阵 + 引用回溯 ---------------------------------


@pytest.mark.parametrize(
    ("channel", "question"),
    [
        ("GRAPH", "交付延迟最高的供应商？"),
        ("METRIC", "供应商交付天数"),
        ("SQL", "供应商"),
    ],
)
async def test_every_route_evidence_is_traceable_to_the_release(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
    channel: str,
    question: str,
) -> None:
    """D35：每一路的证据都带 `releaseId` / `releaseVersion` ⇒ 引用可回溯到发布版本。"""
    await _seedKnowledgeDomain(dbSession)
    release = await _seedActiveRelease(dbSession)

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": question, "channels": [channel]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["routeResults"][0]["channel"] == channel
    assert body["routeResults"][0]["status"] == "OK"
    assert body["evidence"]
    for item in body["evidence"]:
        assert item["channel"] == channel
        assert item["source"] == "RELEASE"
        assert item["releaseId"] == release.id
        assert item["releaseVersion"] == "2026.11.16-1"


async def test_request_scope_narrowing_can_hide_the_domain(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """D35 双向：**请求只能收窄** ⇒ 收窄到别的组织 ⇒ 该域对本次请求**不可见**。

    （域主组织的成员请求 `orgCodes=["OTHER_ORG"]` ⇒ 与用户组织取交集为空
    ⇒ 域不再可见 ⇒ 200 + 空结果 + 警告，**不是** 403。）
    """
    await _seedKnowledgeDomain(dbSession)
    await _seedActiveRelease(dbSession)
    await _seedOrgMember(dbSession, username="kb-narrow", orgCode="PROCUREMENT_ORG")

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers={"X-User-Id": "kb-narrow"},
        json={
            "domain": "PROCUREMENT",
            "question": _QUESTION,
            "scope": {"orgCodes": ["OTHER_ORG"]},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["evidence"] == []
    assert MSG_DOMAIN_NOT_VISIBLE in body["warnings"]
    assert all(row["status"] == "DENIED" for row in body["routeResults"])


async def test_entity_hints_do_not_widen_access(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """D35：实体提示**只是提示**，不能放宽可见性（不能多给证据）。"""
    await _seedKnowledgeDomain(dbSession)
    await _seedActiveRelease(dbSession)

    plain = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": "供应商", "channels": ["GRAPH"]},
    )
    hinted = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={
            "domain": "PROCUREMENT",
            "question": "供应商",
            "channels": ["GRAPH"],
            "entityHints": [{"type": "SUPPLIER", "code": "SUPPLIER_001"}],
        },
    )

    assert plain.status_code == 200
    assert hinted.status_code == 200
    assert hinted.json()["evidence"] == plain.json()["evidence"]


async def test_graph_route_returns_evidence_traceable_to_release(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """图谱路：证据带 `releaseId` / `releaseVersion` ⇒ 引用可回溯到发布版本。"""
    await _seedKnowledgeDomain(dbSession)
    release = await _seedActiveRelease(dbSession)

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": "交付延迟最高的供应商？", "channels": ["GRAPH"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["release"]["id"] == release.id
    assert body["release"]["version"] == "2026.11.16-1"
    assert body["release"]["degraded"] is False
    assert body["evidence"]
    for item in body["evidence"]:
        assert item["channel"] == "GRAPH"
        assert item["source"] == "RELEASE"
        assert item["releaseId"] == release.id
        assert item["releaseVersion"] == "2026.11.16-1"
    routeRow = body["routeResults"][0]
    assert routeRow["status"] == "OK"
    assert routeRow["itemCount"] == len(body["evidence"])


async def test_metric_route_returns_evidence(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """指标路：指标 / KPI 命中并带发布版本（G5 的「引用可回溯」）。"""
    await _seedKnowledgeDomain(dbSession)
    release = await _seedActiveRelease(dbSession)

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": "供应商交付天数", "channels": ["METRIC"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["routeResults"][0]["status"] == "OK"
    assert body["evidence"]
    assert {item["kind"] for item in body["evidence"]} <= {"METRIC", "KPI"}
    assert all(item["releaseId"] == release.id for item in body["evidence"])


async def test_invisible_domain_returns_empty_with_warning(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """域不可见 ⇒ **200 + 空结果 + 警告**（裁决 4：不是 403）。

    这里用「非 admin 且不属于域的主组织」的身份 ⇒ 域不可见。
    """
    await _seedKnowledgeDomain(dbSession)
    await _seedActiveRelease(dbSession)
    await _seedOrgMember(dbSession, username="kb-outsider", orgCode="OTHER_ORG")

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers={"X-User-Id": "kb-outsider"},
        json={"domain": "PROCUREMENT", "question": _QUESTION},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["evidence"] == []
    assert body["release"]["degraded"] is True
    assert body["release"]["id"] is None
    assert MSG_DOMAIN_NOT_VISIBLE in body["warnings"]
    for row in body["routeResults"]:
        assert row["status"] == "DENIED"
        assert row["itemCount"] == 0


async def test_org_member_of_primary_org_can_query_the_domain(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """双向权限的**正向**用例：属于域主组织的用户查得到。"""
    await _seedKnowledgeDomain(dbSession)
    await _seedActiveRelease(dbSession)
    await _seedOrgMember(dbSession, username="kb-member", orgCode="PROCUREMENT_ORG")

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers={"X-User-Id": "kb-member"},
        json={"domain": "PROCUREMENT", "question": _QUESTION, "channels": ["GRAPH"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["routeResults"][0]["status"] == "OK"
    assert body["evidence"]


async def test_domain_without_primary_org_is_invisible_to_non_admin(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """fail-closed（L-P4-1）：域没设主组织 ⇒ 非 admin 一律看不见。"""
    await _seedDomainWithoutOrg(dbSession, domainCode="LOGISTICS")
    await _seedOrgMember(dbSession, username="kb-member", orgCode="PROCUREMENT_ORG")

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers={"X-User-Id": "kb-member"},
        json={"domain": "LOGISTICS", "question": _QUESTION},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["evidence"] == []
    assert MSG_DOMAIN_NOT_VISIBLE in body["warnings"]


async def test_knowledge_query_reports_degraded_release_without_version(
    knowledgeClient: AsyncClient,
) -> None:
    """没有发布面可引 ⇒ 必须**自证降级**，不许挂一个版本号骗人。"""
    response = await knowledgeClient.post(
        KNOWLEDGE_PATH, headers=_USER_HEADERS, json={"domain": "PROCUREMENT", "question": _QUESTION}
    )

    assert response.status_code == 200
    release = response.json()["release"]
    assert release["degraded"] is True
    assert release["id"] is None
    assert release["version"] is None
    assert release["manifestHash"] is None


async def test_knowledge_query_honours_requested_channels(
    knowledgeClient: AsyncClient,
) -> None:
    """只请求一路 ⇒ 只跑那一路（响应里只有一条 routeResult）。"""
    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_USER_HEADERS,
        json={"domain": "PROCUREMENT", "question": _QUESTION, "channels": ["RAG"]},
    )

    assert response.status_code == 200
    assert [row["channel"] for row in response.json()["routeResults"]] == ["RAG"]


async def test_knowledge_query_rejects_unknown_channel(knowledgeClient: AsyncClient) -> None:
    """非法通道在**请求解析层**被拒（422），不静默忽略第五路。"""
    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_USER_HEADERS,
        json={"domain": "PROCUREMENT", "question": _QUESTION, "channels": ["DOCUMENT"]},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("domain", ["", "   ", "X" * 21])
async def test_knowledge_query_rejects_malformed_domain(
    knowledgeClient: AsyncClient, domain: str
) -> None:
    """空 / 全空白 / 超长域码都是 422，不是静默空集。"""
    response = await knowledgeClient.post(
        KNOWLEDGE_PATH, headers=_USER_HEADERS, json={"domain": domain, "question": _QUESTION}
    )

    assert response.status_code == 422


async def test_procurement_alias_forces_procurement_domain(
    knowledgeClient: AsyncClient,
) -> None:
    """薄别名 = 域固定转发：调用方**没有机会**把请求引到别的域。

    请求体里多带一个 `domain` 也必须被忽略（DTO 里根本没有这个字段）。
    """
    response = await knowledgeClient.post(
        PROCUREMENT_KNOWLEDGE_PATH,
        headers=_USER_HEADERS,
        json={"question": _QUESTION, "domain": "OTHER"},
    )

    assert response.status_code == 200
    assert response.json()["domain"] == "PROCUREMENT"


async def test_same_interface_serves_a_second_domain(knowledgeClient: AsyncClient) -> None:
    """扩展性证据（契约层）：同一接口换一个域参数即可服务第二个域。"""
    response = await knowledgeClient.post(
        KNOWLEDGE_PATH, headers=_USER_HEADERS, json={"domain": "QUALITY", "question": _QUESTION}
    )

    assert response.status_code == 200
    assert response.json()["domain"] == "QUALITY"


async def test_knowledge_response_is_not_cacheable(knowledgeClient: AsyncClient) -> None:
    """知识查询结果不进浏览器 / 代理缓存。"""
    response = await knowledgeClient.post(
        KNOWLEDGE_PATH, headers=_USER_HEADERS, json={"domain": "PROCUREMENT", "question": _QUESTION}
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


# --- 钉住发布的授权（`releaseId` 不是越权开关）--------------------------------


async def test_pinned_other_domain_release_is_rejected(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """钉住**别的域**的发布 ⇒ 拒绝：域一致性是正确性，不是权限（admin 也不行）。"""
    await _seedKnowledgeDomain(dbSession)
    await _seedKnowledgeDomain(dbSession, domainCode="QUALITY", orgCode="QUALITY_ORG")
    other = await _insertRelease(
        dbSession,
        version="other-domain-1",
        status="ACTIVE",
        domainCode="QUALITY",
        manifest=sampleManifest(),
    )

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": _QUESTION, "releaseId": other.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["release"]["degraded"] is True
    assert body["release"]["id"] is None
    assert body["release"]["version"] is None
    assert body["evidence"] == []


async def test_pinned_draft_release_is_rejected_for_non_admin(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """非 admin 钉**未激活**（DRAFT）的发布 ⇒ 拒绝（发布治理闸在读侧的对齐）。"""
    await _seedKnowledgeDomain(dbSession)
    await _seedOrgMember(dbSession, username="kb-pinner", orgCode="PROCUREMENT_ORG")
    draft = await _insertRelease(
        dbSession,
        version="draft-1",
        status="DRAFT",
        domainCode="PROCUREMENT",
        manifest=sampleManifest(),
    )

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers={"X-User-Id": "kb-pinner"},
        json={"domain": "PROCUREMENT", "question": _QUESTION, "releaseId": draft.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["release"]["degraded"] is True
    assert body["release"]["version"] is None
    assert body["evidence"] == []


async def test_pinned_draft_release_is_allowed_for_admin(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """admin 可以钉**未激活**的发布（治理角色自查 / 预演）。"""
    await _seedKnowledgeDomain(dbSession)
    draft = await _insertRelease(
        dbSession,
        version="draft-2",
        status="DRAFT",
        domainCode="PROCUREMENT",
        manifest=sampleManifest(),
    )

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": _QUESTION, "releaseId": draft.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["release"]["degraded"] is False
    assert body["release"]["id"] == draft.id
    assert body["release"]["version"] == "draft-2"


async def test_pinned_active_same_domain_release_is_allowed_for_non_admin(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """非 admin 钉**本域 ACTIVE** 发布 ⇒ 允许（钉住 = 重放上一版）。"""
    await _seedKnowledgeDomain(dbSession)
    await _seedOrgMember(dbSession, username="kb-replay", orgCode="PROCUREMENT_ORG")
    active = await _insertRelease(
        dbSession,
        version="replay-1",
        status="ACTIVE",
        domainCode="PROCUREMENT",
        manifest=sampleManifest(),
    )

    response = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers={"X-User-Id": "kb-replay"},
        json={"domain": "PROCUREMENT", "question": _QUESTION, "releaseId": active.id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["release"]["degraded"] is False
    assert body["release"]["id"] == active.id
    assert body["release"]["version"] == "replay-1"
    assert body["evidence"]


async def test_pinned_release_denial_does_not_leak_existence(
    knowledgeClient: AsyncClient,
    dbSession: AsyncSession,
) -> None:
    """「不存在」与「存在但无权」**同一句文案** ⇒ 不给 release id 枚举 oracle。"""
    await _seedKnowledgeDomain(dbSession)
    await _seedKnowledgeDomain(dbSession, domainCode="QUALITY", orgCode="QUALITY_ORG")
    other = await _insertRelease(
        dbSession,
        version="other-domain-2",
        status="ACTIVE",
        domainCode="QUALITY",
        manifest=sampleManifest(),
    )

    missing = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": _QUESTION, "releaseId": 999999},
    )
    unauthorized = await knowledgeClient.post(
        KNOWLEDGE_PATH,
        headers=_ADMIN_HEADERS,
        json={"domain": "PROCUREMENT", "question": _QUESTION, "releaseId": other.id},
    )

    missingRoutes = {row["channel"]: row for row in missing.json()["routeResults"]}
    unauthorizedRoutes = {row["channel"]: row for row in unauthorized.json()["routeResults"]}
    for channel in missingRoutes:
        assert missingRoutes[channel]["degradedReason"] == unauthorizedRoutes[channel]["degradedReason"]
        assert missingRoutes[channel]["degradedReason"] == MSG_PINNED_RELEASE_UNAVAILABLE
    assert missing.json()["release"]["degraded"] is True
    assert unauthorized.json()["release"]["degraded"] is True
