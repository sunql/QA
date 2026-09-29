"""路由鉴权守卫：全应用未鉴权路由集合必须 ⊆ 白名单。

背景：2026-09-30 发现 46 条非公开路由在 AUTH_MODE=real 下匿名可达（含 15 条写/删）。
根因不是「规则缺失」而是「规则无强制」——本测试即强制。
"""
from fastapi.routing import APIRoute, _IncludedRouter

from app.dependencies import getCurrentUser
from app.main import app

# 应公开的路由（键与 (method, APIRoute.path) 同形）
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/v1/health"),
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/password-policy"),
    ("GET", "/api/v1/data-quality/reports/share/{token}"),
})

# 枚举下限：展开失效时必须红，而不是空集通过（vacuous pass）
MIN_ENUMERATED_ROUTES = 300

_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _hasCurrentUser(route: APIRoute, extraDependencies: tuple = ()) -> bool:
    """路由依赖链（含 router 级 dependencies）是否含 getCurrentUser。"""
    for dependency in extraDependencies:
        if getattr(dependency, "dependency", None) is getCurrentUser:
            return True
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


def _enumerateRoutes() -> list[tuple[str, str, bool]]:
    """枚举 (method, path, hasAuth)。必须展开 _IncludedRouter。

    FastAPI 0.141 的 app.routes 里 _IncludedRouter 的 path 为 None 且
    routes/app 属性不可见，只能经 original_router + include_context 展开。
    """
    found: list[tuple[str, str, bool]] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - _IGNORED_METHODS:
                found.append((method, route.path, _hasCurrentUser(route)))
        elif isinstance(route, _IncludedRouter):
            context = route.include_context
            prefix = context.prefix or ""
            for sub in route.original_router.routes:
                if isinstance(sub, APIRoute):
                    for method in sub.methods - _IGNORED_METHODS:
                        found.append((
                            method,
                            prefix + sub.path,
                            _hasCurrentUser(sub, context.dependencies),
                        ))
    return found


def test_enumeratedRouteCountMeetsFloor() -> None:
    """展开失效守卫：内部结构变化导致展开不到路由时，本测试必须红。"""
    routes = _enumerateRoutes()
    assert len(routes) >= MIN_ENUMERATED_ROUTES, (
        f"仅枚举到 {len(routes)} 条路由（下限 {MIN_ENUMERATED_ROUTES}）——"
        "_IncludedRouter 展开很可能已失效，鉴权守卫会空集通过"
    )


def test_noUnauthenticatedRouteOutsideWhitelist() -> None:
    """坏输入被拦：非白名单路由必须全部要求鉴权。"""
    offenders = sorted({
        (method, path)
        for method, path, hasAuth in _enumerateRoutes()
        if not hasAuth and (method, path) not in PUBLIC_ROUTES
    })
    assert offenders == [], (
        "以下路由匿名可达（缺 Depends(getCurrentUser)）：\n  "
        + "\n  ".join(f"{m:7} {p}" for m, p in offenders)
    )


def test_whitelistRoutesAreActuallyPublic() -> None:
    """正确输入不被拦：白名单路由不应被误加鉴权（否则登录/对外分享会断）。"""
    byKey = {(m, p): a for m, p, a in _enumerateRoutes()}
    for key in sorted(PUBLIC_ROUTES):
        assert key in byKey, f"白名单路由 {key} 未枚举到——白名单已陈旧"
        assert byKey[key] is False, (
            f"白名单路由 {key} 现在带了鉴权，请确认它是否仍应公开"
        )
