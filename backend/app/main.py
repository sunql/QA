"""FastAPI 应用工厂。

负责：lifespan（启动建表/关闭释放）、CORS、异常处理、路由挂载、健康检查。
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import __version__
from app.config import getSettings
from app.domain.error_messages import MSG_API_DESCRIPTION
from app.domain.exceptions import DomainError
from app.domain.schemas import ErrorResponse, HealthResponse
from app.infrastructure.database import disposeEngine, getEngine
from app.infrastructure.rate_limit import limiter, rateLimitExceededHandler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时预热引擎 + schema drift 校验，关闭时释放。

    Schema 漂移防护（Harness 工程结构.md）：ORM 与 DB 不一致时启动 fail-fast，
    避免「代码新增模型但 DB 未迁移」运行时踩 "relation does not exist"。
    通过环境变量 SKIP_SCHEMA_CHECK=1 可临时跳过（如紧急回滚 + 旧镜像兼容）。
    """
    settings = getSettings()
    logging.basicConfig(level=getattr(logging, settings.logLevel.upper(), logging.INFO))
    logger.info("启动 QA System 后端 v%s (env=%s)", __version__, settings.appEnv)
    # Phase 4.5 安全护栏：生产 + stub auth 同时启用应大声告警
    if settings.appEnv == "production" and os.environ.get("AUTH_STUB_ENABLED", "1") == "1":
        logger.error(
            "🚨 安全告警：生产环境 (env=production) 仍在使用 stub auth "
            "(AUTH_STUB_ENABLED=1)。任何客户端可伪造 X-User-Roles=admin 绕过 ACL。"
            "生产部署前必须：AUTH_STUB_ENABLED=0 + 反向代理剥离 X-User-* 头，"
            "或接入 JWT/IdP 替换 getCurrentUser。"
        )
    engine = getEngine()
    logger.info("元数据库引擎已就绪: %s", engine.url.render_as_string(hide_password=True))

    # 端口契约预检：连不上时给精确提示（容器外常见 5432 vs 5433 错配）。
    # SSOT 详见 Harness/wiki/operations-runbook.md「SSOT 端口契约」段。
    from sqlalchemy import text
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:
        url = engine.url.render_as_string(hide_password=True)
        hint = ""
        if "localhost:5432" in url or "127.0.0.1:5432" in url:
            hint = (
                "\n[端口契约提示] DATABASE_URL 用了 5432，但宿主端口已重映射到 5433。"
                "\n  - 容器外运行：把 backend/.env 的 DATABASE_URL 改成 localhost:5433，或用 ./scripts/run_local.sh"
                "\n  - 容器内运行：DATABASE_URL 应是 postgres:5432（容器 service 名 + 容器端口）"
            )
        logger.error("❌ 元数据库连接失败: %s%s", e, hint)
        raise RuntimeError(f"启动失败：无法连接到 {url}。{hint}") from e
    # Schema drift 校验：默认开启，SKIP_SCHEMA_CHECK=1 可关闭（紧急场景）
    if os.environ.get("SKIP_SCHEMA_CHECK") != "1":
        from scripts.check_schema_drift import _checkDriftAsync

        try:
            issues = await _checkDriftAsync(engine)
        except Exception as e:
            logger.exception("Schema drift 校验异常: %s", e)
            raise RuntimeError(
                "Schema drift 校验失败：无法确认 ORM 与 DB 一致。"
                "如确认 DB 状态正确可设置 SKIP_SCHEMA_CHECK=1 跳过。"
            ) from e
        if issues:
            logger.error("Schema drift 校验失败（%d 项）：", len(issues))
            for issue in issues:
                logger.error("  - %s", issue)
            raise RuntimeError(
                "DB schema 与 ORM 不一致，禁止启动。"
                "请执行 alembic upgrade head 后重启。"
                "如紧急回滚可设置 SKIP_SCHEMA_CHECK=1。"
            )
    yield
    logger.info("关闭中，释放外部连接...")
    await shutdownCleanup()
    logger.info("已关闭")


async def shutdownCleanup() -> None:
    """释放外部连接。

    每个清理步骤独立 try/except：任一失败只记录日志，不阻断后续清理，
    保证数据库引擎一定能被释放。
    """
    from app.api.v1 import chat as chat_module

    try:
        await chat_module._embeddingService.close()
    except Exception:
        logger.exception("关闭 Embedding 连接失败")
    from app.api.v1 import ontology as ontology_module

    try:
        await ontology_module._embeddingService.close()
    except Exception:
        logger.exception("关闭本体 Embedding 连接失败")
    from app.infrastructure import milvus_client

    try:
        milvus_client.closeConnection()
    except Exception:
        logger.exception("关闭 Milvus 连接失败")
    try:
        from app.infrastructure import neo4j_client
        neo4j_client.closeDriver()
    except Exception:
        logger.exception("关闭 Neo4j driver 失败")
    from app.infrastructure.llm.embedding_provider_factory import (
        invalidateEmbeddingClientCache,
    )

    try:
        await invalidateEmbeddingClientCache()  # 关闭 resolver 缓存的 embedding 客户端
    except Exception:
        logger.exception("关闭 embedding 客户端失败")
    try:
        await disposeEngine()
    except Exception:
        logger.exception("关闭数据库引擎失败")


def createApp() -> FastAPI:
    """构建 FastAPI 应用实例。"""
    settings = getSettings()
    app = FastAPI(
        title="QA System API",
        description=MSG_API_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # 中间件顺序：后添加者更外层。SlowAPI 先生成，CORS 最后添加使其最外层，
    # 保证限流中间件直接产出的 429（非装饰路由路径）也带 CORS 头。
    app.add_middleware(SlowAPIMiddleware)
    # CORS：非生产仅允许本地 Vite dev server；生产由 Nginx 反代同源承载（allow_origins=[]）。
    # 显式 origin 而非 "*"：* 与 allow_credentials=True 组合违反 CORS 规范，浏览器会拒绝 SSE。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.corsOriginList,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.limiter = limiter

    # 注册领域异常 -> JSON 响应
    registerExceptionHandlers(app)
    app.add_exception_handler(RateLimitExceeded, rateLimitExceededHandler)

    # 挂载路由 — 直接包含子路由，避免 FastAPI 0.141 _IncludedRouter
    # prefix 双重叠加 bug（v1Router.include_router(sub, prefix="/x") +
    # app.include_router(v1Router, prefix="/api/v1") 会导致 /api/v1/x/x/...）
    from app.api.v1 import (
        agents,
        audit,
        chat,
        data_lineage,
        data_quality,
        datasource,
        documents,
        embedding_provider,
        entity_mapping,
        features,
        graph,
        graph_traversal,
        kpi_catalog,
        local_import,
        model_config,
        ontology,
        session,
        supplier_360,
        supplier_risk,
        system,
        term_dictionary,
        vectors,
    )

    app.include_router(model_config.router, prefix="/api/v1/models", tags=["models"])
    app.include_router(
        embedding_provider.router,
        prefix="/api/v1/embedding-providers",
        tags=["embedding-providers"],
    )
    app.include_router(session.router, prefix="/api/v1/sessions", tags=["sessions"])
    app.include_router(ontology.router, prefix="/api/v1", tags=["ontology"])
    app.include_router(
        term_dictionary.router, prefix="/api/v1", tags=["term-dictionary"]
    )
    app.include_router(datasource.router, prefix="/api/v1/datasources", tags=["datasources"])
    app.include_router(local_import.router, prefix="/api/v1/datasources", tags=["datasources"])
    app.include_router(
        data_quality.router, prefix="/api/v1/data-quality/rules", tags=["data-quality"]
    )
    app.include_router(
        data_quality.scores_router,
        prefix="/api/v1/data-quality/scores",
        tags=["data-quality"],
    )
    app.include_router(
        data_lineage.router, prefix="/api/v1/lineage/edges", tags=["lineage"]
    )
    app.include_router(
        entity_mapping.router,
        prefix="/api/v1/entity-mappings",
        tags=["entity-mapping"],
    )
    app.include_router(
        kpi_catalog.router, prefix="/api/v1/kpi-catalog", tags=["kpi-catalog"]
    )
    app.include_router(features.router, prefix="/api/v1/features", tags=["features"])
    app.include_router(
        supplier_360.router,
        prefix="/api/v1/supplier-360",
        tags=["supplier-360"],
    )
    app.include_router(
        supplier_risk.router,
        prefix="/api/v1/supplier-risk",
        tags=["supplier-risk"],
    )
    app.include_router(
        documents.router, prefix="/api/v1/documents", tags=["documents"]
    )
    app.include_router(agents.router, prefix="/api/v1/agents", tags=["agents"])
    app.include_router(audit.router, prefix="/api/v1/audit", tags=["audit"])
    app.include_router(chat.router, prefix="/api/v1/chat", tags=["chat"])
    app.include_router(system.router, prefix="/api/v1/system", tags=["system"])
    app.include_router(graph.router, prefix="/api/v1/system", tags=["system"])
    app.include_router(
        graph_traversal.router, prefix="/api/v1/graph", tags=["graph"]
    )
    app.include_router(vectors.router, prefix="/api/v1/system", tags=["system"])

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, appEnv=settings.appEnv)

    return app


def registerExceptionHandlers(app: FastAPI) -> None:
    """注册领域异常处理器，统一错误响应格式。"""

    @app.exception_handler(DomainError)
    async def handleDomainError(request: Request, exc: DomainError) -> JSONResponse:
        status = _statusFor(exc)
        logger.warning("领域异常 %s: %s (path=%s)", type(exc).__name__, exc.message, request.url.path)
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(error=exc.message, detail=exc.detail).model_dump(by_alias=True),
        )


def _statusFor(exc: DomainError) -> int:
    """领域异常 -> HTTP 状态码。"""
    from app.domain.exceptions import (
        ConflictError,
        NotFoundError,
        PermissionDeniedError,
        ValidationError,
    )

    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, ValidationError):
        return 422
    if isinstance(exc, PermissionDeniedError):
        return 403
    return 400


app = createApp()
