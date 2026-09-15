"""评估报告分享 service + API 集成测试（feat-dq-evaluation-report，Phase 8b）。

覆盖：
- create_share + ACL（owner/admin 通过，stranger 403）
- resolve_share：返回 report；access_count 自增
- 过期 token → 410 Gone（NotFoundError 返 404，本实现映射为 NotFoundError）
- revoke_share + 撤销后再 resolve → 404
- 公开端点无需登录
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.dependencies import CurrentUser
from app.domain.enums import DataSourceType, RuleType, Severity
from app.domain.models import (
    DataQualityRule,
    DataSource,
    EvaluationReport,
    EvaluationReportShare,
    OntologyClass,
)
from app.infrastructure.security.crypto import encryptApiKey
from app.services.evaluation_report_share_service import (
    EvaluationReportShareService,
)


ADMIN = CurrentUser(
    userId="share-admin",
    tenantId="default",
    roles=("admin",),
    departments=(),
    dbUserId=None,
)
OWNER_USER = CurrentUser(
    userId="share-proc",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)
STRANGER = CurrentUser(
    userId="share-fin",
    tenantId="default",
    roles=("user",),
    departments=("finance",),
    dbUserId=None,
)


def _headers(actor: CurrentUser | None) -> dict[str, str]:
    if actor is None:
        return {}
    h = {"X-User-Id": actor.userId, "X-User-Roles": ",".join(actor.roles)}
    if actor.departments:
        h["X-User-Departments"] = ",".join(actor.departments)
    return h


async def _seed_report(dbSession, *, owner: str | None) -> int:
    """种一个最小 report 并把 owner 字段更新。返回 id。"""
    ds = DataSource(
        name="share-ds",
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="t",
        username="t",
        password_encrypted=encryptApiKey("t"),
    )
    dbSession.add(ds)
    await dbSession.flush()
    cls = OntologyClass(
        class_name="SHARE_PO",
        object_type="Transaction",
        object_owner="procurement",
    )
    dbSession.add(cls)
    await dbSession.flush()
    rule = DataQualityRule(
        rule_name="share-r",
        rule_code="SHARE-R1",
        datasource_id=ds.id,
        target_table="PORDER",
        target_column="QTY",
        rule_type=RuleType.COMPLETENESS,
        threshold="95.00",
        severity=Severity.MEDIUM,
        is_enabled=True,
        version="v1",
    )
    dbSession.add(rule)
    await dbSession.flush()
    report = EvaluationReport(
        name="share-test",
        description=None,
        class_ids=[cls.id],
        rule_ids=[rule.id],
        time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
        time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
        status="PUBLISHED",
        tags=[],
        snapshot={
            "schema_version": 1,
            "overall": {"score": 80.0, "status": "PASS"},
            "dimensions": {},
            "tables": [],
        },
        snapshot_version=1,
        owner=owner,
        created_by=ADMIN.userId,
    )
    dbSession.add(report)
    await dbSession.commit()
    return report.id


@pytest.mark.asyncio
class TestShareService:
    async def test_create_share_admin_allowed(self, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        svc = EvaluationReportShareService()
        out = await svc.create_share(
            dbSession,
            report_id=rid,
            expires_in_days=7,
            actor=ADMIN,
        )
        assert out.id > 0
        assert out.report_id == rid
        assert len(out.share_token) == 36  # UUID
        assert out.share_url and out.share_url.endswith(out.share_token)

    async def test_create_share_owner_dept_allowed(self, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        out = await EvaluationReportShareService().create_share(
            dbSession, report_id=rid, expires_in_days=3, actor=OWNER_USER,
        )
        assert out.id > 0

    async def test_create_share_stranger_forbidden(self, dbSession) -> None:
        from app.domain.exceptions import PermissionDeniedError

        rid = await _seed_report(dbSession, owner="procurement")
        with pytest.raises(PermissionDeniedError):
            await EvaluationReportShareService().create_share(
                dbSession, report_id=rid, expires_in_days=7, actor=STRANGER,
            )

    async def test_create_share_missing_report_404(self, dbSession) -> None:
        from app.domain.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await EvaluationReportShareService().create_share(
                dbSession, report_id=999999, expires_in_days=7, actor=ADMIN,
            )

    async def test_resolve_share_increments_access_count(self, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        share = await EvaluationReportShareService().create_share(
            dbSession, report_id=rid, expires_in_days=7, actor=ADMIN,
        )
        svc = EvaluationReportShareService()
        report1 = await svc.resolve_share(dbSession, token=share.share_token)
        assert report1.id == rid
        report2 = await svc.resolve_share(dbSession, token=share.share_token)
        assert report2.id == rid
        # access_count 应该被自增到 2
        row = (
            await dbSession.execute(
                text("SELECT access_count FROM evaluation_report_share WHERE id=:id"),
                {"id": share.id},
            )
        ).scalar_one()
        assert row == 2

    async def test_resolve_share_expired_404(self, dbSession) -> None:
        from app.domain.exceptions import NotFoundError

        rid = await _seed_report(dbSession, owner="procurement")
        share = await EvaluationReportShareService().create_share(
            dbSession, report_id=rid, expires_in_days=7, actor=ADMIN,
        )
        # 手动把 expires_at 改成过去
        await dbSession.execute(
            text(
                "UPDATE evaluation_report_share SET expires_at=:exp WHERE id=:id"
            ),
            {"exp": datetime.now(UTC) - timedelta(days=1), "id": share.id},
        )
        await dbSession.commit()
        with pytest.raises(NotFoundError):
            await EvaluationReportShareService().resolve_share(
                dbSession, token=share.share_token,
            )

    async def test_revoke_share_removed(self, dbSession) -> None:
        from app.domain.exceptions import NotFoundError

        rid = await _seed_report(dbSession, owner="procurement")
        share = await EvaluationReportShareService().create_share(
            dbSession, report_id=rid, expires_in_days=7, actor=ADMIN,
        )
        await EvaluationReportShareService().revoke_share(
            dbSession, share_id=share.id, actor=ADMIN,
        )
        with pytest.raises(NotFoundError):
            await EvaluationReportShareService().resolve_share(
                dbSession, token=share.share_token,
            )

    async def test_list_shares_excludes_expired(self, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        svc = EvaluationReportShareService()
        live = await svc.create_share(
            dbSession, report_id=rid, expires_in_days=7, actor=ADMIN,
        )
        expired = await svc.create_share(
            dbSession, report_id=rid, expires_in_days=1, actor=ADMIN,
        )
        await dbSession.execute(
            text(
                "UPDATE evaluation_report_share SET expires_at=:exp WHERE id=:id"
            ),
            {"exp": datetime.now(UTC) - timedelta(days=1), "id": expired.id},
        )
        await dbSession.commit()
        rows = await svc.list_shares(dbSession, report_id=rid)
        ids = {r.id for r in rows}
        assert live.id in ids
        assert expired.id not in ids


@pytest.mark.asyncio
class TestShareApi:
    async def test_public_resolve_no_auth_required(self, client, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        share = await EvaluationReportShareService().create_share(
            dbSession, report_id=rid, expires_in_days=7, actor=ADMIN,
        )
        # 注意：client 默认带 stub auth 头（X-User-Id=DEFAULT_USER_ID），
        # 但本接口路由不走 getCurrentUser，所以匿名也能访问。
        resp = await client.get(
            f"/api/v1/data-quality/reports/share/{share.share_token}",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rid

    async def test_public_resolve_invalid_token_404(self, client) -> None:
        resp = await client.get(
            "/api/v1/data-quality/reports/share/not-a-real-token",
        )
        assert resp.status_code == 404

    async def test_revoke_endpoint_removes_share(self, client, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        share = await EvaluationReportShareService().create_share(
            dbSession, report_id=rid, expires_in_days=7, actor=ADMIN,
        )
        resp = await client.delete(
            f"/api/v1/data-quality/reports/shares/{share.id}",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 204
        # 再次 resolve → 404
        resp2 = await client.get(
            f"/api/v1/data-quality/reports/share/{share.share_token}",
        )
        assert resp2.status_code == 404

    async def test_create_share_endpoint_admin(self, client, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        resp = await client.post(
            f"/api/v1/data-quality/reports/{rid}/share",
            json={"expiresInDays": 5},
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["reportId"] == rid
        assert body["expiresAt"] is not None

    async def test_create_share_stranger_403(self, client, dbSession) -> None:
        rid = await _seed_report(dbSession, owner="procurement")
        resp = await client.post(
            f"/api/v1/data-quality/reports/{rid}/share",
            json={"expiresInDays": 5},
            headers=_headers(STRANGER),
        )
        assert resp.status_code == 403

    async def test_compare_endpoint_returns_delta(self, client, dbSession) -> None:
        from app.services.evaluation_report_service import (
            EvaluationReportService,
        )

        cid, rid_rule = await _seed_min_for_compare(dbSession)
        # 创建两份报告（同一规则 ID）
        left = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload_for_compare("cmp-left", cid, rid_rule, score=80.0),
            headers=_headers(ADMIN),
        )
        right = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload_for_compare("cmp-right", cid, rid_rule, score=90.0),
            headers=_headers(ADMIN),
        )
        left_id = left.json()["id"]
        right_id = right.json()["id"]
        resp = await client.get(
            f"/api/v1/data-quality/reports/{left_id}/compare?otherId={right_id}",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["leftId"] == left_id
        assert body["rightId"] == right_id
        assert body["overallDelta"] is not None


# ---- helpers for compare API test ----


async def _seed_min_for_compare(dbSession) -> tuple[int, int]:
    """种一个 class + datasource + rule，返回 (class_id, rule_id)。"""
    ds = DataSource(
        name="cmp-ds",
        type=DataSourceType.POSTGRESQL,
        host="localhost",
        port=5432,
        database_name="t",
        username="t",
        password_encrypted=encryptApiKey("t"),
    )
    dbSession.add(ds)
    await dbSession.flush()
    cls = OntologyClass(
        class_name="CMP_PO",
        object_type="Transaction",
        object_owner="procurement",
    )
    dbSession.add(cls)
    await dbSession.flush()
    rule = DataQualityRule(
        rule_name="cmp-r",
        rule_code="CMP-R1",
        datasource_id=ds.id,
        target_table="PORDER",
        target_column="QTY",
        rule_type=RuleType.COMPLETENESS,
        threshold="95.00",
        severity=Severity.MEDIUM,
        is_enabled=True,
        version="v1",
    )
    dbSession.add(rule)
    await dbSession.commit()
    return cls.id, rule.id


def _payload_for_compare(name: str, cid: int, rid: int, score: float) -> dict:
    return {
        "name": name,
        "classIds": [cid],
        "ruleIds": [rid],
        "timeWindowStart": "2026-09-01T00:00:00Z",
        "timeWindowEnd": "2026-09-14T23:59:59Z",
        "tags": [],
        "status": "PUBLISHED",
    }