"""Phase 4.1 KPI Catalog 集成测试（真实 PG 5433）。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL + 完整 API 链路，
禁止 sqlite 内存库。client fixture 走 _pg_support.pgApiClient()。

覆盖：
1. 迁移后 kpi_catalog 表存在 + 列齐全
2. CRUD：创建携带治理字段 → 读回一致 → 422 非法 → 更新 revision_count+1
3. 显式 null 清空契约（与 Phase 3.4 object_type 同模式）
4. 列表 / 按 id / 按 kpi_code 查询
5. status 转 DEPRECATED 不影响 revision_count
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text

from app.domain.enums import KpiStatus
from app.domain.models import KpiCatalog


# Phase 4.5：现有 CRUD 测试（非 ACL 专项）需带 admin headers 通过 ACL；专项测试见 test_kpi_catalog_governance.py
ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


class TestKpiMigration:
    async def test_table_and_columns_after_migration(self, dbSession) -> None:
        """Alembic upgrade head 后 kpi_catalog 表 + 关键列齐全。"""
        result = await dbSession.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'kpi_catalog'"
            )
        )
        cols = {row[0] for row in result}
        for c in (
            "id",
            "kpi_code",
            "kpi_name",
            "business_definition",
            "formula",
            "numerator",
            "denominator",
            "grain",
            "unit",
            "data_source",
            "owner",
            "version",
            "revision_count",
            "status",
            "metric_id",
            "created_time",
            "updated_time",
        ):
            assert c in cols, f"missing column {c}"


def _kpiPayload(seed_data_source, **overrides) -> dict:
    """KPI 创建 payload：F2 起 datasourceId 必填，统一从 fixture 注入。"""
    return {"datasourceId": seed_data_source.id, **overrides}


class TestKpiCatalogApi:
    async def test_create_kpi_minimal_fields(self, client, seed_data_source) -> None:
        """最小字段创建：DRAFT + v1.0 + revision_count=0 + datasourceId 回读。"""
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_TEST_OTD", kpiName="测试 OTD"),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["kpiCode"] == "KPI_TEST_OTD"
        assert data["status"] == "DRAFT"
        assert data["version"] == "v1.0"
        assert data["revisionCount"] == 0
        assert data["datasourceId"] == seed_data_source.id

    async def test_create_kpi_full_governance_fields(self, client, seed_data_source) -> None:
        """完整治理字段创建。"""
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(
                seed_data_source,
                kpiCode="KPI_SUPPLIER_OTD",
                kpiName="供应商准时交付率",
                businessDefinition="按采购订单行项按时签收比例",
                formula="COUNT(RECEIVED_QTY<=ORDER_QTY)/COUNT(*)",
                numerator="按时签收订单数",
                denominator="总订单数",
                grain="供应商+月",
                unit="%",
                dataSource="DWS_SUPPLIER_MONTHLY",
                owner="采购部",
                status="PUBLISHED",
                version="v2.0",
            ),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "PUBLISHED"
        assert data["version"] == "v2.0"
        assert data["unit"] == "%"
        assert data["grain"] == "供应商+月"
        assert data["owner"] == "采购部"
        assert data["revisionCount"] == 0
        assert data["datasourceId"] == seed_data_source.id

    async def test_create_kpi_rejects_invalid_status(self, client, seed_data_source) -> None:
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="K1", kpiName="N", status="BOGUS"),
        )
        assert resp.status_code == 422

    async def test_create_kpi_rejects_duplicate_code(self, client, seed_data_source) -> None:
        await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_DUP", kpiName="first"),
        )
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_DUP", kpiName="second"),
        )
        assert resp.status_code == 409  # unique violation

    async def test_get_kpi_by_id(self, client, seed_data_source) -> None:
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_GET", kpiName="G"),
        )
        cid = created.json()["id"]
        resp = await client.get(f"/api/v1/kpi-catalog/{cid}")
        assert resp.status_code == 200
        assert resp.json()["id"] == cid

    async def test_list_kpis(self, client, seed_data_source) -> None:
        await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_L1", kpiName="L1"),
        )
        await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_L2", kpiName="L2"),
        )
        resp = await client.get("/api/v1/kpi-catalog")
        assert resp.status_code == 200
        codes = {k["kpiCode"] for k in resp.json()}
        assert {"KPI_L1", "KPI_L2"}.issubset(codes)

    async def test_update_increments_revision_count(self, client, seed_data_source) -> None:
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_UPD", kpiName="U", status="DRAFT"),
        )
        cid = created.json()["id"]
        assert created.json()["revisionCount"] == 0

        resp = await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"businessDefinition": "补业务定义", "owner": "采购部"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["revisionCount"] == 1
        assert resp.json()["businessDefinition"] == "补业务定义"

    async def test_update_published_increments_revision_count(self, client, seed_data_source) -> None:
        """PUBLISHED 状态下 PUT 仍允许；revision_count+1。"""
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_PUB", kpiName="P", status="PUBLISHED"),
        )
        cid = created.json()["id"]
        resp = await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"unit": "%"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["revisionCount"] == 1
        assert resp.json()["unit"] == "%"

    async def test_update_clears_field_with_explicit_null(self, client, seed_data_source) -> None:
        """显式 null 清空：PUT owner:null → 落 NULL（治理字段清空依赖此契约）。"""
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(
                seed_data_source, kpiCode="KPI_CLR", kpiName="C", owner="采购部", unit="%"
            ),
        )
        cid = created.json()["id"]

        resp = await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"owner": None, "unit": None},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["owner"] is None
        assert resp.json()["unit"] is None

    async def test_update_match_threshold_null_resets_to_default(
        self, client, dbSession, seed_data_source
    ) -> None:
        """显式 match_threshold=null ⇒ 复位为默认 0.75（不是写穿 NOT NULL 约束 ⇒ 500）。

        kpi_catalog.match_threshold 是 Numeric(3,2) NOT NULL（DB 默认 0.75）；
        创建路径已把 None 归一为默认值，update 路径必须对称，否则 commit 抛
        IntegrityError（main.py 无全局处理器）⇒ 用户只看到泛化 500。
        """
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(
                seed_data_source,
                kpiCode="KPI_THR_NULL",
                kpiName="阈值复位",
                matchThreshold=0.6,
            ),
        )
        assert created.status_code == 201
        assert created.json()["matchThreshold"] == 0.6
        cid = created.json()["id"]

        resp = await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"matchThreshold": None},
            headers=ADMIN_HEADERS,
        )
        # 关键断言：不是 500（NOT NULL 写穿会是 500）
        assert resp.status_code == 200
        assert resp.json()["matchThreshold"] == 0.75

        # 再直接读 DB 行，确认落库值也是默认阈值（而非 API 层伪造）
        row = (
            await dbSession.execute(
                select(KpiCatalog.match_threshold).where(KpiCatalog.id == cid)
            )
        ).scalar_one()
        assert row == Decimal("0.75")

    async def test_deprecate_status(self, client, seed_data_source) -> None:
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_DEP", kpiName="D"),
        )
        cid = created.json()["id"]
        resp = await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"status": "DEPRECATED"},
            headers=ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "DEPRECATED"

    async def test_update_status_null_rejected(self, client, seed_data_source) -> None:
        """显式 status=null 应被拒绝（DB NOT NULL 约束，业务规则）。"""
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_NS", kpiName="no status"),
        )
        cid = created.json()["id"]
        resp = await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"status": None},
            headers=ADMIN_HEADERS,
        )
        # 400 = 服务层 ValidationError；422 = Pydantic schema 层拒绝；两者都是合法拒绝
        assert resp.status_code in (400, 422)
        body = resp.json()
        body_str = str(body)
        # 422 Pydantic 错误结构不同，但都应包含 status 提示
        assert "status" in body_str.lower() or "null" in body_str.lower()

    async def test_delete_kpi(self, client, seed_data_source) -> None:
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_DEL", kpiName="X"),
        )
        cid = created.json()["id"]
        resp = await client.delete(
            f"/api/v1/kpi-catalog/{cid}", headers=ADMIN_HEADERS
        )
        assert resp.status_code == 204
        resp2 = await client.get(f"/api/v1/kpi-catalog/{cid}")
        assert resp2.status_code == 404

    async def test_metric_id_optional_fk(self, client, seed_data_source) -> None:
        """metric_id 可空；本期不强制关联（KPI 可先于 metric 存在）。"""
        resp = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(
                seed_data_source, kpiCode="KPI_NM", kpiName="no metric", metricId=None
            ),
        )
        assert resp.status_code == 201
        assert resp.json()["metricId"] is None


class TestKpiCatalogDbConstraints:
    async def test_db_persists_revision_count(self, client, dbSession, seed_data_source) -> None:
        """PUT 后 DB 层 revision_count 真实 +1（端到端）。"""
        created = await client.post(
            "/api/v1/kpi-catalog",
            json=_kpiPayload(seed_data_source, kpiCode="KPI_DB", kpiName="D"),
        )
        cid = created.json()["id"]
        await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"owner": "X"},
            headers=ADMIN_HEADERS,
        )
        await client.put(
            f"/api/v1/kpi-catalog/{cid}",
            json={"owner": "Y"},
            headers=ADMIN_HEADERS,
        )
        row = (
            await dbSession.execute(select(KpiCatalog).where(KpiCatalog.id == cid))
        ).scalar_one()
        assert row.revision_count == 2
        assert row.owner == "Y"

    async def test_db_status_enum_constraint(self, dbSession, seed_data_source) -> None:
        """DB 层 status CHECK 约束（计划文档未要求，但 _ALLOWED_VALUES 防御）。"""
        # 由 alembic 的 CheckConstraint 强制；非法值会抛 IntegrityError
        from sqlalchemy.exc import IntegrityError

        import pytest

        bad = KpiCatalog(
            datasource_id=seed_data_source.id,  # 真实 DS，避免 FK 先报错掩盖 CHECK
            kpi_code="KPI_BAD",
            kpi_name="bad",
            version="1",
            status="BOGUS",  # type: ignore[arg-type]
        )
        dbSession.add(bad)
        with pytest.raises(IntegrityError):
            await dbSession.commit()
