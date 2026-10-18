"""I1-I3 KPI Catalog datasource 校验（per F2 spec §5.2）。

注意：KpiCatalogService.createKpi / updateKpi 签名是
  (session, dto: KpiCatalogCreate|KpiCatalogUpdate, actor: CurrentUser)
不是 dict。构造 DTO 实例后传，actor 用默认 CurrentUser()（含 admin role → ACL 通过）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ValidationError as DomainValidationError
from app.domain.models import DataSource, KpiCatalog
from app.domain.schemas import KpiCatalogCreate, KpiCatalogUpdate
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.messages_zh import (
    MSG_KPI_DATASOURCE_INACTIVE,
    MSG_KPI_DATASOURCE_NOT_FOUND,
)


@pytest.mark.asyncio
async def test_createKpi_with_nonexistent_datasource_id_returns_422(
    dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """I1: datasource_id=不存在 → 422 + MSG_KPI_DATASOURCE_NOT_FOUND。"""
    svc = KpiCatalogService()
    current_user = CurrentUser()  # default roles 含 admin → ACL 通过
    dto = KpiCatalogCreate(
        kpi_code="TEST_I1", kpi_name="test",
        business_definition="d", formula="SUM(x)",
        numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o",
        version="1",  # 注意：DTO 期望 str，不是 int
        metric_id=None,
        created_by="u", semantic_keywords=[], match_threshold=0.5,
        datasource_id=99999,  # 不存在
    )
    with pytest.raises(DomainValidationError) as exc_info:
        await svc.createKpi(dbSession, dto, current_user)
    assert MSG_KPI_DATASOURCE_NOT_FOUND.format(id=99999) in str(exc_info.value)


@pytest.mark.asyncio
async def test_createKpi_with_inactive_datasource_id_returns_422(
    dbSession: AsyncSession, seed_inactive_data_source: DataSource,
) -> None:
    """I2: datasource_id=is_active=false → 422 + MSG_KPI_DATASOURCE_INACTIVE。"""
    svc = KpiCatalogService()
    current_user = CurrentUser()
    ds_id = seed_inactive_data_source.id
    dto = KpiCatalogCreate(
        kpi_code="TEST_I2", kpi_name="test",
        business_definition="d", formula="SUM(x)",
        numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o",
        version="1",  # str
        metric_id=None,
        created_by="u", semantic_keywords=[], match_threshold=0.5,
        datasource_id=ds_id,
    )
    with pytest.raises(DomainValidationError) as exc_info:
        await svc.createKpi(dbSession, dto, current_user)
    assert MSG_KPI_DATASOURCE_INACTIVE.format(id=ds_id, name="INACTIVE_TEST_DS") in str(exc_info.value)


@pytest.mark.asyncio
async def test_updateKpi_with_none_datasource_id_skips_validation(
    dbSession: AsyncSession, seed_data_source: DataSource, sample_kpi: KpiCatalog,
) -> None:
    """I3: datasource_id=None 跳过 datasource 存在/is_active 校验（spec §4.2）。

    顺带验证 spec §4.1：显式 null 解绑 → entity.datasource_id 变 None。
    """
    svc = KpiCatalogService()
    current_user = CurrentUser()
    kpi = sample_kpi
    # 校验起点：kpi 本来绑了 seed_data_source
    assert kpi.datasource_id == seed_data_source.id
    update = KpiCatalogUpdate(datasource_id=None, kpi_name="updated_name")
    result = await svc.updateKpi(dbSession, kpi.id, update, current_user)
    # 显式 null 解绑（spec §4.1）
    assert result.datasource_id is None
    # 其他字段正常更新
    assert result.kpi_name == "updated_name"


@pytest.mark.asyncio
async def test_createKpi_persists_datasource_id_on_valid_input(
    dbSession: AsyncSession, seed_data_source: DataSource,
) -> None:
    """Happy path（spec I1-I5 都是异常路径，无 happy-path → 容易漏持久化字段）。

    F2 §4.2 关键：createKpi 校验通过后必须把 datasource_id 写入列。
    subagent Task 3 漏写 KpiCatalog(...) 实体的 datasource_id 字段，本 test 锁住。
    """
    svc = KpiCatalogService()
    current_user = CurrentUser()
    dto = KpiCatalogCreate(
        kpi_code="TEST_HAPPY", kpi_name="happy",
        business_definition="d", formula="SUM(x)",
        numerator="x", denominator=None, grain="g",
        unit="u", data_source="legacy_text", owner="o",
        version="1", metric_id=None,
        created_by="u", semantic_keywords=[], match_threshold=0.5,
        datasource_id=seed_data_source.id,
    )
    entity = await svc.createKpi(dbSession, dto, current_user)
    assert entity.datasource_id == seed_data_source.id
    # 二次从 DB 读，验证真的落库（不是 SQLAlchemy session 缓存）
    reloaded = await dbSession.get(KpiCatalog, entity.id)
    assert reloaded is not None
    assert reloaded.datasource_id == seed_data_source.id
