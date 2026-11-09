"""语义发布只读 API 的装配 + 门禁回归网（feature-scoped route-auth guard）。

背景：本特性的路由由 `main.py` 与 `tests/_testapp.py` **两处手工装配**，而 API
集成测试全部走 `_testapp.py`。若哪天从 `main.py` 删掉 `Depends(requireAdmin)`
或漏挂前缀，集成套件仍然全绿——两处装配的漂移在测试里是结构性不可见的。

本文件直接构建**生产 app**（`app.main.createApp()`），钉三件事：

1. 开关开启时路由集合恰好 3 条（集合相等：漏/多/改名都会红，空集必然不等）；
2. 每条路由的依赖链里同时声明了 `requireAdmin` 与 `getCurrentUser`
   （后者由前者间接引入，但两条都断言：删掉任一层都要红）；
3. 开关关闭时这 3 条路由一条都不注册（404 的前提）。

反向断言（探测器不恒真）：公开路由 `/api/v1/health` 必须被判为「无 admin 门禁」。
路由树展开与依赖链探测复用 `test_agent_orchestration_route_auth.py` 的实现
（同一套 `_IncludedRouter` 兼容逻辑）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.config import getSettings
from app.dependencies import getCurrentUser, requireAdmin
from app.tests.integration.test_agent_orchestration_route_auth import _iterRoutes

_PREFIX = "/api/v1/semantic/releases"

_EXPECTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", _PREFIX),
        ("GET", f"{_PREFIX}/active"),
        ("GET", f"{_PREFIX}/{{releaseId}}"),
    }
)


def _semanticRoutes(app: FastAPI) -> list[tuple[str, APIRoute]]:
    return [(path, route) for path, route in _iterRoutes(list(app.routes)) if path.startswith(_PREFIX)]


def _declaresDependency(route: APIRoute, dependency: object) -> bool:
    """路由依赖链（含 router 级 dependencies）是否声明了指定依赖。"""
    seen: set[int] = set()
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is dependency:
            return True
        stack.extend(dependant.dependencies)
    return False


def _buildApp(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> FastAPI:
    monkeypatch.setenv("ENABLE_SEMANTIC_RELEASE", "true" if enabled else "false")
    getSettings.cache_clear()
    try:
        from app.main import createApp

        return createApp()
    finally:
        getSettings.cache_clear()


def test_routes_are_exactly_the_expected_three(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _semanticRoutes(_buildApp(monkeypatch, enabled=True))

    assert routes, "路由枚举为空——路由树结构变化会让本守卫空集通过"
    enumerated = {(method, path) for path, route in routes for method in route.methods}
    assert enumerated == _EXPECTED_ROUTES, (
        "路由集合与预期不一致（新增/删除/改名都必须同步本守卫）：\n  "
        f"缺失: {sorted(_EXPECTED_ROUTES - enumerated)}\n"
        f"多出: {sorted(enumerated - _EXPECTED_ROUTES)}"
    )


@pytest.mark.parametrize("dependency", [requireAdmin, getCurrentUser])
def test_every_route_declares_the_admin_guard(monkeypatch: pytest.MonkeyPatch, dependency: object) -> None:
    routes = _semanticRoutes(_buildApp(monkeypatch, enabled=True))
    assert routes, "路由枚举为空——空集不得通过"

    offenders = sorted(
        {
            (method, path)
            for path, route in routes
            for method in route.methods
            if not _declaresDependency(route, dependency)
        }
    )
    assert offenders == [], (
        f"以下路由缺少 {getattr(dependency, '__name__', dependency)}（门禁漂移）：\n  "
        + "\n  ".join(f"{method:7} {path}" for method, path in offenders)
    )


def test_routes_are_absent_when_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关闭 = 整组不注册，这是「404 而不是 403」的实现前提。"""
    app = _buildApp(monkeypatch, enabled=False)

    assert _semanticRoutes(app) == []


def test_guard_detector_is_not_vacuous(monkeypatch: pytest.MonkeyPatch) -> None:
    """反向断言：公开路由必须被判为「无 admin 门禁」，否则上一条可能恒真。"""
    app = _buildApp(monkeypatch, enabled=True)
    public = [route for path, route in _iterRoutes(list(app.routes)) if path == "/api/v1/health"]

    assert public, "未枚举到 /api/v1/health——路由树展开失效"
    assert not _declaresDependency(public[0], requireAdmin)
    assert not _declaresDependency(public[0], getCurrentUser)
