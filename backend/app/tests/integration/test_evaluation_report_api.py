"""数据质量评估报告 HTTP API 集成测试（feat-dq-evaluation-report，Phase 3）。

走真实 PG + httpx ASGITransport：
- GET    /api/v1/data-quality/reports
- POST   /api/v1/data-quality/reports
- GET    /api/v1/data-quality/reports/{id}
- PATCH  /api/v1/data-quality/reports/{id}
- DELETE /api/v1/data-quality/reports/{id}
- POST   /api/v1/data-quality/reports/{id}/regenerate

camelCase JSON 契约 + status 422/403/404 路径。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.dependencies import CurrentUser
from app.domain.enums import DataSourceType, RuleType, Severity
from app.domain.models import DataQualityRule, DataSource, OntologyClass
from app.infrastructure.security.crypto import encryptApiKey

ADMIN = CurrentUser(
    userId="api-admin",
    tenantId="default",
    roles=("admin",),
    departments=(),
    dbUserId=None,
)
OWNER = CurrentUser(
    userId="api-procurement",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)
STRANGER = CurrentUser(
    userId="api-finance",
    tenantId="default",
    roles=("user",),
    departments=("finance",),
    dbUserId=None,
)


def _headers(actor: CurrentUser) -> dict[str, str]:
    h = {"X-User-Id": actor.userId, "X-User-Roles": ",".join(actor.roles)}
    if actor.departments:
        h["X-User-Departments"] = ",".join(actor.departments)
    return h


async def _seed_minimum(dbSession) -> tuple[int, int]:
    """造一个 ontology_class + data_source + data_quality_rule，返回 (cid, rid)。"""
    ds = DataSource(
        name="api-ds",
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
        class_name="API_PURCHASE_ORDER",
        object_type="Transaction",
        object_owner="procurement",
    )
    dbSession.add(cls)
    await dbSession.flush()
    rule = DataQualityRule(
        rule_name="api-r",
        rule_code="API-R1",
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


def _payload(name: str, cid: int, rid: int) -> dict:
    return {
        "name": name,
        "classIds": [cid],
        "ruleIds": [rid],
        "timeWindowStart": "2026-09-01T00:00:00Z",
        "timeWindowEnd": "2026-09-14T23:59:59Z",
        "tags": ["weekly"],
        "status": "PUBLISHED",
    }


class TestEvaluationReportApi:
    async def test_post_creates_and_returns_201(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        resp = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload("api-r-create", cid, rid),
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "api-r-create"
        assert body["status"] == "PUBLISHED"
        assert body["classIds"] == [cid]
        assert body["ruleIds"] == [rid]
        assert body["createdBy"] == ADMIN.userId
        assert "snapshot" in body
        # snapshot 是 dict[str, Any]，内层键不自动 camelCase（Phase 4 用 Pydantic 模型替换）
        assert body["snapshot"]["schema_version"] == 1

    async def test_get_returns_404_for_missing(self, client, dbSession) -> None:
        resp = await client.get(
            "/api/v1/data-quality/reports/99999",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 404

    async def test_list_returns_paginated(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        for i in range(3):
            r = await client.post(
                "/api/v1/data-quality/reports",
                json=_payload(f"list-{i}", cid, rid),
                headers=_headers(ADMIN),
            )
            assert r.status_code == 201
        resp = await client.get(
            "/api/v1/data-quality/reports?limit=10",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["rows"]) == 3

    async def test_list_filter_by_class_id(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        await client.post(
            "/api/v1/data-quality/reports",
            json=_payload("by-class", cid, rid),
            headers=_headers(ADMIN),
        )
        resp = await client.get(
            f"/api/v1/data-quality/reports?classId={cid}",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1
        assert all(cid in r["classIds"] for r in body["rows"])

    async def test_patch_updates_name_only(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        create = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload("patch-src", cid, rid),
            headers=_headers(ADMIN),
        )
        rid_ = create.json()["id"]
        resp = await client.patch(
            f"/api/v1/data-quality/reports/{rid_}",
            json={"name": "patch-dst", "status": "DRAFT"},
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "patch-dst"
        assert body["status"] == "DRAFT"

    async def test_patch_acl_stranger_forbidden(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        create = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload("acl-api", cid, rid),
            headers=_headers(ADMIN),
        )
        rid_ = create.json()["id"]
        # 把 owner 改到 procurement，STRANGER 是 finance
        await dbSession.execute(
            text(
                "UPDATE evaluation_report SET owner='procurement' WHERE id=:id"
            ),
            {"id": rid_},
        )
        await dbSession.commit()
        resp = await client.patch(
            f"/api/v1/data-quality/reports/{rid_}",
            json={"description": "nope"},
            headers=_headers(STRANGER),
        )
        assert resp.status_code == 403

    async def test_delete_soft_deletes(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        create = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload("del-me", cid, rid),
            headers=_headers(ADMIN),
        )
        rid_ = create.json()["id"]
        resp = await client.delete(
            f"/api/v1/data-quality/reports/{rid_}",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200
        # 再次 GET 应 404（软删过滤）
        again = await client.get(
            f"/api/v1/data-quality/reports/{rid_}",
            headers=_headers(ADMIN),
        )
        assert again.status_code == 404

    async def test_regenerate_replaces_snapshot(self, client, dbSession) -> None:
        cid, rid = await _seed_minimum(dbSession)
        create = await client.post(
            "/api/v1/data-quality/reports",
            json=_payload("regen-api", cid, rid),
            headers=_headers(ADMIN),
        )
        rid_ = create.json()["id"]
        resp = await client.post(
            f"/api/v1/data-quality/reports/{rid_}/regenerate",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "dimensions" in body["snapshot"]
        assert "tables" in body["snapshot"]
        assert body["snapshot"]["schema_version"] == 1

    async def test_validation_error_on_empty_class_ids(
        self, client, dbSession
    ) -> None:
        cid, rid = await _seed_minimum(dbSession)
        resp = await client.post(
            "/api/v1/data-quality/reports",
            json={
                "name": "bad-1",
                "classIds": [],
                "ruleIds": [rid],
                "timeWindowStart": "2026-09-01T00:00:00Z",
                "timeWindowEnd": "2026-09-14T23:59:59Z",
            },
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 422
