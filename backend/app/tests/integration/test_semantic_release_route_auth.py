"""语义发布 API（读 + 写）的装配 + 门禁回归网（feature-scoped route-auth guard）。

背景：本特性的路由由 `main.py` 与 `tests/_testapp.py` **两处手工装配**，而 API
集成测试全部走 `_testapp.py`。若哪天从 `main.py` 删掉 `Depends(requireAdmin)`
或漏挂前缀，集成套件仍然全绿——两处装配的漂移在测试里是结构性不可见的。

本文件直接构建**生产 app**（`app.main.createApp()`），钉三件事：

1. 开关开启时路由集合恰好 7 条（3 条读 + 4 条写；集合相等：漏/多/改名都会红，
   空集必然不等）；
2. 每条路由的依赖链里同时声明了 `requireAdmin` 与 `getCurrentUser`：
   前者在 include 层、后者由 `requireAdmin`（参数依赖）与 `mustChangePasswordGuard`
   共同引入，且 4 条写路由还在端点上直接声明。两条分别断言；
   ⚠️ 二者**不是**「删任一层两条都红」的严格双保险 —— `getCurrentUser` 有旁路
   （`mustChangePasswordGuard` 也依赖它），删掉 include 层的 `requireAdmin` 时
   `[requireAdmin]` 会红、`[getCurrentUser]` 可能仍绿；
3. 开关关闭时这 7 条路由一条都不注册（404 的前提）。

反向断言（探测器不恒真）：公开路由 `/api/v1/health` 必须被判为「无 admin 门禁」；
另有正向对照（`test_detector_sees_include_context_dependencies`）证明
`include_router(dependencies=[...])` 能被看见——否则探测器恒 False 时反向断言照样通过。

⚠️ **版本敏感**：本特性的门禁声明在 `include_router(dependencies=[...])` 上，而
FastAPI ≥0.141 把它放进 `_IncludedRouter.include_context`、不并入 `route.dependant`
（0.136.x 会并入）。2026-11-10 的 PR #115 CI 用的正是 `uv.lock` 里的 **0.141.1**，
只沿 dependant 探测会把 7 条路由全判成「没有门禁」⇒ 本文件的路由树展开必须
同时回收 include 上下文里的依赖。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from app.config import getSettings
from app.dependencies import getCurrentUser, requireAdmin
from app.tests._route_auth_support import (
    declaresDependency as _declaresDependency,
)
from app.tests._route_auth_support import (
    iterRoutesWithDeps as _iterRoutesWithDeps,
)

_PREFIX = "/api/v1/semantic/releases"

_EXPECTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", _PREFIX),
        ("GET", f"{_PREFIX}/active"),
        ("GET", f"{_PREFIX}/{{releaseId}}"),
        ("POST", _PREFIX),
        ("POST", f"{_PREFIX}/{{releaseId}}/validate"),
        ("POST", f"{_PREFIX}/{{releaseId}}/activate"),
        ("POST", f"{_PREFIX}/{{releaseId}}/archive"),
    }
)


def _semanticRoutes(app: FastAPI) -> list[tuple[str, APIRoute, tuple[Any, ...]]]:
    return [
        (path, route, inherited)
        for path, route, inherited in _iterRoutesWithDeps(list(app.routes))
        if path.startswith(_PREFIX)
    ]


def _buildApp(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> FastAPI:
    monkeypatch.setenv("ENABLE_SEMANTIC_RELEASE", "true" if enabled else "false")
    getSettings.cache_clear()
    try:
        from app.main import createApp

        return createApp()
    finally:
        getSettings.cache_clear()


def test_routes_are_exactly_the_expected_seven(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = _semanticRoutes(_buildApp(monkeypatch, enabled=True))

    assert routes, "路由枚举为空——路由树结构变化会让本守卫空集通过"
    # 集合相等看不见重复注册（同一 router include 两次）或枚举器把某包装层列了两遍
    # ⇒ 另外钉条数。
    assert len(routes) == len(_EXPECTED_ROUTES), f"路由条数 {len(routes)} ≠ {len(_EXPECTED_ROUTES)}"
    enumerated = {(method, path) for path, route, _ in routes for method in route.methods}
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
            for path, route, inherited in routes
            for method in route.methods
            if not _declaresDependency(route, dependency, inherited)
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
    public = [
        (route, inherited)
        for path, route, inherited in _iterRoutesWithDeps(list(app.routes))
        if path == "/api/v1/health"
    ]

    assert public, "未枚举到 /api/v1/health——路由树展开失效"
    route, inherited = public[0]
    assert not _declaresDependency(route, requireAdmin, inherited)
    assert not _declaresDependency(route, getCurrentUser, inherited)


def test_detector_sees_include_context_dependencies() -> None:
    """正向对照：`include_router(dependencies=[...])` 必须被看见。

    这是本守卫在 FastAPI **≥0.141** 下的关键分支（见 `_iterRoutesWithDeps`
    的 docstring），而 0.136.x 会把它并入 `route.dependant`。两条路径都不许漏：
    探测器若恒返回 False，上一条「公开路由无门禁」的反向断言照样通过 ⇒ 假绿。
    """
    def marker() -> None: ...

    def outer(inner: Any = Depends(marker)) -> None: ...  # noqa: B008 - 探针需真实依赖声明

    guarded = APIRouter(prefix="/guarded")

    @guarded.get("/probe")
    async def probeRoute() -> None: ...

    app = FastAPI()
    app.include_router(guarded, prefix="/api", dependencies=[Depends(outer)])

    routes = [
        (path, route, inherited)
        for path, route, inherited in _iterRoutesWithDeps(list(app.routes))
        if path.startswith("/api/guarded")
    ]

    assert len(routes) == 1, "探针路由未枚举到——展开逻辑失效"
    _, route, inherited = routes[0]
    assert _declaresDependency(route, outer, inherited)
    # 间接依赖（outer 的参数里声明 marker）同样必须可见。
    assert _declaresDependency(route, marker, inherited)
    assert not _declaresDependency(route, requireAdmin, inherited)
