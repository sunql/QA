"""P0：KPI–数据源批量绑定（admin API 与回填脚本共用的单一写入口）。

写路径统一走 ``KpiCatalogService.updateKpi``，因此校验（数据源存在且启用）、
ACL、outbox → audit_log / history、匹配缓存刷新全部复用单条更新语义。

语义（与 entity-mappings/bulk 同模式）：
- 逐行处理、逐行 commit —— 部分成功是设计语义，响应逐行交代
- ``bound``：写入成功
- ``skipped``：已绑定同一数据源（零副作用，不产生 revision/审计噪声）
- ``failed``：KPI 编码不存在 / 数据源不存在或停用 / 已绑其他数据源且
  ``force=False`` / ACL 拒绝
- 单行失败不中断整批
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.domain.schemas import (
    KpiCatalogUpdate,
    KpiDatasourceBulkBindItem,
    KpiDatasourceBulkBindResult,
    KpiDatasourceBulkBindResultRow,
)
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.messages_zh import (
    MSG_KPI_BULK_BIND_ADMIN_REQUIRED,
    MSG_KPI_BULK_BIND_ALREADY_BOUND,
    MSG_KPI_BULK_BIND_TOO_MANY,
    MSG_KPI_CATALOG_CODE_NOT_FOUND,
)

BULK_BIND_MAX_ROWS = 1000


class KpiDatasourceBindingService:
    def __init__(self) -> None:
        self._catalog = KpiCatalogService()

    async def bulkBind(
        self,
        session: AsyncSession,
        items: list[KpiDatasourceBulkBindItem],
        actor: CurrentUser,
        *,
        force: bool = False,
    ) -> KpiDatasourceBulkBindResult:
        """批量绑定 KPI → 数据源；返回逐行结果与计数。"""
        # 防御纵深：路由已用 getAdminOnlyActor 拦截，service 自身 fail-closed，
        # 防止将来被脚本/内部调用绕过路由时静默获得批量写权限。
        if "admin" not in (actor.roles or ()):
            raise PermissionDeniedError(MSG_KPI_BULK_BIND_ADMIN_REQUIRED)
        if len(items) > BULK_BIND_MAX_ROWS:
            raise ValidationError(
                MSG_KPI_BULK_BIND_TOO_MANY.format(
                    limit=BULK_BIND_MAX_ROWS, actual=len(items)
                )
            )

        results: list[KpiDatasourceBulkBindResultRow] = []
        bound = skipped = failed = 0

        for idx, item in enumerate(items, start=1):
            row = await self._bindOne(session, idx, item, actor, force=force)
            results.append(row)
            if row.status == "bound":
                bound += 1
            elif row.status == "skipped":
                skipped += 1
            else:
                failed += 1

        return KpiDatasourceBulkBindResult(
            total=len(items),
            bound=bound,
            skipped=skipped,
            failed=failed,
            results=results,
        )

    async def _bindOne(
        self,
        session: AsyncSession,
        row: int,
        item: KpiDatasourceBulkBindItem,
        actor: CurrentUser,
        *,
        force: bool,
    ) -> KpiDatasourceBulkBindResultRow:
        kpi = await self._catalog.get_by_code(session, item.kpi_code)
        if kpi is None:
            return KpiDatasourceBulkBindResultRow(
                row=row,
                kpi_code=item.kpi_code,
                status="failed",
                datasource_id=item.datasource_id,
                error=MSG_KPI_CATALOG_CODE_NOT_FOUND.format(code=item.kpi_code),
            )
        if kpi.datasource_id == item.datasource_id:
            return KpiDatasourceBulkBindResultRow(
                row=row,
                kpi_code=item.kpi_code,
                status="skipped",
                kpi_id=kpi.id,
                datasource_id=item.datasource_id,
                reason="already_bound",
            )
        if kpi.datasource_id is not None and not force:
            return KpiDatasourceBulkBindResultRow(
                row=row,
                kpi_code=item.kpi_code,
                status="failed",
                kpi_id=kpi.id,
                datasource_id=item.datasource_id,
                error=MSG_KPI_BULK_BIND_ALREADY_BOUND.format(
                    code=item.kpi_code, id=kpi.datasource_id
                ),
            )

        try:
            await self._catalog.updateKpi(
                session,
                kpi.id,
                KpiCatalogUpdate(datasource_id=item.datasource_id),
                actor,
            )
        except (ValidationError, NotFoundError, PermissionDeniedError) as exc:
            return KpiDatasourceBulkBindResultRow(
                row=row,
                kpi_code=item.kpi_code,
                status="failed",
                kpi_id=kpi.id,
                datasource_id=item.datasource_id,
                error=str(exc),
            )
        return KpiDatasourceBulkBindResultRow(
            row=row,
            kpi_code=item.kpi_code,
            status="bound",
            kpi_id=kpi.id,
            datasource_id=item.datasource_id,
        )
