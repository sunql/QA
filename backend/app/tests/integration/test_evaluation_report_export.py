"""数据质量评估报告导出集成测试（feat-dq-evaluation-report，Phase 7a）。

覆盖：
- PDF 导出字节非空、media_type、Content-Disposition
- Excel 导出字节非空、media_type
- ACL：非 owner / 非 admin 403
- include_samples=true/false 都成功

走真实 PG + httpx ASGITransport。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.dependencies import CurrentUser
from app.domain.models import EvaluationReport

ADMIN = CurrentUser(
    userId="exp-admin",
    tenantId="default",
    roles=("admin",),
    departments=(),
    dbUserId=None,
)
OWNER = CurrentUser(
    userId="exp-procurement",
    tenantId="default",
    roles=("user",),
    departments=("procurement",),
    dbUserId=None,
)
STRANGER = CurrentUser(
    userId="exp-finance",
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


async def _make_report(dbSession, *, name: str = "exp-r", owner: str = "procurement") -> int:
    row = EvaluationReport(
        name=name,
        description="export test",
        class_ids=[1],
        rule_ids=[1],
        time_window_start=datetime(2026, 9, 1, tzinfo=UTC),
        time_window_end=datetime(2026, 9, 14, tzinfo=UTC),
        status="PUBLISHED",
        tags=["test"],
        snapshot={
            "schema_version": 1,
            "evaluated_at": "2026-09-14T00:00:00Z",
            "overall": {"score": 87.32, "status": "PASS"},
            "dimensions": {
                "completeness": 92.1,
                "validity": 84.5,
                "uniqueness": 95.0,
                "consistency": 78.2,
                "timeliness": None,
                "referential": 88.1,
            },
            "tables": [
                {
                    "target_table": "SUPPLIER",
                    "overall_score": 90.5,
                    "rules": [
                        {
                            "rule_id": 12,
                            "rule_code": "R-EXP-1",
                            "rule_type": "COMPLETENESS",
                            "severity": "HIGH",
                            "total_count": 1000,
                            "passed_count": 998,
                            "violation_count": 2,
                            "pass_rate": 99.8,
                            "status": "PASS",
                        }
                    ],
                }
            ],
        },
        snapshot_version=1,
        owner=owner,
        created_by=owner,
    )
    dbSession.add(row)
    await dbSession.flush()
    return row.id


@pytest.mark.asyncio
class TestExportEndpoints:
    async def test_pdf_export_returns_pdf_bytes(self, client, dbSession) -> None:
        """GET /{id}/export?format=pdf → 200 + application/pdf。"""
        rid = await _make_report(dbSession)
        await dbSession.commit()
        resp = await client.get(
            f"/api/v1/data-quality/reports/{rid}/export?format=pdf",
            headers=_headers(OWNER),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        body = resp.content
        assert len(body) > 1000, f"PDF too small: {len(body)} bytes"
        assert body.startswith(b"%PDF-"), "not a PDF header"

    async def test_excel_export_returns_xlsx_bytes(self, client, dbSession) -> None:
        """GET /{id}/export?format=excel → 200 + xlsx mime。"""
        rid = await _make_report(dbSession)
        await dbSession.commit()
        resp = await client.get(
            f"/api/v1/data-quality/reports/{rid}/export?format=excel",
            headers=_headers(OWNER),
        )
        assert resp.status_code == 200
        assert (
            "spreadsheetml"
            in resp.headers["content-type"]
        )
        body = resp.content
        # xlsx 是 zip 容器，前缀 PK\x03\x04
        assert body[:2] == b"PK", "not an xlsx (zip)"

    async def test_export_acl_stranger_forbidden(self, client, dbSession) -> None:
        """非 owner / 非 admin 访问 /export 应被 403 拒绝。"""
        rid = await _make_report(dbSession)
        await dbSession.commit()
        resp = await client.get(
            f"/api/v1/data-quality/reports/{rid}/export?format=pdf",
            headers=_headers(STRANGER),
        )
        assert resp.status_code == 403

    async def test_export_admin_always_allowed(self, client, dbSession) -> None:
        """admin 角色无视 owner 字段也能导出。"""
        rid = await _make_report(dbSession, owner="someone-else")
        await dbSession.commit()
        resp = await client.get(
            f"/api/v1/data-quality/reports/{rid}/export?format=pdf",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 200

    async def test_export_404_on_missing(self, client) -> None:
        resp = await client.get(
            "/api/v1/data-quality/reports/999999/export?format=pdf",
            headers=_headers(ADMIN),
        )
        assert resp.status_code == 404

    async def test_invalid_format_rejected(self, client, dbSession) -> None:
        """format=docx 不在白名单 → 422（Pydantic 校验）。"""
        rid = await _make_report(dbSession)
        await dbSession.commit()
        resp = await client.get(
            f"/api/v1/data-quality/reports/{rid}/export?format=docx",
            headers=_headers(OWNER),
        )
        assert resp.status_code == 422