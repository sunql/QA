"""知识图谱多跳推理 REST API（Phase 6.3 feat-graph-traversal-api）。

挂在 /api/v1/graph：
  GET /api/v1/graph/traverse   多跳遍历（start_type + start_key + max_hops）

只读端点；所有登录用户可查询（业务图无行级敏感数据，敏感过滤在
supplier_360 / supplier_risk 路径完成）。Neo4j 不可达 -> 503 降级提示。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import CurrentUser, getCurrentUser
from app.domain.exceptions import DomainError
from app.domain.schemas import GraphTraversalRead
from app.infrastructure.neo4j_client import (
    BUSINESS_ENTITY_LABELS,
    _MAX_TRAVERSAL_HOPS,
)
from app.services.graph_traversal_service import GraphTraversalService

logger = logging.getLogger(__name__)

router = APIRouter()

# 下拉合法值（前端 Select options 与 OpenAPI 文档共用）
VALID_START_TYPES = sorted(BUSINESS_ENTITY_LABELS)


def getGraphTraversalService() -> GraphTraversalService:
    return GraphTraversalService()


@router.get("/traverse", response_model=GraphTraversalRead)
async def traverse(
    _user: CurrentUser = Depends(getCurrentUser),
    startType: str = Query(
        default="Supplier",
        description="业务实体类型：Supplier | Material | PurchaseOrder | "
        "GoodsReceipt | IncomingInspection | NCR | Contract",
    ),
    startKey: str = Query(description="实体键（enterprise_key / document_id）"),
    maxHops: int = Query(default=3, ge=1, le=_MAX_TRAVERSAL_HOPS),
    service: GraphTraversalService = Depends(getGraphTraversalService),
) -> GraphTraversalRead:
    """从起始业务实体做多跳遍历，返回逐跳展开的可达链。"""
    if startType not in BUSINESS_ENTITY_LABELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid startType: {startType!r}，合法值：{VALID_START_TYPES}",
        )
    try:
        return await service.traverse(startType, startKey, maxHops)
    except ValueError as exc:
        # maxHops 越界（Query 校验兜底；service 层二次防御）
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DomainError:
        # NotFoundError 等业务异常 -> 交给全局 DomainError handler（404 等）
        raise
    except Exception as exc:  # noqa: BLE001 - driver 异常兜底为 503（不暴露堆栈）
        logger.warning("graph traverse failed: %s", exc)
        raise HTTPException(
            status_code=503, detail="图遍历执行失败，请稍后重试"
        ) from exc
