"""治理写入口（词条字典 / 本体指标）的**生产装配**门禁守卫。

背景：本仓的 API 集成测试走 `tests/_testapp.py` 手工装配的测试 app，而线上跑的是
`app.main.createApp()`。若生产装配漏挂 `requireAdmin`（例如有人为了「修 403」把
依赖换回 `getCurrentUser`），测试 app 上的行为用例**未必**同步变红 —— 两处装配的
漂移在集成套件里是结构性不可见的。

本文件直接构建生产 app，按路由钉住写/读两面的鉴权契约：

- 写路由必须声明 `requireAdmin`：`POST/DELETE /term-dictionary`、
  `POST/PUT/DELETE /ontology/metrics`；
- 读路由必须**不**声明 `requireAdmin`：`GET /term-dictionary`、
  `GET /ontology/metrics`、`GET /ontology/metrics/{id}`。

路由树展开与依赖探测复用 `tests/_route_auth_support.py`（FastAPI ≥0.141 的
include 上下文敏感逻辑，见该模块 docstring）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from app.dependencies import getCurrentUser, requireAdmin
from app.tests._route_auth_support import (
    declaresDependency,
    iterRoutesWithDeps,
)

# (method, 完整路径)：写入口必须带 admin 门禁。
_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/term-dictionary"),
        ("DELETE", "/api/v1/term-dictionary/{id}"),
        ("POST", "/api/v1/ontology/metrics"),
        ("PUT", "/api/v1/ontology/metrics/{id}"),
        ("DELETE", "/api/v1/ontology/metrics/{id}"),
    }
)

# (method, 完整路径)：读入口保持所有登录用户可用，不得带 admin 门禁。
_READ_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/term-dictionary"),
        ("GET", "/api/v1/ontology/metrics"),
        ("GET", "/api/v1/ontology/metrics/{id}"),
    }
)

_TERM_PREFIX = "/api/v1/term-dictionary"
_METRIC_PREFIX = "/api/v1/ontology/metrics"


def _productionRoutes() -> list[tuple[str, APIRoute, tuple[Any, ...]]]:
    from app.main import createApp

    return list(iterRoutesWithDeps(list(createApp().routes)))


def _routeIndex() -> dict[tuple[str, str], tuple[APIRoute, tuple[Any, ...]]]:
    """(method, path) → (route, include 依赖)。只保留本守卫关心的前缀。"""
    index: dict[tuple[str, str], tuple[APIRoute, tuple[Any, ...]]] = {}
    for path, route, inherited in _productionRoutes():
        if not path.startswith((_TERM_PREFIX, _METRIC_PREFIX)):
            continue
        for method in route.methods:
            index[(method, path)] = (route, inherited)
    return index


@pytest.fixture(scope="module")
def routeIndex() -> dict[tuple[str, str], tuple[APIRoute, tuple[Any, ...]]]:
    """生产 app 只构建一次（createApp 不触库，但装配有成本）。"""
    return _routeIndex()


def test_governance_routes_are_registered(
    routeIndex: dict[tuple[str, str], tuple[APIRoute, tuple[Any, ...]]],
) -> None:
    """枚举非空 + 写/读路由一条不少（空集 ⇒ 后续断言全部空转）。"""
    index = routeIndex
    missing = sorted((_WRITE_ROUTES | _READ_ROUTES) - set(index))

    assert index, "治理路由枚举为空——路由树结构变化会让本守卫空集通过"
    assert missing == [], f"生产 app 缺少以下治理路由：{missing}"


@pytest.mark.parametrize(("method", "path"), sorted(_WRITE_ROUTES))
def test_write_route_declares_admin_guard(
    routeIndex: dict[tuple[str, str], tuple[APIRoute, tuple[Any, ...]]],
    method: str,
    path: str,
) -> None:
    """写路由必须同时声明 requireAdmin（admin 门禁）与 getCurrentUser（身份）。"""
    route, inherited = routeIndex[(method, path)]

    assert declaresDependency(route, requireAdmin, inherited), (
        f"{method} {path} 缺少 requireAdmin（写入口对非管理员开放）"
    )
    assert declaresDependency(route, getCurrentUser, inherited), (
        f"{method} {path} 缺少 getCurrentUser（无法解析身份）"
    )


@pytest.mark.parametrize(("method", "path"), sorted(_READ_ROUTES))
def test_read_route_does_not_require_admin(
    routeIndex: dict[tuple[str, str], tuple[APIRoute, tuple[Any, ...]]],
    method: str,
    path: str,
) -> None:
    """读路由不得带 admin 门禁：普通用户必须能查看词条与指标。"""
    route, inherited = routeIndex[(method, path)]

    assert not declaresDependency(route, requireAdmin, inherited), (
        f"{method} {path} 意外要求 admin（普通用户读请求会 403）"
    )


def test_guard_detector_is_not_vacuous() -> None:
    """反向断言 + 正向前置：探测器对公开路由判「无门禁」，对门禁探针判「有」。"""
    routes = _productionRoutes()
    public = [item for item in routes if item[0] == "/api/v1/health"]
    assert public, "未枚举到 /api/v1/health——路由树展开失效"
    _, healthRoute, healthInherited = public[0]
    assert not declaresDependency(healthRoute, requireAdmin, healthInherited)

    def guardProbe() -> None: ...

    guarded = APIRouter(prefix="/guarded")

    @guarded.get("/probe")
    async def probeRoute() -> None: ...

    app = FastAPI()
    app.include_router(
        guarded,
        prefix="/api",
        dependencies=[Depends(guardProbe)],  # noqa: B008 - 探针需真实依赖声明
    )
    probe = [
        (route, inherited)
        for path, route, inherited in iterRoutesWithDeps(list(app.routes))
        if path == "/api/guarded/probe"
    ]

    assert len(probe) == 1, "探针路由未枚举到——展开逻辑失效"
    probeRoute_, probeInherited = probe[0]
    assert declaresDependency(probeRoute_, guardProbe, probeInherited), (
        "探测器看不到 include_router 声明的依赖——门禁守卫会假绿"
    )
