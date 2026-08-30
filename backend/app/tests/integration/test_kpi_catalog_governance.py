"""Phase 4.5 Governance Hardening 集成测试（真实 PG 5433 + 完整 API 链路）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整 API 链路。
覆盖：
1. ACL：admin 通过 / owner 部门通过 / 其他部门拒绝 / 空 owner 拒绝非 admin
2. 审计：CREATE/UPDATE/DELETE 都写入 audit_log，含 actor + before/after
3. 历史：每次变更完整快照写入 kpi_catalog_history；删 KPI 后 kpi_id=NULL 但 snapshot 仍可查
4. 事务绑定：审计/历史与业务同事务（rollback 一致性）

注：HTTP/1.1 + Starlette header 默认 latin-1；测试用 ASCII department tokens
（如 'procurement'）替代中文，与生产 token-based ACL 模式一致。
"""

from __future__ import annotations

from sqlalchemy import delete, select

from app.domain.models import AuditLog, KpiCatalog, KpiCatalogHistory


# Department tokens（ASCII；HTTP header 安全）。owner 字段存什么 ACL 就比什么。
PROCUREMENT = "procurement"
FINANCE = "finance"
QUALITY = "quality"


class _Helper:
    """每个测试的 Arrange/Assert 公共助手。"""

    @staticmethod
    async def clean(dbSession) -> None:
        """按 kpi_code 前缀删除，避免影响其他测试（scope = 本测试用前缀 KPI_GOV_）。"""
        await dbSession.execute(
            delete(KpiCatalog).where(KpiCatalog.kpi_code.like("KPI_GOV_%"))
        )
        await dbSession.execute(
            delete(AuditLog).where(AuditLog.entity_type == "kpi_catalog")
        )
        await dbSession.execute(delete(KpiCatalogHistory))
        await dbSession.commit()


class TestKpiAcl:
    """PUT/DELETE 走 AclService：admin 通过；owner 部门通过；其他部门 403。"""

    async def _create(self, client, code: str, owner: str) -> dict:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": code, "kpiName": f"测试-{code}", "owner": owner},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_create_does_not_require_acl(self, client, dbSession) -> None:
        """CREATE 不走 ACL；任何部门都能创建新 KPI。"""
        await _Helper.clean(dbSession)
        # 任何部门（甚至无部门）都能 POST
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_A", "kpiName": "A", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 201

    async def test_update_owner_dept_passes(self, client, dbSession) -> None:
        """owner=procurement + user=procurement → 200。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, "KPI_GOV_B", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "改后"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        assert resp.status_code == 200
        assert resp.json()["kpiName"] == "改后"

    async def test_update_other_dept_returns_403(self, client, dbSession) -> None:
        """owner=procurement + user=finance → 403。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, "KPI_GOV_C", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "改后"},
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "无权修改" in body.get("error", "")

    async def test_update_admin_any_owner_passes(self, client, dbSession) -> None:
        """admin 角色可改任意 owner。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, "KPI_GOV_D", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200

    async def test_delete_other_dept_returns_403(self, client, dbSession) -> None:
        """DELETE 其他部门 → 403。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, "KPI_GOV_E", PROCUREMENT)
        resp = await client.delete(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert resp.status_code == 403

    async def test_empty_owner_only_admin_can_modify(self, client, dbSession) -> None:
        """owner 为空 + 非 admin → 403；admin → 200。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, "KPI_GOV_F", "")
        # 非 admin + 有部门 → 403
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "改"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        assert resp.status_code == 403
        # admin → 200
        resp2 = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp2.status_code == 200

    async def test_multi_department_user_can_match_any(self, client, dbSession) -> None:
        """多部门成员：任一匹配即通过。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, "KPI_GOV_G", FINANCE)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "双岗改"},
            headers={
                "X-User-Id": "carol",
                "X-User-Departments": f"{PROCUREMENT},{FINANCE}",
            },
        )
        assert resp.status_code == 200


class TestKpiAudit:
    """audit_log 写入：CREATE / UPDATE / DELETE 都有记录 + actor 正确。"""

    async def test_create_writes_audit_create(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_H", "kpiName": "H", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]

        rows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "kpi_catalog",
                    AuditLog.entity_id == kpiId,
                    AuditLog.action == "CREATE",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor == "alice"
        assert rows[0].actor_departments == PROCUREMENT
        assert rows[0].after_json is not None
        assert rows[0].before_json is None
        assert rows[0].after_json["kpi_code"] == "KPI_GOV_H"

    async def test_update_writes_audit_update_with_before_after(
        self, client, dbSession
    ) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_I", "kpiName": "原名", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]
        await client.put(
            f"/api/v1/kpi-catalog/{kpiId}",
            json={"kpiName": "新名"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )

        rows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "kpi_catalog",
                    AuditLog.entity_id == kpiId,
                    AuditLog.action == "UPDATE",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].before_json["kpi_name"] == "原名"
        assert rows[0].after_json["kpi_name"] == "新名"

    async def test_delete_writes_audit_delete(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_J", "kpiName": "J", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]
        await client.delete(
            f"/api/v1/kpi-catalog/{kpiId}",
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )

        rows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "kpi_catalog",
                    AuditLog.entity_id == kpiId,
                    AuditLog.action == "DELETE",
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].before_json["kpi_code"] == "KPI_GOV_J"
        assert rows[0].after_json is None

    async def test_audit_actor_departments_comma_joined(
        self, client, dbSession
    ) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_K", "kpiName": "K", "owner": PROCUREMENT},
            headers={
                "X-User-Id": "carol",
                "X-User-Departments": f"{PROCUREMENT},{FINANCE}",
            },
        )
        kpiId = resp.json()["id"]
        rows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "kpi_catalog",
                    AuditLog.entity_id == kpiId,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_departments == f"{PROCUREMENT},{FINANCE}"


class TestKpiHistory:
    """kpi_catalog_history 写入：每次变更完整快照 + revision 自增 + FK ON DELETE SET NULL。"""

    async def test_create_writes_history_revision_zero(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": "KPI_GOV_L",
                "kpiName": "L",
                "owner": PROCUREMENT,
                "formula": "F0",
            },
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]
        rows = (
            await dbSession.execute(
                select(KpiCatalogHistory).where(KpiCatalogHistory.kpi_id == kpiId)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].revision == 0
        assert rows[0].snapshot_json["kpi_code"] == "KPI_GOV_L"
        assert rows[0].snapshot_json["formula"] == "F0"
        assert rows[0].changed_by == "alice"

    async def test_revision_increments_on_each_update(self, client, dbSession) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_M", "kpiName": "M", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]
        for newName in ["M1", "M2", "M3"]:
            await client.put(
                f"/api/v1/kpi-catalog/{kpiId}",
                json={"kpiName": newName},
                headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
            )

        rows = (
            await dbSession.execute(
                select(KpiCatalogHistory)
                .where(KpiCatalogHistory.kpi_id == kpiId)
                .order_by(KpiCatalogHistory.revision)
            )
        ).scalars().all()
        revisions = [r.revision for r in rows]
        assert revisions == [0, 1, 2, 3]

    async def test_history_snapshot_preserves_old_value_on_update(
        self, client, dbSession
    ) -> None:
        """update 写入的 snapshot 是「更新后」值（与 audit_log.before 互补）。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "kpiCode": "KPI_GOV_N",
                "kpiName": "N-原",
                "formula": "OLD",
                "owner": PROCUREMENT,
            },
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]
        await client.put(
            f"/api/v1/kpi-catalog/{kpiId}",
            json={"kpiName": "N-新", "formula": "NEW"},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )

        # revision=0 是创建时（name="N-原" formula="OLD"）
        rev0 = (
            await dbSession.execute(
                select(KpiCatalogHistory).where(
                    KpiCatalogHistory.kpi_id == kpiId,
                    KpiCatalogHistory.revision == 0,
                )
            )
        ).scalar_one()
        assert rev0.snapshot_json["kpi_name"] == "N-原"
        assert rev0.snapshot_json["formula"] == "OLD"

        # revision=1 是更新后（name="N-新" formula="NEW"）
        rev1 = (
            await dbSession.execute(
                select(KpiCatalogHistory).where(
                    KpiCatalogHistory.kpi_id == kpiId,
                    KpiCatalogHistory.revision == 1,
                )
            )
        ).scalar_one()
        assert rev1.snapshot_json["kpi_name"] == "N-新"
        assert rev1.snapshot_json["formula"] == "NEW"

    async def test_delete_keeps_history_with_null_kpi_id(
        self, client, dbSession
    ) -> None:
        """DELETE KPI → history.kpi_id 变 NULL（FK ON DELETE SET NULL）；snapshot 仍可查。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_O", "kpiName": "O", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]
        await client.delete(
            f"/api/v1/kpi-catalog/{kpiId}",
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )

        # KPI 已删 → history.kpi_id 应为 NULL，但 snapshot_json 仍含 kpi_code
        rows = (
            await dbSession.execute(
                select(KpiCatalogHistory).where(
                    KpiCatalogHistory.snapshot_json["kpi_code"].astext == "KPI_GOV_O"
                )
            )
        ).scalars().all()
        assert len(rows) >= 1
        assert all(r.kpi_id is None for r in rows)


class TestKpiAclRollbackOnFailure:
    """事务绑定：审计/历史 与 业务 共享事务（任一失败 → 全部回滚）。"""

    async def test_acl_denied_does_not_write_audit_or_history(
        self, client, dbSession
    ) -> None:
        """ACL 拒绝 → 不应产生 audit_log 与 history 行（service 早于 audit 抛错）。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={"kpiCode": "KPI_GOV_P", "kpiName": "P", "owner": PROCUREMENT},
            headers={"X-User-Id": "alice", "X-User-Departments": PROCUREMENT},
        )
        kpiId = resp.json()["id"]

        # bob（非 owner 部门、非 admin）尝试 UPDATE → 应被 ACL 拒绝
        putResp = await client.put(
            f"/api/v1/kpi-catalog/{kpiId}",
            json={"kpiName": "应被拒"},
            headers={"X-User-Id": "bob", "X-User-Departments": FINANCE},
        )
        assert putResp.status_code == 403

        # audit_log 应只有 CREATE 那一条；不应有 UPDATE
        rows = (
            await dbSession.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "kpi_catalog",
                    AuditLog.entity_id == kpiId,
                    AuditLog.action == "UPDATE",
                )
            )
        ).scalars().all()
        assert len(rows) == 0

        # history 应只有 revision=0 一条；不应有 revision=1
        rows = (
            await dbSession.execute(
                select(KpiCatalogHistory).where(KpiCatalogHistory.kpi_id == kpiId)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].revision == 0
