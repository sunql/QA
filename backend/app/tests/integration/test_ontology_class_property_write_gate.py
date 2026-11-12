"""本体类 / 属性写入口 admin 门禁（真实 PG + HTTP 链路）。

背景（P3 终审 I1）：``kpi_alignment_resolver`` 把 ``ontology_property`` 行当作
治理 token → 物理列的映射权威，发布闸门与契约注入都消费解析结果。此前类/属性
写路由仅要求登录态（``Depends(getCurrentUser)``），任意登录用户可伪造映射，
让治理 ⇄ 技术分歧被判 pass 并获 Tier A 资格。

修复：类与属性的 create/update/delete 全部收 admin（与同文件
``POST/PUT/DELETE /ontology/metrics`` 复用 ``requireAdmin`` 依赖）。
读接口（GET 列表 / 详情）保持所有登录用户可用。
"""

from __future__ import annotations

from httpx import AsyncClient

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "normal-user", "X-User-Roles": "user"}


async def _createClass(
    client: AsyncClient, name: str, *, ownerDepartment: str | None = None
) -> int:
    """admin 建类；可带部门 ⇒ 服务端派生 object_owner（让 owner ACL 本会放行）。"""
    headers = dict(ADMIN_HEADERS)
    if ownerDepartment is not None:
        headers["X-User-Departments"] = ownerDepartment
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": name, "sourceTable": f"t_{name.lower()}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _userHeaders(ownerDepartment: str | None = None) -> dict[str, str]:
    headers = dict(USER_HEADERS)
    if ownerDepartment is not None:
        headers["X-User-Departments"] = ownerDepartment
    return headers


async def _createProperty(client: AsyncClient, classId: int, name: str) -> int:
    resp = await client.post(
        "/api/v1/ontology/properties",
        json={"classId": classId, "propertyName": name, "dataType": "STRING"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


# ── 类：全部写路由非 admin ⇒ 403 ──────────────────────────────────────


async def test_non_admin_cannot_create_class(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "DENIED_CLASS", "sourceTable": "t_denied_class"},
        headers=USER_HEADERS,
    )

    assert resp.status_code == 403, resp.text


async def test_non_admin_cannot_update_class(client: AsyncClient) -> None:
    """同部门普通用户：owner ACL 本会放行 ⇒ 必须是 admin 门禁拦下。"""
    classId = await _createClass(client, "UPDATE_DENIED_CLASS", ownerDepartment="procurement")

    resp = await client.put(
        f"/api/v1/ontology/classes/{classId}",
        json={"description": "非 admin 篡改"},
        headers=_userHeaders("procurement"),
    )

    assert resp.status_code == 403, resp.text


async def test_non_admin_cannot_delete_class(client: AsyncClient) -> None:
    """同部门普通用户：owner ACL 本会放行 ⇒ 必须是 admin 门禁拦下。"""
    classId = await _createClass(client, "DELETE_DENIED_CLASS", ownerDepartment="procurement")

    resp = await client.delete(
        f"/api/v1/ontology/classes/{classId}",
        headers=_userHeaders("procurement"),
    )

    assert resp.status_code == 403, resp.text


# ── 属性：全部写路由非 admin ⇒ 403 ────────────────────────────────────


async def test_non_admin_cannot_create_property(client: AsyncClient) -> None:
    classId = await _createClass(client, "PROP_CREATE_DENIED_CLASS")

    resp = await client.post(
        "/api/v1/ontology/properties",
        json={
            "classId": classId,
            "propertyName": "FORGED_ALIAS",
            "dataType": "STRING",
        },
        headers=USER_HEADERS,
    )

    assert resp.status_code == 403, resp.text


async def test_non_admin_cannot_update_property(client: AsyncClient) -> None:
    classId = await _createClass(client, "PROP_UPDATE_DENIED_CLASS")
    propId = await _createProperty(client, classId, "QTY_COLUMN")

    resp = await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"propertyAlias": "伪造别名"},
        headers=USER_HEADERS,
    )

    assert resp.status_code == 403, resp.text


async def test_non_admin_cannot_delete_property(client: AsyncClient) -> None:
    classId = await _createClass(client, "PROP_DELETE_DENIED_CLASS")
    propId = await _createProperty(client, classId, "QTY_COLUMN")

    resp = await client.delete(
        f"/api/v1/ontology/properties/{propId}", headers=USER_HEADERS
    )

    assert resp.status_code == 403, resp.text


# ── admin 仍可写；非 admin 仍可读 ─────────────────────────────────────


async def test_admin_can_write_classes_and_properties(client: AsyncClient) -> None:
    classId = await _createClass(client, "ADMIN_WRITE_CLASS")
    propId = await _createProperty(client, classId, "ADMIN_WRITE_PROP")

    updatedClass = await client.put(
        f"/api/v1/ontology/classes/{classId}",
        json={"description": "admin 更新"},
        headers=ADMIN_HEADERS,
    )
    updatedProp = await client.put(
        f"/api/v1/ontology/properties/{propId}",
        json={"description": "admin 更新属性"},
        headers=ADMIN_HEADERS,
    )
    deletedProp = await client.delete(
        f"/api/v1/ontology/properties/{propId}", headers=ADMIN_HEADERS
    )
    deletedClass = await client.delete(
        f"/api/v1/ontology/classes/{classId}", headers=ADMIN_HEADERS
    )

    assert updatedClass.status_code == 200, updatedClass.text
    assert updatedProp.status_code == 200, updatedProp.text
    assert deletedProp.status_code == 204, deletedProp.text
    assert deletedClass.status_code == 204, deletedClass.text


async def test_non_admin_can_read_classes_and_properties(
    client: AsyncClient,
) -> None:
    classId = await _createClass(client, "READ_ONLY_CLASS")
    propId = await _createProperty(client, classId, "READ_ONLY_PROP")

    classes = await client.get("/api/v1/ontology/classes", headers=USER_HEADERS)
    propList = await client.get(
        f"/api/v1/ontology/classes/{classId}/properties", headers=USER_HEADERS
    )
    propDetail = await client.get(
        f"/api/v1/ontology/properties/{propId}", headers=USER_HEADERS
    )

    assert classes.status_code == 200
    assert propList.status_code == 200
    assert propDetail.status_code == 200
