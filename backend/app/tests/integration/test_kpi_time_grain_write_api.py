"""P3 D4：`time_grain` / `time_column` 管理写入口（真实 PG + 完整 API 链路）。

覆盖：
1. admin 写 KPI `timeGrain` / 本体指标 `timeGrain` + `timeColumn` ⇒ 成功 + 落库 + GET 回读一致；
2. 非 admin 写 ⇒ 403 且不落库（写入口 admin 门禁不放松，新字段跟随既有 ACL）；
3. 发布闸门按**本次请求的新值**判定：KPI 方向的治理覆盖与指标方向的合并技术形态
   都必须携带新值（对照 `test_kpi_metric_backfill_apply.py` 的
   `test_publish_gate_uses_passed_governance_over_stored_values` 写法）。

⚠️ 闸门对账当前走 `resolution=None` 的恒等回退（P3 D3 定下的行为，Task 3 DoD#5），
该路径**不比较 `time_grain`**，所以本文件断言的是「闸门被调用时的输入快照」——
只有新值确实传进闸门，后续接入解析结果时判定才可能正确；若断言 verdict，当前
实现下任何值都会通过，等于假绿（详见 task-4 报告「发布闸门决策」）。

写入语义：`timeGrain` 大小写不敏感，落库统一小写（`TimeGrain` 值域 / VARCHAR(20)）；
非法值在 DTO 边界 422。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from app.services.kpi_metric_switches import SWITCH_GATE

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
USER_HEADERS = {"X-User-Id": "normal-user", "X-User-Roles": "user"}

# 满足本体指标发布闸门自身条件（结构化齐全 + 可执行口径 + 粒度键）。
_METRIC_BASE: dict = {
    "formula": "SELECT SUM(QTY) FROM T",
    "aggFunction": "SUM",
    "metricType": "MEASURE",
    "numeratorMeasure": {"agg": "SUM", "expr": "QTY"},
    "grainKeys": ["plant_code"],
    "executionSource": "COMPILED",
}

# KPI 治理列与技术形态对齐 ⇒ 恒等回退对账 pass（闸门其余 4 项也齐）。
_KPI_GOVERNANCE: dict = {
    "numeratorColumns": ["QTY"],
    "grainKeys": ["plant_code"],
}


async def _createDataSource(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(
        name="TEST_DS_TIME_GRAIN",
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _createMetric(client: AsyncClient, name: str, **overrides: object) -> dict:
    resp = await client.post(
        "/api/v1/ontology/metrics",
        json={**_METRIC_BASE, "metricName": name, **overrides},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _createKpi(client: AsyncClient, *, code: str, datasource: DataSource, **payload: object) -> dict:
    resp = await client.post(
        "/api/v1/kpi-catalog",
        json={
            "kpiCode": code,
            "kpiName": code,
            "datasourceId": datasource.id,
            **payload,
        },
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _enableGate(dbSession: AsyncSession) -> None:
    dbSession.add(
        SystemConfig(
            key=SWITCH_GATE,
            value="true",
            description="KPI 发布闸门开关",
        )
    )
    await dbSession.commit()


async def _reloadKpi(dbSession: AsyncSession, kpiId: int) -> KpiCatalog:
    dbSession.expire_all()
    return (await dbSession.execute(select(KpiCatalog).where(KpiCatalog.id == kpiId))).scalar_one()


async def _reloadMetric(dbSession: AsyncSession, metricId: int) -> OntologyMetric:
    dbSession.expire_all()
    return (await dbSession.execute(select(OntologyMetric).where(OntologyMetric.id == metricId))).scalar_one()


# ── ① 写入口落库 + GET 回读 ─────────────────────────────────────────


async def test_admin_kpi_create_and_update_time_grain_persists(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST 带 timeGrain ⇒ 201；PUT 改 timeGrain（大小写不敏感）⇒ 200 且落库/回读一致。"""
    ds = await _createDataSource(dbSession)
    created = await _createKpi(
        client,
        code="KPI_TG_PERSIST",
        datasource=ds,
        timeGrain="month",
    )
    assert created["timeGrain"] == "month"
    row = await _reloadKpi(dbSession, created["id"])
    assert row.time_grain == "month"

    updated = await client.put(
        f"/api/v1/kpi-catalog/{created['id']}",
        json={"timeGrain": "MONTH"},
        headers=ADMIN_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    # 枚举值域小写：大写输入归一后落库/回读
    assert updated.json()["timeGrain"] == "month"

    row = await _reloadKpi(dbSession, created["id"])
    assert row.time_grain == "month"

    fetched = await client.get(f"/api/v1/kpi-catalog/{created['id']}", headers=ADMIN_HEADERS)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["timeGrain"] == "month"


async def test_admin_metric_create_and_update_time_fields_persist(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """POST/PUT 本体指标 timeGrain + timeColumn ⇒ 落库 + GET 回读一致。"""
    created = await _createMetric(
        client,
        "TG_METRIC_PERSIST",
        timeGrain="quarter",
        timeColumn="RECEIPT_DATE_OLD",
    )
    assert created["timeGrain"] == "quarter"
    assert created["timeColumn"] == "RECEIPT_DATE_OLD"
    row = await _reloadMetric(dbSession, created["id"])
    assert row.time_grain == "quarter"
    assert row.time_column == "RECEIPT_DATE_OLD"

    updated = await client.put(
        f"/api/v1/ontology/metrics/{created['id']}",
        json={"timeGrain": "MONTH", "timeColumn": "RECEIPT_DATE"},
        headers=ADMIN_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["timeGrain"] == "month"
    assert updated.json()["timeColumn"] == "RECEIPT_DATE"

    row = await _reloadMetric(dbSession, created["id"])
    assert row.time_grain == "month"
    assert row.time_column == "RECEIPT_DATE"

    fetched = await client.get(f"/api/v1/ontology/metrics/{created['id']}", headers=ADMIN_HEADERS)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["timeGrain"] == "month"
    assert fetched.json()["timeColumn"] == "RECEIPT_DATE"


async def test_invalid_time_grain_rejected_at_boundary(client: AsyncClient, dbSession: AsyncSession) -> None:
    """非法 timeGrain 在 DTO 边界 422，且不落库（值域由 TimeGrain 守卫）。"""
    ds = await _createDataSource(dbSession)
    created = await _createKpi(client, code="KPI_TG_INVALID", datasource=ds)
    assert created["timeGrain"] is None

    resp = await client.put(
        f"/api/v1/kpi-catalog/{created['id']}",
        json={"timeGrain": "BOGUS"},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 422, resp.text
    row = await _reloadKpi(dbSession, created["id"])
    assert row.time_grain is None


# ── ② 非 admin 写 ⇒ 403（新字段跟随既有 ACL）─────────────────────────


async def test_non_admin_cannot_update_kpi_time_grain(client: AsyncClient, dbSession: AsyncSession) -> None:
    ds = await _createDataSource(dbSession)
    created = await _createKpi(client, code="KPI_TG_ACL", datasource=ds, timeGrain="month")

    resp = await client.put(
        f"/api/v1/kpi-catalog/{created['id']}",
        json={"timeGrain": "quarter"},
        headers=USER_HEADERS,
    )

    assert resp.status_code == 403, resp.text
    assert "admin" in resp.json().get("detail", "")
    row = await _reloadKpi(dbSession, created["id"])
    assert row.time_grain == "month"  # 拒绝路径无写副作用


async def test_non_admin_cannot_update_metric_time_fields(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    created = await _createMetric(client, "TG_METRIC_ACL", timeGrain="month", timeColumn="RECEIPT_DATE")

    resp = await client.put(
        f"/api/v1/ontology/metrics/{created['id']}",
        json={"timeGrain": "quarter", "timeColumn": "OTHER_DATE"},
        headers=USER_HEADERS,
    )

    assert resp.status_code == 403, resp.text
    row = await _reloadMetric(dbSession, created["id"])
    assert row.time_grain == "month"
    assert row.time_column == "RECEIPT_DATE"


# ── ③ 发布闸门按本次请求的新值判定（输入快照）─────────────────────────


async def test_kpi_publish_gate_receives_new_time_grain(
    client: AsyncClient,
    dbSession: AsyncSession,
    monkeypatch,
) -> None:
    """同一请求改 timeGrain + 发布 ⇒ 闸门拿到的治理快照必须是**新值**。

    对照 `test_publish_gate_uses_passed_governance_over_stored_values`：闸门必须按
    传入快照判定，而不是实体上未提交的旧值。这里用 spy 包住真实闸门（仍会执行
    真实闸门逻辑），断言 `governance.time_grain` 来自本次请求而非库里的 quarter。
    """
    import app.services.kpi_catalog_service as kpiCatalogModule

    await _enableGate(dbSession)
    metric = await _createMetric(client, "TG_KPI_GATE", status="PUBLISHED")
    kpi = await _createKpi(
        client,
        code="KPI_TG_GATE",
        datasource=await _createDataSource(dbSession),
        metricId=metric["id"],
        timeGrain="quarter",
        **_KPI_GOVERNANCE,
    )

    captured: dict = {}
    original = kpiCatalogModule.enforceKpiPublishGate

    async def _spy(session, entity, **kwargs):
        captured.update(kwargs)
        return await original(session, entity, **kwargs)

    monkeypatch.setattr(kpiCatalogModule, "enforceKpiPublishGate", _spy)

    resp = await client.put(
        f"/api/v1/kpi-catalog/{kpi['id']}",
        json={"timeGrain": "MONTH", "status": "PUBLISHED"},
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert "governance" in captured, "本次请求改了治理列，闸门必须收到覆盖快照"
    assert captured["governance"].time_grain == "month"
    row = await _reloadKpi(dbSession, kpi["id"])
    assert row.time_grain == "month"
    assert row.status == "PUBLISHED"


async def test_metric_publish_gate_receives_new_time_fields(
    client: AsyncClient,
    dbSession: AsyncSession,
    monkeypatch,
) -> None:
    """同一请求改 timeGrain/timeColumn + 发布 ⇒ 闸门拿到的技术形态必须是**新值**。

    指标闸门在落属性之前判定（`_mergedTechnique`），因此合并快照必须覆盖本次
    请求的两个新字段，而不是实体旧值（quarter / RECEIPT_DATE_OLD）。
    """
    import app.services.ontology_service as ontologyModule

    await _enableGate(dbSession)
    metric = await _createMetric(
        client,
        "TG_METRIC_GATE",
        timeGrain="quarter",
        timeColumn="RECEIPT_DATE_OLD",
    )

    captured: dict = {}
    original = ontologyModule.enforceMetricPublishGate

    async def _spy(session, entity, **kwargs):
        captured.update(kwargs)
        return await original(session, entity, **kwargs)

    monkeypatch.setattr(ontologyModule, "enforceMetricPublishGate", _spy)

    resp = await client.put(
        f"/api/v1/ontology/metrics/{metric['id']}",
        json={
            "timeGrain": "month",
            "timeColumn": "RECEIPT_DATE",
            "status": "PUBLISHED",
        },
        headers=ADMIN_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert "technique" in captured, "发布必须用合并后的技术形态过闸"
    assert captured["technique"].time_grain == "month"
    assert captured["technique"].time_column == "RECEIPT_DATE"
    row = await _reloadMetric(dbSession, metric["id"])
    assert row.time_grain == "month"
    assert row.time_column == "RECEIPT_DATE"
    assert row.status == "PUBLISHED"
