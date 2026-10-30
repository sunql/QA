"""本特性新增路由的鉴权回归网（feature-scoped route-auth guard）。

背景：仓库级守卫 `app/tests/integration/test_route_auth_guard.py` 是全仓唯一
断言「新路由必须带 `Depends(getCurrentUser)`」的地方，但它在 FastAPI 0.141.1
下失效——`app.routes` 把 `include_router()` 的结果包进 `_IncludedRouter`，而守卫
的 `_enumerateRoutes()` 只接受 `APIRoute`，于是只能枚举到 1 条路由。修那个守卫
是仓库级任务（不在本任务范围，且该文件被明确要求不得改动）。

但「新路由必须鉴权」这条不变量不能因此失去回归网：本文件对**本特性新增的两个
router**做特征范围内的独立断言——

1. 递归展开路由树（兼容 `_IncludedRouter` 包装层），取出两组新路由的
   (method, path) 集合；
2. 断言该集合**恰好等于**预期的 12 条——集合相等同时保证「不漏」「不多」，且
   **不可能是空集通过**（空集 != 预期集合 ⇒ 必红）；
3. 逐条断言其依赖链里声明了 `getCurrentUser`；
4. 另有一条反向断言证明探测器不是恒真（`/api/v1/health` 必须被判为「无鉴权」）。

注意：断言的是**路由依赖声明**（静态结构），不是运行时行为。这正是被删掉
`Depends(getCurrentUser)` 时必须变红的那一层——集成套件在 `AUTH_MODE=stub` 下
匿名请求仍会解析成 stub 用户，因此**无法**覆盖这一层。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.config import getSettings
from app.dependencies import getCurrentUser

# 本特性新增的两组路由前缀（与 main.py 的 include_router 前缀一致）。
_NEW_PREFIXES: tuple[str, ...] = (
    "/api/v1/agent-orchestrations",
    "/api/v1/agent-runs",
)

# 期望的完整路由集合：agent-orchestrations 6 条 + agent-runs 6 条。
# 集合相等 ⇒ 漏一条、多一条、路径改错都会红；且空集必然不等 ⇒ 杜绝空集通过。
_EXPECTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/agent-orchestrations"),
        ("GET", "/api/v1/agent-orchestrations"),
        ("GET", "/api/v1/agent-orchestrations/tool-options"),
        ("GET", "/api/v1/agent-orchestrations/{agentCode}"),
        ("PUT", "/api/v1/agent-orchestrations/{agentCode}"),
        ("POST", "/api/v1/agent-orchestrations/{agentCode}/toggle"),
        ("POST", "/api/v1/agent-runs"),
        ("GET", "/api/v1/agent-runs"),
        ("GET", "/api/v1/agent-runs/{runId}"),
        ("GET", "/api/v1/agent-runs/{runId}/steps"),
        ("POST", "/api/v1/agent-runs/{runId}/resume"),
        ("POST", "/api/v1/agent-runs/{runId}/cancel"),
    }
)


def _iterRoutes(routes: list[Any], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """递归展开路由树，产出 (完整路径, APIRoute)。

    FastAPI 0.141.1 用 `_IncludedRouter` 包装 `include_router()` 的结果，且包装
    层**不暴露** `.routes`；只能通过 `original_router` + `include_context.prefix`
    下钻。旧的 flat 列表形态（0.136.x）、嵌套 APIRouter 与 Mount 同样兼容。
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            includeContext = getattr(route, "include_context", None)
            nestedPrefix = prefix + (getattr(includeContext, "prefix", "") or "")
            yield from _iterRoutes(list(original.routes), nestedPrefix)
            continue
        nested = getattr(route, "routes", None)
        if isinstance(nested, list):
            nestedPrefix = prefix + (
                getattr(route, "prefix", "") or getattr(route, "path", "") or ""
            )
            yield from _iterRoutes(nested, nestedPrefix)


def _declaresCurrentUser(route: APIRoute) -> bool:
    """路由依赖链（含 router 级 dependencies）是否声明了 getCurrentUser。"""
    seen: set[int] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is getCurrentUser:
            return True
        stack.extend(dependant.dependencies)
    return False


def _newOrchestrationRoutes(app: FastAPI) -> list[tuple[str, APIRoute]]:
    """只取两个新 router 下的路由（按前缀过滤）。"""
    return [
        (path, route)
        for path, route in _iterRoutes(list(app.routes))
        if path.startswith(_NEW_PREFIXES)
    ]


@pytest.fixture
def orchestrationApp(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """在「开关开启」状态下构建一次**生产 app**（`app.main.createApp`）。

    开关必须在 app 构建之前落定：路由注册发生在 `createApp()` 内部，晚于构建
    再改环境变量不会补注册路由。
    """
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "true")
    getSettings.cache_clear()
    try:
        from app.main import createApp

        return createApp()
    finally:
        getSettings.cache_clear()


def test_newOrchestrationRoutesAreExactlyTheExpectedTwelve(
    orchestrationApp: FastAPI,
) -> None:
    """路由集合完整且非平凡：恰好 12 条，一不多一不少。"""
    routes = _newOrchestrationRoutes(orchestrationApp)
    assert routes, "新路由枚举为空——路由树结构变化会让本守卫空集通过"
    enumerated = {
        (method, path) for path, route in routes for method in route.methods
    }
    assert enumerated == _EXPECTED_ROUTES, (
        "新路由集合与预期不一致（新增/删除/改名都必须同步本守卫）：\n  "
        f"缺失: {sorted(_EXPECTED_ROUTES - enumerated)}\n"
        f"多出: {sorted(enumerated - _EXPECTED_ROUTES)}"
    )
    assert len(routes) == len(_EXPECTED_ROUTES)


def test_everyNewOrchestrationRouteDeclaresGetCurrentUser(
    orchestrationApp: FastAPI,
) -> None:
    """每条新路由都必须声明 Depends(getCurrentUser)。"""
    routes = _newOrchestrationRoutes(orchestrationApp)
    assert routes, "新路由枚举为空——空集不得通过"
    offenders = sorted(
        {
            (method, path)
            for path, route in routes
            for method in route.methods
            if not _declaresCurrentUser(route)
        }
    )
    assert offenders == [], (
        "以下新路由缺少 Depends(getCurrentUser)（匿名可达）：\n  "
        + "\n  ".join(f"{method:7} {path}" for method, path in offenders)
    )


def test_currentUserDetectorIsNotVacuous(orchestrationApp: FastAPI) -> None:
    """反向断言：探测器必须能判「无鉴权」，否则上一条可能恒真。

    `/api/v1/health` 是公开路由（不声明 getCurrentUser）；若探测器恒返回 True，
    上一条的「全部有鉴权」就毫无意义。
    """
    public = [
        route
        for path, route in _iterRoutes(list(orchestrationApp.routes))
        if path == "/api/v1/health"
    ]
    assert public, "未枚举到 /api/v1/health——路由树展开失效"
    assert not _declaresCurrentUser(public[0]), (
        "/api/v1/health 被判为带鉴权——探测器逻辑可疑，请复核"
    )
