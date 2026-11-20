"""M2-2 采购实体解析：`GET /api/v1/procurement/knowledge/entities/search`。

设计稿 §8.3：`q / entityType / limit` ⇒ `enterpriseKey / enterpriseCode /
sourceMappings[] / matchScore`。检索语义复用 `EntityMappingService.searchMappings`
（AutoComplete 同款：全数字精确 key + ILIKE 兜底 + trigram 索引）；
分组/打分在 `services/knowledge/entity_search.buildEntityMatches`（纯函数）。

⚠️ 与 `procurement_knowledge.py`（薄别名，AST 守卫禁逻辑）不同：本文件**有**
业务编排（检索→分组→响应），但逻辑只有一份（builder 纯函数 + 既有 service）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.knowledge_schemas import ProcurementEntityMatchRead
from app.services.entity_mapping_service import EntityMappingService
from app.services.knowledge.entity_search import buildEntityMatches

router = APIRouter(tags=["procurement-knowledge"])


@router.get(
    "/entities/search",
    response_model=list[ProcurementEntityMatchRead],
)
async def searchProcurementEntities(
    response: Response,
    q: str = Query(min_length=1, max_length=100),
    entityType: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=20, ge=1, le=100),
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[ProcurementEntityMatchRead]:
    """实体解析：编码/名称/统一键 → 统一实体 + 全部源系统映射 + 命中强度。"""
    response.headers["Cache-Control"] = "no-store"
    rows = await EntityMappingService().searchMappings(
        session, q=q, entityType=entityType, limit=limit
    )
    return buildEntityMatches(rows, q=q)
