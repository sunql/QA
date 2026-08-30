"""本地导入 API 路由。

POST /api/v1/datasources/{datasourceId}/import-preview  生成导入预览（建议类/属性/关联）
POST /api/v1/datasources/{datasourceId}/import          执行导入（落库本体类/属性/关联）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    ImportExecuteRequest,
    ImportExecuteResponse,
    ImportPreviewRequest,
    ImportPreviewResponse,
)
from app.services.local_import_service import LocalImportService

router = APIRouter(dependencies=[Depends(getCurrentUser)])

# 生产默认实例：内部会构造真实 SchemaIntrospectionService 等依赖。
# 测试通过 app.state.localImportService 注入替换（避免连接不可达的业务数据源）。
_defaultService = LocalImportService()


def getLocalImportService(request: Request) -> LocalImportService:
    """读取 app.state 注入的 service，未注入时回退到模块级默认实例。"""
    service = getattr(request.app.state, "localImportService", None)
    return service if service is not None else _defaultService


@router.post("/{datasourceId}/import-preview", response_model=ImportPreviewResponse)
async def importPreview(
    datasourceId: int,
    payload: ImportPreviewRequest,
    session: AsyncSession = Depends(getDb),
    service: LocalImportService = Depends(getLocalImportService),
) -> ImportPreviewResponse:
    return await service.build_preview(
        session, datasourceId, payload.rules, selected_tables=payload.selected_tables
    )


@router.post("/{datasourceId}/import", response_model=ImportExecuteResponse)
async def importExecute(
    datasourceId: int,
    payload: ImportExecuteRequest,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: LocalImportService = Depends(getLocalImportService),
) -> ImportExecuteResponse:
    return await service.execute_import(
        session, datasourceId, payload, created_by=user.userId, actor=user
    )
