"""发布闸门 HTTP 链路（真实 PG + 完整 API）。

补 FIX-1：「关联 KPI unknown ⇒ 放行」这一分支此前零覆盖。service 级用例里
「指标发布成功」的场景都没有挂 KPI（``linked`` 为空），与「挂了一个治理列全空
的 KPI（层 2 = unknown）后放行」不可区分 ⇒ 反转判定仍然全绿（假绿）。

本文件全部走 HTTP（路由 → service → 真实 PG），并断言 409 正文是**中文**
（FIX-2：正文用 finding 的中文 detail，英文 reason 码只进日志）。
"""

from __future__ import annotations

from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from app.services.kpi_metric_switches import SWITCH_GATE

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

# 满足本体指标方向闸门全部自身条件（结构化齐全 + 可执行口径 + 粒度键）
_METRIC_BASE: dict = {
    "formula": "SELECT SUM(QTY) FROM T",
    "aggFunction": "SUM",
    "metricType": "MEASURE",
    "numeratorMeasure": {"agg": "SUM", "expr": "QTY"},
    "grainKeys": ["plant_code"],
    "executionSource": "COMPILED",
}


async def _enableGate(dbSession: AsyncSession) -> None:
    dbSession.add(
        SystemConfig(
            key=SWITCH_GATE,
            value="true",
            description="KPI 发布闸门开关",
        )
    )
    await dbSession.commit()


async def _createMetric(
    client: AsyncClient, name: str, **overrides: object
) -> dict:
    resp = await client.post(
        "/api/v1/ontology/metrics",
        json={**_METRIC_BASE, "metricName": name, **overrides},
        headers=ADMIN_HEADERS,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _createKpi(
    client: AsyncClient, *, code: str, datasource: DataSource, **payload: object
) -> dict:
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


async def _publishMetric(client: AsyncClient, metricId: int) -> Response:
    return await client.put(
        f"/api/v1/ontology/metrics/{metricId}",
        json={"status": "PUBLISHED"},
        headers=ADMIN_HEADERS,
    )


async def _publishKpi(client: AsyncClient, kpiId: int) -> Response:
    return await client.put(
        f"/api/v1/kpi-catalog/{kpiId}",
        json={"status": "PUBLISHED"},
        headers=ADMIN_HEADERS,
    )


# ── (a) 指标方向 · unknown 放行（正例）────────────────────────────────


async def test_metric_publish_allowed_when_linked_kpi_governance_unknown(
    client: AsyncClient,
    dbSession: AsyncSession,
    seed_data_source: DataSource,
) -> None:
    """关联 KPI 治理列全空 ⇒ 层 2 = unknown ⇒ 放行（打破 KPI/指标互锁）。"""
    await _enableGate(dbSession)
    metric = await _createMetric(client, "HTTP_UNKNOWN_OK")
    kpi = await _createKpi(
        client,
        code="KPI_HTTP_UNKNOWN_OK",
        datasource=seed_data_source,
        metricId=metric["id"],
    )

    resp = await _publishMetric(client, metric["id"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PUBLISHED"
    assert "关联 KPI 口径对账不通过" not in resp.text

    # ⚠️ 必须证明「确实挂了 KPI」：否则「一个 KPI 都没挂」也会同样通过
    # ⇒ 测不到 unknown 放行这个目标分支
    row = (
        await dbSession.execute(
            select(KpiCatalog).where(KpiCatalog.id == kpi["id"])
        )
    ).scalar_one()
    assert row.metric_id == metric["id"]
    # 治理侧全空是「层 2 = unknown」的成因，显式钉住
    assert not row.numerator_columns
    assert not row.denominator_columns
    assert not row.grain_keys


# ── (b) 指标方向 · mismatch 拦截（反向对照）──────────────────────────


async def test_metric_publish_blocked_when_linked_kpi_grain_mismatch(
    client: AsyncClient,
    dbSession: AsyncSession,
    seed_data_source: DataSource,
) -> None:
    """关联 KPI 粒度键与技术侧不一致 ⇒ 层 2 = mismatch ⇒ 拦下 + 中文正文。"""
    await _enableGate(dbSession)
    metric = await _createMetric(client, "HTTP_MISMATCH_BLOCK")
    await _createKpi(
        client,
        code="KPI_HTTP_MISMATCH",
        datasource=seed_data_source,
        metricId=metric["id"],
        grainKeys=["other_plant"],
    )

    resp = await _publishMetric(client, metric["id"])

    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    # 具体是 linked_mismatch 这一项拦的（该文案只属于此 item）
    assert "关联 KPI 口径对账不通过" in error
    # FIX-2：正文用中文 detail，不是英文 reason 码
    assert "治理与技术粒度键不一致" in error
    assert "grain_mismatch" not in error

    # 闸门失败 ⇒ 库里仍是 DRAFT
    row = (
        await dbSession.execute(
            select(OntologyMetric).where(OntologyMetric.id == metric["id"])
        )
    ).scalar_one()
    await dbSession.refresh(row)
    assert row.status == "DRAFT"


# ── (c) KPI 方向 · unknown 拦截 + 409 中文正文 ───────────────────────


async def test_kpi_publish_blocked_when_reconcile_unknown(
    client: AsyncClient,
    dbSession: AsyncSession,
    seed_data_source: DataSource,
) -> None:
    """指标 PUBLISHED 但治理列全空 ⇒ 层 2 = unknown ⇒ KPI 发布被拦。"""
    await _enableGate(dbSession)
    metric = await _createMetric(
        client, "HTTP_KPI_UNKNOWN", status="PUBLISHED"
    )
    kpi = await _createKpi(
        client,
        code="KPI_HTTP_UNKNOWN_BLOCK",
        datasource=seed_data_source,
        metricId=metric["id"],
    )

    resp = await _publishKpi(client, kpi["id"])

    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    # 具体是 reconcile 项拦的：其余 4 项的文案一个都不应出现
    assert "口径对账未完成" in error
    assert "未挂靠本体指标" not in error
    assert "本体指标未发布" not in error
    assert "无可执行口径" not in error
    assert "未绑定业务数据源" not in error
    # FIX-2：中文层名 + 真实成因（不是「治理侧未填结构化列」静态文案）
    assert "[治理层]" in error
    assert "治理侧未声明分子列" in error
    assert "governance_columns_absent" not in error
