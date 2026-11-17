"""P5 审批面**完整 API 链路**测试（真实 PostgreSQL + 真实 HTTP 入口）。

为什么放在 ``unit/`` 而不是 ``integration/``（D-12）：
CI 的 ``backend-integration`` 分片按「排序下标 mod 4」轮转 ⇒ **新增 integration
文件会轮转其它文件的分片归属** ⇒ 触发假 net-new。而 ``backend-unit`` 已改成
「路径稳定哈希」分片 ⇒ 新增 unit 文件**不影响**任何既有文件的归属。
本文件因此自带 HTTP 客户端夹具（复用 ``app/tests/_pg_support.pgApiClient``），
在 unit 目录里跑**完整链路**：路由 → 中间件（鉴权 / 限流）→ service → 真实 PG。

被测：审批七守卫在**真实 HTTP 入口**上的表现，以及
「开关关闭 ⇒ 路由不存在（404）」。

⚠️ 断言口径：断**状态码 + 响应体语义**（``detail`` 携带错误码），并落到真实 PG 行上核对。

⚠️ 审批决策要求**已校验身份**（``CurrentUser.isRealIdentity``：JWT 验签过 ⇒
``jti`` 非空）⇒ 本文件必须**播种真实用户与角色**并走**真实 JWT 链路**
（``app/tests/_auth_support.py``）。stub 头**只能**冒充某个 DB 账号
（``X-User-Id: lead``）⇒ 治理写面绝不能只信它。
"""

from __future__ import annotations

import os

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.workflow_models import ApprovalTask, WorkflowDefinition
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

_ROLE_PURCHASE_MANAGER = "purchase_manager"
_ROLE_INTERN = "intern"


def _headers(userId: str, roles: str = "") -> dict[str, str]:
    """stub 身份头（stub auth 是本仓既有机制：X-User-Id + X-User-Roles）。

    ⚠️ 对**真实 DB 用户**，``X-User-Roles`` 会被忽略（角色来自 DB）⇒ 伪造头无效。
    """

    headers = {"X-User-Id": userId}
    if roles:
        headers["X-User-Roles"] = roles
    return headers


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


async def _seedDefinition(dbSession: AsyncSession) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="purchase_order_approve",
        version="v1",
        definition_json={
            "riskLevel": "MEDIUM",
            "steps": [
                {"code": "collect", "order": 0, "commandType": "po.collect"},
                {"code": "approve", "order": 1, "requiresApproval": True},
            ],
            "approvalMatrix": _MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _seedActors(dbSession: AsyncSession) -> dict[str, dict[str, str]]:
    """播种真实用户与角色 + 签发 JWT ⇒ 返回 ``{用户名: Bearer 头}``。

    决策接口要求**已校验身份** ⇒ 每个要发起决策的角色都必须拿到真 JWT。
    """

    managerRole = Role(code=_ROLE_PURCHASE_MANAGER, name="采购经理")
    internRole = Role(code=_ROLE_INTERN, name="实习生")
    dbSession.add_all([managerRole, internRole])
    await dbSession.commit()

    submitter = User(username="submitter", display_name="提交人", enabled=True)
    lead = User(username="lead", display_name="审批主管", enabled=True)
    intern = User(username="intern", display_name="实习生", enabled=True)
    forged = User(username="forged", display_name="无角色用户", enabled=True)
    dbSession.add_all([submitter, lead, intern, forged])
    await dbSession.commit()

    dbSession.add_all(
        [
            UserRole(user_id=lead.id, role_id=managerRole.id),
            UserRole(user_id=intern.id, role_id=internRole.id),
            # 提交人也带审批角色 ⇒ 「自审批被拒」这条用例能走到真正的四眼判定
            UserRole(user_id=submitter.id, role_id=managerRole.id),
        ]
    )
    await dbSession.commit()

    headers: dict[str, dict[str, str]] = {}
    for user in (submitter, lead, intern, forged):
        token = await _auth_support.issueToken(dbSession, user)
        headers[user.username] = _auth_support.bearerHeaders(token)
    return headers


async def _submit(ac: AsyncClient, *, idempotencyKey: str = "api-key-1") -> int:
    resp = await ac.post(
        "/api/v1/workflow/instances",
        json={
            "domainCode": "procurement",
            "capabilityCode": "purchase_order_approve",
            "version": "v1",
            "idempotencyKey": idempotencyKey,
            "request": {"amount": 100},
        },
        headers=_headers("submitter"),
    )
    # 201 = 本次新建；200 = 幂等重放
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _requestApproval(ac: AsyncClient, instanceId: int) -> list[dict]:
    resp = await ac.post(
        f"/api/v1/workflow/instances/{instanceId}/approval-request",
        json={},
        headers=_headers("submitter"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# =============================================================================
# 端到端：七守卫
# =============================================================================


async def test_提交后发起审批产生任务(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    tasks = await _requestApproval(workflowClient, instanceId)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "PENDING"
    assert tasks[0]["matrixCode"] == "po_medium_manager"

    rows = (
        (
            await dbSession.execute(
                select(ApprovalTask).where(ApprovalTask.instance_id == instanceId)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_越权审批返回403(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=actors["intern"],
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "APPROVAL_NOT_AUTHORIZED"
    # 拒绝即零副作用：任务仍是 PENDING
    detail = await workflowClient.get(
        f"/api/v1/workflow/instances/{instanceId}/approvals",
        headers=_headers("submitter"),
    )
    assert detail.json()[0]["status"] == "PENDING"


async def test_提交人不能自审批(workflowClient, dbSession):
    """四眼原则：提交人即使角色对也不能批自己的单。"""

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=actors["submitter"],
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "APPROVAL_SELF_FORBIDDEN"


async def test_重复审批返回409(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    first = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=actors["lead"],
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "APPROVED"

    second = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=actors["lead"],
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == "APPROVAL_ALREADY_DECIDED"


async def test_超时审批返回409(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    # 把截止时间推到过去（模拟超时）
    task = (
        await dbSession.execute(
            select(ApprovalTask).where(ApprovalTask.id == tasks[0]["id"])
        )
    ).scalar_one()
    task.due_at = task.due_at.replace(year=task.due_at.year - 1)
    await dbSession.commit()

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=actors["lead"],
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "APPROVAL_TIMEOUT"

    # 超时落库：任务 TIMEOUT、实例 FAILED（不死等）
    # ⚠️ 本测试的 session 是**另一个连接** ⇒ 必须 populate_existing 重读，
    # 否则拿到 identity map 里的旧对象（status 仍是 PENDING）。
    refreshed = (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == tasks[0]["id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "TIMEOUT"


async def test_审批通过后实例变APPROVED(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE", "note": "同意"},
        headers=actors["lead"],
    )
    assert resp.status_code == 200, resp.text

    detail = await workflowClient.get(
        f"/api/v1/workflow/instances/{instanceId}",
        headers=_headers("submitter"),
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["instance"]["status"] == "APPROVED"


async def test_重复提交同一幂等键返回同一实例(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    first = await _submit(workflowClient)
    second = await _submit(workflowClient)
    assert first == second


async def test_幂等重放返回200而非201(workflowClient, dbSession):
    """重放是既有实例 ⇒ 200（不是 201「新建」）。"""

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    await _submit(workflowClient)

    resp = await workflowClient.post(
        "/api/v1/workflow/instances",
        json={
            "domainCode": "procurement",
            "capabilityCode": "purchase_order_approve",
            "version": "v1",
            "idempotencyKey": "api-key-1",
            "request": {"amount": 100},
        },
        headers=_headers("submitter"),
    )
    assert resp.status_code == 200, resp.text


async def test_矩阵未覆盖返回409(workflowClient, dbSession):
    """定义的风险级别没有矩阵条目 ⇒ 失败关闭（409）。

    ⚠️ 定义里**必须有可执行步骤**（否则提交面就 422 了）：这里刻意用**不带审批门**
    的步骤 ⇒ 提交能成功（201），409 落在**显式审批请求**这个接口上 ⇒ 断的正是
    「矩阵未覆盖 ⇒ 拒绝发起审批」这条纪律。
    """

    await _seedActors(dbSession)
    definition = WorkflowDefinition(
        domain_code="procurement",
        capability_code="purchase_order_approve",
        version="v9",
        definition_json={
            "riskLevel": "HIGH",
            "steps": [{"code": "collect", "order": 0, "commandType": "po.collect"}],
            "approvalMatrix": _MATRIX,
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(definition)
    await dbSession.commit()

    resp = await workflowClient.post(
        "/api/v1/workflow/instances",
        json={
            "domainCode": "procurement",
            "capabilityCode": "purchase_order_approve",
            "version": "v9",
            "idempotencyKey": "api-key-high",
            "request": {},
        },
        headers=_headers("submitter"),
    )
    assert resp.status_code == 201, resp.text
    instanceId = resp.json()["id"]

    resp = await workflowClient.post(
        f"/api/v1/workflow/instances/{instanceId}/approval-request",
        json={},
        headers=_headers("submitter"),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "APPROVAL_MATRIX_NOT_MATCHED"


# =============================================================================
# 边界：风险级别服务端绑定 / 真鉴权 / 对象级授权 / 载荷上限
# =============================================================================


async def test_客户端自传风险级别被拒422(workflowClient, dbSession):
    """风险级别由服务端绑定 ⇒ 客户端多传字段被拒（不是静默忽略）。"""

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await workflowClient.post(
        f"/api/v1/workflow/instances/{instanceId}/approval-request",
        json={"riskLevel": "LOW"},
        headers=_headers("submitter"),
    )
    assert resp.status_code == 422, resp.text


async def test_stub身份不能审批(workflowClient, dbSession):
    """非 DB 用户（stub 身份）⇒ 403（决策面要求已校验身份）。"""

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=_headers("ghost", _ROLE_PURCHASE_MANAGER),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "APPROVAL_REAL_AUTH_REQUIRED"


async def test_用请求头冒充DB用户不能审批(workflowClient, dbSession):
    """**冒充**：请求头写着审批人的名字 ⇒ 403（决策面要求已校验身份）。

    回归点：旧实现只查 ``dbUserId is not None`` ⇒ stub 头命中 DB 用户即放行 ⇒
    **任意客户端**发 ``X-User-Id: lead`` 就能以 lead 的身份审批 ⇒ 四眼原则与
    「谁批的」审计同时失真。
    ⇒ 决策必须验签（``jti`` 非空 ⇒ 服务端验过 JWT）。
    """

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers=_headers("lead"),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "APPROVAL_REAL_AUTH_REQUIRED"
    # 拒绝即零副作用：任务仍是 PENDING
    row = (
        await dbSession.execute(
            select(ApprovalTask)
            .where(ApprovalTask.id == tasks[0]["id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "PENDING"


async def test_伪造角色头不能越权(workflowClient, dbSession):
    """DB 用户的角色来自 DB ⇒ 请求头里的 X-User-Roles 无效。

    身份已校验（真 JWT）⇒ 能走到角色判定 ⇒ 但角色**来自 DB**（``forged``
    没有采购经理角色）⇒ 请求头自称 ``purchase_manager`` 仍然被拒。
    """

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)
    tasks = await _requestApproval(workflowClient, instanceId)

    resp = await workflowClient.post(
        f"/api/v1/workflow/approvals/{tasks[0]['id']}/decision",
        json={"decision": "APPROVE"},
        headers={**actors["forged"], "X-User-Roles": _ROLE_PURCHASE_MANAGER},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "APPROVAL_NOT_AUTHORIZED"


async def test_无关第三方读实例返回404(workflowClient, dbSession):
    """对象级授权：无关者看不到实例（无存在性预言机）。"""

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await workflowClient.get(
        f"/api/v1/workflow/instances/{instanceId}",
        headers=_headers("intern"),
    )
    assert resp.status_code == 404, resp.text

    resp = await workflowClient.get(
        f"/api/v1/workflow/instances/{instanceId}/approvals",
        headers=_headers("intern"),
    )
    assert resp.status_code == 404, resp.text


async def test_请求体超限被拒422(workflowClient, dbSession):
    """载荷体积闸：超限在边界被拒。"""

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)

    resp = await workflowClient.post(
        "/api/v1/workflow/instances",
        json={
            "domainCode": "procurement",
            "capabilityCode": "purchase_order_approve",
            "version": "v1",
            "idempotencyKey": "api-key-big",
            "request": {"blob": "x" * (70 * 1024)},
        },
        headers=_headers("submitter"),
    )
    assert resp.status_code == 422, resp.text


async def test_开关关闭时路由不存在(offClient, dbSession):
    """默认关闭 ⇒ 整组路由不注册 ⇒ 404（对外完全不存在）。"""

    resp = await offClient.post(
        "/api/v1/workflow/instances",
        json={
            "domainCode": "procurement",
            "capabilityCode": "purchase_order_approve",
            "version": "v1",
            "idempotencyKey": "api-key-off",
            "request": {},
        },
        headers=_headers("submitter"),
    )
    assert resp.status_code == 404, resp.text
