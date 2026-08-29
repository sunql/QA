"""系统级只读端点。

GET /api/v1/system/status   核心依赖服务状态（PG/Neo4j/Milvus/Embedding）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.dependencies import getCurrentUser
from app.domain.schemas import ServiceStatusResponse
from app.services.service_status_service import ServiceStatusService

router = APIRouter(dependencies=[Depends(getCurrentUser)])


@router.get("/status", response_model=ServiceStatusResponse, tags=["system"])
async def getServiceStatus() -> ServiceStatusResponse:
    """返回核心依赖服务的连通性状态（每项探测独立、并发、带超时）。"""
    return await ServiceStatusService().checkAll()
