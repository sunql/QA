"""采购只读工具的数据源 / adapter 中性入口（2026-11-25，I7 解环）。

背景：`pending_delivery_query` 曾在函数内惰性 import
`agent_tools_procurement._adapterFor` / `_loadActiveDatasource`，而后者又在
模块级 import 前者 ⇒ 双向依赖。三个函数抽到本模块后依赖单向：
两个工具都 → 本模块（本模块不 import 任何工具）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError
from app.domain.models import DataSource


def adapterFor(dsId: int, ds: DataSource) -> Any:
    """业务库 adapter 获取（惰性导入 `business_db_pool`，保持 CI 收集期闭包干净）。"""
    from app.infrastructure.business_db_pool import get_adapter

    return get_adapter(dsId, ds)


def ensureDatasourceActive(ds: DataSource) -> None:
    """停用源 fail-closed（`DataSourceInactiveError` 是 `ConflictError` ⇒ 409）。"""
    from app.services.datasource_service import ensureDatasourceActive as impl

    impl(ds)


async def loadActiveDatasource(
    session: AsyncSession, datasourceId: int
) -> DataSource:
    """按 id 取数据源；**不存在与停用统一 409**（配置/环境问题，不是用户输入）。

    旧实现两处不一致：待到货缺行 ⇒ 409，到货跟踪经 `DataSourceService.get`
    ⇒ 404。同一个「THBI 数据源缺失」给两种码，排障时先被 404 误导。
    """
    ds = (
        await session.execute(select(DataSource).where(DataSource.id == datasourceId))
    ).scalars().first()
    if ds is None:
        raise ConflictError(f"数据源不存在（id={datasourceId}）")
    ensureDatasourceActive(ds)
    return ds
