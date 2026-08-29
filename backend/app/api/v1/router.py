"""v1 路由聚合。

随各 Phase 推进逐步挂载子路由。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


def _register() -> None:
    """懒注册各子路由，避免循环导入。"""
    from app.api.v1.model_config import router as modelConfigRouter
    from app.api.v1.ontology import router as ontologyRouter
    from app.api.v1.session import router as sessionRouter

    router.include_router(modelConfigRouter, prefix="/models", tags=["models"])
    router.include_router(sessionRouter, prefix="/sessions", tags=["sessions"])
    router.include_router(ontologyRouter, prefix="/ontology", tags=["ontology"])


_register()
