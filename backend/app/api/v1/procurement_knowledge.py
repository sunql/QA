"""P4 采购薄别名：`POST /api/v1/procurement/knowledge/query`。

**这个文件不许有任何业务逻辑**（G5 第二条判据：「采购薄别名无逻辑」）。

它做的**全部**事情：
1. 读请求体（契约与主接口逐字一致，只是少了 `domain`）；
2. 补上固定的域 `PROCUREMENT`；
3. 转交同一个编排器 `KnowledgeFacade`；
4. 原样返回同一个响应体。

为什么要有它：跨板块扩展文档写的是「**如需冻结路径**，加
`/api/v1/procurement/knowledge/query` 薄别名（只做 domain 固定转发）」⇒
给已经冻结了采购侧调用路径的集成方一个稳定入口，而**逻辑只有一份**。

⇒ 判据是**可检查的**：本模块的源码里**不许出现任何分支 / 循环**
（`if` / `for` / `while` / `match`）⇒ 由
`app/tests/unit/test_knowledge_procurement_alias.py` 的 AST 守卫钉住。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.knowledge_facade import (
    KNOWLEDGE_QUERY_LIMIT,
    KNOWLEDGE_QUERY_LIMIT_SCOPE,
    knowledgeFacade,
)
from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.knowledge_schemas import (
    KnowledgeQueryResponse,
    ProcurementKnowledgeQueryRequest,
)
from app.infrastructure.rate_limit import enforceRouteLimit

router = APIRouter(tags=["procurement-knowledge"])


@router.post("/query", response_model=KnowledgeQueryResponse)
async def queryProcurementKnowledge(
    request: Request,
    body: ProcurementKnowledgeQueryRequest,
    response: Response,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> KnowledgeQueryResponse:
    """采购域知识查询（薄别名）：域固定为采购域，其余与主接口完全一致。"""
    enforceRouteLimit(request, KNOWLEDGE_QUERY_LIMIT, KNOWLEDGE_QUERY_LIMIT_SCOPE)
    response.headers["Cache-Control"] = "no-store"
    return await knowledgeFacade.query(
        body.toCanonicalRequest(), user=user, session=session
    )
