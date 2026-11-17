"""P5 工作流**定义管理面**测试（真实 PostgreSQL + 真实 HTTP 入口）。

为什么在 ``unit/``：CI 的 ``backend-integration`` 分片按「排序下标 mod 4」轮转
⇒ 新增 integration 文件会把既有文件挤到别的分片 ⇒ 假 net-new（D-12）。

被测（前端「新建工作流定义」所需的管理面）：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | ``/admin/definitions`` | 全部状态的定义（**含定义体**，仅 admin） |
| POST | ``/admin/definitions`` | 新建 **DRAFT** |
| PUT | ``/admin/definitions/{id}`` | 编辑（**仅 DRAFT**） |
| POST | ``/admin/definitions/{id}/publish`` | 发布（DRAFT → PUBLISHED） |
| POST | ``/admin/definitions/{id}/disable`` | 停用（PUBLISHED → DISABLED） |

三条硬约束（都是「权限/安全边界」，不是版本管理细节）：

1. **只 admin 可写**：定义体里的审批矩阵决定「谁有资格批」⇒ 能改定义 = 能改审批层级；
2. **PUBLISHED 不可改**（只能新版本）：在跑的实例读的是步骤快照，但审批矩阵
   在发起审批时读定义 ⇒ 改已生效定义 = 改「谁批」；
3. **同一域能力至多一条 PUBLISHED**（库层部分唯一索引）⇒ 发布冲突必须显式
   拒绝而不是自动替换旧版本（自动替换 = 静默改审批层级）。
"""

from __future__ import annotations

import os

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.models import AuditLog
from app.models.rbac import Role, User, UserRole
from app.tests import _auth_support, _pg_support

_MATRIX = [
    {
        "matrixCode": "po_medium_manager",
        "subjectType": "workflow_instance",
        "capabilityCode": "purchase_order_approve",
        "riskLevel": "MEDIUM",
        "approverRole": "purchase_manager",
        "timeoutHours": 24,
    }
]

_DEFINITION_JSON = {
    "riskLevel": "MEDIUM",
    "steps": [
        {"code": "collect", "order": 0, "commandType": "po.write"},
        {"code": "approve", "order": 1, "requiresApproval": True},
    ],
    "approvalMatrix": _MATRIX,
}


@pytest_asyncio.fixture()
async def workflowClient() -> AsyncClient:
    """开启 ``ENABLE_WORKFLOW`` 的完整 API 链路客户端。"""

    previous = os.environ.get("ENABLE_WORKFLOW")
    os.environ["ENABLE_WORKFLOW"] = "1"
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        if previous is None:
            os.environ.pop("ENABLE_WORKFLOW", None)
        else:
            os.environ["ENABLE_WORKFLOW"] = previous
        getSettings.cache_clear()


@pytest_asyncio.fixture()
async def offClient() -> AsyncClient:
    """**开关关闭**时的客户端（整组路由不注册 ⇒ 404）。"""

    previous = os.environ.get("ENABLE_WORKFLOW")
    os.environ.pop("ENABLE_WORKFLOW", None)
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            yield ac
    finally:
        if previous is not None:
            os.environ["ENABLE_WORKFLOW"] = previous
        getSettings.cache_clear()


async def _seedActors(dbSession: AsyncSession) -> dict[str, dict[str, str]]:
    """播种真实用户 + 角色 + JWT ⇒ ``{用户名: Bearer 头}``。"""

    adminRole = Role(code="admin", name="管理员")
    internRole = Role(code="intern", name="实习生")
    dbSession.add_all([adminRole, internRole])
    await dbSession.commit()

    admin = User(username="admin", display_name="管理员", enabled=True)
    intern = User(username="intern", display_name="实习生", enabled=True)
    dbSession.add_all([admin, intern])
    await dbSession.commit()

    dbSession.add_all(
        [
            UserRole(user_id=admin.id, role_id=adminRole.id),
            UserRole(user_id=intern.id, role_id=internRole.id),
        ]
    )
    await dbSession.commit()

    headers: dict[str, dict[str, str]] = {}
    for user in (admin, intern):
        token = await _auth_support.issueToken(dbSession, user)
        headers[user.username] = _auth_support.bearerHeaders(token)
    return headers


def _payload(**overrides) -> dict:
    body = {
        "domainCode": "procurement",
        "capabilityCode": "purchase_order_approve",
        "version": "v1",
        "definitionJson": _DEFINITION_JSON,
    }
    body.update(overrides)
    return body


async def _create(ac: AsyncClient, headers: dict[str, str], **overrides) -> dict:
    resp = await ac.post(
        "/api/v1/workflow/admin/definitions", json=_payload(**overrides), headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# =============================================================================
# 开关关闭 ⇒ 管理面不存在
# =============================================================================


async def test_开关关闭时管理面404(offClient):
    for method, path in (
        ("get", "/api/v1/workflow/admin/definitions"),
        ("post", "/api/v1/workflow/admin/definitions"),
    ):
        resp = await getattr(offClient, method)(path, headers={"X-User-Id": "admin"})
        assert resp.status_code == 404, (path, resp.text)


# =============================================================================
# 授权：只 admin 可写
# =============================================================================


async def test_非admin创建被拒403且不落库(workflowClient, dbSession):
    actors = await _seedActors(dbSession)

    resp = await workflowClient.post(
        "/api/v1/workflow/admin/definitions",
        json=_payload(),
        headers=actors["intern"],
    )
    assert resp.status_code == 403, resp.text

    listed = await workflowClient.get(
        "/api/v1/workflow/admin/definitions", headers=actors["admin"]
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 0


async def test_非admin读管理列表被拒403(workflowClient, dbSession):
    actors = await _seedActors(dbSession)

    resp = await workflowClient.get(
        "/api/v1/workflow/admin/definitions", headers=actors["intern"]
    )
    assert resp.status_code == 403, resp.text


# =============================================================================
# 新建：DRAFT + 校验 + 审计
# =============================================================================


async def test_创建定义落为DRAFT并带定义体(workflowClient, dbSession):
    actors = await _seedActors(dbSession)

    created = await _create(workflowClient, actors["admin"])
    assert created["status"] == "DRAFT"
    assert created["domainCode"] == "procurement"
    assert created["definitionJson"]["riskLevel"] == "MEDIUM"

    # DRAFT 不进提交候选集（不发版就不能跑）
    public = await workflowClient.get(
        "/api/v1/workflow/definitions", headers=actors["admin"]
    )
    assert public.status_code == 200, public.text
    assert public.json() == []

    # 管理列表看得见，并且带定义体（编辑要用）
    listed = await workflowClient.get(
        "/api/v1/workflow/admin/definitions", headers=actors["admin"]
    )
    body = listed.json()
    assert body["total"] == 1
    assert body["rows"][0]["definitionJson"]["steps"][0]["code"] == "collect"

    # 治理审计：谁建了哪条定义
    audits = (
        (
            await dbSession.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "workflow_definition")
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.action for row in audits] == ["CREATE"]


async def test_创建时步骤无动作被拒422(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    broken = {
        "riskLevel": "MEDIUM",
        "steps": [{"code": "empty", "order": 0}],
    }

    resp = await workflowClient.post(
        "/api/v1/workflow/admin/definitions",
        json=_payload(definitionJson=broken),
        headers=actors["admin"],
    )
    assert resp.status_code == 422, resp.text
    assert "可执行动作" in resp.json()["error"]

    listed = await workflowClient.get(
        "/api/v1/workflow/admin/definitions", headers=actors["admin"]
    )
    assert listed.json()["total"] == 0


async def test_重复三元组创建被拒409(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    await _create(workflowClient, actors["admin"])

    resp = await workflowClient.post(
        "/api/v1/workflow/admin/definitions",
        json=_payload(),
        headers=actors["admin"],
    )
    assert resp.status_code == 409, resp.text


# =============================================================================
# 编辑：只有 DRAFT 可改
# =============================================================================


async def test_DRAFT可编辑且写审计(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    created = await _create(workflowClient, actors["admin"])

    updated = await workflowClient.put(
        f"/api/v1/workflow/admin/definitions/{created['id']}",
        json=_payload(
            definitionJson={
                "riskLevel": "HIGH",
                "steps": [{"code": "only", "order": 0, "commandType": "po.write"}],
            }
        ),
        headers=actors["admin"],
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["definitionJson"]["riskLevel"] == "HIGH"

    actions = (
        (
            await dbSession.execute(
                select(AuditLog.action)
                .where(AuditLog.entity_type == "workflow_definition")
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert actions == ["CREATE", "UPDATE"]


async def test_已发布定义不可编辑409(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    created = await _create(workflowClient, actors["admin"])
    published = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{created['id']}/publish",
        json={},
        headers=actors["admin"],
    )
    assert published.status_code == 200, published.text

    resp = await workflowClient.put(
        f"/api/v1/workflow/admin/definitions/{created['id']}",
        json=_payload(),
        headers=actors["admin"],
    )
    assert resp.status_code == 409, resp.text


# =============================================================================
# 发布：可运行性校验 + 生效版本唯一
# =============================================================================


async def test_有审批门但矩阵未覆盖时发布被拒422(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    created = await _create(
        workflowClient,
        actors["admin"],
        definitionJson={
            "riskLevel": "MEDIUM",
            "steps": [{"code": "gate", "order": 0, "requiresApproval": True}],
        },
    )

    resp = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{created['id']}/publish",
        json={},
        headers=actors["admin"],
    )
    assert resp.status_code == 422, resp.text

    listed = await workflowClient.get(
        "/api/v1/workflow/admin/definitions", headers=actors["admin"]
    )
    assert listed.json()["rows"][0]["status"] == "DRAFT"


async def test_发布后进入提交候选集(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    created = await _create(workflowClient, actors["admin"])

    resp = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{created['id']}/publish",
        json={},
        headers=actors["admin"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PUBLISHED"

    public = await workflowClient.get(
        "/api/v1/workflow/definitions", headers=actors["admin"]
    )
    assert [row["id"] for row in public.json()] == [created["id"]]
    # 公共面仍然**不下发定义体**
    assert "definitionJson" not in public.json()[0]


async def test_同域能力已有生效版本时发布被拒409(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    first = await _create(workflowClient, actors["admin"])
    await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{first['id']}/publish",
        json={},
        headers=actors["admin"],
    )
    second = await _create(workflowClient, actors["admin"], version="v2")

    resp = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{second['id']}/publish",
        json={},
        headers=actors["admin"],
    )
    assert resp.status_code == 409, resp.text


# =============================================================================
# 停用：PUBLISHED → DISABLED，退出提交候选集
# =============================================================================


async def test_停用后不再可提交且不可重复停用(workflowClient, dbSession):
    actors = await _seedActors(dbSession)
    created = await _create(workflowClient, actors["admin"])
    await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{created['id']}/publish",
        json={},
        headers=actors["admin"],
    )

    resp = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{created['id']}/disable",
        json={},
        headers=actors["admin"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "DISABLED"

    public = await workflowClient.get(
        "/api/v1/workflow/definitions", headers=actors["admin"]
    )
    assert public.json() == []

    again = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{created['id']}/disable",
        json={},
        headers=actors["admin"],
    )
    assert again.status_code == 409, again.text


async def test_停用后可发布新版本(workflowClient, dbSession):
    """停用 ≠ 删除历史；新版本可以接替生效。"""

    actors = await _seedActors(dbSession)
    first = await _create(workflowClient, actors["admin"])
    for action in ("publish", "disable"):
        resp = await workflowClient.post(
            f"/api/v1/workflow/admin/definitions/{first['id']}/{action}",
            json={},
            headers=actors["admin"],
        )
        assert resp.status_code == 200, resp.text

    second = await _create(workflowClient, actors["admin"], version="v2")
    resp = await workflowClient.post(
        f"/api/v1/workflow/admin/definitions/{second['id']}/publish",
        json={},
        headers=actors["admin"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PUBLISHED"
