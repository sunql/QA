"""P4 知识服务 Facade 的 REST 端点（D32 契约层）。

- `POST /api/v1/semantic/knowledge/query` —— 跨域通用入口（**跨板块扩展文档 §6 P4** 的指定接口）。
- 采购薄别名在 `app/api/v1/procurement_knowledge.py`（**只做 domain 固定转发**）。

门禁分层
--------
- **登录即可**（`getCurrentUser`）：本组是**只读**知识编排面。
  但**读也要按权限收窄** ⇒ 域级可见性由服务层的 `permission.resolveKnowledgeScope` 判定
  （不可见的域 ⇒ 空结果 + 警告，**不是 403**：验收口径是「无权限**查不到**」，
  403 会把「域不可见」与「域不存在」混为一谈，还给探测者一个枚举域的状态码）。
- 路由级 `mustChangePasswordGuard` + `rateLimitDep` 由 `main.py` / `_testapp.py`
  在 `include_router` 时统一挂上（与仓内其余 router 一致）。
- 端点内再显式限流一次（`enforceRouteLimit`）：作用域用**领域常量**而不是 URL 路径
  （挂载前缀会变，路径做键会静默失效 —— 见 `infrastructure/rate_limit.py` 的说明）。

⚠️ 响应一律 `Cache-Control: no-store`：知识查询结果含业务语义口径，
不进浏览器 / 代理缓存（与 `ontology_discovery` 的做法一致）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.knowledge_schemas import KnowledgeQueryRequest, KnowledgeQueryResponse
from app.infrastructure.rate_limit import enforceRouteLimit
from app.services.knowledge.facade import KnowledgeFacade

#: 端点级限流桶（领域:动作 常量，**不用 URL 路径**）。
KNOWLEDGE_QUERY_LIMIT = "30/minute"
KNOWLEDGE_QUERY_LIMIT_SCOPE = "knowledge:query"

router = APIRouter(tags=["semantic-knowledge"])

#: 编排器单例（无状态）。
knowledgeFacade = KnowledgeFacade()


def _noStore(response: Response) -> None:
    """知识查询结果不进浏览器 / 代理缓存。"""
    response.headers["Cache-Control"] = "no-store"


@router.post("/query", response_model=KnowledgeQueryResponse)
async def queryKnowledge(
    request: Request,
    body: KnowledgeQueryRequest,
    response: Response,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> KnowledgeQueryResponse:
    """跨域知识查询：一次查询内编排 图谱 / 指标 / RAG / SQL 四路。

    Args:
        request: 原始请求（端点级限流按它取桶键）。
        body: 契约请求体（域 / 问题 / 通道 / 实体提示 / topK / 钉住 / 收窄 scope）。
        response: 响应对象（写 no-store 头）。
        user: 当前调用方（权限口径的输入）。
        session: 数据库会话。

    Returns:
        `KnowledgeQueryResponse`：发布引用 + 编排摘要 + 实体 + 证据 + 每路状态。
    """
    enforceRouteLimit(request, KNOWLEDGE_QUERY_LIMIT, KNOWLEDGE_QUERY_LIMIT_SCOPE)
    _noStore(response)
    return await knowledgeFacade.query(body, user=user, session=session)
