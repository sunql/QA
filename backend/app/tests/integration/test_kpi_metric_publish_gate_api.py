"""发布闸门服务层链路（真实 PG + service 直调）。

断言必须覆盖「**是谁拒的**」：不只是状态码，还要断言异常类型与消息里
缺了哪一项（防「只断言状态码 = 假绿」家族）。

闸门由 ``KPI_METRIC_GATE_ENABLED`` 开关控制（默认 false ⇒ 只观察不阻断）。
本文件显式把开关打开 / 关闭，覆盖两条分支。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.enums import KpiStatus, MetricStatus
from app.domain.exceptions import ConflictError
from app.domain.models import DataSource, KpiCatalog, OntologyMetric
from app.domain.schemas import (
    KpiCatalogCreate,
    KpiCatalogUpdate,
    OntologyMetricCreate,
    OntologyMetricUpdate,
)
from app.models.system_config import SystemConfig
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.kpi_metric_switches import SWITCH_GATE
from app.services.ontology_service import OntologyService

ADMIN = CurrentUser(userId="u1", roles=("admin",), departments=("d1",))


async def _enableGate(dbSession: AsyncSession, enabled: bool) -> None:
    dbSession.add(
        SystemConfig(
            key=SWITCH_GATE,
            value="true" if enabled else "false",
            description="KPI 发布闸门开关",
        )
    )
    await dbSession.commit()


async def _addSource(dbSession: AsyncSession, name: str) -> DataSource:
    ds = DataSource(
        name=name,
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


async def _seedMetric(dbSession: AsyncSession, **overrides) -> OntologyMetric:
    data = dict(
        metric_name="闸门测试指标",
        formula="SELECT SUM(QTY) FROM T",
        agg_function="SUM",
        target_class_id=None,
        dimension_defaults={},
        metric_type="MEASURE",
        numerator_measure={"agg": "SUM", "expr": "QTY"},
        denominator_measure=None,
        aggregation_order=None,
        grain_keys=["plant_code"],
        filter_expression=None,
        status="PUBLISHED",
        owner="tech",
        execution_source="COMPILED",
    )
    data.update(overrides)
    metric = OntologyMetric(**data)
    dbSession.add(metric)
    await dbSession.commit()
    await dbSession.refresh(metric)
    return metric


async def _seedKpi(dbSession: AsyncSession, **overrides) -> KpiCatalog:
    data = dict(
        kpi_code="KPI_GATE_SEED",
        kpi_name="闸门种子",
        business_definition="d",
        formula="SUM(x)",
        numerator="x",
        denominator=None,
        grain="g",
        unit="u",
        data_source="T",
        owner="o",
        version="1",
        status="DRAFT",
        metric_id=None,
        created_by="u",
        semantic_keywords=[],
    )
    data.update(overrides)
    kpi = KpiCatalog(**data)
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


# ── updateKpi 路径 ───────────────────────────────────────────────────


async def test_publish_kpi_without_metric_is_blocked(
    dbSession: AsyncSession,
) -> None:
    await _enableGate(dbSession, True)
    kpi = await _seedKpi(dbSession)

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.updateKpi(
            dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
        )
    assert "未挂靠本体指标" in str(exc.value)


async def test_publish_kpi_blocked_when_datasource_missing(
    dbSession: AsyncSession,
) -> None:
    """「齐备」用例刻意不绑数据源 ⇒ 命中条件 ⑤。"""
    await _enableGate(dbSession, True)
    metric = await _seedMetric(dbSession)
    kpi = await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_NO_DS",
        metric_id=metric.id,
        numerator_columns=["QTY"],
        grain_keys=["plant_code"],
    )

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.updateKpi(
            dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
        )
    assert "未绑定业务数据源" in str(exc.value)


async def test_publish_kpi_succeeds_when_all_conditions_met(
    dbSession: AsyncSession,
) -> None:
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_OK")
    metric = await _seedMetric(dbSession)
    kpi = await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_OK",
        datasource_id=ds.id,
        metric_id=metric.id,
        numerator_columns=["QTY"],
        grain_keys=["plant_code"],
    )

    service = KpiCatalogService()
    updated = await service.updateKpi(
        dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
    )
    assert updated.status == KpiStatus.PUBLISHED.value


async def test_gate_disabled_allows_publish(
    dbSession: AsyncSession,
) -> None:
    """开关关闭 ⇒ 只观察不阻断（灰度期）。"""
    await _enableGate(dbSession, False)
    kpi = await _seedKpi(dbSession, kpi_code="KPI_GATE_OFF")

    service = KpiCatalogService()
    updated = await service.updateKpi(
        dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
    )
    assert updated.status == KpiStatus.PUBLISHED.value


async def test_publish_error_message_lists_every_missing_item(
    dbSession: AsyncSession,
) -> None:
    """409 消息必须一次报全缺项（不是只报第一项）。"""
    await _enableGate(dbSession, True)
    kpi = await _seedKpi(dbSession, kpi_code="KPI_GATE_MULTI")

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.updateKpi(
            dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
        )
    message = str(exc.value)
    assert "未挂靠本体指标" in message
    assert "未绑定业务数据源" in message


async def test_publish_kpi_blocked_when_metric_not_published(
    dbSession: AsyncSession,
) -> None:
    """条件 ②：挂靠的指标自身不是 PUBLISHED ⇒ 拦下。"""
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_DRAFT")
    metric = await _seedMetric(dbSession, status="DRAFT")
    kpi = await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_METRIC_DRAFT",
        datasource_id=ds.id,
        metric_id=metric.id,
        numerator_columns=["QTY"],
        grain_keys=["plant_code"],
    )

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.updateKpi(
            dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
        )
    assert "本体指标未发布" in str(exc.value)


async def test_publish_kpi_blocked_when_governance_not_reconciled(
    dbSession: AsyncSession,
) -> None:
    """条件 ④：治理侧列与指标结构化列不一致 ⇒ 对账 mismatch ⇒ 拦下。"""
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_MISMATCH")
    metric = await _seedMetric(dbSession)
    kpi = await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_MISMATCH",
        datasource_id=ds.id,
        metric_id=metric.id,
        numerator_columns=["OTHER_COL"],
        grain_keys=["plant_code"],
    )

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.updateKpi(
            dbSession, kpi.id, KpiCatalogUpdate(status=KpiStatus.PUBLISHED), ADMIN
        )
    assert "口径对账不通过" in str(exc.value)


async def test_publish_kpi_uses_new_governance_columns(
    dbSession: AsyncSession,
) -> None:
    """同一次请求里「补治理列 + 发布」⇒ 按**新值**判定，不按旧口径。"""
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_NEWGOV")
    metric = await _seedMetric(dbSession)
    kpi = await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_NEWGOV",
        datasource_id=ds.id,
        metric_id=metric.id,
    )

    service = KpiCatalogService()
    updated = await service.updateKpi(
        dbSession,
        kpi.id,
        KpiCatalogUpdate(
            status=KpiStatus.PUBLISHED,
            numerator_columns=["QTY"],
            grain_keys=["plant_code"],
        ),
        ADMIN,
    )
    assert updated.status == KpiStatus.PUBLISHED.value


async def test_publish_kpi_blocked_when_metric_unlinked_in_same_request(
    dbSession: AsyncSession,
) -> None:
    """同一次请求里「解绑指标 + 发布」⇒ 必须按新值（无指标）判定并拦下。"""
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_UNLINK")
    metric = await _seedMetric(dbSession)
    kpi = await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_UNLINK",
        datasource_id=ds.id,
        metric_id=metric.id,
        numerator_columns=["QTY"],
        grain_keys=["plant_code"],
    )

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.updateKpi(
            dbSession,
            kpi.id,
            KpiCatalogUpdate(status=KpiStatus.PUBLISHED, metric_id=None),
            ADMIN,
        )
    assert "未挂靠本体指标" in str(exc.value)


# ── createKpi 路径 ───────────────────────────────────────────────────


async def test_create_kpi_published_without_metric_is_blocked(
    dbSession: AsyncSession,
) -> None:
    """createKpi 也要过闸；失败不留半条记录。"""
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_CREATE")

    service = KpiCatalogService()
    with pytest.raises(ConflictError) as exc:
        await service.createKpi(
            dbSession,
            KpiCatalogCreate(
                kpi_code="KPI_GATE_CREATE_BLOCKED",
                kpi_name="创建即发布",
                datasource_id=ds.id,
                status=KpiStatus.PUBLISHED,
            ),
            ADMIN,
        )
    assert "未挂靠本体指标" in str(exc.value)

    rows = await dbSession.execute(
        select(KpiCatalog).where(KpiCatalog.kpi_code == "KPI_GATE_CREATE_BLOCKED")
    )
    assert rows.scalars().all() == []


async def test_create_kpi_published_succeeds_when_all_conditions_met(
    dbSession: AsyncSession,
) -> None:
    await _enableGate(dbSession, True)
    ds = await _addSource(dbSession, "GATE_DS_CREATE_OK")
    metric = await _seedMetric(dbSession, metric_name="创建闸门指标")

    service = KpiCatalogService()
    created = await service.createKpi(
        dbSession,
        KpiCatalogCreate(
            kpi_code="KPI_GATE_CREATE_OK",
            kpi_name="创建即发布（齐备）",
            datasource_id=ds.id,
            metric_id=metric.id,
            status=KpiStatus.PUBLISHED,
            numerator_columns=["QTY"],
            grain_keys=["plant_code"],
        ),
        ADMIN,
    )
    assert created.status == KpiStatus.PUBLISHED.value


# ── 本体指标方向 ─────────────────────────────────────────────────────


async def test_create_metric_published_without_structure_is_blocked(
    dbSession: AsyncSession,
) -> None:
    """创建即 PUBLISHED 也要过闸；失败不留半条记录。"""
    await _enableGate(dbSession, True)

    service = OntologyService()
    with pytest.raises(ConflictError) as exc:
        await service.createMetric(
            dbSession,
            OntologyMetricCreate(
                metric_name="闸门缺结构指标",
                formula="SELECT SUM(QTY) FROM T",
                agg_function="SUM",
                status=MetricStatus.PUBLISHED,
            ),
            actor="u1",
        )
    message = str(exc.value)
    assert "未配置分子度量" in message
    assert "未配置结构化粒度键" in message

    rows = await dbSession.execute(
        select(OntologyMetric).where(OntologyMetric.metric_name == "闸门缺结构指标")
    )
    assert rows.scalars().all() == []


async def test_update_metric_published_blocked_when_linked_kpi_mismatch(
    dbSession: AsyncSession,
) -> None:
    """关联 KPI 口径 mismatch ⇒ 指标发布被拦（unknown 放行，只拦 mismatch）。"""
    await _enableGate(dbSession, True)
    metric = await _seedMetric(
        dbSession,
        metric_name="闸门关联失配指标",
        status="DRAFT",
    )
    # 治理侧声明的分子列与指标结构化列不一致 ⇒ 层 2 mismatch
    await _seedKpi(
        dbSession,
        kpi_code="KPI_GATE_LINKED_MISMATCH",
        metric_id=metric.id,
        numerator_columns=["OTHER_COL"],
        grain_keys=["plant_code"],
    )

    service = OntologyService()
    with pytest.raises(ConflictError) as exc:
        await service.updateMetric(
            dbSession,
            metric.id,
            OntologyMetricUpdate(status=MetricStatus.PUBLISHED),
            actor="u1",
        )
    assert "关联 KPI 口径对账不通过" in str(exc.value)

    # 闸门失败 ⇒ 实体保持原状（不留下「内存已改、库里没改」的脏状态）
    row = await dbSession.get(OntologyMetric, metric.id)
    await dbSession.refresh(row)
    assert row.status == "DRAFT"


async def test_update_metric_published_uses_new_structure(
    dbSession: AsyncSession,
) -> None:
    """同一请求里「补结构化列 + 发布」⇒ 按**新值**判定。"""
    await _enableGate(dbSession, True)
    metric = await _seedMetric(
        dbSession,
        metric_name="闸门补结构指标",
        status="DRAFT",
        numerator_measure=None,
        grain_keys=None,
    )

    service = OntologyService()
    updated = await service.updateMetric(
        dbSession,
        metric.id,
        OntologyMetricUpdate(
            status=MetricStatus.PUBLISHED,
            numerator_measure={"agg": "SUM", "expr": "QTY"},
            grain_keys=["plant_code"],
        ),
        actor="u1",
    )
    assert updated.status == MetricStatus.PUBLISHED.value
    assert updated.grain_keys == ["plant_code"]


async def test_update_metric_published_succeeds_when_all_conditions_met(
    dbSession: AsyncSession,
) -> None:
    await _enableGate(dbSession, True)
    metric = await _seedMetric(
        dbSession,
        metric_name="闸门齐备指标",
        status="DRAFT",
    )

    service = OntologyService()
    updated = await service.updateMetric(
        dbSession,
        metric.id,
        OntologyMetricUpdate(status=MetricStatus.PUBLISHED),
        actor="u1",
    )
    assert updated.status == MetricStatus.PUBLISHED.value


async def test_update_metric_published_blocked_when_ratio_missing_denominator(
    dbSession: AsyncSession,
) -> None:
    """RATIO 型缺分母 ⇒ 结构化不齐 ⇒ 拦下。"""
    await _enableGate(dbSession, True)
    metric = await _seedMetric(
        dbSession,
        metric_name="闸门比率指标",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        denominator_measure=None,
        status="DRAFT",
    )

    service = OntologyService()
    with pytest.raises(ConflictError) as exc:
        await service.updateMetric(
            dbSession,
            metric.id,
            OntologyMetricUpdate(status=MetricStatus.PUBLISHED),
            actor="u1",
        )
    assert "未配置分母度量" in str(exc.value)

