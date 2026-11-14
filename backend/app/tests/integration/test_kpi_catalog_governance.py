"""Phase 4.5 Governance Hardening 集成测试（真实 PG 5433 + 完整 API 链路）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整 API 链路。
覆盖：
1. ACL：写入口一律 admin；非 admin（含 owner 部门成员）一律 403
2. 审计：CREATE/UPDATE/DELETE 都写入 audit_log，含 actor + before/after
3. 历史：每次变更完整快照写入 kpi_catalog_history；删 KPI 后 kpi_id=NULL 但 snapshot 仍可查
4. 事务绑定：审计/历史与业务同事务（rollback 一致性）

注：HTTP/1.1 + Starlette header 默认 latin-1；测试用 ASCII department tokens
（如 'procurement'）替代中文，与生产 token-based ACL 模式一致。
"""

from __future__ import annotations

from sqlalchemy import delete, select

from app.domain.models import AuditLog, AuditOutbox, KpiCatalog, KpiCatalogHistory
from app.workers.audit_worker import AuditWorker


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
        await dbSession.execute(delete(AuditOutbox).where(AuditOutbox.entity_type == "kpi_catalog"))
        await dbSession.commit()

    @staticmethod
    async def drainOutbox(dbSession) -> int:
        """跑一轮 worker 消费（feat-audit-outbox：审计异步落地）。

        业务 service 只入队；audit_log / history 由 worker 写入。
        测试里同步跑一轮 drain 模拟 worker 处理完该事件。
        """
        worker = AuditWorker()
        n = await worker.drainOnce(dbSession)
        return n


class TestKpiAcl:
    """写入口一律 admin：create / update / delete；部门归属不再授予写权限。

    2026-11-11（feat-kpi-match-v2 评审 HIGH-1）：KPI 名称与 semantic_keywords 决定
    L1 匹配与 B3 契约注入方向，若部门 owner 可写，就能「先建自己的 KPI 再改名劫持」。
    因此写权限与词条字典 / 本体指标同口径收口到 admin；service 层的 owner ACL
    保留（admin 恒通过），仅用于审计语义。
    """

    async def _create(self, client, seed_data_source, code: str, owner: str) -> dict:
        """用 admin 身份建 KPI（写入口 admin-only）；owner 由业务指定，与请求方无关。"""
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": code,
                "kpiName": f"测试-{code}",
                "owner": owner,
            },
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_create_requires_admin(self, client, dbSession, seed_data_source) -> None:
        """非 admin 不能创建 KPI（部门归属不授予写权限）。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_A",
                "kpiName": "A",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "user",
                "X-User-Departments": PROCUREMENT,
            },
        )
        assert resp.status_code == 403
        # 拒绝原因必须落在 admin 语义上，而不是碰巧的 422/409
        assert "admin" in resp.json().get("detail", "")

    async def test_create_published_requires_admin(
        self, client, dbSession, seed_data_source
    ) -> None:
        """发布态是唯一进入匹配索引的状态，非 admin 不得直接创建。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_PUB",
                "kpiName": "发布态",
                "owner": PROCUREMENT,
                "status": "PUBLISHED",
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "user",
                "X-User-Departments": PROCUREMENT,
            },
        )
        assert resp.status_code == 403

    async def test_admin_can_create_published(self, client, dbSession, seed_data_source) -> None:
        """对照片：同一发布态 payload 换 admin 头 ⇒ 201（证明上面的 403 不是 payload 问题）。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_ADMIN_PUB",
                "kpiName": "管理员发布",
                "owner": PROCUREMENT,
                "status": "PUBLISHED",
            },
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "PUBLISHED"

    async def test_non_admin_can_still_read(self, client, dbSession, seed_data_source) -> None:
        """反向守卫：写入口收口不得误伤读路径。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, seed_data_source, "KPI_GOV_READ", PROCUREMENT)
        userHeaders = {
            "X-User-Id": "alice",
            "X-User-Roles": "user",
            "X-User-Departments": PROCUREMENT,
        }

        listed = await client.get("/api/v1/kpi-catalog", headers=userHeaders)
        assert listed.status_code == 200, listed.text
        assert "KPI_GOV_READ" in {k["kpiCode"] for k in listed.json()}

        fetched = await client.get(f"/api/v1/kpi-catalog/{kpi['id']}", headers=userHeaders)
        assert fetched.status_code == 200, fetched.text

    async def test_update_own_department_returns_403(
        self, client, dbSession, seed_data_source
    ) -> None:
        """owner=procurement + user 也在 procurement → 仍 403（唯 admin 可写）。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, seed_data_source, "KPI_GOV_C", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "改后"},
            headers={
                "X-User-Id": "bob",
                "X-User-Roles": "user",
                "X-User-Departments": PROCUREMENT,
            },
        )
        assert resp.status_code == 403
        assert "admin" in resp.json().get("detail", "")

    async def test_update_admin_any_owner_passes(self, client, dbSession, seed_data_source) -> None:
        """admin 角色可改任意 owner。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, seed_data_source, "KPI_GOV_D", PROCUREMENT)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp.status_code == 200

    async def test_delete_non_admin_returns_403(self, client, dbSession, seed_data_source) -> None:
        """DELETE 非 admin → 403（部门不授予写权限）。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, seed_data_source, "KPI_GOV_E", PROCUREMENT)
        resp = await client.delete(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            headers={
                "X-User-Id": "bob",
                "X-User-Roles": "user",
                "X-User-Departments": FINANCE,
            },
        )
        assert resp.status_code == 403
        assert "admin" in resp.json().get("detail", "")

    async def test_empty_owner_only_admin_can_modify(self, client, dbSession, seed_data_source) -> None:
        """owner 为空 + 非 admin → 403；admin → 200。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, seed_data_source, "KPI_GOV_F", "")
        # 非 admin + 有部门 → 403
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "改"},
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "user",
                "X-User-Departments": PROCUREMENT,
            },
        )
        assert resp.status_code == 403
        # admin → 200
        resp2 = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "admin改"},
            headers={"X-User-Id": "root", "X-User-Roles": "admin"},
        )
        assert resp2.status_code == 200

    async def test_multi_department_non_admin_returns_403(
        self, client, dbSession, seed_data_source
    ) -> None:
        """多部门成员也 403：部门集合再匹配也不是写权限。"""
        await _Helper.clean(dbSession)
        kpi = await self._create(client, seed_data_source, "KPI_GOV_G", FINANCE)
        resp = await client.put(
            f"/api/v1/kpi-catalog/{kpi['id']}",
            json={"kpiName": "双岗改"},
            headers={
                "X-User-Id": "carol",
                "X-User-Roles": "user",
                "X-User-Departments": f"{PROCUREMENT},{FINANCE}",
            },
        )
        assert resp.status_code == 403


class TestKpiAudit:
    """audit_log 写入：CREATE / UPDATE / DELETE 都有记录 + actor 正确。"""

    async def test_create_writes_audit_create(self, client, dbSession, seed_data_source) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_H",
                "kpiName": "H",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        await _Helper.drainOutbox(dbSession)

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
        self, client, dbSession, seed_data_source
    ) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_I",
                "kpiName": "原名",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        await client.put(
            f"/api/v1/kpi-catalog/{kpiId}",
            json={"kpiName": "新名"},
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        await _Helper.drainOutbox(dbSession)

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

    async def test_delete_writes_audit_delete(self, client, dbSession, seed_data_source) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_J",
                "kpiName": "J",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        await client.delete(
            f"/api/v1/kpi-catalog/{kpiId}",
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        await _Helper.drainOutbox(dbSession)

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
        self, client, dbSession, seed_data_source
    ) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_K",
                "kpiName": "K",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "carol",
                "X-User-Roles": "admin",
                "X-User-Departments": f"{PROCUREMENT},{FINANCE}",
            },
        )
        kpiId = resp.json()["id"]
        await _Helper.drainOutbox(dbSession)
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

    async def test_create_writes_history_revision_zero(self, client, dbSession, seed_data_source) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_L",
                "kpiName": "L",
                "owner": PROCUREMENT,
                "formula": "F0",
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        await _Helper.drainOutbox(dbSession)
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

    async def test_revision_increments_on_each_update(self, client, dbSession, seed_data_source) -> None:
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_M",
                "kpiName": "M",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        for newName in ["M1", "M2", "M3"]:
            await client.put(
                f"/api/v1/kpi-catalog/{kpiId}",
                json={"kpiName": newName},
                headers={
                    "X-User-Id": "alice",
                    "X-User-Roles": "admin",
                    "X-User-Departments": PROCUREMENT,
                },
            )
        await _Helper.drainOutbox(dbSession)

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
        self, client, dbSession, seed_data_source
    ) -> None:
        """update 写入的 snapshot 是「更新后」值（与 audit_log.before 互补）。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_N",
                "kpiName": "N-原",
                "formula": "OLD",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        await client.put(
            f"/api/v1/kpi-catalog/{kpiId}",
            json={"kpiName": "N-新", "formula": "NEW"},
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )

        await _Helper.drainOutbox(dbSession)

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
        self, client, dbSession, seed_data_source
    ) -> None:
        """DELETE KPI → history.kpi_id 变 NULL（FK ON DELETE SET NULL）；snapshot 仍可查。"""
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_O",
                "kpiName": "O",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]
        # create 事件先消费（worker 及时处理），保证 history 已落地
        await _Helper.drainOutbox(dbSession)
        await client.delete(
            f"/api/v1/kpi-catalog/{kpiId}",
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        # delete 事件再消费
        await _Helper.drainOutbox(dbSession)

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
    """写拒绝不产生审计/历史副作用（事务绑定）。"""

    async def test_route_acl_denied_does_not_write_audit_or_history(
        self, client, dbSession, seed_data_source
    ) -> None:
        """路由层 ACL 拒绝 ⇒ 不应产生 UPDATE audit_log 与 revision=1 history。

        2026-11-11 起写入口在路由依赖 `getAdminOnlyActor` 就 403，不再进入
        `KpiCatalogService`（service 层 owner ACL 仅保留审计语义）。
        因此本条覆盖的是「拒绝路径无写副作用」，不是 service 内部的事务顺序。
        """
        await _Helper.clean(dbSession)
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": "KPI_GOV_P",
                "kpiName": "P",
                "owner": PROCUREMENT,
            },
            headers={
                "X-User-Id": "alice",
                "X-User-Roles": "admin",
                "X-User-Departments": PROCUREMENT,
            },
        )
        kpiId = resp.json()["id"]

        # bob（非 owner 部门、非 admin）尝试 UPDATE → 应被 ACL 拒绝
        putResp = await client.put(
            f"/api/v1/kpi-catalog/{kpiId}",
            json={"kpiName": "应被拒"},
            headers={
                "X-User-Id": "bob",
                "X-User-Roles": "user",
                "X-User-Departments": FINANCE,
            },
        )
        assert putResp.status_code == 403
        await _Helper.drainOutbox(dbSession)

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


class TestKpiStatusTransitionMatrix:
    """P5：KPI 状态流转矩阵（非法流转 409，且零写副作用）。"""

    ADMIN = {"X-User-Id": "root", "X-User-Roles": "admin"}

    async def _createDraft(self, client, seed_data_source, code: str) -> int:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json={
                "datasourceId": seed_data_source.id,
                "kpiCode": code,
                "kpiName": f"测试-{code}",
                "owner": PROCUREMENT,
            },
            headers=self.ADMIN,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def test_published_kpi_cannot_revert_to_draft(
        self, client, dbSession, seed_data_source
    ) -> None:
        """PUBLISHED → DRAFT 非法：409 + 结构 details，且不写库 / 不增修订 / 不入队。"""
        await _Helper.clean(dbSession)
        kpiId = await self._createDraft(client, seed_data_source, "KPI_GOV_TRANS_A")
        published = await client.put(
            f"/api/v1/kpi-catalog/{kpiId}", json={"status": "PUBLISHED"}, headers=self.ADMIN
        )
        assert published.status_code == 200, published.text
        # 发布事件先消费，使 outbox 断言只反映「被拒绝的那次更新」
        await _Helper.drainOutbox(dbSession)

        rejected = await client.put(
            f"/api/v1/kpi-catalog/{kpiId}", json={"status": "DRAFT"}, headers=self.ADMIN
        )
        assert rejected.status_code == 409, rejected.text
        body = rejected.json()
        assert body["detail"] == "INVALID_STATUS_TRANSITION"
        assert body["details"] == {
            "from": "PUBLISHED",
            "to": "DRAFT",
            "allowed": ["DEPRECATED"],
        }

        row = await dbSession.get(KpiCatalog, kpiId)
        assert row is not None
        await dbSession.refresh(row)
        assert row.status == "PUBLISHED"
        assert row.revision_count == 1  # 只有发布那次 +1；拒绝的更新未落库

        pending = (
            await dbSession.execute(
                select(AuditOutbox).where(
                    AuditOutbox.entity_type == "kpi_catalog",
                    AuditOutbox.entity_id == kpiId,
                    AuditOutbox.processed_at.is_(None),
                )
            )
        ).scalars().all()
        assert pending == []

    async def test_published_kpi_can_be_deprecated_then_draft(
        self, client, dbSession, seed_data_source
    ) -> None:
        """合法链：DRAFT → PUBLISHED → DEPRECATED → DRAFT。"""
        await _Helper.clean(dbSession)
        kpiId = await self._createDraft(client, seed_data_source, "KPI_GOV_TRANS_B")
        for target in ("PUBLISHED", "DEPRECATED", "DRAFT"):
            resp = await client.put(
                f"/api/v1/kpi-catalog/{kpiId}", json={"status": target}, headers=self.ADMIN
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == target

        row = await dbSession.get(KpiCatalog, kpiId)
        assert row is not None
        await dbSession.refresh(row)
        assert row.status == "DRAFT"
        assert row.revision_count == 3
