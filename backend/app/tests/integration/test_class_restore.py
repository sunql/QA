"""本体类恢复（restore）集成测试（fix-class-tombstone-restore）。

覆盖：
  - 软删 → restore 后 valid_to IS NULL；audit 写入 action="RESTORE"
  - 已活类 → 抛 MSG_CLASS_NOT_EXPIRED（HTTP 400）
  - 不存在的 id → 404
  - 非 owner 非 admin → 403
  - DELETE → restore → DELETE 循环可来回
  - Neo4j 不可用时 restore 仍成功（best-effort，与 deleteClass 同语义）

走真实 PostgreSQL（pytest fixture 由 conftest 提供）；Neo4j/Milvus 在本目录
conftest 已 mock，不依赖外部服务。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


ADMIN_HEADERS = {
    "X-User-Id": "test-admin",
    "X-User-Roles": "admin",
}

# owner 派生：服务端从 departments[0] 取（createClass 的 object_owner 派生逻辑）
OWNER_DEPT = "procurement"
OWNER_HEADERS = {
    "X-User-Id": "test-owner",
    "X-User-Roles": "user",
    "X-User-Departments": OWNER_DEPT,
}
NON_OWNER_HEADERS = {
    "X-User-Id": "test-stranger",
    "X-User-Roles": "user",
    "X-User-Departments": "quality",  # 部门不匹配
}


async def test_restore_makes_valid_to_null(client: AsyncClient) -> None:
    """软删类被 restore → valid_to 回到 NULL；audit 多一行 RESTORE。"""
    # 1) 创建（owner 派生）
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "WAREHOUSE_X", "sourceTable": "t_warehouse_x"},
        headers=OWNER_HEADERS,
    )
    assert create.status_code == 201, create.text
    cid = create.json()["id"]

    # 2) 软删（admin 也行；ACL 通过）
    delete = await client.delete(
        f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS
    )
    assert delete.status_code == 204
    # 软删后 GET 仍可见，validTo 非空
    afterDel = await client.get(f"/api/v1/ontology/classes/{cid}")
    assert afterDel.json()["validTo"] is not None

    # 3) restore（owner）
    restore = await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )
    assert restore.status_code == 204, restore.text

    # 4) 验证：validTo 回到 NULL
    afterRestore = await client.get(f"/api/v1/ontology/classes/{cid}")
    assert afterRestore.json()["validTo"] is None
    # 默认列表里再次出现
    listed = await client.get("/api/v1/ontology/classes")
    assert any(c["id"] == cid for c in listed.json())


async def test_restore_creates_audit_record(client: AsyncClient) -> None:
    """restore 必须在 audit_history 留痕（action="RESTORE"，actor=userId）。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "AUDIT_RESTORE_X", "sourceTable": "t_audit_restore_x"},
        headers=OWNER_HEADERS,
    )
    cid = create.json()["id"]
    await client.delete(f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS)
    await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )

    # 审计查询：全局 audit API 按实体查
    audit = await client.get(
        f"/api/v1/audit/by-entity/ONTOLOGY_CLASS/{cid}",
        headers=ADMIN_HEADERS,
    )
    assert audit.status_code == 200
    actions = [r["action"] for r in audit.json()]
    assert "RESTORE" in actions
    restoreRow = next(r for r in audit.json() if r["action"] == "RESTORE")
    assert restoreRow["actor"] == "test-owner"


async def test_restore_active_class_returns_422(client: AsyncClient) -> None:
    """已活类 restore → 422（MSG_CLASS_NOT_EXPIRED，ValidationError 映射）。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "ACTIVE_X", "sourceTable": "t_active_x"},
        headers=OWNER_HEADERS,
    )
    cid = create.json()["id"]
    restore = await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )
    assert restore.status_code == 422
    assert "未软删除" in restore.json()["error"]


async def test_restore_nonexistent_class_returns_404(client: AsyncClient) -> None:
    """不存在的 id → 404（路由级 404，不是 service 异常）。"""
    restore = await client.post(
        "/api/v1/ontology/classes/999999/restore", headers=ADMIN_HEADERS
    )
    assert restore.status_code == 404


async def test_restore_non_owner_returns_403(client: AsyncClient) -> None:
    """非 owner 非 admin → 403（AclService.assertCanModify 守卫）。

    ACL 通用 403 消息不暴露 owner / 当前用户部门等内部状态
    （acl_service.py:43-51，防枚举侧信道）。
    """
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "ACL_GUARD_X", "sourceTable": "t_acl_guard_x"},
        headers=OWNER_HEADERS,
    )
    cid = create.json()["id"]
    await client.delete(f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS)

    # 部门不匹配的非 owner 用户
    restore = await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=NON_OWNER_HEADERS
    )
    assert restore.status_code == 403
    # 403 消息不能含部门名 / 实体名（侧信道）
    detail = str(restore.json().get("detail", ""))
    assert OWNER_DEPT not in detail
    assert "test-owner" not in detail


async def test_delete_restore_delete_cycle(client: AsyncClient) -> None:
    """活 → 死 → 活 → 死 可循环，每次 audit 都留痕。"""
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "CYCLE_X", "sourceTable": "t_cycle_x"},
        headers=OWNER_HEADERS,
    )
    cid = create.json()["id"]

    # 第一次死
    await client.delete(f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS)
    # 活
    await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )
    # 第二次死
    await client.delete(f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS)
    # 再活
    await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )

    final = await client.get(f"/api/v1/ontology/classes/{cid}")
    assert final.json()["validTo"] is None

    audit = await client.get(
        f"/api/v1/audit/by-entity/ONTOLOGY_CLASS/{cid}", headers=ADMIN_HEADERS
    )
    # listByEntity 按 created_at DESC 排序：actions[0] 是最新一条。
    actions = [r["action"] for r in audit.json()]
    # 序列：CREATE → DELETE → RESTORE → DELETE → RESTORE
    assert actions[0] == "RESTORE"
    # 至少 2 个 RESTORE + 2 个 DELETE + 1 个 CREATE
    assert actions.count("RESTORE") == 2
    assert actions.count("DELETE") == 2
    assert actions.count("CREATE") == 1

async def test_restore_backfills_missing_id_mapping(
    client: AsyncClient, dbSession, neo4jCleanDriver
) -> None:
    """老类（id_mapping 行缺失）restore 时必须补注册，否则 Neo4j 无节点、
    且 updateClass 会因 resolveByExternal 拿不到行而抛 RuntimeError。

    真实生产背景：id_mapping 特性晚于部分老类落库，回填只覆盖活类；
    墓碑类没有行（见 2026-10-03 DWD_BUSINESS_PARTNER id=9 / DWD_CUSTOMER id=11）。
    """
    from sqlalchemy import text

    # 1) 建类（createClass 会写 id_mapping）
    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "LEGACY_X", "sourceTable": "t_legacy_x"},
        headers=OWNER_HEADERS,
    )
    assert create.status_code == 201, create.text
    cid = create.json()["id"]
    unified_id = f"obj:CLASS:{cid}"

    # 2) 手工删掉 id_mapping 行 —— 模拟 id_mapping 特性之前落库的老类
    await dbSession.execute(
        text("DELETE FROM id_mapping WHERE business_object='CLASS' AND external_id=:e"),
        {"e": str(cid)},
    )
    await dbSession.commit()
    gone = await dbSession.execute(
        text("SELECT 1 FROM id_mapping WHERE business_object='CLASS' AND external_id=:e"),
        {"e": str(cid)},
    )
    assert gone.first() is None, "前置：id_mapping 行应已删除"

    # 3) 软删 → 复活
    assert (
        await client.delete(f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS)
    ).status_code == 204
    restore = await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )
    assert restore.status_code == 204, restore.text

    # 4) id_mapping 行被补回
    back = await dbSession.execute(
        text("SELECT unified_id FROM id_mapping WHERE business_object='CLASS' AND external_id=:e"),
        {"e": str(cid)},
    )
    row = back.first()
    assert row is not None, "restore 后 id_mapping 行必须存在"
    assert row[0] == unified_id

    # 5) Neo4j 节点真的建了（不只是 PG 行）
    with neo4jCleanDriver.session() as ns:
        found = ns.run(
            "MATCH (c:Class {unified_id: $uid}) RETURN c.name AS name",
            uid=unified_id,
        ).single()
    assert found is not None, "restore 后 Neo4j 应有 Class 节点"
    assert found["name"] == "LEGACY_X"


async def test_restore_legacy_class_then_update_works(
    client: AsyncClient, dbSession, neo4jCleanDriver
) -> None:
    """端到端：老类 restore 后编辑，Neo4j 属性也必须真的更新。

    ⚠️ 只断言 HTTP 200 是**假绿** —— updateClass 把
    `RuntimeError("id_mapping not found")` 吞进 `_logNeo4jFailure`（best-effort），
    请求照样 200。必须断言 Neo4j 侧的 alias 变化才是真证据。
    """
    from sqlalchemy import text

    create = await client.post(
        "/api/v1/ontology/classes",
        json={"className": "LEGACY_EDIT_X", "sourceTable": "t_legacy_edit_x"},
        headers=OWNER_HEADERS,
    )
    cid = create.json()["id"]
    unified_id = f"obj:CLASS:{cid}"
    await dbSession.execute(
        text("DELETE FROM id_mapping WHERE business_object='CLASS' AND external_id=:e"),
        {"e": str(cid)},
    )
    await dbSession.commit()
    await client.delete(f"/api/v1/ontology/classes/{cid}", headers=ADMIN_HEADERS)
    await client.post(
        f"/api/v1/ontology/classes/{cid}/restore", headers=OWNER_HEADERS
    )

    # 编辑：updateClass 内部 resolveByExternal 必须命中
    upd = await client.put(
        f"/api/v1/ontology/classes/{cid}",
        json={"classAlias": "老类别名"},
        headers=OWNER_HEADERS,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["classAlias"] == "老类别名"

    # 真证据：Neo4j 节点存在且 alias 已更新
    with neo4jCleanDriver.session() as ns:
        rec = ns.run(
            "MATCH (c:Class {unified_id: $uid}) RETURN c.alias AS alias",
            uid=unified_id,
        ).single()
    assert rec is not None, "restore 后 Neo4j 应有 Class 节点"
    assert rec["alias"] == "老类别名", "Neo4j alias 必须随 update 同步"
