"""MCP Server（feat-wiki-knowledge，Phase 6）。

把 qa-system 的知识能力暴露给外部 LLM Agent（Claude Desktop / Cursor /
其他 MCP 客户端），避免每个 Agent 重复实现 wiki 检索 / 读取。

## 启动方式

1. **stdio 模式**（Claude Desktop 等）：

    ```bash
    uv run python -m app.services.mcp_server --transport stdio
    ```

    配置文件示例（claude_desktop_config.json）：

    ```json
    {
      "mcpServers": {
        "qa-system": {
          "command": "uv",
          "args": ["--directory", "/path/to/backend", "run",
                   "python", "-m", "app.services.mcp_server"],
          "env": {"MCP_USER_ID": "<本机用户名>", "DATABASE_URL": "..."}
        }
      }
    }
    ```

    stdio 没有 HTTP 头，身份由 ``MCP_USER_ID`` 注入（默认 ``anonymous``）——
    这是本机进程身份，不走 Bearer 鉴权。

2. **HTTP/SSE 模式**（已挂到 FastAPI app `/mcp`）—— 见 ``app/main.py`` 的
   ``createApp()`` 末尾 Mount 子 app + 合并 lifespan。HTTP 入口统一经
   ``McpAuthMiddleware`` 校验 Bearer（CLI ``--transport http`` 亦同）。

## 暴露的工具（10 个）

**只读（5 个）**：
- ``wiki_status`` — 健康检查 + 当前用户
- ``wiki_search`` — 按标题/正文模糊检索
- ``wiki_read`` — 读单条 page 全文 + claims + relations
- ``wiki_graph_insights`` — 拉取 Phase 3 graph insights
- ``wiki_graph_communities`` — 拉取 Louvain 社区列表

**两步预览（3 个）**：与 Phase 5.5 知识缺口操作入口对齐
- ``wiki_preview_classify`` — MISSING_DIMENSION 预览
- ``wiki_preview_relations`` — ISOLATED_PAGE 预览
- ``wiki_preview_community_topic`` — SPARSE_COMMUNITY 预览

**写入（2 个）**：MCP 写操作都进学习反馈 + 审计，与前端 PATCH 路径等价
- ``wiki_update_dimension`` — PATCH dimension + autoClassification
- ``wiki_update_community_topic`` — PATCH community topic

## 设计约束

1. **不复用 REST HTTP 调用** —— MCP tool 直接走 service 层，避免 HTTP 循环
   （哪怕是 localhost 自调），也避开跨进程的认证 schema 漂移
2. **身份来自 ContextVar，工具不自己造身份** —— ``/mcp`` 的 HTTP 路径由
   ``app/api/mcp_auth.py`` 的 ``McpAuthMiddleware`` 校验 Bearer 后写入
   ``mcpCurrentUser``；stdio 路径没有 HTTP 头，用环境变量 ``MCP_USER_ID``
   （默认 ``anonymous``）。两条路径的身份来源不同，见 ``_resolveCurrentUserId``。
   生产部署的 HTTP 入口必须经 ``McpAuthMiddleware``（``main.py`` 的 ``/mcp``
   挂载与 CLI ``--transport http`` 都已接上）
3. **响应限长** —— 单条返回最多 50KB（fastmcp 默认），超过截断，避免把 LLM
   上下文塞爆
4. **写操作必须有 preview** —— 与 Phase 5.5 「两步预览」一致：MCP 客户端拿到
   ``wiki_preview_*`` 的建议后再调 ``wiki_update_*`` 写库
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from fastmcp import Context, FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.mcp_auth import mcpCurrentUser
from app.dependencies import DEFAULT_STUB_USER_ID
from app.domain.exceptions import NotFoundError
from app.domain.wiki_models import (
    KnowledgeCommunity,
    KnowledgeRelation,
    WikiPage,
)
from app.services.learning.community_topic_suggester import (
    CommunityTopicSuggester,
    loadCommunityTitles,
)
from app.services.learning.llm_invoker import LearningLLMInvoker
from app.services.model_config_service import ModelConfigService
from app.services.wiki_page_service import WikiPageService

logger = logging.getLogger(__name__)

# 单次响应的硬上限（字符）—— LLM 上下文塞太满会被截断且用户感知不到
_MAX_RESPONSE_CHARS = 50_000

# search/insights 的默认上限
_SEARCH_LIMIT_DEFAULT = 5
_INSIGHTS_LIMIT = 10

# stdio 模式下的默认用户名（生产部署必须显式覆盖）
_DEFAULT_USER_ID = os.environ.get("MCP_USER_ID", DEFAULT_STUB_USER_ID)


def _resolveCurrentUserId() -> str:
    """解析当前 MCP 调用的用户 ID。

    **两条路径，身份来源不同**（2026-09-30 安全批次）：

    1. **HTTP/SSE（``/mcp``）**：``McpAuthMiddleware`` 校验 ``Authorization:
       Bearer <jwt>`` 成功后把 ``CurrentUser`` 写进 ``mcpCurrentUser``
       ContextVar。这是 HTTP 路径**唯一**被信任的身份来源 —— 工具函数只读它。
    2. **stdio / 进程内直接调用**：没有 HTTP 请求，也就没有中间件与 Bearer
       头，用 ``MCP_USER_ID``（默认 ``anonymous``）。这是**本机进程**的身份，
       与 HTTP 路径的 Bearer 身份无关，不具备跨进程效力。

    路径 1 不可能落到路径 2：中间件包裹了 ``/mcp`` 与 CLI ``--transport http``
    两个 HTTP 入口，未通过鉴权的请求在抵达 tool 之前就被 403 拦下。新增 HTTP
    挂载点时**必须**同样包上 ``McpAuthMiddleware``，否则就是静默降级为
    ``anonymous``（这正是本批次要消灭的洞）。
    """
    current = mcpCurrentUser.get()
    if current is not None:
        return current.userId
    return _DEFAULT_USER_ID


@dataclass
class McpContext:
    """MCP tool 内部上下文：DB session + 当前用户。

    与 fastmcp 的 ``Context`` 不同 —— fastmcp.Context 来自框架本身，承载
    request state。我们自己这套 ``McpContext`` 在 tool 内部按需拉取：

    - ``dbSession``：直接进 service 层（不走 HTTP 调用，避免循环）。
      ``async with getSessionFactory()`` 已经在 ``_openContext`` 里包了，
      session close 由其 ``__aexit__`` 触发；这里的 ``close()`` 只是防御性
      兜底（万一 ``async with`` 块异常退出 + tool 没让异常冒到 ``finally``）。
    - ``currentUserId``：HTTP 路径由 ``McpAuthMiddleware`` 鉴权后写 ContextVar；
      stdio 路径由 ``MCP_USER_ID`` 注入（见 ``_resolveCurrentUserId``）
    """

    dbSession: AsyncSession
    currentUserId: str

    async def close(self) -> None:
        # async_sessionmaker 的 __aexit__ 已经 close 过了；这里再 close 是
        # 幂等的（SQLAlchemy 内部判 in_transaction 后 noop），保证 tool 的
        # ``finally`` 写 ``await mcpCtx.close()`` 不重复也无害。
        await self.dbSession.close()


async def _openContext() -> McpContext:
    """开一次 DB session + 解析当前用户身份。

    **不用 ``getDb()`` 依赖**：MCP 工具函数不是 FastAPI 路由，无法走 Depends。
    直接调 ``getSessionFactory()`` 的 ``async with`` 进入 session，把 commit /
    rollback / close 拆到 tool 的 ``finally`` 里手动管理（与 getDb 异常分支
    语义一致：失败 rollback、始终 close；成功路径需 tool 自己 commit）。

    身份只从 ContextVar 读（见 ``_resolveCurrentUserId``）—— **不再**自己构造
    stub 身份、**不再**直接调 ``getCurrentUser``。后者是 FastAPI 依赖函数，
    未传的参数拿到的是 ``Header(...)`` 哨兵对象（不是 ``None``），
    ``authorization.lower()`` 必抛 ``AttributeError``，曾让全部 10 个工具崩溃。

    Returns:
        McpContext：含 ``dbSession`` 和 ``currentUserId``。调用方负责 ``commit()``
        （写入场景）+ ``close()``（在 ``finally`` 里）。
    """
    from app.infrastructure.database import getSessionFactory

    factory = getSessionFactory()
    async with factory() as session:
        return McpContext(
            dbSession=session, currentUserId=_resolveCurrentUserId()
        )


def _truncate(text: str, limit: int = _MAX_RESPONSE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated: {len(text) - limit} bytes omitted]"


def _json(data: Any, limit: int = _MAX_RESPONSE_CHARS) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str, indent=2)
    return _truncate(text, limit)


# ---------------------------------------------------------------------------
# MCP Server 定义
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="qa-system-wiki",
    instructions=(
        "qa-system 企业知识图谱的 MCP 入口。支持只读检索（search/read/insights）"
        " + 受控写入（update dimension / community topic）。所有写操作对应"
        "Phase 5.5 两步预览原则：先用 preview 工具拿到建议，确认后再用"
        "update 工具落库。"
    ),
)


@mcp.tool(
    name="wiki_status",
    description="健康检查：DB 可达 + 当前调用者身份。HTTP 入口需 Bearer 鉴权，未鉴权请求在抵达本工具前即 403。",
    tags={"readonly"},
)
async def wiki_status(ctx: Context) -> str:
    mcpCtx = await _openContext()
    try:
        # 不再单独 SELECT 1：lifespan 启动时已经走过 ``SELECT 1`` 探活
        # （见 ``app/main.py`` 端口契约预检）；DB 不可达会直接 fail-fast。
        # 重复探活只会浪费一次连接池握手，且会污染日志。
        authRaw = os.environ.get("AUTH_STUB_ENABLED", "1").strip().lower()
        authMode = "stub" if authRaw in ("1", "true", "yes") else "production"
        return _json({
            "ok": True,
            "userId": mcpCtx.currentUserId,
            "authMode": authMode,
        })
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_search",
    description="按标题/正文模糊检索知识条目（ILIKE 匹配）。返回摘要，不含全文。",
    tags={"readonly"},
)
async def wiki_search(
    ctx: Context,
    query: str,
    dimension: str | None = None,
    limit: int = _SEARCH_LIMIT_DEFAULT,
) -> str:
    """Args:
    - query: 检索关键词（标题或正文子串）
    - dimension: 可选，按知识维度过滤（RULE / POLICY / PROCESS 等）
    - limit: 最大返回条数，默认 5（Agent 用：避免上下文塞爆）
    """
    mcpCtx = await _openContext()
    try:
        rows, total = await WikiPageService().searchPages(
            mcpCtx.dbSession,
            query=query,
            dimension=dimension,
            limit=min(limit, 50),
            offset=0,
        )
        results = [
            {
                "pageId": r.page_id,
                "title": r.title,
                "dimension": r.dimension,
                "status": r.status,
                "snippet": (r.content or "")[:200],
            }
            for r in rows
        ]
        return _json({"total": total, "results": results})
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_read",
    description="读单条 page 的全文 + claims + 已确认关系。Agent 上下文检索的核心动作。",
    tags={"readonly"},
)
async def wiki_read(
    ctx: Context,
    page_id: str,
    include_content: bool = True,
    include_claims: bool = True,
    include_relations: bool = True,
) -> str:
    """Args:
    - page_id: 知识条目 pageId
    - include_content: 是否含全文（默认 True；正文太长时可 False）
    - include_claims: 是否含事实原子
    - include_relations: 是否含已确认关系
    """
    mcpCtx = await _openContext()
    try:
        # WikiPageService.getPage 找不到时抛 NotFoundError（统一错误契约），
        # FastMCP 自动转 ToolError 给客户端。
        page = await WikiPageService().getPage(mcpCtx.dbSession, page_id)

        payload: dict[str, Any] = {
            "pageId": page.page_id,
            "title": page.title,
            "dimension": page.dimension,
            "status": page.status,
            "version": page.version,
            "validFrom": page.valid_from.isoformat() if page.valid_from else None,
        }
        if include_content:
            payload["content"] = page.content
        if include_claims:
            claims = await WikiPageService().listClaims(mcpCtx.dbSession, page_id)
            payload["claims"] = [
                {
                    "id": c.id,
                    "subject": c.subject_id,
                    "predicate": c.predicate,
                    "object": c.object_value,
                    "objectType": c.object_type,
                }
                for c in claims
            ]
        if include_relations:
            rels = await WikiPageService().listRelations(
                mcpCtx.dbSession, page_id, confirmedOnly=True
            )
            payload["relations"] = [
                {
                    "id": r.id,
                    "downstreamId": r.downstream_id,
                    "downstreamType": r.downstream_type,
                    "relationType": r.relation_type,
                    "confidence": float(r.confidence) if r.confidence is not None else None,
                }
                for r in rels
            ]
        return _json(payload)
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_graph_insights",
    description="拉取最新一次 Graph Insights 扫描结果：意外连接 + 知识缺口 + 桥接节点。",
    tags={"readonly"},
)
async def wiki_graph_insights(
    ctx: Context,
    gap_kind: str | None = None,
    limit: int = _INSIGHTS_LIMIT,
) -> str:
    """Args:
    - gap_kind: 可选过滤 — MISSING_DIMENSION / ISOLATED_PAGE / SPARSE_COMMUNITY
    - limit: 每个分类的最大返回条数
    """
    mcpCtx = await _openContext()
    try:
        from app.services.learning.insight_service import (
            _bridgeKey,
            _gapKey,
            _surprisingKey,
        )
        from app.domain.wiki_graph_insight import WikiGraphInsight

        rows = (await mcpCtx.dbSession.execute(
            select(WikiGraphInsight).order_by(WikiGraphInsight.updated_time.desc())
        )).scalars().all()

        surprising: list[dict] = []
        gaps: list[dict] = []
        bridges: list[dict] = []
        for row in rows:
            payload = row.payload or {}
            if row.kind == "SURPRISING_CONNECTION":
                surprising.append({
                    "key": row.key,
                    "headline": row.headline,
                    "sourcePageId": payload.get("sourcePageId"),
                    "targetPageId": payload.get("targetPageId"),
                    "relationType": payload.get("relationType"),
                    "explanation": row.explanation,
                })
            elif row.kind.startswith("KNOWLEDGE_GAP_"):
                kind = payload.get("kind", row.kind.removeprefix("KNOWLEDGE_GAP_"))
                if gap_kind is not None and kind != gap_kind:
                    continue
                gaps.append({
                    "key": row.key,
                    "kind": kind,
                    "headline": row.headline,
                    "pageId": payload.get("pageId"),
                    "communityKey": payload.get("communityKey"),
                    "topic": payload.get("topic"),
                })
            elif row.kind == "BRIDGE_NODE":
                bridges.append({
                    "key": row.key,
                    "pageId": payload.get("pageId"),
                    "title": payload.get("title"),
                    "communities": payload.get("communities"),
                    "explanation": row.explanation,
                })
        return _json({
            "surprisingConnections": surprising[:limit],
            "knowledgeGaps": gaps[:limit],
            "bridgeNodes": bridges[:limit],
            "scannedAt": rows[0].updated_time.isoformat() if rows else None,
        })
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_graph_communities",
    description="拉取 Louvain 社区列表（含 topic）。社区粒度 + 命名让 Agent 理解「知识领域」。",
    tags={"readonly"},
)
async def wiki_graph_communities(ctx: Context) -> str:
    mcpCtx = await _openContext()
    try:
        from app.services.knowledge_graph import listCommunities

        communities = await listCommunities(mcpCtx.dbSession)
        return _json([
            {
                "id": c.id,
                "communityKey": c.community_key,
                "name": c.name,
                "topic": c.topic,
                "pageCount": c.page_count,
                "cohesionScore": float(c.cohesion_score),
                "topPages": c.top_pages,
            }
            for c in communities
        ])
    finally:
        await mcpCtx.close()


# ---------------------------------------------------------------------------
# 两步预览（与 Phase 5.5 一致）
# ---------------------------------------------------------------------------


@mcp.tool(
    name="wiki_preview_classify",
    description=(
        "MISSING_DIMENSION 知识缺口的两步预览：调 LLM 分类一次，**不写库**。"
        "返回 primary + alternatives + reason + confidence。"
        "用户确认后调 wiki_update_dimension 落库。"
    ),
    tags={"write-preview"},
)
async def wiki_preview_classify(ctx: Context, page_id: str) -> str:
    mcpCtx = await _openContext()
    try:
        # WikiPageService.getPage 找不到时抛 NotFoundError（统一错误契约）。
        page = await WikiPageService().getPage(mcpCtx.dbSession, page_id)
        activeModels = await ModelConfigService().list(mcpCtx.dbSession, activeOnly=True)
        if not activeModels:
            raise NotFoundError("No active LLM models configured")
        invoker = LearningLLMInvoker(mcpCtx.dbSession, primaryModelId=activeModels[0].id)
        await invoker.preflight()
        from app.services.learning.auto_classifier import AutoClassifier

        suggestion, _ = await AutoClassifier().classify(
            invoker, title=page.title, content=page.content
        )
        if suggestion is None:
            return _json({"primary": None, "confidence": 0.0, "reason": "无可信建议"})
        return _json({
            "pageId": page_id,
            "primary": suggestion.primary,
            "confidence": suggestion.confidence,
            "alternatives": list(suggestion.alternatives),
            "reason": suggestion.reason,
        })
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_preview_relations",
    description=(
        "ISOLATED_PAGE 知识缺口的两步预览：发现候选关系，**不写库**。"
        "返回 candidates + ghost references。"
        "用户确认后经后端 ``POST /api/v1/wiki/pages/{id}/relations/discover`` 落库"
        "（MCP 无对应写工具，避免双重写入口漂移）。"
    ),
    tags={"write-preview"},
)
async def wiki_preview_relations(ctx: Context, page_id: str) -> str:
    mcpCtx = await _openContext()
    try:
        from app.services.learning.relation_discovery import RelationDiscovery

        result = await RelationDiscovery().discoverForPage(
            mcpCtx.dbSession, page_id, dryRun=True
        )
        # 批量补 title
        rawProposals = list(result.rawProposals)
        pageIds = {p["downstream_id"] for p in rawProposals if p["downstream_type"] == "PAGE"}
        titleByPageId: dict[str, str] = {}
        if pageIds:
            rows = (await mcpCtx.dbSession.execute(
                select(WikiPage.page_id, WikiPage.title).where(
                    WikiPage.page_id.in_(pageIds)
                )
            )).all()
            titleByPageId = {pid: title for pid, title in rows}

        return _json({
            "pageId": page_id,
            "total": len(rawProposals),
            "classExtractionStatus": result.classExtractionStatus,
            "candidates": [
                {
                    "downstreamType": p["downstream_type"],
                    "downstreamId": p["downstream_id"],
                    "downstreamTitle": titleByPageId.get(p["downstream_id"], ""),
                    "relationType": p["relation_type"],
                    "confidence": float(p.get("confidence") or 0.0),
                }
                for p in rawProposals
            ],
        })
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_preview_community_topic",
    description=(
        "SPARSE_COMMUNITY 知识缺口的两步预览：调 LLM 生成主题，**不写库**。"
        "用户确认后调 wiki_update_community_topic 落库。"
    ),
    tags={"write-preview"},
)
async def wiki_preview_community_topic(ctx: Context, community_key: str) -> str:
    mcpCtx = await _openContext()
    try:
        community = (await mcpCtx.dbSession.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == community_key
            )
        )).scalar_one_or_none()
        if community is None:
            # 抛 NotFoundError 与 WikiPageService.getPage 保持一致；
            # FastMCP 把 DomainError 转 ToolError 给客户端（is_error=true）。
            raise NotFoundError(f"社区 {community_key} 不存在")

        activeModels = await ModelConfigService().list(mcpCtx.dbSession, activeOnly=True)
        if not activeModels:
            # 同样的契约：把业务级错误转 ToolError，而不是返回 success+error JSON。
            raise NotFoundError("No active LLM models configured")
        invoker = LearningLLMInvoker(mcpCtx.dbSession, primaryModelId=activeModels[0].id)
        await invoker.preflight()
        suggestion = await CommunityTopicSuggester(invoker).suggest(
            mcpCtx.dbSession, community_key
        )
        titles = await loadCommunityTitles(mcpCtx.dbSession, community_key)
        return _json({
            "communityKey": community_key,
            "topic": suggestion.topic,
            "pageCount": suggestion.pageCount,
            "pageTitles": titles,
        })
    finally:
        await mcpCtx.close()


# ---------------------------------------------------------------------------
# 写入：复用 Phase 5.5 的写库路径
# ---------------------------------------------------------------------------


@mcp.tool(
    name="wiki_update_dimension",
    description=(
        "写入 page.dimension + autoClassification。**与 wiki_preview_classify 配对**："
        "拿到 LLM 建议后用此工具落库，否则下次 graph scan 仍会报 MISSING_DIMENSION。"
    ),
    tags={"write"},
)
async def wiki_update_dimension(
    ctx: Context,
    page_id: str,
    dimension: str,
    auto_classification: dict[str, Any] | None = None,
) -> str:
    """Args:
    - page_id: 知识条目 pageId
    - dimension: 维度（必须是 KNOWLEDGE_DIMENSIONS 白名单之一）
    - auto_classification: 可选 —— wiki_preview_classify 返回的完整建议 dict
      （含 primary / confidence / alternatives / reason），透传写库
    """
    mcpCtx = await _openContext()
    try:
        from app.domain.wiki_schemas import WikiPageUpdate
        page = await WikiPageService().updatePage(
            mcpCtx.dbSession,
            page_id,
            WikiPageUpdate(dimension=dimension, auto_classification=auto_classification),
        )
        await mcpCtx.dbSession.commit()
        return _json({"pageId": page.page_id, "dimension": page.dimension})
    finally:
        await mcpCtx.close()


@mcp.tool(
    name="wiki_update_community_topic",
    description=(
        "写入 community.topic。**与 wiki_preview_community_topic 配对**："
        "拿到 LLM 建议主题后用此工具落库。"
    ),
    tags={"write"},
)
async def wiki_update_community_topic(
    ctx: Context,
    community_key: str,
    topic: str | None,
) -> str:
    """Args:
    - community_key: 社区编号（如 C001）
    - topic: 新主题，传 null 表示清空
    """
    mcpCtx = await _openContext()
    try:
        community = (await mcpCtx.dbSession.execute(
            select(KnowledgeCommunity).where(
                KnowledgeCommunity.community_key == community_key
            )
        )).scalar_one_or_none()
        if community is None:
            raise NotFoundError(f"社区 {community_key} 不存在")
        community.topic = topic
        await mcpCtx.dbSession.commit()
        return _json({
            "communityKey": community_key,
            "topic": community.topic,
        })
    finally:
        await mcpCtx.close()


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="qa-system MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="传输协议：stdio（Claude Desktop）/ http（独立服务）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info(
        "Starting qa-system MCP Server (transport=%s, user=%s)",
        args.transport, _DEFAULT_USER_ID,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # HTTP：必须包 McpAuthMiddleware。裸跑 fastmcp 自带 HTTP server 会绕过鉴权，
    # 而 ``_resolveCurrentUserId`` 在无 ContextVar 时回退到 ``MCP_USER_ID``
    # （默认 anonymous + 默认 admin 桩角色）—— 那等于把 2 个写工具匿名暴露。
    # 包装后本入口与 ``main.py`` 的 ``/mcp`` 挂载走同一条 Bearer 鉴权链路。
    import uvicorn

    from app.api.mcp_auth import McpAuthMiddleware

    httpApp = McpAuthMiddleware(
        mcp.http_app(path="/mcp", transport="streamable-http")
    )
    uvicorn.run(httpApp, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
