"""测试 FastAPI 应用构建（供 sqlite 与真实 PG 两套 client fixture 复用）。

把路由挂载 / 中间件 / 异常处理器 / 依赖注入统一封装，避免两套 client 各写一遍。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.v1 import (
    agent_runtime,
    agent_tools,
    agents,
    audit,
    business_object,
    chat,
    data_lineage,
    data_quality,
    data_quality_generate,
    data_quality_rule_params,
    datasource,
    documents,
    embedding_provider,
    entity_mapping,
    features,
    feature_rules,
    graph_traversal,
    kpi_catalog,
    local_import,
    menu_config,
    model_config,
    ontology,
    organizations,
    roles,
    session,
    supplier_360,
    supplier_risk,
    system,
    system_config,
    term_dictionary,
    users,
    wiki,
    wiki_import,
    wiki_compile,
    wiki_graph,
)
from app.config import getSettings
from app.dependencies import getDb
from app.domain.exceptions import DomainError
from app.domain.schemas import ErrorResponse, HealthResponse
from app.infrastructure.rate_limit import limiter, rateLimitExceededHandler


def buildTestApp(testFactory: Any) -> FastAPI:
    """构建干净的测试 app，并把 getDb 依赖覆盖为 testFactory 提供的会话。

    关键修复（FastAPI 0.141 _IncludedRouter bug）：
    v1Router.include_router(sub) 会把子路由的 prefix 叠加到路径上（如 '/ontology'），
    再在 app.include_router(v1Router, prefix='/api/v1') 时又叠加一次，
    导致最终路径变成 '/api/v1/ontology/ontology/classes'，匹配失败。
    解决：直接从各子路由模块 import 并以正确前缀挂载到测试 app，绕过 v1Router。
    """
    testApp = FastAPI(title="QA System API (test)")

    # 限流：中间件 + 状态 + 429 处理器（与 main.py 一致），默认关闭。
    # 顺序与 main.py 保持一致：CORS 后添加在最外层，SlowAPI 直接产出的 429 也带 CORS 头。
    limiter.enabled = False
    testApp.state.limiter = limiter
    testApp.add_exception_handler(RateLimitExceeded, rateLimitExceededHandler)
    testApp.add_middleware(SlowAPIMiddleware)
    testApp.add_middleware(
        CORSMiddleware,
        allow_origins=getSettings().corsOriginList,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册领域异常处理器（与 main.py 一致）：用 isinstance 而非 __class__.__name__，
    # 支持子类化（如 _ToolInUseConflict(ConflictError)）正确映射 409。
    # 异常 → 状态码映射复用 main.py 同一份实现（exceptions.statusForError）：
    # 各写一份会漂移，曾导致测试 app 把 LLMUnavailableError 返成 400。
    from app.domain.exceptions import statusForError

    @testApp.exception_handler(DomainError)
    async def handleDomainError(request, exc: DomainError) -> JSONResponse:
        status = statusForError(exc)
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error=exc.message,
                detail=exc.detail,
                details=getattr(exc, "details", None),
            ).model_dump(by_alias=True),
        )

    # 直接挂载子路由（子路由自身已有 prefix，故用 /api/v1 前缀覆盖）
    testApp.include_router(model_config.router, prefix="/api/v1/models", tags=["models"])
    testApp.include_router(
        embedding_provider.router,
        prefix="/api/v1/embedding-providers",
        tags=["embedding-providers"],
    )
    testApp.include_router(session.router, prefix="/api/v1/sessions", tags=["sessions"])
    testApp.include_router(ontology.router, prefix="/api/v1", tags=["ontology"])
    testApp.include_router(
        term_dictionary.router, prefix="/api/v1", tags=["term-dictionary"]
    )
    testApp.include_router(datasource.router, prefix="/api/v1/datasources", tags=["datasources"])
    testApp.include_router(local_import.router, prefix="/api/v1/datasources", tags=["datasources"])
    testApp.include_router(
        data_quality.router, prefix="/api/v1/data-quality/rules", tags=["data-quality"]
    )
    testApp.include_router(
        data_quality.scores_router,
        prefix="/api/v1/data-quality/scores",
        tags=["data-quality"],
    )
    testApp.include_router(
        data_quality_rule_params.router,
        prefix="/api/v1",
        tags=["dq-rule-params"],
    )
    testApp.include_router(
        data_quality_generate.router,
        prefix="/api/v1/data-quality/rules/generate",
        tags=["data-quality-generate"],
    )
    testApp.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
    testApp.include_router(
        data_lineage.router, prefix="/api/v1/lineage/edges", tags=["lineage"]
    )
    testApp.include_router(
        entity_mapping.router,
        prefix="/api/v1/entity-mappings",
        tags=["entity-mapping"],
    )
    testApp.include_router(
        kpi_catalog.router, prefix="/api/v1/kpi-catalog", tags=["kpi-catalog"]
    )
    testApp.include_router(
        business_object.router, prefix="/api/v1", tags=["business-object"]
    )
    testApp.include_router(features.router, prefix="/api/v1/features", tags=["features"])
    testApp.include_router(
        supplier_360.router,
        prefix="/api/v1/supplier-360",
        tags=["supplier-360"],
    )
    testApp.include_router(
        supplier_risk.router,
        prefix="/api/v1/supplier-risk",
        tags=["supplier-risk"],
    )
    testApp.include_router(
        documents.router, prefix="/api/v1/documents", tags=["documents"]
    )
    testApp.include_router(agents.router, prefix="/api/v1/agents", tags=["agents"])
    testApp.include_router(
        agent_runtime.router, prefix="/api/v1/agents", tags=["agents"]
    )
    testApp.include_router(agent_tools.router, tags=["agent-tools"])
    testApp.include_router(users.router, tags=["users"])
    testApp.include_router(roles.router, tags=["roles"])
    testApp.include_router(organizations.router, tags=["organizations"])
    testApp.include_router(
        graph_traversal.router, prefix="/api/v1/graph", tags=["graph"]
    )
    testApp.include_router(audit.router, prefix="/api/v1/audit", tags=["audit"])
    testApp.include_router(feature_rules.router, tags=["feature-rules"])
    testApp.include_router(system.router, prefix="/api/v1/system", tags=["system"])
    testApp.include_router(
        system_config.router,
        prefix="/api/v1/admin/system-config",
        tags=["system-config"],
    )
    testApp.include_router(
        menu_config.router, prefix="/api/v1/menu-config", tags=["menu-config"]
    )
    testApp.include_router(wiki.router, prefix="/api/v1", tags=["wiki"])
    testApp.include_router(wiki_import.router, prefix="/api/v1", tags=["wiki"])
    testApp.include_router(wiki_compile.router, prefix="/api/v1", tags=["wiki"])
    testApp.include_router(wiki_graph.router, prefix="/api/v1", tags=["wiki"])

    @testApp.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, appEnv="test")

    # 注入测试数据库会话
    # 注意：getDb() 函数在模块导入时已关闭了对 getSessionFactory 的引用，
    # 直接 override getDb 无法让 dbModule.getSessionFactory 被调用。
    # 因此调用方需先替换 dbModule._sessionFactory，这里只 override getDb。
    async def _overrideDb() -> AsyncIterator[AsyncSession]:
        async with testFactory() as s:
            yield s

    testApp.dependency_overrides[getDb] = _overrideDb
    return testApp
