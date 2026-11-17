"""P5 工作流**只读列表面**测试（真实 PostgreSQL + 真实 HTTP 入口）。

为什么在 ``unit/``：CI 的 ``backend-integration`` 分片按「排序下标 mod 4」轮转
⇒ 新增 integration 文件会把既有文件挤到别的分片 ⇒ 假 net-new。``backend-unit``
按「路径稳定哈希」分片 ⇒ 新增 unit 文件不影响既有归属（D-12）。

被测（补前端最小批次所需的三面读接口）：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | ``/instances`` | 实例列表（自作用域 / 审批持有人 / 真实 admin） |
| GET | ``/approvals`` | **我的待办**（审批持有人匹配，要求已校验身份） |
| GET | ``/definitions`` | 已发布定义（提交表单的候选集） |

⚠️ 列表面的授权口径必须与详情面 ``_assertInstanceVisible`` **逐条一致**：
「列表里看得到」但「详情 404」= 列表泄露存在性；反过来「详情看得到」但
「列表看不到」= 页面永远点不进去。

⚠️ 审批持有人匹配有两条来源：任务行上的 ``approver_role``（创建时快照）+
定义体审批矩阵里的 ``approverUser`` / ``approverRole``（``approver_user``
列**只在决策那一刻**写签署人 ⇒ 待办不能只看列）。
"""

from __future__ import annotations

import os

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.workflow_models import WorkflowDefinition
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

_STEPS = [
    {"code": "collect", "order": 0, "commandType": "po.collect"},
    {"code": "approve", "order": 1, "requiresApproval": True},
]

_ROLE_PURCHASE_MANAGER = "purchase_manager"
_ROLE_INTERN = "intern"


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
    """**开关关闭**时的客户端（路由不注册 ⇒ 404）。"""

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


async def _seedDefinition(
    dbSession: AsyncSession,
    *,
    status: str = "PUBLISHED",
    capabilityCode: str = "purchase_order_approve",
) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code=capabilityCode,
        version="v1",
        definition_json={
            "riskLevel": "MEDIUM",
            "steps": _STEPS,
            "approvalMatrix": _MATRIX,
        },
        status=status,
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _seedActors(dbSession: AsyncSession) -> dict[str, dict[str, str]]:
    """播种真实用户 + 角色 + JWT ⇒ ``{用户名: Bearer 头}``。"""

    managerRole = Role(code=_ROLE_PURCHASE_MANAGER, name="采购经理")
    internRole = Role(code=_ROLE_INTERN, name="实习生")
    adminRole = Role(code="admin", name="管理员")
    dbSession.add_all([managerRole, internRole, adminRole])
    await dbSession.commit()

    submitter = User(username="submitter", display_name="提交人", enabled=True)
    lead = User(username="lead", display_name="审批主管", enabled=True)
    intern = User(username="intern", display_name="实习生", enabled=True)
    admin = User(username="admin", display_name="管理员", enabled=True)
    outsider = User(username="outsider", display_name="无关用户", enabled=True)
    dbSession.add_all([submitter, lead, intern, admin, outsider])
    await dbSession.commit()

    dbSession.add_all(
        [
            UserRole(user_id=lead.id, role_id=managerRole.id),
            UserRole(user_id=intern.id, role_id=internRole.id),
            UserRole(user_id=admin.id, role_id=adminRole.id),
        ]
    )
    await dbSession.commit()

    headers: dict[str, dict[str, str]] = {}
    for user in (submitter, lead, intern, admin, outsider):
        token = await _auth_support.issueToken(dbSession, user)
        headers[user.username] = _auth_support.bearerHeaders(token)
    return headers


async def _submit(
    ac: AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    idempotencyKey: str = "list-key-1",
) -> int:
    resp = await ac.post(
        "/api/v1/workflow/instances",
        json={
            "domainCode": "procurement",
            "capabilityCode": "purchase_order_approve",
            "version": "v1",
            "idempotencyKey": idempotencyKey,
            "request": {"amount": 100},
        },
        headers=headers or {"X-User-Id": "submitter"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _requestApproval(ac: AsyncClient, instanceId: int, headers: dict[str, str]) -> list[dict]:
    resp = await ac.post(
        f"/api/v1/workflow/instances/{instanceId}/approval-request",
        json={},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# =============================================================================
# 开关关闭 ⇒ 整个读面不存在
# =============================================================================


async def test_开关关闭时三个列表接口均为404(offClient):
    for path in (
        "/api/v1/workflow/instances",
        "/api/v1/workflow/approvals",
        "/api/v1/workflow/definitions",
    ):
        resp = await offClient.get(path, headers={"X-User-Id": "submitter"})
        assert resp.status_code == 404, (path, resp.text)


# =============================================================================
# 实例列表
# =============================================================================


async def test_实例列表_自作用域只看到自己提交的实例(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient, headers=actors["submitter"], idempotencyKey="k-sub")

    seen = await workflowClient.get(
        "/api/v1/workflow/instances", headers=actors["submitter"]
    )
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["total"] == 1
    assert [row["id"] for row in body["rows"]] == [instanceId]

    # 无关用户：既不是提交人也不是审批持有人 ⇒ 空列表（不是 404、也不是别人的行）
    other = await workflowClient.get(
        "/api/v1/workflow/instances", headers=actors["outsider"]
    )
    assert other.status_code == 200, other.text
    assert other.json() == {"rows": [], "total": 0}


async def test_实例列表_审批持有人可见待审实例(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient, headers=actors["submitter"], idempotencyKey="k-hold")
    await _requestApproval(workflowClient, instanceId, actors["submitter"])

    # lead 持有 purchase_manager 角色 ⇒ 矩阵条目 approverRole 命中
    resp = await workflowClient.get("/api/v1/workflow/instances", headers=actors["lead"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body["rows"]] == [instanceId]
    # 列表行必须带定义上下文，否则页面只能显示裸 id
    row = body["rows"][0]
    assert row["domainCode"] == "procurement"
    assert row["capabilityCode"] == "purchase_order_approve"
    assert row["definitionVersion"] == "v1"
    assert row["status"] == "WAITING_APPROVAL"

    # 实习生角色不匹配 ⇒ 看不到
    intern = await workflowClient.get(
        "/api/v1/workflow/instances", headers=actors["intern"]
    )
    assert intern.json() == {"rows": [], "total": 0}


async def test_实例列表_真实admin可见全部(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    first = await _submit(workflowClient, headers=actors["submitter"], idempotencyKey="k-a")
    second = await _submit(workflowClient, headers=actors["lead"], idempotencyKey="k-b")

    resp = await workflowClient.get("/api/v1/workflow/instances", headers=actors["admin"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    # 新→旧
    assert [row["id"] for row in body["rows"]] == [second, first]


async def test_实例列表_分页与总数独立(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    ids = [
        await _submit(workflowClient, headers=actors["submitter"], idempotencyKey=f"k-{i}")
        for i in range(3)
    ]

    page = await workflowClient.get(
        "/api/v1/workflow/instances",
        params={"limit": 1, "offset": 1},
        headers=actors["submitter"],
    )
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == 3
    assert [row["id"] for row in body["rows"]] == [ids[1]]


# =============================================================================
# 我的待办
# =============================================================================


async def test_我的待办_按矩阵角色匹配且带实例上下文(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient, headers=actors["submitter"], idempotencyKey="k-todo")
    tasks = await _requestApproval(workflowClient, instanceId, actors["submitter"])

    resp = await workflowClient.get("/api/v1/workflow/approvals", headers=actors["lead"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    row = body["rows"][0]
    assert row["id"] == tasks[0]["id"]
    assert row["instanceId"] == instanceId
    assert row["status"] == "PENDING"
    # 待办行必须能直接渲染「这是什么业务、卡在什么状态」
    assert row["domainCode"] == "procurement"
    assert row["capabilityCode"] == "purchase_order_approve"
    assert row["instanceStatus"] == "WAITING_APPROVAL"


async def test_我的待办_无关用户与提交人为空(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient, headers=actors["submitter"], idempotencyKey="k-none")
    await _requestApproval(workflowClient, instanceId, actors["submitter"])

    for name in ("outsider", "intern", "submitter"):
        resp = await workflowClient.get("/api/v1/workflow/approvals", headers=actors[name])
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json() == {"rows": [], "total": 0}, name


async def test_我的待办_拒绝未验签的stub身份(workflowClient, dbSession):
    """stub 头可冒充任意账号 ⇒ 跨用户读待办必须验签（与决策面同口径）。

    自作用域（读自己提交的实例）不要求验签；但「我的待办」本质是跨用户读
    ⇒ 未验签一律 403，否则 ``X-User-Id: lead`` 就能读穿整个审批面。
    """

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)

    resp = await workflowClient.get(
        "/api/v1/workflow/approvals", headers={"X-User-Id": "lead"}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "APPROVAL_REAL_AUTH_REQUIRED"


async def test_我的待办_决策后PENDING不再返回(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient, headers=actors["submitter"], idempotencyKey="k-done")
    tasks = await _requestApproval(workflowClient, instanceId, actors["submitter"])

    decided = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=actors["lead"],
    )
    assert decided.status_code == 200, decided.text

    pending = await workflowClient.get(
        "/api/v1/workflow/approvals", headers=actors["lead"]
    )
    assert pending.json() == {"rows": [], "total": 0}

    approved = await workflowClient.get(
        "/api/v1/workflow/approvals",
        params={"status": "APPROVED"},
        headers=actors["lead"],
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["total"] == 1
    # 已决策的任务行上 approver_user = 实际签署人
    assert body["rows"][0]["approverUser"] == "lead"


# =============================================================================
# 已发布定义（提交表单候选集）
# =============================================================================


async def test_定义列表_只返回已发布(workflowClient, dbSession):
    published = await _seedDefinition(dbSession)
    await _seedDefinition(dbSession, status="DRAFT", capabilityCode="purchase_order_draft")

    resp = await workflowClient.get(
        "/api/v1/workflow/definitions", headers={"X-User-Id": "submitter"}
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [row["id"] for row in rows] == [published.id]
    assert rows[0]["domainCode"] == "procurement"
    assert rows[0]["capabilityCode"] == "purchase_order_approve"
    assert rows[0]["version"] == "v1"
    assert rows[0]["status"] == "PUBLISHED"


async def test_定义列表_不含定义体(workflowClient, dbSession):
    """定义体（审批矩阵 / 步骤编排）不下发：客户端不需要，也不该看到。

    矩阵里有审批人 → 下发等于把「谁批这件事」暴露给所有登录用户。
    """

    await _seedDefinition(dbSession)
    resp = await workflowClient.get(
        "/api/v1/workflow/definitions", headers={"X-User-Id": "submitter"}
    )
    assert resp.status_code == 200, resp.text
    assert "definitionJson" not in resp.json()[0]
    assert "approvalMatrix" not in resp.text
