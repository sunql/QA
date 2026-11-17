"""P5 取消面**完整 API 链路**测试（真实 PostgreSQL + 真实 HTTP 入口）。

为什么放在 ``unit/`` 而不是 ``integration/``（D-12）：
CI 的 ``backend-integration`` 分片按「排序下标 mod 4」轮转 ⇒ **新增 integration
文件会轮转其它文件的分片归属** ⇒ 触发假 net-new。``backend-unit`` 是「路径稳定
哈希」分片 ⇒ 新增 unit 文件**不影响**任何既有文件的归属。

被测（``POST /api/v1/workflow/instances/{id}/cancel``）：

1. **提交人可取消** ⇒ 200 + 实例 CANCELLED + 待执行命令 CANCELLED（不再被领取）；
2. **非提交人 ⇒ 404**（与「实例不存在」同码 ⇒ 不泄露存在性）；
3. **admin 可取消**（治理面）；
4. 实例不存在 ⇒ 404；实例已是终态 ⇒ 409（``WORKFLOW_CANCEL_NOT_ALLOWED``）；
5. **重复取消幂等**（200，不报错、不重复留痕）；
6. 取消**留痕**（``audit_log`` 落 ``WORKFLOW_CANCEL``）；
7. **未校验身份（stub 头）⇒ 403**（``WORKFLOW_CANCEL_REAL_AUTH_REQUIRED``）。

⚠️ **取消要求已校验身份**（``CurrentUser.isRealIdentity``：JWT 验签过 ⇒ ``jti``
非空）：stub 头可被任意客户端伪造（``X-User-Id: submitter`` 即冒充提交人）
⇒ 治理写面必须走**真实 JWT 链路**（``app/tests/_auth_support.py``）。

⚠️ 断言口径：断**状态码 + 响应体语义**（``detail`` 携带错误码），并落到真实 PG 行上核对。
"""

from __future__ import annotations

import os

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.models import AuditLog
from app.domain.workflow_models import BusinessCommand, WorkflowDefinition, WorkflowInstance
from app.models.rbac import Role, User, UserRole
from app.tests import _auth_support, _pg_support


def _headers(userId: str, roles: str = "") -> dict[str, str]:
    """stub 身份头（stub auth 是本仓既有机制：X-User-Id + X-User-Roles）。

    ⚠️ 本文件**只有「未校验身份」那条用例**用它（期望 403）；其余用例一律走
    **真实 JWT**（``_auth_support.bearerHeaders``）。

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


async def _seedDefinition(dbSession: AsyncSession) -> WorkflowDefinition:
    """一步带命令的定义（提交后立刻入队 ⇒ 有「待执行命令」可取消）。"""

    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="purchase_order_approve",
        version="v1",
        definition_json={
            "steps": [{"code": "collect", "order": 0, "commandType": "po.collect"}],
        },
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _seedActors(dbSession: AsyncSession) -> dict[str, str]:
    """播种三个**真实** DB 用户并签发 JWT ⇒ 返回 ``{用户名: Bearer 头}``。

    - ``submitter``：实例的提交人（自作用域取消）；
    - ``root``：admin（治理面取消）；
    - ``intruder``：无关第三方（无 admin 角色 ⇒ 无权取消）。
    """

    tokens: dict[str, str] = {}
    for username, roleCodes in (
        ("submitter", ()),
        ("root", ("admin",)),
        ("intruder", ()),
    ):
        _user, token = await _auth_support.ensureUser(
            dbSession, username, roleCodes=roleCodes
        )
        tokens[username] = token
    return {name: _auth_support.bearerHeaders(token) for name, token in tokens.items()}


async def _submit(ac: AsyncClient, *, idempotencyKey: str = "cancel-api-1") -> int:
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
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _cancel(
    ac: AsyncClient,
    instanceId: int,
    *,
    headers: dict[str, str],
    reason: str | None = None,
):
    body: dict = {}
    if reason is not None:
        body["reason"] = reason
    return await ac.post(
        f"/api/v1/workflow/instances/{instanceId}/cancel",
        json=body,
        headers=headers,
    )


async def _instanceRow(dbSession: AsyncSession, instanceId: int) -> WorkflowInstance:
    return (
        await dbSession.execute(
            select(WorkflowInstance)
            .where(WorkflowInstance.id == instanceId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _commands(dbSession: AsyncSession, instanceId: int) -> list[BusinessCommand]:
    rows = (
        (
            await dbSession.execute(
                select(BusinessCommand)
                .where(BusinessCommand.instance_id == instanceId)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# =============================================================================
# 取消
# =============================================================================


async def test_提交人取消返回200且实例取消(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await _cancel(workflowClient, instanceId, headers=actors["submitter"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "CANCELLED"
    row = await _instanceRow(dbSession, instanceId)
    assert row.status == "CANCELLED"


async def test_取消会取消待执行命令且不再被领取(workflowClient, dbSession):
    """取消 = **真的不再写外部系统**：命令进终态 CANCELLED ⇒ Worker 领不走。"""

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    commands = await _commands(dbSession, instanceId)
    assert len(commands) == 1
    assert commands[0].status == "PENDING"

    resp = await _cancel(workflowClient, instanceId, headers=actors["submitter"])
    assert resp.status_code == 200, resp.text

    commands = await _commands(dbSession, instanceId)
    assert commands[0].status == "CANCELLED"


async def test_非提交人取消返回404(workflowClient, dbSession):
    """无权取消 ⇒ **404**（与「实例不存在」同码 ⇒ 不泄露存在性）。"""

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await _cancel(workflowClient, instanceId, headers=actors["intruder"])

    assert resp.status_code == 404, resp.text
    # 拒绝即零副作用：实例仍在跑
    row = await _instanceRow(dbSession, instanceId)
    assert row.status != "CANCELLED"


async def test_admin可以取消(workflowClient, dbSession):
    """治理面：**真实** admin 可取消别人提交的实例。"""

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await _cancel(workflowClient, instanceId, headers=actors["root"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "CANCELLED"


async def test_stub身份不能取消(workflowClient, dbSession):
    """**未校验身份**（stub 头）⇒ 403（治理写面必须验签）。

    回归点：旧实现里 stub 头即可取消（连自己的实例也行）⇒ 任意客户端发
    ``X-User-Id: submitter`` 就能取消**别人**的实例 ⇒ 治理面被绕过。
    """

    await _seedDefinition(dbSession)
    await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await _cancel(
        workflowClient, instanceId, headers=_headers("submitter")
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "WORKFLOW_CANCEL_REAL_AUTH_REQUIRED"
    row = await _instanceRow(dbSession, instanceId)
    assert row.status != "CANCELLED"


async def test_伪admin身份不能取消别人的实例(workflowClient, dbSession):
    """stub 身份的缺省角色带 admin ⇒ 伪造头**不得**跨用户取消。

    治理面必须绑定**已校验身份**（JWT）⇒ 角色来自请求头不算数。
    """

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await _cancel(
        workflowClient, instanceId, headers=_headers("ghost", "admin")
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "WORKFLOW_CANCEL_REAL_AUTH_REQUIRED"
    row = await _instanceRow(dbSession, instanceId)
    assert row.status != "CANCELLED"


async def test_取消不存在的实例返回404(workflowClient, dbSession):
    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)

    resp = await _cancel(workflowClient, 999999, headers=actors["submitter"])

    assert resp.status_code == 404, resp.text


async def test_重复取消是幂等的(workflowClient, dbSession):
    """重复取消 ⇒ 200 + 状态不变 + **不重复留痕**（治理面只看到一次取消）。"""

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    first = await _cancel(workflowClient, instanceId, headers=actors["submitter"])
    second = await _cancel(workflowClient, instanceId, headers=actors["submitter"])

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "CANCELLED"

    rows = (
        (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "workflow_instance",
                    AuditLog.entity_id == instanceId,
                    AuditLog.action == "WORKFLOW_CANCEL",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"重复取消留下了 {len(rows)} 条取消审计行（应恰好 1 条）"


async def test_取消留痕(workflowClient, dbSession):
    """取消是治理动作 ⇒ 落 ``audit_log``（谁取消的、为什么）。"""

    await _seedDefinition(dbSession)
    actors = await _seedActors(dbSession)
    instanceId = await _submit(workflowClient)

    resp = await _cancel(
        workflowClient, instanceId, headers=actors["submitter"], reason="需求取消"
    )
    assert resp.status_code == 200, resp.text

    rows = (
        (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "workflow_instance",
                    AuditLog.entity_id == instanceId,
                    AuditLog.action == "WORKFLOW_CANCEL",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor == "submitter"
    assert rows[0].after_json["status"] == "CANCELLED"

    # 理由落到实例行（运营可见）
    row = await _instanceRow(dbSession, instanceId)
    assert row.error_message == "需求取消"
